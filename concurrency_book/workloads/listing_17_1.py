import threading
from datetime import datetime
from datetime import timedelta
from time import sleep

import mysqlsh

# noinspection PyUnresolvedReferences
from concurrency_book import libs
# noinspection PyUnresolvedReferences
import concurrency_book.libs.query
# noinspection PyUnresolvedReferences
import concurrency_book.libs.util
# noinspection PyUnresolvedReferences
import concurrency_book.libs.log
# noinspection PyUnresolvedReferences
import concurrency_book.libs.metrics

DEFAULT_RUNTIME = 15
DEFAULT_SLEEP_FACTOR = 15


class ForeignKeys(object):
    """Implements the workload triggering lock contention due to
    foreign keys."""
    _workload = None
    _session = None
    _log = None
    _done = None
    _mutex_payment_start = None
    _mutex_payment_commit = None
    _log_lock = None
    _processlist_ids = {}
    _thread_ids = {}
    _event_ids = {}
    _sql_formatter = None
    _runtime = None
    _sleep_factor = None

    def __init__(self, workload, session, log):
        """Initialize the instance."""
        self._workload = workload
        self._session = session
        self._log = log
        self._done = None
        self._mutex_payment_start = None
        self._mutex_payment_commit = None
        self._log_lock = threading.Lock()
        self._processlist_ids = {}
        self._thread_ids = {}
        self._event_ids = {}
        self._sql_formatter = None
        self._runtime = None
        self._sleep_factor = None
        self._prompt_settings()

    def _prompt_settings(self):
        """Prompt for the settings for the workload."""
        prompt = 'Specify the number of seconds to run for'
        self._runtime = libs.util.prompt_int(10, 3600, DEFAULT_RUNTIME, prompt)

        prompt = 'Specify the sleep factor'
        self._sleep_factor = libs.util.prompt_int(0, 30, DEFAULT_SLEEP_FACTOR,
                                                  prompt)

        print('')

    def _wait_metadata_lock(self, session, thread_ids, min_count=1):
        """Monitor for a metadata lock wait situation."""
        local = threading.local()
        try:
            local.id_list = list(thread_ids)
        except TypeError:
            local.id_list = [thread_ids]
        local.in_list = ', '.join([str(local.thd_id)
                                   for local.thd_id in local.id_list])
        local.sql = f"""
    SELECT COUNT(*)
      FROM performance_schema.metadata_locks
     WHERE owner_thread_id IN ({local.in_list})
           AND lock_status = 'PENDING'""".strip()

        local.count = 0
        while local.count < min_count and not self._done.wait(0):
            try:
                local.result = session.run_sql(local.sql)
                local.count = local.result.fetch_one()[0]
            except (SystemError, IndexError, AttributeError):
                # MySQL Shell is not entirely thread safe
                pass

    def _exec_and_print_sql(self, session, sql, dump_format='table'):
        """Execute and print the result of a query.
        This method assumes the log lock is taken ahead of time in
        the calling routine."""
        local = threading.local()
        local.result = session.run_sql(sql)
        if dump_format == 'vertical':
            local.delimiter = r'\G'
        else:
            local.delimiter = ';'
        print('mysql> ' + self._sql_formatter.indent_sql(sql, 7) +
              local.delimiter)
        mysqlsh.globals.shell.dump_rows(local.result, dump_format)
        print('')

    def _exec_with_error_handling(self, session, sql, verbose=True):
        """Execute a query. Exceptions caused by the execution are
        handled with the error printed. If there is no error, the
        result is printed if verbose is set to true."""
        local = threading.local()
        try:
            local.result = session.run_sql(sql)
        except mysqlsh.DBError as e:
            self._log_lock.acquire()
            print(f'mysql> {sql};')
            local.msg = libs.log.RE_ERROR_MSG.sub('', e.msg)
            print(f'ERROR: {e.code}: {local.msg}')
            print('')
            self._log_lock.release()
        else:
            if verbose:
                self._log_lock.acquire()
                print(f'mysql> {sql};')
                print(f'Query OK, 0 rows affected ' +
                      f'({local.result.execution_time})')
                mysqlsh.globals.shell.dump_rows(local.result)
                print('')
                self._log_lock.release()

    def _customer(self, session, customer_id, other_thread_id):
        """Execute the queries on the customer table."""
        local = threading.local()
        local.sql = "UPDATE sakila.customer " + \
                    "SET active = IF(active = 1, 0, 1) WHERE customer_id = ?"
        local.done = False
        # The timeout for the mutex acquire() calls ensures that if
        # a mistake happens and the test locks up that the thread blocks
        # for at most 20 seconds.
        self._mutex_payment_commit.acquire(timeout=20)
        self._log.debug('Acquired commit mutex', customer_id)
        while not local.done:
            self._mutex_payment_start.acquire(timeout=20)
            self._log.debug('Acquired start mutex', customer_id)
            session.start_transaction()
            self._log.debug('Releasing commit mutex', customer_id)
            try:
                self._mutex_payment_commit.release()
            except RuntimeError:
                pass
            session.run_sql(local.sql, (customer_id, ))

            # Ensure the ALTER TABLE thread is waiting for the
            # metadata lock before proceeding.
            self._wait_metadata_lock(session, self._thread_ids['alter'])
            self._log.debug('Metadata locks after run_sql()', customer_id)
            self._log.debug('Releasing start mutex', customer_id)
            try:
                self._mutex_payment_start.release()
            except RuntimeError:
                pass
            self._mutex_payment_commit.acquire(timeout=20)
            self._log.debug('Acquired commit mutex', customer_id)
            # Need to wait to ensure the other thread updating the
            # the customer table has started as they block each other
            # due to a metadata lock on the inventory table.
            self._wait_metadata_lock(session, other_thread_id)
            self._log.debug('Metadata locks before commit()', customer_id)
            sleep(0.1 * self._sleep_factor)
            session.commit()
            local.done = self._done.wait(0)

        session.commit()
        # Ensure the mutexes are released so the other thread is not blocked
        try:
            self._mutex_payment_start.release()
        except RuntimeError:
            pass
        try:
            self._mutex_payment_commit.release()
        except RuntimeError:
            pass

    def _alter(self, session):
        """Execute the ALTER TABLE statement in the inventory table."""
        local = threading.local()
        local.sql = "ALTER TABLE sakila.inventory FORCE"
        local.done = False
        session.run_sql('SET SESSION lock_wait_timeout = 1')
        while not local.done:
            self._log.debug('Executing ALTER TABLE')
            self._exec_with_error_handling(session, local.sql)
            local.done = self._done.wait(5)

    def _film_category(self, session):
        """Execute the UPDATE on the film_category table."""
        local = threading.local()
        local.sql = "UPDATE sakila.film_category " + \
                    "SET category_id = IF(category_id = 7, 16, 7) " + \
                    "WHERE film_id = 64"
        local.done = False
        session.run_sql("SET SESSION autocommit = 0")
        while not local.done:
            self._log.debug('Executing update on film_category')
            session.run_sql(local.sql)
            local.done = self._done.wait(4)
            session.commit()

    def _category(self, session):
        """Execute the UPDATE on the category table."""
        local = threading.local()
        local.sql = "UPDATE sakila.category " + \
                    "SET name = IF(name = 'Travel', 'Exploring', 'Travel')" + \
                    " WHERE category_id = 16"
        session.run_sql("SET SESSION innodb_lock_wait_timeout = 1")
        local.done = False
        while not local.done:
            self._log.debug('Executing update on category')
            self._exec_with_error_handling(session, local.sql, False)
            local.done = self._done.wait(0)

    def _monitor_waits(self, session):
        """Monitor for lock waits."""
        local = threading.local()
        local.sql_mdl = """
SELECT object_name, lock_type, lock_status,
       owner_thread_id, owner_event_id
  FROM performance_schema.metadata_locks
 WHERE object_type = 'TABLE'
       AND object_schema = 'sakila'
 ORDER BY owner_thread_id, object_name, lock_type""".strip()

        local.sql_mdl_summary = """
SELECT object_name, COUNT(*)
  FROM performance_schema.metadata_locks
 WHERE object_type = 'TABLE'
       AND object_schema = 'sakila'
 GROUP BY object_name
 ORDER BY object_name""".strip()

        local.sql_mdl_waits = "SELECT * FROM sys.schema_table_lock_waits"

        local.sql_mdl_waits_summary = """
SELECT blocking_pid, COUNT(*)
  FROM sys.schema_table_lock_waits
 WHERE waiting_pid <> blocking_pid
 GROUP BY blocking_pid
 ORDER BY COUNT(*) DESC""".strip()

        # {thread_ids} is a placeholder and will be replaced
        # when executing the query.
        local.sql_session_template = """
SELECT thd_id, conn_id, command, state,
       current_statement, time, statement_latency,
       trx_latency, trx_state
  FROM sys.session
 WHERE thd_id IN ({thread_ids})
 ORDER BY conn_id""".strip()

        local.sql_innodb_waits = "SELECT * FROM sys.innodb_lock_waits"

        local.sql_session = self._sql_formatter.sql_global_sub(
            local.sql_session_template, 0)

        # Set statement_truncate_len high enough to include the whole
        # statement for all the queries.
        session.run_sql("SET @sys.statement_truncate_len = 128")

        # Wait for the ALTER thread to have a pending metadata lock
        local.thread_ids = [
            self._thread_ids['customer_1'],
            self._thread_ids['customer_2'],
            self._thread_ids['alter'],
        ]
        self._wait_metadata_lock(session, local.thread_ids, 2)
        self._log.debug('Detected pending metadata lock for the alter ' +
                        'thread')

        self._log_lock.acquire()
        self._exec_and_print_sql(session, local.sql_mdl, 'vertical')
        self._exec_and_print_sql(session, local.sql_mdl_summary)
        self._exec_and_print_sql(session, local.sql_mdl_waits, 'vertical')
        self._exec_and_print_sql(session, local.sql_mdl_waits_summary)
        self._exec_and_print_sql(session, local.sql_session, 'vertical')
        self._log_lock.release()
        local.rows = []
        while len(local.rows) == 0:
            local.result = session.run_sql(local.sql_innodb_waits)
            sleep(0.1)  # Race condition in mysqlsh
            local.rows = local.result.fetch_all()

        self._log_lock.acquire()
        print('mysql> ' +
              self._sql_formatter.indent_sql(local.sql_innodb_waits, 7) +
              r'\G')
        local.i = 0
        for local.row in local.rows:
            local.i += 1
            print(f'*************************** {local.i}. row ' +
                  '***************************')
            local.j = 0
            for local.column in local.result.columns:
                print(f'{local.column.column_label:>28s}: ' +
                      f'{local.row[local.j]}')
                local.j += 1
        if len(local.rows) == 1:
            local.rows = f'{len(local.rows)} row in'
        else:
            local.rows = f'{len(local.rows)} rows in'
        print(f'{local.rows} set ({local.result.execution_time})')
        print('')
        self._log_lock.release()

    def _monitor_metrics(self, session, done):
        """Collect monitoring information."""
        local = threading.local()
        # Collect InnoDB and global metrics
        local.metrics = libs.metrics.Metrics(session)

        local.count_metrics = [
            'innodb_row_lock_current_waits',
            'lock_row_lock_current_waits',
        ]
        local.delta_metrics = [
            'innodb_row_lock_time',
            'innodb_row_lock_waits',
            'lock_deadlocks',
            'lock_timeouts',
        ]
        local.names = ',\n          '.join(
            [f"'{name}'" for name in
             local.count_metrics + local.delta_metrics]
        )

        # Get the number of lock errors
        local.sql_errors = """
SELECT error_number, error_name, sum_error_raised
  FROM performance_schema.events_errors_summary_global_by_error
 WHERE error_name IN ('ER_LOCK_WAIT_TIMEOUT', 'ER_LOCK_DEADLOCK')""".strip()

        # Get statistics per statement type
        local.sql_stmts_events = """
SELECT event_name, count_star, sum_errors
  FROM performance_schema.events_statements_summary_global_by_event_name
 WHERE event_name IN ('statement/sql/alter_table',
                      'statement/sql/update')
"""
        # Get lock related metrics
        local.sql_metrics = f"""
SELECT Variable_name, Variable_value
  FROM sys.metrics
 WHERE Variable_name IN (
          {local.names}
       )""".strip()

        self._log_lock.acquire()
        print('')
        self._exec_and_print_sql(session, local.sql_errors)
        self._exec_and_print_sql(session, local.sql_stmts_events)
        self._exec_and_print_sql(session, local.sql_metrics)
        self._log_lock.release()

        local.interval = timedelta(seconds=1)
        local.next_wakeup = datetime.now()
        local.done = False
        while not local.done:
            local.metrics.collect()
            local.next_wakeup += local.interval
            local.sleep_time = (local.next_wakeup -
                                datetime.now()).total_seconds()
            local.done = done.wait(local.sleep_time)

        self._log_lock.acquire()
        print('')
        print('-- Metrics reported by count collected during the test:')
        local.metrics.write_csv(local.count_metrics)
        print('')
        print('-- Metrics reported by rate collected during the test:')
        local.metrics.write_rate_csv(local.delta_metrics)
        print('')
        self._exec_and_print_sql(session, local.sql_errors)
        self._exec_and_print_sql(session, local.sql_stmts_events)
        self._exec_and_print_sql(session, local.sql_metrics)
        self._log_lock.release()

    def execute(self, uri):
        """This workload uses five connections in four threads:
          * Two that keeps updating the sakila.payment table in such a
            way that there is always a transaction ongoing but none of
            the transactions are long running.
          * One that executes ALTER TABLE sakila.inventory.
          * One that updates the sakila.film_category table.
          * One that updates the sakila.category table.
        """

        # Create the mutexes used to ensure the correct flow
        self._log.lock = self._log_lock
        # self._log.level = libs.log.DEBUG
        self._mutex_payment_start = threading.Lock()
        self._mutex_payment_commit = threading.Lock()
        self._done = threading.Event()

        # Create the five sessions
        sessions = {
            'film_category': libs.util.get_session(self._workload, uri),
            'category': libs.util.get_session(self._workload, uri),
            'customer_1': libs.util.get_session(self._workload, uri),
            'customer_2': libs.util.get_session(self._workload, uri),
            'alter': libs.util.get_session(self._workload, uri),
        }

        # Get the processlist, thread, and event ids for the connections
        for name in sessions:
            (self._processlist_ids[name],
             self._thread_ids[name],
             self._event_ids[name]
             ) = libs.query.get_connection_ids(sessions[name])

        # Output the connection and thread ids, so make it easier to
        # investigate the workload.
        self._log.ids(
            list(self._processlist_ids.values()),
            list(self._thread_ids.values()),
            list(self._event_ids.values())
        )
        self._sql_formatter = libs.query.Formatter(
            list(self._processlist_ids.values()),
            list(self._thread_ids.values()),
            list(self._event_ids.values())
        )

        # Start a monitoring thread which will output diagnostics
        # information. The monitoring thread is stopped by through
        # a threading event.
        monitor_done = threading.Event()
        monitor_metrics_session = libs.util.get_session(self._workload, uri)
        monitor_waits_session = libs.util.get_session(self._workload, uri)
        monitor_metrics = threading.Thread(
            target=self._monitor_metrics,
            daemon=True,
            args=(monitor_metrics_session, monitor_done)
        )
        monitor_metrics.start()
        monitor_waits = threading.Thread(
            target=self._monitor_waits,
            daemon=True,
            args=(monitor_waits_session, )
        )
        monitor_waits.start()
        self._log.debug('Waiting 2 seconds to let the monitoring collect ' +
                        'some information before starting the test.')
        sleep(2)

        # Create the threads
        threads = []
        for name in sessions:
            args = [sessions[name]]
            if name in ('customer_1', 'customer_2'):
                target = getattr(self, f'_customer')
                # Set the customer_id for each thread
                if name == 'customer_1':
                    args.append(42)
                    args.append(self._thread_ids['customer_2'])
                else:
                    args.append(99)
                    args.append(self._thread_ids['customer_1'])
            else:
                target = getattr(self, f'_{name}')

            threads.append(
                threading.Thread(target=target, daemon=True, name=name,
                                 args=args)
            )

        # Start the threads - sleep a second between each thread to
        # ensure that they execute their first statement in the right
        # order.
        self._log.debug('Starting the threads.')
        for thread in threads:
            thread.start()
            sleep(0.5)
        self._log.debug('All threads started.')

        # Let the test run for a while
        sleep(self._runtime)

        # Output the monitoring data
        monitor_done.set()
        monitor_metrics.join()
        monitor_metrics_session.close()

        # Clean up
        self._log.info('Stopping the threads.')
        self._done.set()
        monitor_waits.join()
        monitor_waits_session.close()
        # Wait for the threads to complete their work
        for thread in threads:
            thread.join()
            sessions[thread.name].close()

        self._log.lock = None
        print('')
