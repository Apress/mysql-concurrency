import multiprocessing
import threading
import random
from time import sleep
from datetime import datetime
from datetime import timedelta

import mysqlsh

# noinspection PyUnresolvedReferences
from concurrency_book import libs
# noinspection PyUnresolvedReferences
import concurrency_book.libs.innodb_monitor
# noinspection PyUnresolvedReferences
import concurrency_book.libs.innodb_mutex
# noinspection PyUnresolvedReferences
import concurrency_book.libs.innodb_buffer_pool
# noinspection PyUnresolvedReferences
import concurrency_book.libs.metrics
# noinspection PyUnresolvedReferences
import concurrency_book.libs.log
# noinspection PyUnresolvedReferences
import concurrency_book.libs.query
# noinspection PyUnresolvedReferences
import concurrency_book.libs.util

DEFAULT_RUNTIME = 10
# The minimum number of semaphores in the semaphore section before
# outputting it.
MIN_SEMAPHORES = 1
# The minimum time in seconds between InnoDB monitor outputs
MIN_MONITOR_WAIT = 4
# The prefix used when naming indexes created by this test
INDEX_PREFIX = 'idx_concurrency_book_'

# Get the min, avg, and max salaries by department
# noinspection SqlAggregates
SQL_SCAN = """
SELECT dept_name, MIN(salary) min_salary,
       AVG(salary) AS avg_salary, MAX(salary) AS max_salary
  FROM employees.departments
       INNER JOIN employees.dept_emp USING (dept_no)
       INNER JOIN employees.salaries USING (emp_no)
 WHERE dept_emp.to_date = '9999-01-01'
       AND salaries.to_date = '9999-01-01'
 GROUP BY dept_no
 ORDER BY dept_name""".strip()


