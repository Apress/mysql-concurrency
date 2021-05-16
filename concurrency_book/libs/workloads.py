import re
import pathlib
from collections import namedtuple

import concurrency_book.libs.log

DEBUG = False

# Known lists in the yaml definition
KNOWN_LISTS = ['queries', 'completions', 'investigations']

# The workload named tuple that is returned after parsing the yaml file
WORKLOAD = namedtuple(
    'Workload',
    ['name', 'description', 'concurrent', 'connections', 'loops', 'queries',
     'completions', 'investigations', 'implementation', 'protocol']
)
# The named tuple fields for a query. These are both used for a query and
# a completion (query).
QUERY_FIELDS = ['connection', 'wait', 'sql', 'format', 'silent', 'show_result',
                'store', 'parameters', 'comment', 'sleep']
QUERY = namedtuple('Query', QUERY_FIELDS)
COMPLETION = namedtuple('Completion', QUERY_FIELDS)
INVESTIGATION = namedtuple(
    'Investigation', ['connection', 'sql', 'format', 'parameters'])
IMPLEMENTATION = namedtuple('Implementation', ['module', 'class_name', 'args'])

# The required keys for a workload
KEYS_WORKLOAD_REQUIRED = {
    'name': 'string',
    'description': 'string',
}
# The value is (data type, default value)
KEYS_WORKLOAD_OPTIONAL = {
    'connections': ('integer', 0),
    'concurrent': ('boolean', False),
    'completions': ('list', []),
    'loops': ('integer', 1),
    'queries': ('list', []),
    'implementation': ('implementation', (None, None, '{}')),
    'investigations': ('list', []),
    'protocol': ('string', None),
}
KEYS_QUERY_REQUIRED = {
    'connection': 'integer',
    'sql': 'string',
}
# The value is (data type, default value)
KEYS_QUERY_OPTIONAL = {
    'wait': ('boolean', True),
    'format': ('string', 'table'),
    'show_result': ('boolean', True),
    'silent': ('boolean', False),
    'store': ('boolean', False),
    'parameters': ('list', []),
    'comment': ('string', ''),
    'sleep': ('integer', 0),
}

KEYS_COMPLETION_REQUIRED = KEYS_QUERY_REQUIRED
KEYS_COMPLETION_OPTIONAL = KEYS_QUERY_OPTIONAL

KEYS_INVESTIGATION_REQUIRED = {
    'sql': 'string',
}

# The value is (data type, default value)
KEYS_INVESTIGATION_OPTIONAL = {
    'format': ('string', 'table'),
    'parameters': ('list', []),
}

# Supported values per key (irrespective of the part of the definition)
CHOICES = {
    'format': ['table', 'tabbed', 'vertical', 'json', 'ndjson', 'json/raw',
               'json/array', 'json/pretty'],
    'protocol': [None, 'mysql', 'mysqlx']
}

RE_KEYWORD_LINE = re.compile('^( *)(- )?(\\w+):(.*)$')
RE_COMMENT = re.compile('#.*')
RE_INT = re.compile('^-?[1-9][0-9]*$')

LOG = concurrency_book.libs.log.Log()


def _validate_datatype(file, key, value, required_datatype):
    """Determine the data type of the actual value and validate it
    against the required data type."""
    if value == str(value):
        actual_datatype = 'string'
    elif RE_INT.match(str(value)):
        actual_datatype = 'integer'
    elif value is True or value is False:
        actual_datatype = 'boolean'
    elif type(value) is list:
        actual_datatype = 'list'
    else:
        actual_datatype = 'undetermined'

    if (required_datatype == 'implementation' and
            actual_datatype == 'list' and
            len(value) in [2, 3]):
        return True
    if actual_datatype == required_datatype:
        return True
    else:
        LOG.error(f'Workload {file}: Wrong data type for {key} - Expected' +
                  f' {required_datatype} - Actual: {actual_datatype}')
        return False


