"""Parse named format placeholders without permitting executable traversal."""
from string import Formatter
from .common import fields, text


def signature(message):
    if not isinstance(message, str) or len(message) > 10000:
        raise ValueError('message must be a bounded string')
    result = {}
    for _, name, spec, conversion in Formatter().parse(message):
        if name is None:
            continue
        if not name.isidentifier() or spec or conversion:
            raise ValueError('only simple named placeholders are supported')
        result[name] = True
    return set(result)


def validate_message(row):
    fields(row, 'value variables')
    if not isinstance(row['variables'], dict):
        raise ValueError('variables must be an object')
    variables = row['variables']
    for name, kind in variables.items():
        if not isinstance(name, str) or not name.isidentifier() or kind not in ('string','integer'):
            raise ValueError('invalid variable type or name')
    if signature(row['value']) != set(variables):
        raise ValueError('placeholder set differs from declared variables')
    return {'value':row['value'],'variables':dict(variables)}


def render(message, variables):
    if not isinstance(variables, dict) or set(variables) != set(message['variables']):
        raise ValueError('render variables must exactly match')
    for name, kind in message['variables'].items():
        value = variables[name]
        if kind == 'integer' and type(value) is not int:
            raise ValueError('integer variable required')
        if kind == 'string' and (not isinstance(value, str) or len(value) > 10000):
            raise ValueError('bounded string variable required')
    return message['value'].format_map(variables)
