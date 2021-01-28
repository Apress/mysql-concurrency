import re
from datetime import datetime
from collections import namedtuple

SQL = "SHOW ENGINE INNODB MUTEX"
MUTEX_ROW = namedtuple('MutexRow', ['Type', 'Name', 'Status'])
RE_STATUS_WAITS = re.compile(r'\bwaits=(\d+)\b')
RE_NAME_FILE = re.compile(r'(\w+\.\w+):(\d+)$')


def _get_delta_waits(last, prev):
    """Calculate the delta for all mutex waits."""
    delta = {}
    min_width_key = 0
    min_width_waits = 0
    for key in last:
        total_waits = last[key]
        try:
            prev_waits = prev[key]
        except KeyError:
            prev_waits = 0
        delta_waits = total_waits - prev_waits
        if delta_waits > 0:
            delta[key] = delta_waits
            min_width_key = max(min_width_key, len(key))
            min_width_waits = max(min_width_waits, len(str(delta_waits)))

    return delta


def _gen_delta_report(delta, key_header):
    """Generate a report using the MySQL table format for a given
    set of delta values."""
    min_width_key = len(key_header)
    min_width_waits = len('Waits')

    for key in delta:
        min_width_key = max(min_width_key, len(key))
        min_width_waits = max(min_width_waits, len(str(delta[key])))

    bar = '+-' + '-' * min_width_key + '-'
    bar += '+-' + '-' * min_width_waits + '-+'
    fmt_header = f'| {{0:{min_width_key}s}} '
    fmt_header += f'| {{1:{min_width_waits}s}} |\n'
    fmt = f'| {{0:{min_width_key}s}} '
    fmt += f'| {{1:{min_width_waits}d}} |\n'
    report = bar + '\n'
    report += fmt_header.format(key_header, 'Waits')
    report += bar + '\n'
    delta_sorted = sorted(delta.items(), key=lambda item: item[1])
    for key, waits in delta_sorted:
        report += fmt.format(key, waits)
    report += bar
    return report


class InnodbMutexMonitor(object):
    """Class for working with the InnoDB mutex monitor"""
    _session = None
    _rows = []
    _total_waits = 0
    _prev_total_waits = 0
    _prev_waits_by_file = {}
    _prev_waits_by_file_line = {}
    _waits_by_name = {}
    _waits_by_file = {}
    _waits_by_file_line = {}
    _output_time = None

    def __init__(self, session):
        """Initialize the instance"""
        self._session = session
        self._prev_total_waits = 0
        self._prev_waits_by_file = {}
        self._prev_waits_by_file_line = {}
        self._reset()

    @property
    def output_time(self):
        """Return the time the output."""
        return self._output_time

    @property
    def total_waits(self):
        """The total number of waits."""
        return self._total_waits

    @property
    def waits_increased(self):
        """Whether the number of waits have increased compared to the
        previous data collection."""
        return self._total_waits > self._prev_total_waits

    @property
    def report(self):
        """Return the latest output using the MySQL table format."""
        min_width_type = len('Type')
        min_width_name = len('Name')
        min_width_status = len('Status')
        for row in self._rows:
            min_width_type = max(min_width_type, len(row.Type))
            min_width_name = max(min_width_name, len(row.Name))
            min_width_status = max(min_width_status, len(row.Status))
        bar = '+-' + '-' * min_width_type + '-'
        bar += '+-' + '-' * min_width_name + '-'
        bar += '+-' + '-' * min_width_status + '-+'
        fmt = f'| {{0:{min_width_type}s}} '
        fmt += f'| {{1:{min_width_name}s}} '
        fmt += f'| {{2:{min_width_status}s}} |\n'
        report = f'mysql> {SQL};\n'
        report += bar + '\n'
        report += fmt.format('Type', 'Name', 'Status')
        report += bar + '\n'
        for row in self._rows:
            report += fmt.format(row.Type, row.Name, row.Status)
        report += bar
        return report

    def delta_by_file(self, fmt='dict'):
        """Return the delta compared to the previous output grouped by
        file name. The format can be 'dict' or 'table'."""
        delta = _get_delta_waits(self._waits_by_file, self._prev_waits_by_file)

        if fmt == 'dict':
            return delta
        else:
            return _gen_delta_report(delta, 'File')

    def delta_by_file_line(self, fmt='dict'):
        """Return the delta compared to the previous output grouped by
        file name and line number. The format can be 'dict' or
        'table'."""
        delta = _get_delta_waits(self._waits_by_file_line,
                                 self._prev_waits_by_file_line)

        if fmt == 'dict':
            return delta
        else:
            return _gen_delta_report(delta, 'File:Line')

    def get_waits_by_name(self, name):
        """Get the number of waits by the full name."""
        try:
            waits = self._waits_by_name[name]
        except KeyError:
            waits = 0
        return waits

    def get_waits_by_file(self, filename):
        """Get the number of waits grouped by the file name."""
        try:
            waits = self._waits_by_file[filename]
        except KeyError:
            waits = 0
        return waits

    def get_waits_by_file_line(self, filename_line):
        """Get the number of waits grouped by file name and line
        number."""
        try:
            waits = self._waits_by_file_line[filename_line]
        except KeyError:
            waits = 0
        return waits

    def _reset(self):
        """Reset the statistics, for example before collecting a new
        output."""
        self._rows = []
        self._output_time = None
        self._total_waits = 0
        self._waits_by_name = {}
        self._waits_by_file = {}
        self._waits_by_file_line = {}

    def _analyze(self):
        """Analyze the latest fetched output."""
        self._total_waits = 0
        for row in self._rows:
            m_waits = RE_STATUS_WAITS.search(row.Status)
            if m_waits is not None:
                waits = int(m_waits[1])
                self._total_waits += waits
                try:
                    self._waits_by_name[row.Name] += waits
                except KeyError:
                    self._waits_by_name[row.Name] = waits

                file = RE_NAME_FILE.search(row.Name)
                if file is not None:
                    try:
                        self._waits_by_file[file[1]] += waits
                    except KeyError:
                        self._waits_by_file[file[1]] = waits

                    file_line = f'{file[1]}:{file[2]}'
                    try:
                        self._waits_by_file_line[file_line] += waits
                    except KeyError:
                        self._waits_by_file_line[file_line] = waits

    def fetch(self):
        """Fetch the mutex monitor output and store it line by line."""
        self._prev_total_waits = self._total_waits
        self._prev_waits_by_file = self._waits_by_file
        self._prev_waits_by_file_line = self._waits_by_file_line
        self._reset()
        self._output_time = datetime.now()
        result = self._session.run_sql(SQL)
        for row in result.fetch_all():
            self._rows.append(MUTEX_ROW(*row))

        self._analyze()