def _validate_sublist(file, name, items, required, optional):
    """Validate sublists (e.g. queries) of the workload."""

    all_known_keys = list(required.keys()) + list(optional.keys())
    # Validate that all required keys are present
    for key in required:
        required_datatype = required[key]
        i = 0

        for item in items:
            item_keys = item.keys()

            if key not in item_keys:
                LOG.error(f'Workload {file}: The key "{key}" was not ' +
                          f'found for the {name} number {i}.')
                return None

            if not _validate_datatype(file, key, item[key], required_datatype):
                return None

            if key == 'implementation':
                item[key] = IMPLEMENTATION(*item[key])

            # Validate that no unknown keys are present
            for item_key in item_keys:
                if item_key not in all_known_keys:
                    LOG.error(f'Workload {file}: The key "{item_key}" is ' +
                              f'given for {name} number {i} but is not a ' +
                              'known key.')
                    return None
                if key in CHOICES and item[key] not in CHOICES[key]:
                    LOG.error(f'Workload {file}: The {name} value for ' +
                              '"{key}" is not an allowed value. Allowed ' +
                              f'values: {CHOICES[key]}')
                    return None
            i += 1

    # Validate the optional keys for the sublist
    # Copy items to avoid modifying the list that is being iterated over
    items_copy = [val for val in items]
    for key in optional:
        required_datatype = optional[key][0]
        i = 0
        for item in items_copy:
            try:
                value = item[key]
            except KeyError:
                default_value = optional[key][1]
                items[i][key] = default_value
            else:
                if not _validate_datatype(file, key, value, required_datatype):
                    return None

            if key == 'implementation':
                items[i][key] = IMPLEMENTATION(*item[key])

            _parse_implementation(items[i])
            i += 1

    return items


def _parse_implementation(dictionary):
    """Parse the implementation key of a workload."""
    try:
        value = dictionary['implementation']
    except KeyError:
        # Nothing to do
        return

    module = value[0]
    class_name = value[1]
    args = {}
    try:
        args_string = value[2]
    except IndexError:
        # No arguments given
        pass
    else:
        # Remove curly braces
        key_value_pairs = args_string.lstrip('{').rstrip('}').split(',')
        for key_value in key_value_pairs:
            try:
                key, value = key_value.split(':')
            except ValueError:
                pass
            else:
                args[key.strip()] = value.strip()

    dictionary['implementation'] = IMPLEMENTATION(module, class_name, args)


def _validate(path, workload_dict):
    """Validates that a dictionary can be converted into a
    workload. For optional elements, they are added with their
    default value if they do not already exist.

    Returns the updated dictionary upon success, otherwise None.
    """

    file = path.name
    workload_keys = workload_dict.keys()
    # Verify all required workload keys are present
    # and of the right data type
    for key in KEYS_WORKLOAD_REQUIRED:
        required_datatype = KEYS_WORKLOAD_REQUIRED[key]
        if key not in workload_keys:
            LOG.error(f'Workload {file}: The key "{key}" was not found')
            return None

        if not _validate_datatype(
                file, key, workload_dict[key], required_datatype):
            return None

    # Verify optional workload keys
    # and assign values for those not present
    for key in KEYS_WORKLOAD_OPTIONAL:
        required_datatype = KEYS_WORKLOAD_OPTIONAL[key][0]
        try:
            value = workload_dict[key]
        except KeyError:
            default_value = KEYS_WORKLOAD_OPTIONAL[key][1]
            workload_dict[key] = default_value
        else:
            if not _validate_datatype(file, key, value, required_datatype):
                return None

    # Verify only known keys are present and if the supported values are
    # limited that it has a supported value.
    for key in workload_keys:
        if (key not in KEYS_WORKLOAD_REQUIRED and
                key not in KEYS_WORKLOAD_OPTIONAL):
            LOG.error(f'Workload {file}: The key "{key}" is given but ' +
                      'is not a known key.')
        if key in CHOICES and workload_dict[key] not in CHOICES[key]:
            LOG.error(f'Workload {file}: The value for "{key}" is not an' +
                      f'allowed value. Allowed values: {CHOICES[key]}')

    _parse_implementation(workload_dict)

    # Validate required keys for the queries, completions, and investigations
    queries = _validate_sublist(file, 'query', workload_dict['queries'],
                                KEYS_QUERY_REQUIRED, KEYS_QUERY_OPTIONAL)
    if queries is None:
        return None
    else:
        workload_dict['queries'] = queries

    completions = _validate_sublist(file, 'completion',
                                    workload_dict['completions'],
                                    KEYS_COMPLETION_REQUIRED,
                                    KEYS_COMPLETION_OPTIONAL)
    if completions is None:
        workload_dict['completions'] = []
    else:
        workload_dict['completions'] = completions

    investigations = _validate_sublist(file, 'investigation',
                                       workload_dict['investigations'],
                                       KEYS_INVESTIGATION_REQUIRED,
                                       KEYS_INVESTIGATION_OPTIONAL)
    if investigations is None:
        workload_dict['investigations'] = []
    else:
        workload_dict['investigations'] = investigations

    return workload_dict


def _queries_to_tuple(queries_dict, connections):
    """Covert a query dictionary to a QUERY tuple."""
    queries = []
    for query_dict in queries_dict:
        if query_dict['connection'] == -1:
            # Add the query for all connections
            for connection in range(connections):
                conn_query = dict(query_dict)
                conn_query['connection'] = connection + 1
                queries.append(QUERY(**conn_query))
        else:
            queries.append(QUERY(**query_dict))

    return queries


