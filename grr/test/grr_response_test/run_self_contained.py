#!/usr/bin/env python
"""Helper script for running end-to-end tests."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import atexit
import multiprocessing
import os
import socket
import sys
import tempfile
import threading
import time
import traceback

from absl import app
from absl import flags
import portpicker
import psutil
import requests

from grr_api_client import api
from grr_response_core import config
from grr_response_core.lib import config_lib
from grr_response_core.lib import package
from grr_response_core.lib.util import compatibility


class Error(Exception):
  """Module-specific base error class."""


class TCPPortTimeout(Error):
  """Raised when a TCP port didn't open in time."""


class ClientEnrollmentTimeout(Error):
  """Raised when a client does not enroll in time."""


flags.DEFINE_list(
    "tests", [],
    "(Optional) comma-separated list of tests to run (skipping all others). "
    "If this flag is not specified, all tests available for the platform "
    "will run.")

flags.DEFINE_list(
    "manual_tests", [],
    "A comma-separated list of extra tests to run (such tests are not run by "
    "default and have to be manually enabled with this flag).")

flags.DEFINE_string("mysql_database", "grr_test_db",
                    "MySQL database name to use.")

flags.DEFINE_string("mysql_username", None, "MySQL username to use.")

flags.DEFINE_string("mysql_password", None, "MySQL password to use.")

flags.DEFINE_string("logging_path", None,
                    "Base logging path for server components to use.")


def GetServerComponentArgs(config_path):
  """Returns a set of command line arguments for server components.

  Args:
    config_path: Path to a config path generated by
      self_contained_config_writer.

  Returns:
    An iterable with command line arguments to use.
  """

  primary_config_path = package.ResourcePath(
      "grr-response-core", "install_data/etc/grr-server.yaml")
  secondary_config_path = package.ResourcePath(
      "grr-response-test", "grr_response_test/test_data/grr_test.yaml")
  return [
      "--config",
      primary_config_path,
      "--secondary_configs",
      ",".join([secondary_config_path, config_path]),
      "-p",
      "Monitoring.http_port=%d" % portpicker.pick_unused_port(),
      "-p",
      "AdminUI.webauth_manager=NullWebAuthManager",
  ]


def _RunServerComponent(name, import_main_fn, args):
  """Runs a server component with a given name, main module and args.

  NOTE: this function will run in a subprocess created via
  multiprocessing.Process. The reason why all the imports happen inside this
  function and not in the parent process is that we don't want to pollute parent
  namespace with server and client imports, as they don't play nicely
  together. To be more precise:
  1) Server init hooks make no sense for the client (and vice-versa). When
  client and server are
  mixed in the same process, client will try to initialize the datastore and
  fail.
  2) Client logging subsystem is initialized differently from the server one,
  still they share the same API.
  3) Singletons like stats.STATS and config.CONFIG are initialized differently
  in the client and server code (i.e. we don't want the server to try to read
  config values from the registry on Windows). Even more: config.CONFIG should
  contain different values for different server components, depending on the
  component.
  To work around these issues run_self_contained.py imports neither server- nor
  client-specific modules and does necessary imports only in subprocesses.

  Args:
    name: Component name (used for logging purposes).
    import_main_fn: A function that does necessary component-specific imports
      and returns a "main" function to run.
    args: An iterable with program arguments (not containing the program name,
      which is passed in the "name" argument).
  """
  # pylint: disable=g-import-not-at-top,unused-variable
  from grr_response_test.lib import shared_mem_db
  from grr_response_server.databases import registry_init as db_registry_init
  # pylint: enable=g-import-not-at-top,unused-variable

  db_registry_init.REGISTRY[compatibility.GetName(
      shared_mem_db.SharedMemoryDB)] = shared_mem_db.SharedMemoryDB

  main_fn = import_main_fn()

  # Result of the invocation of a main function is passed to the `sys.exit`. In
  # most cases, these functions simply return `None` and this is usually fine as
  # the program is going to exit with return code of 0. However, things change
  # for subprocesses: if a subprocess exits with `None` return code of 1 is used
  # instead. Since server components run as subprocesses, this causes the main
  # process to fail. To fix this behaviour, we inspect the result and explicitly
  # translate it to 0 if it is `None` and pass-through any other value.
  def MainWrapper(*args, **kwargs):
    result = main_fn(*args, **kwargs)
    if result is None:
      return 0
    else:
      return result

  sys.argv = [name] + args
  app.run(MainWrapper)


def StartServerComponent(name, import_main_fn, args):
  """Starts a new process with a server component.

  Args:
    name: Component name (used for logging purposes).
    import_main_fn: A function that does necessary component-specific imports
      and returns a "main" function to run.
    args: An iterable with program arguments (not containing the program name,
      which is passed in the "name" argument).

  Returns:
    multiprocessing.Process instance corresponding to a started process.
  """
  print("Starting %s component" % name)
  process = multiprocessing.Process(
      name=name, target=_RunServerComponent, args=(name, import_main_fn, args))
  process.daemon = True
  process.start()
  return process


