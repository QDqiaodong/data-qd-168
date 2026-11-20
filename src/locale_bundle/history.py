"""Offline reviewed-translation history built from an append-only fact log.

Facts (source revisions, translation revisions, review decisions) are
validated once globally; every query then derives its own historical view
from the facts visible at its cutoff (effective <= at and seq <= known_seq).
No derived state is shared across queries, so late-arriving facts and review
revocations only affect the views whose cutoffs can see them, and old
cutoffs stay reproducible.
"""
from .common import fields, text, integer, array
from .messages import validate_message

FACT_LIMIT = 10000
KEY_LIMIT = 10000


def _message(row):
    return validate_message({'value': row['value'], 'variables': row['variables']})


def _source(row):
    fields(row, 'id seq effective key value variables')
    message = _message(row)
    return {'id': text(row['id']), 'seq': integer(row['seq'], 1),
            'effective': integer(row['effective'], 0), 'key': text(row['key']),
            'value': message['value'], 'variables': message['variables']}


def _translation(row):
    fields(row, 'id seq effective locale key source_id value variables')
    message = _message(row)
    return {'id': text(row['id']), 'seq': integer(row['seq'], 1),
            'effective': integer(row['effective'], 0), 'locale': text(row['locale']),
            'key': text(row['key']), 'source_id': text(row['source_id']),
            'value': message['value'], 'variables': message['variables']}


def _review(row):
    fields(row, 'seq effective translation_id approved')
    if type(row['approved']) is not bool:
        raise ValueError('approved must be a boolean')
    return {'seq': integer(row['seq'], 1), 'effective': integer(row['effective'], 0),
            'translation_id': text(row['translation_id']), 'approved': row['approved']}


def _query(row):
    fields(row, 'id at known_seq')
    return {'id': text(row['id']), 'at': integer(row['at'], 0),
            'known_seq': integer(row['known_seq'], 1)}


def _latest(rows, at, known_seq):
    """Newest visible row by (effective, seq); None when nothing is visible."""
    best = None
    for row in rows:
        if row['effective'] <= at and row['seq'] <= known_seq and (
                best is None or (row['effective'], row['seq']) > (best['effective'], best['seq'])):
            best = row
    return best