def _dict_to_tuple(workload_dict):
    """Convert the workload dictionary to a workload named tuple.
    This includes converting the queries, completions, and
    investigations as well."""

    connections = workload_dict['connections']
    queries = _queries_to_tuple(workload_dict['queries'], connections)
    completions = _queries_to_tuple(workload_dict['completions'], connections)

    investigations = []
    for investigation_dict in workload_dict['investigations']:
        investigation_dict['connection'] = workload_dict['connections'] + 1
        investigations.append(INVESTIGATION(**investigation_dict))

    workload = WORKLOAD(
        workload_dict['name'],
        workload_dict['description'],
        workload_dict['concurrent'],
        workload_dict['connections'],
        workload_dict['loops'],
        queries,
        completions,
        investigations,
        workload_dict['implementation'],
        workload_dict['protocol']
    )
    return workload


def _parse_yaml(path):
    """Parse a workload YAML definition file.

    Note: This parse is by no means a full blown YAML parser - it just
    supports what is expected for the workload definition.
    It would be better to use the yaml module (the PyYAML PyPi package
    for this, but it is not supported in MySQL Shell.
    """
    with open(path, 'rt', encoding='utf-8') as yml:
        definition = yml.readlines()

    workload = {}
    level = 'workflow'
    sql_indent = 0  # To remove the right amount of prefix whitespace
    current_list = {}
    current_sql = ''
    list_type = None
    for line in definition:
        line = line.rstrip()
        if DEBUG:
            print(f'line: {line}')
        line = RE_COMMENT.sub('', line)
        if line.strip() == '---':
            continue

        m = RE_KEYWORD_LINE.match(line)
        if m:
            (indent_str, list_item, keyword, value) = m.groups()
            indent = len(indent_str)
            value = value.strip()

            # Handle date type conversion
            if value.upper() == 'NO':
                value = False
            elif value.upper() == 'YES':
                value = True
            elif (len(value) > 2 and
                  value[0] in ('"', "'") and
                  value[-1] == value[0]):
                # The string is quoted - remove the quotes
                # Note: it is not checked whether the end quote is escaped!
                value = value[1:-1]
            elif len(value) > 2 and value[0] == '[' and value[-1] == ']':
                # A list
                value = [val.strip() for val in value[1:-1].split(',')]
            else:
                # Handle integer values
                try:
                    value = int(value)
                except ValueError:
                    pass

            if DEBUG:
                print(f'   level     = {level}')
                print(f'   list_item = {list_item}')
                print(f'   keyword   = {keyword}')
                print(f'   value     = {value}')
            if level == 'sql':
                # SQL statement has completed
                if DEBUG:
                    print('   SQL statement has completed')
                current_list['sql'] = current_sql.strip()
                current_sql = ''
                level = 'list'

            if keyword in KNOWN_LISTS:
                if len(current_list) > 0:
                    workload[list_type].append(current_list)

                level = 'list'
                list_type = keyword
                current_list = {}
                workload[keyword] = []
            elif list_item is not None:
                level = 'list'
                if len(current_list) > 0:
                    workload[list_type].append(current_list)
                if keyword == 'sql' and value == '|':
                    if DEBUG:
                        print('   Starting new SQL statement')
                    level = 'sql'
                    current_sql = ''
                    sql_indent = 0
                    current_list = {}
                else:
                    current_list = {keyword: value}
            elif keyword == 'sql':
                if value == '|':
                    if DEBUG:
                        print('   Starting new SQL statement')
                    level = 'sql'
                    current_sql = ''
                    sql_indent = 0
                else:
                    current_list['sql'] = value
            elif indent == 0:
                level = 'workflow'
                if len(current_list) > 0:
                    workload[list_type].append(current_list)
                    current_list = {}
                workload[keyword] = value
            elif level == 'list':
                current_list[keyword] = value
        else:
            if sql_indent == 0:
                sql_indent = len(line) - len(line.lstrip())
            current_sql += line[sql_indent:] + '\n'

    if current_sql != '':
        current_list['sql'] = current_sql.strip()
    if len(current_list) > 0:
        workload[list_type].append(current_list)

    return workload


def load(path):
    """Look for all YAML files in the path provided and pass them for
    workload definitions. Both .yaml and .yml files are parsed.

    Returns the definitions as a dictionary with the workload name as
    the key."""
    path = pathlib.Path(path)
    workloads = {}
    for glob in ['*.yaml', '*.yml']:
        for yaml_path in path.glob(glob):
            workload_dict = _parse_yaml(yaml_path)
            workload_validated = _validate(yaml_path, workload_dict)
            if workload_validated is not None:
                workload = _dict_to_tuple(workload_validated)
                workloads[workload.name] = workload

    return workloads
