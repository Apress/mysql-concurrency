import sys
import time
import queue
import re
from collections import namedtuple

RESULT = namedtuple('Result', ['query', 'result', 'error'])

RE_INDENT = re.compile(r'^', re.MULTILINE)
RE_SQL_PARAM = re.compile(r'\?')
RE_SQL_PROCESSLIST_IDS = re.compile(r'{processlist_ids}')
RE_SQL_THREAD_IDS = re.compile(r'{thread_ids}')
RE_SQL_THREAD_IDS_NOT_SELF = re.compile(r'{thread_ids_not_self}')
RE_SQL_ID_FOR_CONNECTION = re.compile(
    r'{(event|processlist|thread)_id_connection_(\d+)(?:\+(\d+))?}')
RE_SQL_THREAD_ID_FOR_CONNECTION = re.compile(
    r'{thread_id_connection_(\d+)(?:\+(\d+))?}')
RE_SQL_PROCESSLIST_ID_FOR_CONNECTION = re.compile(
    r'{processlist_id_connection_(\d+)(?:\+(\d+))?}')
RE_SQL_EVENT_ID_FOR_CONNECTION = re.compile(
    r'{event_id_connection_(\d+)(?:\+(\d+))?}')


def get_connection_ids(session):
    """Obtain the connection and thread id for a connection."""

    sql = """
SELECT PROCESSLIST_ID, THREAD_ID, EVENT_ID
  FROM performance_schema.threads
       INNER JOIN performance_schema.events_statements_current
            USING (THREAD_ID)
 WHERE PROCESSLIST_ID = CONNECTION_ID()""".strip()
    result = session.run_sql(sql)
    row = result.fetch_one()
    return row[0], row[1], row[2]


def get_from_queue(q, timeout=None, iterations=1, on_empty=None):
    """Get the next value from a queue with support for looping with
    short timeouts."""
    for i in range(iterations):
        try:
            item = q.get(timeout=timeout)
        except queue.Empty:
            pass
        else:
            return item

    return on_empty


class Formatter(object):
    """Adds support for replacing parameters in the queries and to
    indent lines other than the first to make the lines align."""

    _processlist_ids = []
    _thread_ids = []
    _event_ids = []

    def __init__(self, processlist_ids, thread_ids, event_ids):
        """Initialize the class."""
        self._processlist_ids = processlist_ids
        self._thread_ids = thread_ids
        self._event_ids = event_ids

    @staticmethod
    def indent_sql(sql, spaces):
        """Indent all but the first line of an SQL statement to make the
        lines align nicely e.g. depending on the width of the prompt"""

        return RE_INDENT.sub(' ' * spaces, sql).strip()

    @staticmethod
    def sql_param_sub(value, sql):
        """Substitute the next ? parameter in an SQL statement."""
        return RE_SQL_PARAM.sub(str(value), sql, 1)

    def sql_global_sub(self, sql, connection):
        """Replace placeholders for "global" properties such as all
        processlist ids and thread ids."""

        # Processlist ids
        ids = [str(pid) for pid in self._processlist_ids]
        sql = RE_SQL_PROCESSLIST_IDS.sub(', '.join(ids), sql)

        # Thread ids
        ids = [str(tid) for tid in self._thread_ids]
        sql = RE_SQL_THREAD_IDS.sub(', '.join(ids), sql)

        # Thread ids except for the connection itself
        ids = [str(self._thread_ids[i]) for i in range(len(self._thread_ids))
               if i + 1 != connection]
        sql = RE_SQL_THREAD_IDS_NOT_SELF.sub(', '.join(ids), sql)

        # Individual thread ids
        for m in RE_SQL_ID_FOR_CONNECTION.finditer(sql):
            id_type = m[1]
            connection = int(m[2])
            adjust = m[3]
            if id_type == 'event':
                value = self._event_ids[connection-1]
                sub_expression = RE_SQL_EVENT_ID_FOR_CONNECTION
            elif id_type == 'processlist':
                value = self._processlist_ids[connection-1]
                sub_expression = RE_SQL_PROCESSLIST_ID_FOR_CONNECTION
            else:
                value = self._thread_ids[connection-1]
                sub_expression = RE_SQL_THREAD_ID_FOR_CONNECTION

            if adjust is not None:
                value += int(adjust)
            sql = sub_expression.sub(str(value), sql)
        return sql

    def for_connection(self, sql, connection, parameters, indent):
        """Make parameter substitutions for a connection using
        the connection's process list, thread, event ids."""
        for parameter in parameters:
            try:
                key, adjust = parameter.split('+')
            except ValueError:
                # No + sign in the parameter
                key = parameter
                adjust = None
            value = None
            if key == 'processlist_id':
                value = self._processlist_ids[connection]
            elif key == 'thread_id':
                value = self._thread_ids[connection]
            elif key == 'event_id':
                value = self._event_ids[connection]
            elif key == 'thread_ids':
                value = ', '.join(self._thread_ids)

            if adjust is not None:
                value = int(value) + int(adjust)

            sql = self.sql_param_sub(value, sql)
        sql = self.indent_sql(sql, indent)

        return sql


class Query(object):
    """Class for handling query execution. This keep track of for
    example the number of outstanding queries."""
    _outstanding = 0
    _query_queue = None
    _result_queue = None
    _processlist_ids = None
    _thread_ids = None
    _log = None
    _concurrent = False

    def __init__(self, query_queue, result_queue, processlist_ids, thread_ids,
                 log, concurrent):
        """Initialize the class."""
        self._outstanding = 0
        self._query_queue = query_queue
        self._result_queue = result_queue
        self._processlist_ids = processlist_ids
        self._thread_ids = thread_ids
        self._log = log
        self._concurrent = concurrent

    def exec(self, query):
        """Execute a query."""
        if not query.silent:
            self._log.sql(query)
            if query.show_result:
                self._outstanding += 1
            else:
                print('')
        self._log.debug(f'Adding query to queue: {query}')
        self._query_queue[query.connection - 1].put(query)
        if query.sleep > 0:
            time.sleep(query.sleep)

        if query.wait:
            # Wait for all outstanding results to be returned
            results = {}
            self._log.debug('Waiting for result, outstanding ' +
                            f'{self._outstanding}')
            while self._outstanding > 0:
                result = get_from_queue(
                    self._result_queue, timeout=10, iterations=1,
                    on_empty=RESULT(None, None, None))
                self._outstanding -= 1

                if result.query is None:
                    self._log.error("Didn't receive a result. Abandoning.")
                    sys.exit(1)
                elif result.query.show_result and not result.query.silent:
                    results[result.query.connection] = result

            # Sort the results so the last query to be executed is
            # handled first (if possible) to reduce the number of times
            # the connection id changes.
            result_conns = list(results.keys())
            result_conns.sort(
                key=lambda i: -1 if i == query.connection else i)
            for connection in result_conns:
                self._log.result(results[connection])
        elif not query.silent and query.show_result:
            print('')

        if not query.wait and query.sleep == 0 and not self._concurrent:
            # Wait for a brief moment to allow the query to start
            time.sleep(0.1)