def _RunClient(config_path):
  """Runs GRR client with a provided client configuration path.

  NOTE: this function will run in a subprocess created via
  multiprocessing.Process. The reason why all the imports happen inside this
  function and not in the parent process is that we don't want to pollute parent
  namespace with server and client imports, as they don't play nicely
  together. run_self_contained.py imports neither server- nor client-specific
  modules and does necessary imports only in subprocesses.

  Args:
    config_path: A string with a path to a client configuration file path
      generated by self_contained_config_writer.
  """
  # pylint: disable=g-import-not-at-top,unused-variable
  from grr_response_client import client
  # pylint: enable=g-import-not-at-top,unused-variable

  sys.argv = ["Client", "--config", config_path]
  try:
    app.run(client.main)
  except Exception as e:  # pylint: disable=broad-except
    print("Client process error: %s" % e)
    traceback.print_exc()


def StartClient(config_path):
  """Start GRR client with a provided client configuration path.

  Args:
    config_path: A string with a path to a client configuration file path
      generated by self_contained_config_writer.

  Returns:
    multiprocessing.Process instance corresponding to a started process.
  """
  print("Starting client component")
  process = multiprocessing.Process(
      name="Client", target=_RunClient, args=(config_path,))
  process.daemon = True
  process.start()
  return process


def ImportAdminUI():
  """Imports AdminUI main module, to be used with StartServerComponent."""
  # pylint: disable=g-import-not-at-top,unused-variable
  from grr_response_server.gui import admin_ui
  # pylint: enable=g-import-not-at-top,unused-variable
  return admin_ui.main


def ImportFrontend():
  """Imports Frontend main module, to be used with StartServerComponent."""
  # pylint: disable=g-import-not-at-top,unused-variable
  from grr_response_server.bin import frontend
  # pylint: enable=g-import-not-at-top,unused-variable
  return frontend.main


def ImportWorker():
  """Imports Worker main module, to be used with StartServerComponent."""
  # pylint: disable=g-import-not-at-top,unused-variable
  from grr_response_server.bin import worker
  # pylint: enable=g-import-not-at-top,unused-variable
  return worker.main


def ImportSelfContainedConfigWriter():
  """Imports config writer main module, to be used with StartServerComponent."""
  # pylint: disable=g-import-not-at-top,unused-variable
  from grr_response_test.lib import self_contained_config_writer
  # pylint: enable=g-import-not-at-top,unused-variable
  return self_contained_config_writer.main


def ImportRunEndToEndTests():
  """Imports e2e runner main module, to be used with StartServerComponent."""
  # pylint: disable=g-import-not-at-top,unused-variable
  from grr_response_test import run_end_to_end_tests
  # pylint: enable=g-import-not-at-top,unused-variable
  return run_end_to_end_tests.main


_PROCESS_CHECK_INTERVAL = 0.1


def DieIfSubProcessDies(processes):
  """Kills the process if any of given processes dies.

  This function is supposed to run in a background thread and monitor provided
  processes to ensure they don't die silently.

  Args:
    processes: An iterable with multiprocessing.Process instances.
  """
  while True:
    for p in processes:
      if not p.is_alive():
        # DieIfSubProcessDies runs in a background thread, raising an exception
        # will just kill the thread while what we want is to fail the whole
        # process.
        print("Subprocess %s died unexpectedly. Killing main process..." %
              p.name)
        sys.exit(1)
    time.sleep(_PROCESS_CHECK_INTERVAL)


_TCP_PORT_WAIT_TIMEOUT_SECS = 15


def WaitForTCPPort(port):
  """Waits for a given local TCP port to open.

  If the port in question does not open within ~10 seconds, main process gets
  killed.

  Args:
    port: An integer identifying the port.

  Raises:
    TCPPortTimeout: if the port doesn't open.
  """
  start_time = time.time()
  while time.time() - start_time < _TCP_PORT_WAIT_TIMEOUT_SECS:
    try:
      sock = socket.create_connection(("localhost", port))
      sock.close()
      return
    except socket.error:
      pass
    time.sleep(_PROCESS_CHECK_INTERVAL)

  raise TCPPortTimeout("TCP port %d didn't open." % port)


_CLIENT_ENROLLMENT_WAIT_TIMEOUT_SECS = 15
_CLIENT_ENROLLMENT_CHECK_INTERVAL = 1


