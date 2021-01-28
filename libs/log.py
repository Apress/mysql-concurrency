from datetime import datetime
import re

import mysqlsh

DEBUG = 0
INFO = 1
WARNING = 2
ERROR = 3

RE_ERROR_MSG = re.compile('^.+.run_sql: ')


def _now():
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.%f")


class Log(object):
    """Provides logging for the locks_book_2 module. This includes keeping
    track of the connection that the latest printed query and result
    were for, so a "-- Connection <number>" can be printed when the
    connection changes."""
    # The last connection id that there was logged query or result
    # information for
    _last_connection = None
    # The current workload. When that changes, _last_conn_id is set to None
    _workload = None
    _level = 2
    # An optional lock object. If present, writes are only done when the
    # lock is held.
    _lock = None
    _sql_formatter = None

    def __init__(self, level=INFO):
        self._last_connection = None
        self._workload = None
        self._level = level
        self._lock = None
        self._sql_formatter = None

    @property
    def level(self):
        return self._level

    @level.setter
    def level(self, level):
        self._level = level

    @property
    def lock(self):
        return self._lock

    @lock.setter
    def lock(self, lock):
        self._lock = lock

    @property
    def sql_formatter(self):
        return self._sql_formatter

    @sql_formatter.setter
    def sql_formatter(self, formatter):
        self._sql_formatter = formatter

    def _lock_acquire(self):
        if self._lock is not None:
            self._lock.acquire()

    def _lock_release(self):
        if self._lock is not None:
            self._lock.release()

    def _write(self, msg):
        self._lock_acquire()
        print(msg)
        self._lock_release()

    def _write_result(self, result, dump_format):
        self._lock_acquire()
        num_rows = mysqlsh.globals.shell.dump_rows(result, dump_format)
        self._lock_release()
        return num_rows

    def _connection_comment(self, connection):
        if connection != self._last_connection:
            self._last_connection = connection
            self._write(f'-- Connection {connection}')

    def ids(self, processlist_ids, thread_ids, event_ids):
        """Write the processlist ids and thread ids for the connections
        involved in the workload. This makes it easier to investigate
        the result."""

        if len(processlist_ids) > 0:
            msg = '-- Connection   Processlist ID   Thread ID   Event ID\n'
            msg += '-- ' + '-' * 50 + '\n'
            for i in range(len(processlist_ids)):
                processlist_id = processlist_ids[i]
                thread_id = thread_ids[i]
                event_id = event_ids[i]
                msg += f'-- {i + 1:10d}   {processlist_id:14d}   '
                msg += f'{thread_id:9d}   {event_id:8d}' + '\n'
            self._write(msg)

    def _log(self, connection, severity, msg):
        now = _now()
        self._write(f'{now} {connection:2d} [{severity.upper()}] {msg}')

    def error(self, msg, connection=0):
        if self._level <= ERROR:
            self._log(connection, 'ERROR', msg)

    def warning(self, msg, connection=0):
        if self._level <= WARNING:
            self._log(connection, 'WARNING', msg)

    def info(self, msg, connection=0):
        if self._level <= INFO:
            self._log(connection, 'INFO', msg)

    def debug(self, msg, connection=0):
        if self._level <= DEBUG:
            self._log(connection, 'DEBUG', msg)

    def caption(self):
        msg = f'*   {self.workload.name}. {self.workload.description}   *'
        banner = '*' * len(msg)
        space = '*' + ' ' * (len(msg) - 2) + '*'
        caption = f"""
{banner}
{space}
{msg}
{space}
{banner}

"""
        self._write(caption)

    def sql(self, query, sql=None, indent=0):
        """Write a query with it's prompt indicating which connection
        is executing the query. Each time the connection changes
        prefix with a comment line showing the new connection id.

        If sql is given that is used, otherwise the sql attribute of
        the query is used. The indent argument specifies how much
        subsequent lines than the first are already indented."""

        msg = ''
        self._connection_comment(query.connection)
        try:
            if query.comment != '':
                msg += f'-- {query.comment}\n'
        except AttributeError:
            pass

        # Indent the query with 14 characters except for the first line
        # This ensures the lines align nicely.
        if sql is None:
            sql = query.sql
        if self._sql_formatter is not None:
            sql = self._sql_formatter.sql_global_sub(sql, query.connection)
            sql = self._sql_formatter.indent_sql(sql, 14 - indent)
        if query.format == 'vertical':
            delimiter = '\\G'
        else:
            delimiter = ';'
        msg += f'Connection {query.connection}> {sql}{delimiter}'
        self._write(msg)

    def result(self, result):
        """Print the result in the desired format (same formats as for
        shell.dump_rows() are supported). Prefix with a comment line
        showing the new connection id if the connection id has changed
        since the last message."""

        self._connection_comment(result.query.connection)
        if result.error:
            msg = RE_ERROR_MSG.sub('', result.error.msg)
            try:
                self._write(f'ERROR: {result.error.code}: {msg}')
            except AttributeError:
                self._write(f'ERROR: {result.error}')
        else:
            timing = result.result.execution_time
            has_data = result.result.has_data()
            if not has_data:
                # Query OK, 1 row affected (0.3618 sec)
                items = result.result.affected_items_count
                if items == 1:
                    rows = 'row'
                else:
                    rows = 'rows'
                self._write(f'Query OK, {items} {rows} affected ({timing})')

            # For result without data this will output information about
            # the number of matched and changed rows and number of
            # warnings
            num_rows = self._write_result(result.result, result.query.format)

            if has_data:
                # Empty set (0.0015 sec)
                # 2 rows in set (0.2705 sec)
                if num_rows == 0:
                    rows = 'Empty'
                elif num_rows == 1:
                    rows = f'{num_rows} row in'
                else:
                    rows = f'{num_rows} rows in'
                self._write(f'{rows} set ({timing})')

            if result.result.warnings_count > 0:
                msg = ''
                for warning in result.result.warnings:
                    msg += f'{warning[0]} (code {warning[1]}): {warning[2]}\n'
                self._write(msg)

        self._write('')

    @property
    def workload(self):
        return self._workload

    @workload.setter
    def workload(self, new_workload):
        self._workload = new_workload
        self._last_connection = None
