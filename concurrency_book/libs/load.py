"""The functions and classes required to load schemas into the database.
The supported schemas are available in KNOWN_SCHEMAS and can also be
retrieved using the get_jobs() function."""

import zipfile
from urllib import request
import re
import platform
import pathlib
import os
from collections import namedtuple

import mysqlsh

import concurrency_book.libs.log

LOAD_JOB = namedtuple('LoadJob', ['name', 'description'])

# The schemas that are known how to load.
# The key and value are used when listing the schemas.
KNOWN_SCHEMAS = {
    'employees': 'The employee database',
    'employees partitioned': 'The employee database with partitions',
    'sakila': 'The sakila database',
    'world': 'The world database',
}

# The URLs used to download each schema
URLS = {
    'employees': 'https://github.com/datacharmer/test_db/archive/master.zip',
    'sakila': 'https://downloads.mysql.com/docs/sakila-db.zip',
    'world': 'https://downloads.mysql.com/docs/world-db.zip',
}
URLS['employees partitioned'] = URLS['employees']

if platform.system() == 'Windows':
    SAVE_DIR = pathlib.Path(os.environ['APPDATA']).joinpath(
        'mysql_concurrency_book', 'sample_data')
else:
    SAVE_DIR = pathlib.Path(os.environ['HOME']).joinpath(
        '.mysql_concurrency_book', 'sample_data')

RE_COMMENT = re.compile('--+( +(.+)?)?$', re.MULTILINE)
RE_DELIMITER = re.compile(r'^DELIMITER\s+(.+)\s*$',
                          re.MULTILINE | re.IGNORECASE)

LOG = concurrency_book.libs.log.Log(concurrency_book.libs.log.INFO)


def get_jobs():
    """Return a dictionary with LOAD_JOB named tuples with the
    supported load jobs."""

    jobs = {}
    for job_name in KNOWN_SCHEMAS:
        jobs[job_name] = LOAD_JOB(job_name, KNOWN_SCHEMAS[job_name])

    return jobs