def WaitForClientToEnroll(admin_ui_port):
  """Waits for an already started client to enroll.

  If the client doesn't enroll within ~100 seconds, main process gets killed.

  Args:
    admin_ui_port: AdminUI port to be used with API client library to check for
      an enrolled client.

  Returns:
    A string with an enrolled client's client id.

  Raises:
    ClientEnrollmentTimeout: if the client fails to enroll in time.
  """
  api_endpoint = "http://localhost:%d" % admin_ui_port

  start_time = time.time()
  while time.time() - start_time < _CLIENT_ENROLLMENT_WAIT_TIMEOUT_SECS * 10:
    try:
      api_client = api.InitHttp(api_endpoint=api_endpoint)
      clients = list(api_client.SearchClients(query="."))
    except requests.exceptions.ConnectionError:
      #      print("Connection error (%s), waiting..." % api_endpoint)
      time.sleep(_CLIENT_ENROLLMENT_CHECK_INTERVAL)
      continue

    if clients:
      return clients[0].client_id

    print("No clients enrolled, waiting...")
    time.sleep(_CLIENT_ENROLLMENT_CHECK_INTERVAL)

  raise ClientEnrollmentTimeout("Client didn't enroll.")


def GetRunEndToEndTestsArgs(client_id, server_config):
  """Returns arguments needed to configure run_end_to_end_tests process.

  Args:
    client_id: String with a client id pointing to an already running client.
    server_config: GRR configuration object with a server configuration.

  Returns:
    An iterable with command line arguments.
  """
  api_endpoint = "http://localhost:%d" % server_config["AdminUI.port"]
  args = [
      "--api_endpoint",
      api_endpoint,
      "--api_user",
      "admin",
      "--api_password",
      "admin",
      "--client_id",
      client_id,
      "--ignore_test_context",
      "True",
      "-p",
      "Client.binary_name=%s" % psutil.Process(os.getpid()).name(),
  ]
  if flags.FLAGS.tests:
    args += ["--whitelisted_tests", ",".join(flags.FLAGS.tests)]
  if flags.FLAGS.manual_tests:
    args += ["--manual_tests", ",".join(flags.FLAGS.manual_tests)]

  return args


def main(argv):
  del argv  # Unused.

  if flags.FLAGS.mysql_username is None:
    raise ValueError("--mysql_username has to be specified.")

  # Create 2 temporary files to contain server and client configuration files
  # that we're about to generate.
  #
  # TODO(user): migrate to TempFilePath as soon grr.test_lib is moved to
  # grr_response_test.
  fd, built_server_config_path = tempfile.mkstemp(".yaml")
  os.close(fd)
  print("Using temp server config path: %s" % built_server_config_path)
  fd, built_client_config_path = tempfile.mkstemp(".yaml")
  os.close(fd)
  print("Using temp client config path: %s" % built_client_config_path)

  def CleanUpConfigs():
    os.remove(built_server_config_path)
    os.remove(built_client_config_path)

  atexit.register(CleanUpConfigs)

  # Generate server and client configs.
  config_writer_flags = [
      "--dest_server_config_path",
      built_server_config_path,
      "--dest_client_config_path",
      built_client_config_path,
      "--config_mysql_database",
      flags.FLAGS.mysql_database,
  ]

  if flags.FLAGS.mysql_username is not None:
    config_writer_flags.extend(
        ["--config_mysql_username", flags.FLAGS.mysql_username])

  if flags.FLAGS.mysql_password is not None:
    config_writer_flags.extend(
        ["--config_mysql_password", flags.FLAGS.mysql_password])

  if flags.FLAGS.logging_path is not None:
    config_writer_flags.extend(
        ["--config_logging_path", flags.FLAGS.logging_path])

  p = StartServerComponent("ConfigWriter", ImportSelfContainedConfigWriter,
                           config_writer_flags)
  p.join()
  if p.exitcode != 0:
    raise RuntimeError("ConfigWriter execution failed: {}".format(p.exitcode))

  server_config = config_lib.LoadConfig(config.CONFIG.MakeNewConfig(),
                                        built_server_config_path)

  # Start all remaining server components.
  processes = [
      StartServerComponent("AdminUI", ImportAdminUI,
                           GetServerComponentArgs(built_server_config_path)),
      StartServerComponent("Frontend", ImportFrontend,
                           GetServerComponentArgs(built_server_config_path)),
      StartServerComponent("Worker", ImportWorker,
                           GetServerComponentArgs(built_server_config_path)),
      StartClient(built_client_config_path),
  ]

  # Start a background thread that kills the main process if one of the
  # subprocesses dies.
  t = threading.Thread(target=DieIfSubProcessDies, args=(processes,))
  t.daemon = True
  t.start()

  # Wait for the client to enroll and get its id.
  client_id = WaitForClientToEnroll(server_config["AdminUI.port"])
  print("Found client id: %s" % client_id)

  # Run the test suite against the enrolled client.
  p = StartServerComponent(
      "RunEndToEndTests", ImportRunEndToEndTests,
      GetServerComponentArgs(built_server_config_path) +
      GetRunEndToEndTestsArgs(client_id, server_config))
  p.join()
  if p.exitcode != 0:
    raise RuntimeError("RunEndToEndTests execution failed.")

  print("RunEndToEndTests execution succeeded.")


if __name__ == "__main__":
  app.run(main)
