import threading
import queue
import pathlib
import sys
from importlib import import_module

import mysqlsh

# In MySQL Shell to be able to use the modules in libs as for example
# libs.query it is necessary to import both libs separately and the
# whole module
# noinspection PyUnresolvedReferences
from concurrency_book import libs
# noinspection PyUnresolvedReferences
import concurrency_book.libs.load
# noinspection PyUnresolvedReferences
import concurrency_book.libs.log
# noinspection PyUnresolvedReferences
import concurrency_book.libs.query
# noinspection PyUnresolvedReferences
import concurrency_book.libs.util
# noinspection PyUnresolvedReferences
import concurrency_book.libs.workloads

LOG = libs.log.Log(libs.log.INFO)
WORKLOADS_PATH = pathlib.Path(__file__).parent.joinpath('workloads').resolve()
WORKLOADS = libs.workloads.load(WORKLOADS_PATH)
SCHEMA_LOADS = libs.load.get_jobs()


def _run_connection(connection, session, query_queue, return_queue,
                    sql_formatter):
    """Run the work done by a single connection."""

    local = threading.local()
    try:
        session.set_fetch_warnings(True)
    except (IndexError, AttributeError):
        # Not available in the legacy protocol. MySQL Shell 8.0.21 and earlier
        # return IndexError, 8.0.22 and later return AttributeError
        pass

    local.data = {}
    while True:
        local.query = libs.query.get_from_queue(query_queue)
        if local.query is None:
            # No more work
            query_queue.task_done()
            return None

        local.result = None
        local.error = None
        local.sql = sql_formatter.sql_global_sub(local.query.sql, connection)
        if local.sql.upper() in ('START TRANSACTION', 'BEGIN'):
            local.result = session.start_transaction()
        elif local.sql.upper() == 'COMMIT':
            local.result = session.commit()
        elif local.sql.upper() == 'ROLLBACK':
            local.result = session.rollback()
        else:
            local.params = []
            for local.parameter in local.query.parameters:
                try:
                    local.params.append(local.data[local.parameter])
                except KeyError as e:
                    local.error = f'KeyError: {e} not found in the stored ' + \
                                   'result'
                    local.result = None

            if local.error is None:
                try:
                    local.result = session.run_sql(local.sql, local.params)
                except mysqlsh.DBError as e:
                    local.error = e
                else:
                    if local.query.store:
                        # Only the result of the first row is stored.
                        # Ensure the whole result set is consumed
                        try:
                            local.columns = [col.column_label for col
                                             in local.result.columns]
                            local.data = dict(
                                zip(local.columns, local.result.fetch_one()))
                            local.result.fetch_all()
                        except AttributeError as e:
                            print(f'sql .......: {local.sql}')
                            print(f'result ....: {local.result}')
                            print(f'dir .......: {dir(local.result)}')
                            print(f'str .......: {local.result.__str__()}')
                            print(f'repr ......: {local.result.__repr__()}')
                            print(e)

        if not local.query.silent and local.query.show_result:
            return_queue.put(
                libs.query.RESULT(local.query, local.result, local.error)
            )
        query_queue.task_done()


def _run_load(schema):
    """Load a schema."""
    loader = libs.load.Load(schema.name)
    loader.execute()


def _run_implementation(workload, uri):
    """Execute the workload as defined in a class."""
    implementation = workload.implementation
    module_name = f'concurrency_book.{implementation.module}'
    try:
        module = import_module(module_name)
    except ModuleNotFoundError:
        LOG.error(f'Unknown module: {implementation.module}. ' +
                  'Check the code for errors.')
        return False

    try:
        class_object = getattr(module, implementation.class_name)
    except AttributeError:
        LOG.error(f'Unknown class name - module: {implementation.module} ' +
                  f'- class: {implementation.class_name}')
        return False

    try:
        task = class_object(workload=workload,
                            session=mysqlsh.globals.session,
                            log=LOG,
                            **implementation.args)
    except (TypeError, ValueError) as e:
        LOG.error('Wrong arguments provided for the class - module:' +
                  f'{implementation.module} - class: ' +
                  f'{implementation.class_name} - arguments: ' +
                  f'{implementation.args} - error: {e}')
        return False

    task.execute(uri)

    return True


