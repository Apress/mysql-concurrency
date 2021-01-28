import sys
import re

import mysqlsh

RE_NUMBER = re.compile('([0-9]+)')
RE_LISTING = re.compile(r'^Listing\s+(\d+)-(\d+)$')
RE_SPACE = re.compile('  +')


def _task_name_part_convert(name_part):
    """Helper function for sorting task names."""
    if name_part.isdigit():
        return int(name_part)
    else:
        return name_part.lower()


def _task_name_split(name):
    """Used for sorting task names."""
    return [_task_name_part_convert(p) for p in RE_NUMBER.split(name)]


def normalize_task_name(name):
    """Normalize the task name (removing duplicate whitespace)."""
    return RE_SPACE.sub(' ', name)


def list_tasks(task_name, tasks):
    """List the available tasks. Returns a list of task names
    ordered by name."""

    heading = f'Available {task_name}s:'
    print(heading)
    print('=' * len(heading))
    print('')
    print('{0:>2s} {1:22s} {2}'.format('#', 'Name', 'Description'))
    print('-' * 100)
    names = list(tasks.keys())
    names.sort(key=_task_name_split)
    count = 0
    for name in names:
        task = tasks[name]
        count += 1
        # For names that refer to a code listing from the book, make the
        # chapter numbers right align.
        m = RE_LISTING.match(name)
        if m is not None:
            name = f'Listing {int(m[1]):2d}-{m[2]}'
        print(f'{count:2d} {name:22s} {task.description}')
    print('')

    return names


def prompt_task(task_name, tasks):
    """Ask which task to execute."""

    names = list_tasks(task_name, tasks)
    task = None
    prompt = f'Choose {task_name} (# or name - empty to exit): '
    while task is None:
        answer = mysqlsh.globals.shell.prompt(prompt)
        if answer == '':
            return None
        # Try to convert the answer to an int - if that does not work,
        # treat it as a name.
        try:
            name = names[int(answer) - 1]
        except ValueError:
            # Could not convert the answer to an integer
            # Normalise the name (e.g. remove duplice spaces)
            answer = normalize_task_name(answer)
            try:
                task = tasks[answer]
            except KeyError:
                pass
        except IndexError:
            pass
        else:
            task = tasks[name]

        if task is None:
            print(f'Unknown workload: "{answer}"')

    return task


def prompt_investigation(investigations, sql_formatter, num_connections):
    """Ask for an investigation to perform."""

    print('Available investigations:')
    print('=' * 25)
    print('')
    print('{0:>2s} {1}'.format('#', 'Query'))
    print('-' * 50)
    count = 0
    statements = []
    answer_to_investigation = []
    for investigation in investigations:
        # Replace global parameters such as a list of all thread ids
        sql_template = sql_formatter.sql_global_sub(investigation.sql, 0)
        if investigation.parameters:
            for connection in range(num_connections):
                count += 1
                sql = sql_formatter.for_connection(sql_template, connection,
                                                   investigation.parameters, 3)
                answer_to_investigation.append(investigation)
                statements.append(sql)
                print(f'{count:2d} {sql}')
                print('')
        else:
            count += 1
            sql = sql_formatter.indent_sql(sql_template, 3)
            answer_to_investigation.append(investigation)
            statements.append(sql)
            print(f'{count:2d} {sql}')
            print('')

    answer = None
    investigation = None
    sql = None
    prompt = 'Choose investigation (# - empty to exit): '
    while investigation is None:
        answer = mysqlsh.globals.shell.prompt(prompt)
        if answer == '':
            return None, None, None
        # Try to convert the answer to an int - if that does not work,
        # treat it as a name.
        try:
            answer = int(answer)
        except ValueError:
            pass
        else:
            try:
                investigation = answer_to_investigation[answer - 1]
                sql = statements[answer - 1]
            except IndexError:
                investigation = None

    return answer, investigation, sql


def get_uri():
    """Obtain the URI for the global session."""

    uri = mysqlsh.globals.session.uri
    args = mysqlsh.globals.shell.parse_uri(uri)

    prompt = 'Password for connections: '
    options = {'type': 'password'}
    args['password'] = mysqlsh.globals.shell.prompt(prompt, options)

    uri = mysqlsh.globals.shell.unparse_uri(args)
    return uri


def verify_session():
    """Verify a session is open."""
    session = mysqlsh.globals.session
    try:
        has_connection = session.is_open()
    except AttributeError:
        has_connection = False

    if not has_connection:
        print('MySQL Shell must be connected to MySQL Server.',
              file=sys.stderr)

    return has_connection


def get_session(workload, uri=None):
    """Return a connection based on the workload settings. Optionally,
    an uri can be provided. If it is not provided, it will be determined
    using the get_uri() function."""

    if uri is None:
        uri = get_uri()

    session = None
    if workload.protocol == 'mysql':
        try:
            session = mysqlsh.globals.mysql.get_classic_session(uri)
        except mysqlsh.DBError as e:
            if e.code == 2007:
                print('This workload requires the classic MySQL ' +
                      'protocol. Please exit MySQL Shell and reconnect ' +
                      'using the --mysql option.',
                      file=sys.stderr)
            else:
                print('Error when trying to create a connection ' +
                      f'using the classic MySQL protocol:\n{e}',
                      file=sys.stderr)
    elif workload.protocol == 'mysqlx':
        try:
            session = mysqlsh.globals.mysqlx.get_session(uri)
        except mysqlsh.DBError as e:
            if e.code == 2027:
                print('This workload requires the MySQL X protocol. ' +
                      'Please exit MySQL Shell and reconnect using ' +
                      'the --mysqlx option.',
                      file=sys.stderr)
            else:
                print('Error when trying to create a connection ' +
                      f'using the MySQL X protocol:\n{e}',
                      file=sys.stderr)
    else:
        session = mysqlsh.globals.shell.open_session(uri)

    return session


def prompt_int(min_val, max_val, default_val, prompt_base):
    """Ask for an integer in a specified interval with support for a
    default value."""
    value = None
    prompt = f'{prompt_base} ({min_val}-{max_val}) [{default_val}]: '
    while value is None:
        answer = mysqlsh.globals.shell.prompt(prompt)
        if answer == '':
            value = default_val
        else:
            try:
                value = int(answer)
            except ValueError:
                print('The value must be an integer.')
                value = None
            else:
                if value < min_val or value > max_val:
                    print(f'The value must be an integer between {min_val} ' +
                          f'and {max_val}.')
                    value = None

    return value


def prompt_bool(default_val, prompt_base):
    """Prompt for a boolean answer (Y/N/YES/NO)."""
    value = None
    prompt = f'{prompt_base} (Y|Yes|N|No) [{default_val}]: '
    while value is None:
        answer = mysqlsh.globals.shell.prompt(prompt)
        if answer == '':
            answer = default_val
        if answer.upper() in ('Y', 'YES'):
            value = True
        elif answer.upper() in ('N', 'NO'):
            value = False
        else:
            print('The value must be one of Y, Yes, N, and No')

    return value
