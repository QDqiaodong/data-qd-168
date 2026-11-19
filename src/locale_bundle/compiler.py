"""Build complete locale catalogs with explicit resolution provenance."""
from .common import fields, text, array
from .messages import validate_message


def compile_bundle(data):
    fields(data, 'default_locale fallback catalogs requested')
    default = text(data['default_locale'])
    if not isinstance(data['catalogs'], dict) or not 1 <= len(data['catalogs']) <= 100:
        raise ValueError('bounded locale catalogs required')
    catalogs = {}
    for locale, messages in data['catalogs'].items():
        text(locale)
        if not isinstance(messages, dict) or len(messages) > 10000:
            raise ValueError('bounded message object required')
        catalogs[locale] = {text(key):validate_message(row) for key,row in messages.items()}
    if default not in catalogs or not catalogs[default]:
        raise ValueError('nonempty default catalog required')
    if not isinstance(data['fallback'], dict):
        raise ValueError('fallback map required')
    fallback = data['fallback']
    for locale, parent in fallback.items():
        if locale not in catalogs or parent not in catalogs or locale == default:
            raise ValueError('invalid fallback edge')
    chains = {}
    for locale in catalogs:
        chain = []
        cursor = locale
        while cursor != default:
            if cursor in chain:
                raise ValueError('fallback cycle')
            chain.append(cursor)
            cursor = fallback.get(cursor, default)
        chains[locale] = chain + [default]
    for locale, messages in catalogs.items():
        if set(messages) - set(catalogs[default]):
            raise ValueError('translation contains unknown key')
        for key, row in messages.items():
            if row['variables'] != catalogs[default][key]['variables']:
                raise ValueError('translation variable types differ')
    requested = [text(x) for x in array(data['requested'],100)]
    if not requested or len(set(requested)) != len(requested) or set(requested) - set(catalogs):
        raise ValueError('requested locales must be known and unique')
    resolved, missing = {}, []
    for locale in requested:
        resolved[locale] = {}
        for key in sorted(catalogs[default]):
            source = next(parent for parent in chains[locale] if key in catalogs[parent])
            resolved[locale][key] = {**catalogs[source][key], 'source_locale':source}
            if source != locale:
                missing.append({'locale':locale,'key':key,'source_locale':source})
    return {'default_locale':default,'catalogs':resolved,'fallback_messages':missing}