def _run_main(workload, uri):
    """Run by the main thread. This is where the worker threads are
    created. It is also here the work among the worker threads is
    coordinated."""

    LOG.workload = workload
    LOG.info(f'Starting the workload {workload.name}')
    LOG.caption()

    if workload.implementation.class_name is not None:
        success = _run_implementation(workload, uri)
        if not success:
            return None

    query_queue = []
    connections = []
    sessions = []
    result_queue = queue.Queue()
    processlist_ids = [None] * workload.connections
    thread_ids = [None] * workload.connections
    event_ids = [None] * workload.connections

    # Set up the query queues and the sessions for the workload
    for i in range(workload.connections):
        query_queue.append(queue.Queue())
        sessions.append(libs.util.get_session(workload, uri))
        if sessions[i] is None:
            # Failed to get a connection
            return None
        (processlist_ids[i],
         thread_ids[i],
         event_ids[i]) = libs.query.get_connection_ids(sessions[i])

    # Output the connection and thread ids, so make it easier to
    # investigate the workload.
    LOG.ids(processlist_ids, thread_ids, event_ids)

    sql_formatter = libs.query.Formatter(processlist_ids,
                                         thread_ids,
                                         event_ids)
    LOG.sql_formatter = sql_formatter
    for i in range(workload.connections):
        connections.append(threading.Thread(
            target=_run_connection, daemon=True,
            args=(i + 1, sessions[i], query_queue[i], result_queue,
                  sql_formatter)
        ))
        connections[i].start()

    query_handler = libs.query.Query(
        query_queue, result_queue, processlist_ids, thread_ids, LOG,
        workload.concurrent)
    for loop in range(workload.loops):
        for query in workload.queries:
            query_handler.exec(query)

    # Allow the user to run investigations
    if workload.investigations:
        session = mysqlsh.globals.session

        number, investigation, sql = libs.util.prompt_investigation(
            workload.investigations, sql_formatter, workload.connections)
        while investigation is not None:
            print(f'-- Investigation #{number}')
            LOG.sql(investigation, sql, 3)
            result = None
            error = None
            try:
                result = session.run_sql(sql)
            except mysqlsh.DBError as e:
                error = e

            LOG.result(libs.query.RESULT(investigation, result, error))
            number, investigation, sql = libs.util.prompt_investigation(
                workload.investigations, sql_formatter,
                workload.connections)
            print('')

    LOG.info(f'Completing the workload {workload.name}')

    for query in workload.completions:
        query_handler.exec(query)

    # Tell the connection threads to stop and disconnect
    LOG.info(f'Disconnecting for the workload {workload.name}')
    for i in range(workload.connections):
        query_queue[i].put(None)
    for i in range(workload.connections):
        query_queue[i].join()
        connections[i].join()
        sessions[i].close()

    LOG.info(f'Completed the workload {workload.name}')
    LOG.workload = None


def load(schema_name=None):
    """High level steps to load a schema."""

    # An existing connection is required to load a schema
    has_connection = libs.util.verify_session()
    if not has_connection:
        return None

    # Loading a schema requires changing the default schema.
    # This is currently only supported using the set_current_schema()
    # method which is not available for classic protocol. So verify
    # the method is available before proceeding.
    try:
        getattr(mysqlsh.globals.session, 'set_current_schema')
    except AttributeError:
        print('Loading data requires the session.set_current_schema() which. ' +
              'is only available with the mysqlx protocol. Please exit MySQL ' +
              'Shell and reconnect using the --mysqlx option.', file=sys.stderr)
        return None

    schema = None
    keep_asking = True
    if schema_name is not None:
        keep_asking = False
        try:
            schema = SCHEMA_LOADS[schema_name]
        except KeyError:
            print(f'Invalid schema name: "{schema_name}"')

    if schema is None:
        schema = libs.util.prompt_task('Schema load job', SCHEMA_LOADS)

    while schema is not None:
        _run_load(schema)
        print('')
        if keep_asking:
            schema = libs.util.prompt_task('Schema load job', SCHEMA_LOADS)
        else:
            schema = None


def run(workload_name=None):
    """High level steps to execute a workload."""

    # An existing connection is required to run a workload
    has_connection = libs.util.verify_session()
    if not has_connection:
        return None

    workload = None
    keep_asking = True
    if workload_name is not None:
        keep_asking = False
        try:
            workload_name = libs.util.normalize_task_name(workload_name)
            workload = WORKLOADS[workload_name]
        except KeyError:
            print(f'Invalid workload name: "{workload_name}"')

    if workload is None:
        workload = libs.util.prompt_task('workload', WORKLOADS)

    uri = None
    if workload is not None:
        uri = libs.util.get_uri()
    while workload is not None:
        _run_main(workload, uri)
        print('')
        if keep_asking:
            workload = libs.util.prompt_task('workload', WORKLOADS)
        else:
            workload = None


def show():
    """List the available workloads and load jobs."""

    libs.util.list_tasks('workload', WORKLOADS)
    print('')
    libs.util.list_tasks('Schema load job', SCHEMA_LOADS)


def help():
    """Display help."""

    print('The following actions are supported:')
    print('=' * 36)
    print('')
    print('* help()')
    print('    Display this help.')
    print('')
    print('* load(schema_name=None)')
    print('    Load a schema. Optionally takes the name of the schema to be')
    print('    loaded. If no schema name or an invalid is given, you')
    print('    will be prompted to select one.')
    print('')
    print('* show()')
    print('    List the available workloads. The function takes no arguments.')
    print('')
    print('* run(workload_name=None)')
    print('    Execute a workload. Optionally the name of the workload can be')
    print('    specified. If no workload name or an invalid is given, you')
    print('    will be prompted to select one. You will also be required to')
    print('    enter the password.')
    print('')
