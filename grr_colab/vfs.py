#!/usr/bin/env python
"""VFS GRR Colab module.

The module contains classes that interact with VFS.
"""

from __future__ import absolute_import
from __future__ import division

from __future__ import print_function
from __future__ import unicode_literals

import io
from typing import Text, List, Iterator, Callable

from grr_api_client import client
from grr_api_client import errors as api_errors
from grr_colab import errors
from grr_colab import flags
from grr_response_proto import jobs_pb2

FLAGS = flags.FLAGS


class VfsFile(io.BufferedIOBase):
  """Wrapper for a VFS File.

  Allows working with VFS file like it's a usual binary file object.

  Currently this file is readable only, not seekable and not writable. Read
  operations are buffered.
  """
  _buffer_pos = None  # type: int

  def __init__(self, fetch):
    super(VfsFile, self).__init__()
    self._data = fetch(0)
    self._buffer = b''
    self._buffer_pos = 0
    self._pos = 0
    self._eof = False
    self._closed = False
    self._fetch = fetch

  def _ensure_not_closed(self):
    if self.closed:
      raise ValueError('File has already been closed.')

  def _load_buffer(self):
    if self._eof:
      return
    try:
      self._buffer = next(self._data)
    except StopIteration:
      self._eof = True
      self._buffer = bytes()
    self._buffer_pos = 0

  def _is_buffer_empty(self):
    return self._buffer_pos == len(self._buffer)

  def _read_from_buffer(self, size = -1):
    if self._is_buffer_empty():
      self._load_buffer()
    available = len(self._buffer) - self._buffer_pos
    size = min(size, available)
    size = available if size < 0 else size
    self._buffer_pos += size
    self._pos += size
    return self._buffer[self._buffer_pos - size:self._buffer_pos]

  @property
  def closed(self):
    return self._closed

  def close(self):
    self._closed = True

  def fileno(self):
    raise io.UnsupportedOperation()

  def flush(self):
    pass

  def isatty(self):
    return False

  def seekable(self):
    return True

  def seek(self, offset, whence = io.SEEK_SET):
    self._ensure_not_closed()

    if whence == io.SEEK_SET:
      new_pos = offset
    elif whence == io.SEEK_CUR:
      new_pos = self.tell() + offset
    else:
      msg = 'Whence point {} is not supported.'.format(whence)
      raise io.UnsupportedOperation(msg)

    buffer_start_pos = self._pos - self._buffer_pos
    if buffer_start_pos <= new_pos <= buffer_start_pos + len(self._buffer):
      self._buffer_pos += new_pos - self._pos
      self._pos = new_pos
    else:
      self._data = self._fetch(new_pos)
      self._pos = new_pos
      self._buffer = b''
      self._buffer_pos = 0
    self._eof = False

    return self.tell()

  def tell(self):
    self._ensure_not_closed()
    return self._pos

  def truncate(self, size = None):
    raise io.UnsupportedOperation()

  def writable(self):
    return False

  def write(self, b):
    raise io.UnsupportedOperation()

  def writelines(self, lines):
    raise io.UnsupportedOperation()

  def detach(self):
    raise io.UnsupportedOperation()

  def readable(self):
    return True

  def read(self, size = -1):
    self._ensure_not_closed()
    size = size or -1
    data = bytearray()
    while not self._eof and (size < 0 or len(data) < size):
      data += self._read_from_buffer(size=size - len(data))
    return bytes(data)

  def read1(self, size = -1):
    self._ensure_not_closed()
    has_data = not self._is_buffer_empty()
    data = self._read_from_buffer(size=size)
    if has_data and (size < 0 or len(data) < size):
      data += self._read_from_buffer(size=size - len(data))
    return bytes(data)

  def readinto1(self, b):
    self._ensure_not_closed()
    data = self.read1(size=len(b))
    b[:len(data)] = data
    return len(data)


class VFS(object):
  """Wrapper for VFS.

  Offers easy to use interface to perform operations on GRR VFS from Colab.
  """

  def __init__(self, client_):
    self._client = client_

  def ls(self, path, max_depth = 1):
    """Lists contents of a given VFS directory.

    Args:
      path: A path to the directory to list the contents of.
      max_depth: Max depth of subdirectories to explore. If max_depth is >1,
        then the results will also include the contents of subdirectories (and
        sub-subdirectories and so on).

    Returns:
      A sequence of stat entries.
    """
    if max_depth < 1:
      return []

    try:
      f = self._get_file(path).Get()
    except api_errors.AccessForbiddenError as e:
      raise errors.ApprovalMissingError(self._client.client_id, e)

    if not f.is_directory:
      raise errors.NotDirectoryError(self._client.client_id, path)

    try:
      stat_entries = [_.data.stat for _ in f.ListFiles()]
    except api_errors.AccessForbiddenError as e:
      raise errors.ApprovalMissingError(self._client.client_id, e)

    inner_entries = []
    for entry in stat_entries:
      try:
        inner_entries += self.ls(entry.pathspec.path, max_depth - 1)
      except errors.NotDirectoryError:
        inner_entries += []
    return stat_entries + inner_entries

  def refresh(self, path, max_depth = 1):
    """Syncs the collected VFS with current filesystem of the client.

    Args:
      path: A path to the directory to sync.
      max_depth: Max depth of subdirectories to sync. If max_depth is >1, then
        subdirectories (and sub-subdirectories and so on) are going to be synced
        as well.

    Returns:
      Nothing.
    """
    f = self._get_file(path)

    try:
      if max_depth > 1:
        f.RefreshRecursively(max_depth).WaitUntilDone()
      else:
        f.Refresh().WaitUntilDone()
    except api_errors.AccessForbiddenError as e:
      raise errors.ApprovalMissingError(self._client.client_id, e)

  def open(self, path):
    """Opens a file object corresponding to the given path in the VFS.

    The returned file object is read-only.

    Args:
      path: A path to the file to open.

    Returns:
      A file-like object (implementing standard IO interface).
    """
    f = self._get_file(path)

    try:
      self._client.VerifyAccess()
    except api_errors.AccessForbiddenError as e:
      raise errors.ApprovalMissingError(self._client.client_id, e)

    return VfsFile(f.GetBlobWithOffset)

  def wget(self, path):
    """Returns a link to the file specified.

    Args:
      path: A path to the file.

    Returns:
      A link to the file.
    """
    if not FLAGS.grr_admin_ui_url:
      raise ValueError('GRR Admin UI URL has not been specified')

    try:
      f = self._get_file(path).Get()
    except api_errors.AccessForbiddenError as e:
      raise errors.ApprovalMissingError(self._client.client_id, e)

    if f.is_directory:
      raise ValueError('`{}` is a directory'.format(path))

    link = '{}/api/clients/{}/vfs-blob/{}'
    return link.format(FLAGS.grr_admin_ui_url, self._client.client_id,
                       get_vfs_path(path))

  def _get_file(self, path):
    return self._client.File(get_vfs_path(path))


def get_vfs_path(path):
  return 'fs/os{}'.format(path)
