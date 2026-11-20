"""Per-application release bundles from shared components.

Applications name root components; each application package contains only
the message keys declared by the transitive closure of those roots, resolved
per requested locale along the v1 fallback chain. Components and keys are
shared between applications, but one application's keys never leak into
another application's package.
"""
from .common import fields, text, array
from .compiler import catalog_context, resolve_key

COMPONENT_LIMIT = 10000
APPLICATION_LIMIT = 10000


def _identifier_list(value, label):
    items = [text(item) for item in array(value)]
    if len(set(items)) != len(items):
        raise ValueError('duplicate ' + label)
    return items


def _component(row):
    fields(row, 'id requires keys')
    return {'id': text(row['id']),
            'requires': _identifier_list(row['requires'], 'dependency'),
            'keys': _identifier_list(row['keys'], 'key')}


def _application(row):
    fields(row, 'id roots')
    return {'id': text(row['id']),
            'roots': _identifier_list(row['roots'], 'root component')}


def component_closure(application, components):
    """Transitive component ids reachable from the application roots."""
    seen = set()
    stack = list(application['roots'])
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(components[current]['requires'])
    return seen


def validate_component_graph(components_value, applications_value, known_keys,
                             key_origin='default catalog'):
    """Validate the shared component/application graph against known keys.

    Returns (components, applications, by_id). The dependency graph must be
    acyclic across the whole declared graph, even over components no
    application reaches.
    """
    components = [_component(row) for row in array(components_value, COMPONENT_LIMIT)]
    component_ids = [component['id'] for component in components]
    if len(set(component_ids)) != len(component_ids):
        raise ValueError('duplicate component id')
    by_id = {component['id']: component for component in components}
    applications = [_application(row) for row in array(applications_value, APPLICATION_LIMIT)]
    application_ids = [application['id'] for application in applications]
    if len(set(application_ids)) != len(application_ids):
        raise ValueError('duplicate application id')

    for component in components:
        for dependency in component['requires']:
            if dependency not in by_id:
                raise ValueError('unknown component reference: ' + dependency)
        for key in component['keys']:
            if key not in known_keys:
                raise ValueError('component key missing from ' + key_origin + ': ' + key)
    for application in applications:
        for root in application['roots']:
            if root not in by_id:
                raise ValueError('unknown root component: ' + root)

    # Reject cycles across the whole declared graph, requested or not.
    state = {}
    for start in component_ids:
        if state.get(start) == 'done':
            continue
        stack = [(start, iter(by_id[start]['requires']))]
        state[start] = 'active'
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if state.get(child) == 'active':
                    raise ValueError('component dependency cycle')
                if state.get(child) is None:
                    state[child] = 'active'
                    stack.append((child, iter(by_id[child]['requires'])))
                    advanced = True
                    break
            if not advanced:
                state[node] = 'done'
                stack.pop()
    return components, applications, by_id


def compile_release_bundles(data):
    fields(data, 'default_locale fallback catalogs requested components applications')
    default, catalogs, chains, requested = catalog_context(
        data['default_locale'], data['fallback'], data['catalogs'], data['requested'])

    components, applications, by_id = validate_component_graph(
        data['components'], data['applications'], set(catalogs[default]))

    packages = []
    for application in applications:
        closure = component_closure(application, by_id)
        included_by = {}
        for component_id in closure:
            for key in by_id[component_id]['keys']:
                included_by.setdefault(key, set()).add(component_id)
        keys = sorted(included_by)
        catalogs_out = {}
        fallback_messages = []
        for locale in sorted(requested):
            resolved = {}
            for key in keys:
                source, message = resolve_key(catalogs, chains, locale, key)
                resolved[key] = {**message, 'included_by': sorted(included_by[key])}
                if source != locale:
                    fallback_messages.append({
                        'locale': locale, 'key': key, 'source_locale': source,
                        'included_by': sorted(included_by[key])})
            catalogs_out[locale] = resolved
        fallback_messages.sort(key=lambda row: (row['locale'], row['key']))
        packages.append({'application_id': application['id'],
                         'catalogs': catalogs_out,
                         'fallback_messages': fallback_messages})
    packages.sort(key=lambda package: package['application_id'])
    return {'applications': packages}