class SemaphoreWaits(object):
    """Implement a workload causing semaphore waits for the adaptive
    hash index."""
    _workload = None
    _session = None
    _log = None
    _max_id = None
    _num_read = None
    _num_write = None
    _max_runtime = None
    _restart_mysql = False
    _delete_indexes = True

    def __init__(self, workload, session, log):
        """Initialize the class"""
        self._workload = workload
        self._session = session
        self._log = log
        self._max_id = None
        self._num_connections = None
        self._max_runtime = None
        self._restart_mysql = False
        self._delete_indexes = True
        self._prompt_settings()

    def _prompt_settings(self):
        """Prompt for the settings to use for the workload."""
        num_cpus = multiprocessing.cpu_count()
        prompt = 'Specify the number of read-write connections'
        self._num_write = libs.util.prompt_int(0, 4 * num_cpus - 1, 1, prompt)
        prompt = 'Specify the number of read-only connections'
        self._num_read = libs.util.prompt_int(
            1,
            4 * num_cpus - self._num_write,
            max(1, num_cpus - self._num_write),
            prompt
        )

        prompt = 'Specify the number of seconds to run for'
        self._max_runtime = libs.util.prompt_int(1, 3600, DEFAULT_RUNTIME,
                                                 prompt)

        prompt = 'Restart MySQL before executing the test?'
        self._restart_mysql = libs.util.prompt_bool('No', prompt)

        prompt = 'Delete the test specific indexes after executing the test?'
        self._delete_indexes = libs.util.prompt_bool('Yes', prompt)

        print('')

    def _execute_scans(self, uri):
        """Execute the queries triggering scans for the secondary
        indexes."""
        local = threading.local()
        local.session = libs.util.get_session(self._workload, uri)

        time_start = datetime.now()
        runtime = 0
        while runtime < self._max_runtime:
            try:
                # The session is not entirely thread safe, so some
                # system and index errors can occur. For the purpose of
                # this test, those can be ignored.
                local.result = local.session.run_sql(SQL_SCAN)
                local.result.fetch_all()
            except (SystemError, IndexError, AttributeError):
                pass

            runtime = (datetime.now() - time_start).total_seconds()

        local.session.close()

    def _execute_updates(self, uri):
        """Execute the statements involved in the write workload."""
        local = threading.local()
        local.session = libs.util.get_session(self._workload, uri)
        local.session.run_sql("SET SESSION autocommit = 0")

        local.time_start = datetime.now()
        local.runtime = 0
        while local.runtime < self._max_runtime:
            # Get a random last name
            local.sql = """
SELECT last_name
  FROM employees.employees
 WHERE emp_no = ?""".strip()
            local.last_name = None
            while local.last_name is None:
                local.emp_no = random.randint(10001, 499999)
                try:
                    local.row = local.session.run_sql(
                        local.sql, (local.emp_no,)
                    ).fetch_one()
                except (SystemError, IndexError, AttributeError):
                    pass
                else:
                    if local.row is not None:
                        local.last_name = local.row[0]

            # Increase the salary for all employees with last_name
            # First, insert the rows with the new salaries
            # Use the current from date + 1 day as the new from date
            local.sql = """
SELECT emp_no, salary, from_date + INTERVAL 1 DAY
  FROM employees.employees
       INNER JOIN employees.salaries USING (emp_no)
 WHERE employees.last_name = ?
       AND to_date = '9999-01-01'""".strip()
            local.rows = None
            while local.rows is None:
                try:
                    local.rows = local.session.run_sql(
                        local.sql, (local.last_name,)
                    ).fetch_all()
                except (SystemError, IndexError, AttributeError):
                    pass

            local.sql_insert = """
INSERT INTO employees.salaries
VALUES (?, ?, ?, '9999-01-01')"""
            local.sql_update = """
UPDATE employees.salaries
   SET to_date = ?
 WHERE emp_no = ? AND to_date = '9999-01-01'""".strip()
            for local.row in local.rows:
                try:
                    local.session.run_sql(local.sql_insert, local.row)
                    local.session.run_sql(
                        local.sql_update, (local.row[2], local.row[0])
                    )
                except (SystemError, IndexError, AttributeError):
                    pass

            # Commit - if this fails, it'll just combine two iterations
            # into one transaction. That's fine.
            try:
                local.session.commit()
            except (SystemError, IndexError, AttributeError):
                pass
            local.runtime = (
                        datetime.now() - local.time_start).total_seconds()

        local.session.close()

    def _monitor(self, uri, lock, done):
        """Monitor MySQL during the test."""
        local = threading.local()
        # Get a database session
        local.session = libs.util.get_session(self._workload, uri)
        # Collect the InnoDB monitor
        local.innodb = libs.innodb_monitor.InnodbMonitor(local.session)
        # Collect InnoDB and global metrics
        local.metrics = libs.metrics.Metrics(local.session)
        # Collect buffer pool statistics before and after the test
        local.bp_stats = libs.innodb_buffer_pool.Stats(local.session)
        local.bp_stats.collect()
        # Collect mutex statuses before and after the test
        local.mutex = libs.innodb_mutex.InnodbMutexMonitor(local.session)
        local.mutex.fetch()

        local.interval = timedelta(seconds=1)
        local.next_wakeup = datetime.now()
        local.last_innodb = datetime(2020, 1, 1)
        local.not_done = True
        while local.not_done:
            try:
                local.metrics.collect()
            except (SystemError, IndexError, AttributeError):
                pass

            try:
                local.innodb.fetch()
            except (SystemError, IndexError, AttributeError):
                pass

            # At most output the InnoDB monitor every 5 seconds to avoid
            # spamming.
            local.now = datetime.now()
            local.delta_innodb = local.now - local.last_innodb
            local.semaphores = local.innodb.get_section('SEMAPHORES')
            if (local.delta_innodb.total_seconds() >= MIN_MONITOR_WAIT and
                    local.semaphores is not None and
                    local.semaphores.num_waits >= MIN_SEMAPHORES):
                local.last_innodb = local.now
                lock.acquire()
                print('')
                print(r'mysql> SHOW ENGINE INNODB STATUS\G')
                print('...')
                print(local.semaphores.content)
                print('...')
                print('')
                lock.release()

            local.next_wakeup += local.interval
            local.sleep_time = (local.next_wakeup -
                                datetime.now()).total_seconds()
            local.not_done = not done.wait(local.sleep_time)

        local.bp_stats.collect()
        local.young = local.bp_stats.delta('pages_made_young')
        local.not_young = local.bp_stats.delta('pages_not_made_young')

        # Get the INSERT BUFFER AND ADAPTIVE HASH INDEX
        local.innodb.fetch()
        local.innodb_ahi_section = local.innodb.get_section(
            'INSERT BUFFER AND ADAPTIVE HASH INDEX')

        # Get mutex statistics for the entire run
        local.mutex.fetch()

        # Write some monitoring information
        local.rwlock_metrics = [
            'innodb_rwlock_s_spin_waits',
            'innodb_rwlock_s_spin_rounds',
            'innodb_rwlock_s_os_waits',
            'innodb_rwlock_x_spin_waits',
            'innodb_rwlock_x_spin_rounds',
            'innodb_rwlock_x_os_waits',
        ]
        local.ahi_metrics = [
            'adaptive_hash_searches',
            'adaptive_hash_searches_btree',
        ]
        lock.acquire()
        print('')
        print('-- rwlock metrics collected during the test:')
        local.metrics.write_rate_csv(local.rwlock_metrics)
        print('')
        print('-- Adaptive hash index metrics collected during the test:')
        local.metrics.write_rate_csv(local.ahi_metrics)
        print('')
        print('-- Pages made or not made young:')
        print(f'Made young ......: {local.young.value:6d} pages ' +
              f'({local.young.rate:8.2f} pages/s)')
        print(f'Not made young ..: {local.not_young.value:6d} pages ' +
              f'({local.not_young.rate:8.2f} pages/s)')
        print('')
        print(r'mysql> SHOW ENGINE INNODB STATUS\G')
        print('...')
        print(local.innodb_ahi_section.content)
        print('...')
        print('')
        print('-- Total mutex and rw-semaphore waits during test:')
        print(local.mutex.delta_by_file_line('report'))
        print('')
        lock.release()

        local.session.close()

    def _cleanup(self):
        """Clean up the configuration and index changes made for this
        workload."""
        sql_drop_index = """
ALTER TABLE employees.`{0}`
 {1}""".strip()

        sql_get_indexes = """
SELECT DISTINCT table_name, index_name
  FROM information_schema.STATISTICS
 WHERE table_schema = 'employees'
       AND index_name LIKE ?
 ORDER BY table_name, index_name""".strip()

        sql_check = """
SELECT 1
  FROM information_schema.STATISTICS
 WHERE table_schema = 'employees'
       AND table_name = ?
       AND index_name = ?
 LIMIT 1""".strip()

        self._session.run_sql("SET GLOBAL innodb_old_blocks_time = 1000")

        if not self._delete_indexes:
            # No more to do
            return

        # Some indexes may have been removed when creating the
        # indexes for this test. Restore those indexes as well.
        restore_indexes = {
            'dept_emp': {
                'dept_no': '`dept_no`',
            },
        }
        result = self._session.run_sql(sql_get_indexes, (INDEX_PREFIX + '%', ))
        indexes = result.fetch_all()
        current_table = None
        drop_clauses = []
        for index in indexes:
            table, index_name = index
            if table != current_table:
                add_clauses = []
                if len(drop_clauses) > 0:
                    try:
                        restore = restore_indexes[current_table]
                    except KeyError:
                        restore = []
                    for add_index in restore:
                        row = self._session.run_sql(
                            sql_check, (current_table, add_index)
                        ).fetch_one()
                        if row is None:
                            columns = restore[add_index]
                            add_clause = f' ADD INDEX `{add_index}` ' + \
                                         f'({columns})'
                            add_clauses.append(add_clause)
                    sql = sql_drop_index.format(
                        current_table, ',\n '.join(add_clauses + drop_clauses)
                    )
                    self._log.info(f'Dropping indexes on the {current_table}' +
                                   ' table.')
                    self._session.run_sql(sql)

                    drop_clauses = []
                current_table = table

            drop_clauses.append(f'DROP INDEX {index_name}')

        if len(drop_clauses) > 0:
            add_clauses = []
            if len(drop_clauses) > 0:
                try:
                    restore = restore_indexes[current_table]
                except KeyError:
                    restore = []
                for add_index in restore:
                    row = self._session.run_sql(
                        sql_check, (current_table, add_index)
                    ).fetch_one()
                    if row is None:
                        columns = restore_indexes[current_table][add_index]
                        add_clause = f' ADD INDEX `{add_index} ({columns})'
                        add_clauses.append(add_clause)

            sql = sql_drop_index.format(
                current_table, ',\n '.join(add_clauses + drop_clauses)
            )
            self._log.info(f'Dropping indexes on the {current_table} ' +
                           'table.')
            self._session.run_sql(sql)

    def _prepare(self):
        """Make indexes and configuration changes for the test."""
        # noinspection SqlAggregates
        sql_check = """
SELECT CONCAT('`',
              GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR '`, `'),
              '`'
             ) AS index_columns
  FROM information_schema.STATISTICS
 WHERE table_schema = 'employees'
       AND table_name = ?
 GROUP BY index_name
HAVING index_columns = ?"""

        sql_add_index = """
ALTER TABLE employees.`{0}`
  {1}""".strip()

        indexes = {
            'dept_emp': (
                '`dept_no`, `to_date`',
            ),
            'employees': (
                '`last_name`, `first_name`',
            ),
            'salaries': (
                '`emp_no`, `to_date`, `salary`',
            ),
        }

        # If requested, restart MySQL
        if self._restart_mysql:
            try:
                # Restarting MySQL causes a DBError exception as the
                # connection goes away while executing the statement.
                self._log.info('Restarting MySQL')
                self._session.run_sql("RESTART")
            except mysqlsh.DBError as e:
                if e.code != 2006:
                    # Unexpected error
                    raise e

            # Reconnect
            start_time = datetime.now()
            connected = False
            self._log.info('Waiting for up to 60 seconds for MySQL to ' +
                           'complete the restart.')
            while (not connected and
                   (datetime.now() - start_time).total_seconds() < 60):
                try:
                    mysqlsh.globals.shell.reconnect()
                except SystemError:
                    pass
                else:
                    connected = self._session.is_open()

            if not self._session.is_open():
                self._log.error('Did not reconnect in 60 seconds.')
                exit(-1)
            self._log.info('MySQL restarted and session reconnected.')

        # Set innodb_buffer_old_blocks_time to 0 to avoid too early
        # eviction of the pages read in by the workload.
        self._session.run_sql("SET GLOBAL innodb_old_blocks_time = 0")

        i = 0
        for table in indexes:
            add_clauses = []
            for index_columns in indexes[table]:
                row = self._session.run_sql(
                    sql_check, (table, index_columns, )
                ).fetch_one()
                if row is None:
                    # The index does not exists
                    add_clauses.append(f'ADD INDEX {INDEX_PREFIX}{i}' +
                                       f' ({index_columns})')
                    i += 1

            if len(add_clauses) > 0:
                if len(add_clauses) > 1:
                    index_noun = 'indexes'
                else:
                    index_noun = 'index'
                self._log.info(f'Adding {len(add_clauses)} {index_noun} to ' +
                               f'the {table} table')
                sql = sql_add_index.format(table, ',\n  '.join(add_clauses))
                self._session.run_sql(sql)

    def _warm_up(self):
        """Warm up the InnoDB buffer pool."""
        self._log.info('Warming up the InnoDB buffer pool.')

        # Disable the derived merge feature to make it possible to
        # query all rows in a table without fetching them.
        # noinspection SqlResolve
        sql_fmt = """
SELECT /*+ NO_MERGE() */ COUNT(*)
  FROM (SELECT *
          FROM employees.`{0}`
       ) a""".strip()
        for table in ['salaries', 'employees', 'departments']:
            self._session.run_sql(sql_fmt.format(table)).fetch_all()

        # Perform the scan query three times
        for i in range(3):
            self._session.run_sql(SQL_SCAN).fetch_all()

    def execute(self, uri):
        """Execute the workload."""
        self._prepare()
        self._warm_up()

        # Create a lock object so two threads can print without
        # stepping on each others toes.
        lock = threading.Lock()
        self._log.lock = lock

        # Start a monitoring thread which will output diagnostics
        # information. The monitoring thread is stopped by through
        # a threading event.
        done = threading.Event()
        monitor = threading.Thread(target=self._monitor,
                                   daemon=True,
                                   args=(uri, lock, done))
        monitor.start()
        self._log.info('Waiting 2 seconds to let the monitoring collect ' +
                       'some information before starting the test.')
        sleep(2)

        # Start the connections doing the work.
        self._log.info('Starting the work connections.')
        total_connections = self._num_read + self._num_write
        connections = []
        time_start = datetime.now()
        for i in range(total_connections):
            if i < self._num_read:
                target = self._execute_scans
            else:
                target = self._execute_updates
            connections.append(threading.Thread(target=target,
                                                daemon=True,
                                                args=(uri, )))
            connections[i].start()

        # Print the progress information, print at least ten times
        # but at most every ten seconds
        interval_float = min(self._max_runtime/10, 10)
        interval_sec = int(interval_float)
        interval_microsec = int((interval_float - interval_sec) * 1000000)
        interval = timedelta(
            seconds=interval_sec,
            microseconds=interval_microsec
        )
        next_wakeup = time_start
        runtime = 0
        count = 0
        while runtime < self._max_runtime:
            count += 1
            next_wakeup += interval
            sleep_time = (next_wakeup - datetime.now()).total_seconds()
            if sleep_time > 0:
                sleep(sleep_time)
            runtime = (datetime.now() - time_start).total_seconds()
            pct_done = min(100, 100 * runtime / self._max_runtime)
            self._log.info(f'Completed {pct_done:3.0f}%')

        self._log.info('Waiting for the connections to complete the current' +
                       ' iteration.')
        for i in range(total_connections):
            connections[i].join()
        time_end = datetime.now()

        self._log.info('Waiting 2 seconds to let the monitoring collect ' +
                       'some information after completing the test.')
        sleep(2)
        done.set()
        monitor.join()
        self._log.lock = None

        # Print total execution time
        time_delta = time_end - time_start
        print(f'-- Total execution time: {time_delta.total_seconds()} seconds')
        print('')

        self._cleanup()
        print('')
