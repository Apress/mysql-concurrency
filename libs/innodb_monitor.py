import re
from datetime import datetime

RE_TIME = r'\d\d\d\d-\d\d-\d\d [\d ]\d:\d\d:\d\d'
RE_HEX = r'(?:0x)?[0-9a-f]*'
RE_BANNER = 'INNODB MONITOR OUTPUT'
RE_START = re.compile(rf'^({RE_TIME}) {RE_HEX} {RE_BANNER}\s+=+\s*\n',
                      re.MULTILINE | re.DOTALL)
RE_END = re.compile(r'^-+\s*\nEND OF INNODB MONITOR OUTPUT($)',
                    re.MULTILINE | re.DOTALL)
RE_SECTION = re.compile(r'^-+\n(.+)\n-+(\n)', re.MULTILINE)
RE_INT_OR_FLOAT = r'\d+(?:\.\d+)?'
RE_SEMAPHORE = re.compile(
    rf'^--Thread \d+ has waited at .+ line \d+ for ({RE_INT_OR_FLOAT}) ' +
    'seconds the semaphore:$',
    re.MULTILINE)


class InnodbMonitor(object):
    """Collect and return information from the InnoDB monitor."""
    _session = None
    _output = None
    _sections = {}
    _output_time = None

    def __init__(self, session):
        """Initialize the data."""
        self._session = session
        self._output = None
        self._reset_sections()

    def _reset_sections(self):
        """Reset the stored sections."""
        self._sections = {}

    def _parse_output(self):
        """Parse the InnoDB monitor report."""
        self._reset_sections()
        m_start = RE_START.search(self._output)
        m_end = RE_END.search(self._output)
        if m_start is None or m_end is None:
            # Invalid output
            self._output = None
            return False

        self._output_time = datetime.fromisoformat(m_start[1])

        # Find the header and offsets for each section
        # Each section ends just before the next section, and the last
        # section ends just before the end banner.
        headers = []
        start_offsets = []
        end_offsets = []
        count = 0
        sections = RE_SECTION.finditer(self._output)
        for section in sections:
            if count > 0:
                end_offsets.append(section.start() - 1)
            start_offsets.append(section.end())
            headers.append(section[1])
            count += 1
        end_offsets.append(m_end.start() - 1)

        # Create an object for each section with the section specific
        # logic.
        for i in range(len(headers)):
            name = headers[i]
            text = self._output[start_offsets[i]:end_offsets[i]]
            self._sections[name] = SECTIONS[name](text)

        return True

    def get_section(self, name):
        """Return a single section."""
        try:
            section = self._sections[name.upper()]
        except KeyError:
            section = None

        return section

    def fetch(self):
        """Fetch the InnoDB monitor report."""
        sql = "SHOW ENGINE INNODB STATUS"
        self._output = self._session.run_sql(sql).fetch_one()[2]
        self._parse_output()


class StatusSection(object):
    """Class for handling individual InnoDB monitor sections."""
    _name = None  # Set in the individual classes
    _body = None

    def __init__(self, body):
        """Initialize the section body."""
        self._body = body

    @property
    def body(self):
        """Return the content of the body."""
        return self._body

    @property
    def name(self):
        """Return the name of the section."""
        return self._name

    @property
    def header(self):
        """Returns the banner at the start of the section."""
        header = '-' * len(self._name) + '\n'
        header += self._name + '\n'
        header += '-' * len(self._name)
        return header

    @property
    def content(self):
        """The section header and body"""
        return f'{self.header}\n{self.body}'


class SectionBackgroundThread(StatusSection):
    """The class for the section with the background thread"""
    _name = 'BACKGROUND THREAD'


class SectionBufferPoolAndMemory(StatusSection):
    """The class for the section for the buffer pool"""
    _name = 'BUFFER POOL AND MEMORY'


class SectionFileIO(StatusSection):
    """The class for the section for file I/O"""
    _name = 'FILE I/O'


class SectionIndividualBufferPoolInfo(StatusSection):
    """The class for the section for individual buffer pool
    instances"""
    _name = 'INDIVIDUAL BUFFER POOL INFO'


class SectionInsertBufferAndAdaptiveHashIndex(StatusSection):
    """The class for the section for the change buffer and the
    adaptive hash index"""
    _name = 'INSERT BUFFER AND ADAPTIVE HASH INDEX'


class SectionLatestDetectedDeadlock(StatusSection):
    """The class for the section with the latest detected deadlock"""
    _name = 'LATEST DETECTED DEADLOCK'


class SectionLatestForeignKeyError(StatusSection):
    """The class for the section with the latest foreign key error"""
    _name = 'LATEST FOREIGN KEY ERROR'


class SectionLog(StatusSection):
    """The class for the section with log information"""
    _name = 'LOG'


class SectionRowOperations(StatusSection):
    """The class for the section with row operations information"""
    _name = 'ROW OPERATIONS'


class SectionSemaphores(StatusSection):
    """The class for the section with semaphore information"""
    _name = 'SEMAPHORES'
    _max_wait = 0
    _num_waits = 0

    def __init__(self, body):
        super().__init__(body)
        self._max_wait = 0
        self._num_waits = 0
        self._parse()

    @property
    def num_waits(self):
        return self._num_waits

    @property
    def max_wait(self):
        return self._max_wait

    def _parse(self):
        waits = RE_SEMAPHORE.findall(self._body)
        self._num_waits = len(waits)
        try:
            self._max_wait = max([float(wait) for wait in waits])
        except ValueError:
            # No waits
            self._max_wait = 0


class SectionTransactions(StatusSection):
    """The class for the section with transaction information"""
    _name = 'TRANSACTIONS'


# Map of sections names to class
SECTIONS = {
    'BACKGROUND THREAD': SectionBackgroundThread,
    'SEMAPHORES': SectionSemaphores,
    'LATEST FOREIGN KEY ERROR': SectionLatestForeignKeyError,
    'LATEST DETECTED DEADLOCK': SectionLatestDetectedDeadlock,
    'TRANSACTIONS': SectionTransactions,
    'FILE I/O': SectionFileIO,
    'INSERT BUFFER AND ADAPTIVE HASH INDEX':
        SectionInsertBufferAndAdaptiveHashIndex,
    'LOG': SectionLog,
    'BUFFER POOL AND MEMORY': SectionBufferPoolAndMemory,
    'INDIVIDUAL BUFFER POOL INFO': SectionIndividualBufferPoolInfo,
    'ROW OPERATIONS': SectionRowOperations,
}