def compile_reviewed_history(data):
    fields(data, 'default_locale locales fallback requested sources translations reviews queries')
    default = text(data['default_locale'])
    locales = [text(locale) for locale in array(data['locales'], 100)]
    if len(set(locales)) != len(locales) or default not in locales:
        raise ValueError('locales must be distinct and contain the default locale')
    known = set(locales)
    if not isinstance(data['fallback'], dict):
        raise ValueError('fallback map required')
    fallback = {}
    for locale, parent in data['fallback'].items():
        text(locale)
        text(parent)
        if locale not in known or parent not in known or locale == default:
            raise ValueError('invalid fallback edge')
        fallback[locale] = parent
    chains = {}
    for locale in known:
        chain = []
        cursor = locale
        while cursor != default:
            if cursor in chain:
                raise ValueError('fallback cycle')
            chain.append(cursor)
            cursor = fallback.get(cursor, default)
        chains[locale] = chain + [default]
    requested = [text(locale) for locale in array(data['requested'], 100)]
    if not requested or len(set(requested)) != len(requested) or set(requested) - known:
        raise ValueError('requested locales must be known and unique')

    sources = [_source(row) for row in array(data['sources'])]
    translations = [_translation(row) for row in array(data['translations'])]
    reviews = [_review(row) for row in array(data['reviews'])]
    if len(sources) + len(translations) + len(reviews) > FACT_LIMIT:
        raise ValueError('fact limit exceeded')
    if len({source['key'] for source in sources}) > KEY_LIMIT:
        raise ValueError('source key limit exceeded')
    fact_ids = [row['id'] for row in sources + translations]
    if len(set(fact_ids)) != len(fact_ids):
        raise ValueError('source and translation ids must be globally distinct')
    seqs = [row['seq'] for row in sources + translations + reviews]
    if len(set(seqs)) != len(seqs):
        raise ValueError('fact seq values must be globally distinct')

    source_by_id = {source['id']: source for source in sources}
    translation_by_id = {row['id']: row for row in translations}
    for row in translations:
        if row['locale'] == default or row['locale'] not in known:
            raise ValueError('translation locale must be a declared non-default locale')
        origin = source_by_id.get(row['source_id'])
        if origin is None or origin['seq'] >= row['seq']:
            raise ValueError('translation must reference an earlier source revision')
        if row['key'] != origin['key'] or row['variables'] != origin['variables']:
            raise ValueError('translation must match the referenced source revision')
    for row in reviews:
        target = translation_by_id.get(row['translation_id'])
        if target is None or target['seq'] >= row['seq']:
            raise ValueError('review must reference an earlier translation')

    queries = [_query(row) for row in array(data['queries'], 100)]
    if not queries:
        raise ValueError('at least one query required')
    query_ids = [query['id'] for query in queries]
    if len(set(query_ids)) != len(query_ids):
        raise ValueError('query ids must be distinct')

    sources_by_key = {}
    for source in sources:
        sources_by_key.setdefault(source['key'], []).append(source)
    translations_by_slot = {}
    for row in translations:
        translations_by_slot.setdefault((row['locale'], row['key']), []).append(row)
    reviews_by_translation = {}
    for row in reviews:
        reviews_by_translation.setdefault(row['translation_id'], []).append(row)

    non_default = sorted(known - {default})

    def view(query):
        at, known_seq = query['at'], query['known_seq']
        current = {}
        for key, rows in sources_by_key.items():
            latest = _latest(rows, at, known_seq)
            if latest is not None:
                current[key] = latest
        if not current:
            raise ValueError('query has no visible source revision: ' + query['id'])
        keys = sorted(current)
        states = {}
        for locale in non_default:
            for key in keys:
                row = {'locale': locale, 'key': key, 'translation_id': None,
                       'review_seq': None, 'state': 'missing'}
                translation = _latest(translations_by_slot.get((locale, key), ()), at, known_seq)
                if translation is not None:
                    review = _latest(reviews_by_translation.get(translation['id'], ()), at, known_seq)
                    row['translation_id'] = translation['id']
                    if review is not None:
                        row['review_seq'] = review['seq']
                    if translation['source_id'] != current[key]['id']:
                        row['state'] = 'stale_source'
                    elif review is None:
                        row['state'] = 'pending'
                    elif review['approved']:
                        row['state'] = 'approved'
                    else:
                        row['state'] = 'rejected'
                states[(locale, key)] = row
        catalogs = {}
        fallback_messages = []
        for locale in requested:
            resolved = {}
            for key in keys:
                message = None
                for parent in chains[locale]:
                    if parent == default:
                        source = current[key]
                        message = {'value': source['value'], 'variables': dict(source['variables']),
                                   'source_locale': default, 'source_id': source['id'],
                                   'translation_id': None, 'review_seq': None}
                        break
                    state = states[(parent, key)]
                    if state['state'] == 'approved':
                        translation = translation_by_id[state['translation_id']]
                        message = {'value': translation['value'],
                                   'variables': dict(translation['variables']),
                                   'source_locale': parent, 'source_id': current[key]['id'],
                                   'translation_id': translation['id'],
                                   'review_seq': state['review_seq']}
                        break
                resolved[key] = message
                if message['source_locale'] != locale:
                    fallback_messages.append({'locale': locale, 'key': key,
                                              'source_locale': message['source_locale']})
            catalogs[locale] = resolved
        fallback_messages.sort(key=lambda row: (row['locale'], row['key']))
        translation_states = [states[(locale, key)] for locale in non_default for key in keys]
        return {'query_id': query['id'], 'at': at, 'known_seq': known_seq,
                'default_locale': default, 'catalogs': catalogs,
                'fallback_messages': fallback_messages, 'translation_states': translation_states}

    return {'views': [view(query) for query in queries]}