class Load(object):
    """Class for loading a schema."""
    _schema = None
    _session = None
    _delimiter = ';'

    def __init__(self, schema):
        """Initialize the Load class setting the name of the schema to
        be loaded."""

        if schema in URLS:
            self._schema = schema
        else:
            raise ValueError(f'Unknown schema: {schema} - Supported schemas:' +
                             f' {list(URLS.keys())}')
        self._delimiter = ';'

    def _download(self):
        """Provide the path to the downloaded file with the statements
        to load a schema. If the file has not yet been downloaded, it
        will be downloaded first."""

        try:
            SAVE_DIR.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            LOG.error(f'The path "{SAVE_DIR}" exists but is not a directory')

        path = SAVE_DIR.joinpath(pathlib.Path(URLS[self._schema]).name)
        if not path.is_file():
            LOG.info(f'Downloading {URLS[self._schema]} to {path}')
            with open(path, mode='xb') as fs:
                with request.urlopen(URLS[self._schema]) as url:
                    data = url.read(8 * 1024)
                    LOG.debug(f'Downloaded data length: {len(data)} bytes')
                    while len(data) > 0:
                        fs.write(data)
                        data = url.read(8 * 1024)
                        LOG.debug(f'Downloaded data length: {len(data)} bytes')
        else:
            LOG.info(f'Using existing file in {path}')

        return path

    def _sql_execute(self, chunk_sql, delimiter, sql_file, zip_fs):
        """Execute a list of SQL statements."""
        session = mysqlsh.globals.session

        # Split the file content up to the start of the chunk with
        # the delimiter into statements using the previous delimiter
        re_statement = re.compile(re.escape(delimiter) + r'\s*$', re.MULTILINE)
        statements = re_statement.split(chunk_sql)
        LOG.debug(f'Found {len(statements)} statements in the chunk.')
        for statement in statements:
            sql = RE_COMMENT.sub('', statement.strip()).strip()

            if sql.upper() in ('START TRANSACTION', 'BEGIN'):
                session.start_transaction()
            elif sql.upper() == f'COMMIT':
                session.commit()
            elif sql.upper() == f'ROLLBACK':
                session.rollback()
            elif sql.upper()[0:4] == f'USE ':
                _, schema = sql.split(' ', 1)
                session.set_current_schema(schema.strip('`'))
            elif sql.upper()[0:7] == 'SOURCE ':
                _, filename = sql.split(' ', 1)
                path = pathlib.PurePosixPath(sql_file.name)
                new_path = path.with_name(filename.strip())
                with zip_fs.open(str(new_path)) as new_fs:
                    self._sql_file(new_fs, zip_fs=zip_fs)
            elif sql != '':
                try:
                    session.run_sql(sql)
                except mysqlsh.DBError as e:
                    LOG.error(f'Error for sql: {sql}\n{e}')

    def _sql_file(self, sql_file, zip_fs):
        """Execute the SQL statements in a given file. Takes a
        file-like object and optional the file descriptor to the
        zip file (allows handling SOURCE commands inside the
        sql file."""

        delimiter = ';'
        content = sql_file.read().decode('utf-8')
        path = pathlib.PurePosixPath(sql_file.name)
        LOG.info(f'Processing statements in {path.name}')
        LOG.debug(f'Read {len(content)} characters from {path.name}')

        # Split the file into chunks separated by the DELIMITER
        # command.
        chunks = RE_DELIMITER.finditer(content)
        offset = 0
        LOG.debug(f'Starting to process chunks')
        for chunk in chunks:
            LOG.debug(f'Processing chunk starting at position {offset} to ' +
                      f'{chunk.start()}')
            chunk_sql = content[offset:chunk.start()]
            self._sql_execute(chunk_sql, delimiter, sql_file, zip_fs)
            offset = chunk.end() + 1
            delimiter = chunk[1]
            LOG.debug(f'Setting the delimiter to {delimiter}')

        # Handle the part of the file after the last DELIMITER command.
        # This includes handling the whole file is there are no
        # DELIMITER commands.
        LOG.debug(f'Processing chunk starting at position {offset} to ' +
                  f'{len(content)}')
        chunk_sql = content[offset:]
        self._sql_execute(chunk_sql, delimiter, sql_file, zip_fs)

    def _exec_employees(self, partitioned=False):
        """Execute the steps required to load the employees schema."""
        file = self._download()
        with zipfile.ZipFile(file) as zip_fs:
            if partitioned:
                filename = 'employees_partitioned.sql'
            else:
                filename = 'employees.sql'
            self._delimiter = ';'
            with zip_fs.open(f'test_db-master/{filename}') as schema:
                self._sql_file(schema, zip_fs)

            with zip_fs.open('test_db-master/objects.sql') as objects:
                self._sql_file(objects, zip_fs)

        LOG.info('Load of the employees schema completed')
        return True

    def _exec_employees_partitioned(self):
        """Load the partitioned employees database."""
        return self._exec_employees(True)

    def _exec_sakila(self):
        """Execute the steps required to load the sakila schema."""
        file = self._download()
        with zipfile.ZipFile(file) as zip_fs:
            self._delimiter = ';'
            with zip_fs.open('sakila-db/sakila-schema.sql') as schema:
                self._sql_file(schema, zip_fs)

            self._delimiter = ';'
            with zip_fs.open('sakila-db/sakila-data.sql') as data:
                self._sql_file(data, zip_fs)

        LOG.info('Load of the sakila schema completed')
        return True

    def _exec_world(self):
        """Execute the steps required to load the world schema."""
        file = self._download()
        with zipfile.ZipFile(file) as zip_fs:
            self._delimiter = ';'
            with zip_fs.open('world.sql') as world:
                self._sql_file(world, zip_fs)

        LOG.info('Load of the world schema completed')
        return True

    def execute(self):
        """Execute a job."""
        method = f'_exec_{self._schema.replace(" ", "_")}'
        return getattr(self, method)()
