"""Offline release delivery (contract v6).

Headquarters builds one self-contained release snapshot from the v2
review-history fact log plus the v3 component/application graph, naming an
existing query cutoff and a positive integer release number. A store
initializes from one full snapshot and afterwards only applies delta files
whose pinned baseline is exactly the store's current snapshot and whose
target release number is newer than every release the store has ever
installed. Withdrawal notices are an independent input: each notice blocks
a set of exact approval evidence ``(translation_id, review_seq)`` so that
every current package referencing it becomes unreadable immediately, until
a newer release or a clean rollback removes the reference.

The store keeps exactly the current snapshot, the previous snapshot (the
rollback candidate), the highest release number ever installed and the
accepted notices in one local JSON file; every successful operation is
validated completely before the file is replaced atomically, so a failed
command can never leave a partially installed release.
"""
import hashlib
import json

from .common import fields, text, integer, array
from .history import validate_fact_log, _current_sources, _slot_state
from .messages import validate_message
from .release import validate_component_graph, component_closure

PACKAGE_LIMIT = 10000
MESSAGE_LIMIT = 100000
NOTICE_LIMIT = 10000


class DeliveryError(ValueError):
    """A validation or conflict error that fails the whole command."""


def _canonical(value):
    """Deterministic JSON bytes used for identity hashes (same rules as v5)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _package_id(package):
    return (package['application_id'], package['locale'])


# --------------------------------------------------------------------------
# Evidence, messages, packages
# --------------------------------------------------------------------------

def _evidence(row):
    fields(row, 'translation_id review_seq')
    return {'translation_id': text(row['translation_id']),
            'review_seq': integer(row['review_seq'], 1)}


def _evidence_key(evidence):
    return (evidence['translation_id'], evidence['review_seq'])


def _sorted_evidence(rows):
    items = [_evidence(row) for row in array(rows, NOTICE_LIMIT)]
    keys = [_evidence_key(item) for item in items]
    if len(set(keys)) != len(keys):
        raise DeliveryError('duplicate evidence entry')
    return [item for _, item in sorted(zip(keys, items))]


def _check_supply(message, default):
    if message['source_locale'] == default:
        if message['translation_id'] is not None:
            raise DeliveryError('default-locale supply cannot carry review evidence')
    elif message['translation_id'] is None:
        raise DeliveryError('non-default supply must carry review evidence')


def _message(row):
    fields(row, 'key value variables source_locale source_id '
                'translation_id review_seq')
    message = validate_message({'value': row['value'],
                                'variables': row['variables']})
    translation_id = row['translation_id']
    review_seq = row['review_seq']
    if (translation_id is None) != (review_seq is None):
        raise DeliveryError('supply evidence must be both null or both set')
    result = {'key': text(row['key']), 'value': message['value'],
              'variables': message['variables'],
              'source_locale': text(row['source_locale']),
              'source_id': text(row['source_id']),
              'translation_id': None, 'review_seq': None}
    if translation_id is not None:
        result['translation_id'] = text(translation_id)
        result['review_seq'] = integer(review_seq, 1)
    return result


def _sorted_messages(rows):
    messages = [_message(row) for row in array(rows, MESSAGE_LIMIT)]
    keys = [message['key'] for message in messages]
    if len(set(keys)) != len(keys) or keys != sorted(keys):
        raise DeliveryError('messages must be unique and sorted by key')
    return messages


def _package(row, default):
    fields(row, 'application_id locale messages')
    package = {'application_id': text(row['application_id']),
               'locale': text(row['locale']),
               'messages': _sorted_messages(row['messages'])}
    for message in package['messages']:
        _check_supply(message, default)
    return package


def _sorted_packages(rows, default):
    packages = [_package(row, default) for row in array(rows, PACKAGE_LIMIT)]
    identities = [_package_id(package) for package in packages]
    if len(set(identities)) != len(identities) or identities != sorted(identities):
        raise DeliveryError('packages must be unique and sorted by identity')
    return packages


# --------------------------------------------------------------------------
# Snapshot: identity, validation, headquarters build
# --------------------------------------------------------------------------

def snapshot_identity(release, default_locale, packages):
    """The content that determines a release's identity.

    The release number, the default locale, every package identity and every
    key's message together with its supply evidence participate; equivalent
    reorderings of facts, component lists, roots or JSON object fields do
    not change it.
    """
    return {'release': release, 'default_locale': default_locale,
            'packages': packages}


def validate_snapshot(raw):
    """Validate a snapshot file and verify its self-contained identity."""
    fields(raw, 'version release query_id at known_seq default_locale '
                'packages snapshot_id')
    if type(raw['version']) is not int or raw['version'] != 1:
        raise DeliveryError('unsupported snapshot version')
    release = integer(raw['release'], 1)
    snapshot = {'version': 1, 'release': release,
                'query_id': text(raw['query_id']),
                'at': integer(raw['at'], 0),
                'known_seq': integer(raw['known_seq'], 1),
                'default_locale': text(raw['default_locale']),
                'packages': _sorted_packages(raw['packages'],
                                             text(raw['default_locale'])),
                'snapshot_id': text(raw['snapshot_id'])}
    expected = _hash(snapshot_identity(release, snapshot['default_locale'],
                                       snapshot['packages']))
    if snapshot['snapshot_id'] != expected:
        raise DeliveryError('snapshot id does not match its content')
    return snapshot


def _resolve(ctx, current, locale, key, at, known_seq):
    """Resolve one key along the v1 chain under the v2 approval rules."""
    source = current[key]
    for parent in ctx['chains'][locale]:
        if parent == ctx['default']:
            return {'key': key, 'value': source['value'],
                    'variables': dict(source['variables']),
                    'source_locale': ctx['default'], 'source_id': source['id'],
                    'translation_id': None, 'review_seq': None}
        state = _slot_state(ctx['translations_by_slot'],
                            ctx['reviews_by_translation'],
                            parent, key, source['id'], at, known_seq)
        if state['state'] == 'approved':
            translation = ctx['translation_by_id'][state['translation_id']]
            return {'key': key, 'value': translation['value'],
                    'variables': dict(translation['variables']),
                    'source_locale': parent, 'source_id': source['id'],
                    'translation_id': translation['id'],
                    'review_seq': state['review_seq']}
    raise DeliveryError('fallback chain did not reach the default locale')


def build_snapshot(data, query_id, release):
    """Build one release snapshot for one query cutoff and release number.

    The input is the v2 fact log plus v3 components/applications. Component
    keys must be source keys declared in the log; keys whose source revision
    is not yet visible at the selected cutoff are not packaged.
    """
    release = integer(release, 1)
    query_id = text(query_id)
    ctx = validate_fact_log(data, ('components', 'applications'))
    queries = {query['id']: query for query in ctx['queries']}
    if query_id not in queries:
        raise DeliveryError('unknown query: ' + query_id)
    query = queries[query_id]
    at, known_seq = query['at'], query['known_seq']
    _, applications, by_id = validate_component_graph(
        data['components'], data['applications'], set(ctx['sources_by_key']),
        'source facts')
    current = _current_sources(ctx['sources_by_key'], at, known_seq)
    if not current:
        raise DeliveryError('query has no visible source revision: ' + query_id)

    packages = []
    for application in applications:
        keys = set()
        for component_id in component_closure(application, by_id):
            keys.update(by_id[component_id]['keys'])
        keys = sorted(keys & set(current))
        for locale in sorted(ctx['requested']):
            messages = [_resolve(ctx, current, locale, key, at, known_seq)
                        for key in keys]
            packages.append({'application_id': application['id'],
                             'locale': locale, 'messages': messages})
    packages.sort(key=_package_id)
    snapshot_id = _hash(snapshot_identity(release, ctx['default'], packages))
    return validate_snapshot({'version': 1, 'release': release,
                              'query_id': query_id, 'at': at,
                              'known_seq': known_seq,
                              'default_locale': ctx['default'],
                              'packages': packages,
                              'snapshot_id': snapshot_id})


# --------------------------------------------------------------------------
# Delta: build, validate, apply
# --------------------------------------------------------------------------

def _package_ref(row):
    fields(row, 'application_id locale')
    return {'application_id': text(row['application_id']),
            'locale': text(row['locale'])}


def _package_change(row, default):
    fields(row, 'application_id locale keys_removed keys_upserted')
    change = {'application_id': text(row['application_id']),
              'locale': text(row['locale'])}
    keys_removed = [text(key) for key in array(row['keys_removed'], MESSAGE_LIMIT)]
    if len(set(keys_removed)) != len(keys_removed) or keys_removed != sorted(keys_removed):
        raise DeliveryError('removed keys must be unique and sorted')
    upserted = _sorted_messages(row['keys_upserted'])
    if not keys_removed and not upserted:
        raise DeliveryError('empty package change')
    if set(keys_removed) & {message['key'] for message in upserted}:
        raise DeliveryError('a key cannot be both removed and upserted')
    for message in upserted:
        _check_supply(message, default)
    return {**change, 'keys_removed': keys_removed, 'keys_upserted': upserted}


def _delta_content(target, baseline_id, packages_removed, packages_added,
                   packages_changed):
    return {'target_release': target['release'], 'baseline_id': baseline_id,
            'target_id': target['snapshot_id'],
            'target_header': {'query_id': target['query_id'], 'at': target['at'],
                              'known_seq': target['known_seq'],
                              'default_locale': target['default_locale']},
            'packages_removed': packages_removed,
            'packages_added': packages_added,
            'packages_changed': packages_changed}


def _check_package_lists(removed, added, changed):
    id_sets = []
    for label, rows in (('removed', removed), ('added', added), ('changed', changed)):
        identities = [_package_id(row) for row in rows]
        if len(set(identities)) != len(identities) or identities != sorted(identities):
            raise DeliveryError('delta packages_%s must be unique and sorted' % label)
        id_sets.append(set(identities))
    if id_sets[0] & id_sets[1] or id_sets[0] & id_sets[2] or id_sets[1] & id_sets[2]:
        raise DeliveryError('delta package lists must be pairwise disjoint')


def build_delta(baseline_raw, target_raw):
    """Diff two snapshots into one delta file covering the whole release.

    Unchanged messages and their identical evidence are never retransmitted;
    a message whose text survived an evidence change is still an upsert.
    """
    baseline = validate_snapshot(baseline_raw)
    target = validate_snapshot(target_raw)
    base_by_id = {_package_id(package): package for package in baseline['packages']}
    target_by_id = {_package_id(package): package for package in target['packages']}
    packages_removed = [{'application_id': app, 'locale': locale}
                        for app, locale in sorted(set(base_by_id) - set(target_by_id))]
    packages_added = [target_by_id[key]
                      for key in sorted(set(target_by_id) - set(base_by_id))]
    packages_changed = []
    for key in sorted(set(base_by_id) & set(target_by_id)):
        old = {message['key']: message for message in base_by_id[key]['messages']}
        new = {message['key']: message for message in target_by_id[key]['messages']}
        keys_removed = sorted(set(old) - set(new))
        upserted = [new[name] for name in sorted(new)
                    if name not in old or old[name] != new[name]]
        if keys_removed or upserted:
            packages_changed.append({'application_id': key[0], 'locale': key[1],
                                     'keys_removed': keys_removed,
                                     'keys_upserted': upserted})
    content = _delta_content(target, baseline['snapshot_id'], packages_removed,
                             packages_added, packages_changed)
    return validate_delta({'version': 1, **content, 'delta_id': _hash(content)})


def validate_delta(raw):
    """Validate a delta file and verify its integrity hash."""
    fields(raw, 'version target_release baseline_id target_id target_header '
                'packages_removed packages_added packages_changed delta_id')
    if type(raw['version']) is not int or raw['version'] != 1:
        raise DeliveryError('unsupported delta version')
    header = raw['target_header']
    fields(header, 'query_id at known_seq default_locale')
    header = {'query_id': text(header['query_id']),
              'at': integer(header['at'], 0),
              'known_seq': integer(header['known_seq'], 1),
              'default_locale': text(header['default_locale'])}
    content = {'target_release': integer(raw['target_release'], 1),
               'baseline_id': text(raw['baseline_id']),
               'target_id': text(raw['target_id']),
               'target_header': header,
               'packages_removed': [_package_ref(row) for row in
                                    array(raw['packages_removed'], PACKAGE_LIMIT)],
               'packages_added': _sorted_packages(raw['packages_added'],
                                                  header['default_locale']),
               'packages_changed': [_package_change(row, header['default_locale'])
                                    for row in array(raw['packages_changed'],
                                                     PACKAGE_LIMIT)]}
    _check_package_lists(content['packages_removed'], content['packages_added'],
                         content['packages_changed'])
    delta_id = text(raw['delta_id'])
    if delta_id != _hash(content):
        raise DeliveryError('delta id does not match its content')
    return {'version': 1, **content, 'delta_id': delta_id}


def _apply_delta(current, delta):
    """Rebuild the target snapshot from the current one; identity unchecked."""
    by_id = {_package_id(package): package for package in current['packages']}
    for ref in delta['packages_removed']:
        key = _package_id(ref)
        if key not in by_id:
            raise DeliveryError('delta removes a package the baseline does not have')
        del by_id[key]
    for change in delta['packages_changed']:
        key = _package_id(change)
        package = by_id.get(key)
        if package is None:
            raise DeliveryError('delta changes a package the baseline does not have')
        messages = {message['key']: message for message in package['messages']}
        for name in change['keys_removed']:
            if name not in messages:
                raise DeliveryError('delta removes a key the baseline does not have')
            del messages[name]
        for message in change['keys_upserted']:
            messages[message['key']] = message
        by_id[key] = {'application_id': key[0], 'locale': key[1],
                      'messages': [messages[name] for name in sorted(messages)]}
    for package in delta['packages_added']:
        key = _package_id(package)
        if key in by_id:
            raise DeliveryError('delta adds a package the baseline already has')
        by_id[key] = package
    packages = [by_id[key] for key in sorted(by_id)]
    header = delta['target_header']
    release = delta['target_release']
    return {'version': 1, 'release': release, 'query_id': header['query_id'],
            'at': header['at'], 'known_seq': header['known_seq'],
            'default_locale': header['default_locale'], 'packages': packages,
            'snapshot_id': _hash(snapshot_identity(
                release, header['default_locale'], packages))}


# --------------------------------------------------------------------------
# Store file
# --------------------------------------------------------------------------

def _notice_row(row):
    fields(row, 'notice_id withdrawn')
    return {'notice_id': text(row['notice_id']),
            'withdrawn': _sorted_evidence(row['withdrawn'])}


def validate_notice(raw):
    """Validate one withdrawal notice file."""
    fields(raw, 'version notice_id withdrawn')
    if type(raw['version']) is not int or raw['version'] != 1:
        raise DeliveryError('unsupported notice version')
    return _notice_row({'notice_id': raw['notice_id'],
                        'withdrawn': raw['withdrawn']})


def validate_store(raw):
    """Validate a store file and return a normalized, freely usable copy."""
    fields(raw, 'version current previous highest_release notices')
    if type(raw['version']) is not int or raw['version'] != 1:
        raise DeliveryError('unsupported store version')
    current = validate_snapshot(raw['current'])
    previous = raw['previous']
    if previous is not None:
        previous = validate_snapshot(previous)
        if previous['snapshot_id'] == current['snapshot_id']:
            raise DeliveryError('rollback candidate must differ from current')
    highest = integer(raw['highest_release'], 1)
    if current['release'] > highest:
        raise DeliveryError('current release exceeds the highest release')
    if previous is not None and previous['release'] >= highest:
        raise DeliveryError('rollback candidate release must be below the highest')
    notices = [_notice_row(row) for row in array(raw['notices'], NOTICE_LIMIT)]
    ids = [notice['notice_id'] for notice in notices]
    if len(set(ids)) != len(ids) or ids != sorted(ids):
        raise DeliveryError('notices must be unique and sorted by id')
    return {'version': 1, 'current': current, 'previous': previous,
            'highest_release': highest, 'notices': notices}


def _blocked_set(store):
    blocked = set()
    for notice in store['notices']:
        for evidence in notice['withdrawn']:
            blocked.add(_evidence_key(evidence))
    return blocked


def _package_blocked_by(package, blocked):
    found = set()
    for message in package['messages']:
        if message['translation_id'] is not None:
            key = (message['translation_id'], message['review_seq'])
            if key in blocked:
                found.add(key)
    return [{'translation_id': translation_id, 'review_seq': review_seq}
            for translation_id, review_seq in sorted(found)]


def init_store(snapshot_raw):
    """Create the store state from one complete snapshot."""
    snapshot = validate_snapshot(snapshot_raw)
    return {'version': 1, 'current': snapshot, 'previous': None,
            'highest_release': snapshot['release'], 'notices': []}


def install_delta(store_raw, delta_raw):
    """Install one delta; the whole switch commits or nothing changes.

    The delta's pinned baseline must be exactly the current snapshot (a
    stopped, unreadable current snapshot is still a valid baseline) and the
    target release number must be newer than the highest release ever
    installed. The restored target identity must match the pin and every
    package of the restored target must be servable, including unchanged
    entries inherited from the baseline.
    """
    store = validate_store(store_raw)
    delta = validate_delta(delta_raw)
    current = store['current']
    if delta['baseline_id'] != current['snapshot_id']:
        raise DeliveryError('delta baseline does not match the current snapshot')
    if delta['target_release'] <= store['highest_release']:
        raise DeliveryError('target release %d is not newer than the highest '
                            'installed release %d'
                            % (delta['target_release'], store['highest_release']))
    candidate = _apply_delta(current, delta)
    if candidate['snapshot_id'] != delta['target_id']:
        raise DeliveryError('restored snapshot does not match the target identity')
    blocked = _blocked_set(store)
    for package in candidate['packages']:
        bad = _package_blocked_by(package, blocked)
        if bad:
            raise DeliveryError(
                'target release references withdrawn evidence in package '
                '%s/%s: %s' % (package['application_id'], package['locale'],
                               ', '.join('%s#%d' % (item['translation_id'],
                                                    item['review_seq'])
                                         for item in bad)))
    return {'version': 1, 'current': candidate, 'previous': current,
            'highest_release': delta['target_release'],
            'notices': store['notices']}


def receive_notice(store_raw, notice_raw):
    """Accept one withdrawal notice; returns (store, changed).

    A repeated notice with the identical id and set is a no-op; the same id
    carrying a different set rejects the whole operation. Accepted evidence
    stays blocked forever, including evidence nothing has installed yet.
    """
    store = validate_store(store_raw)
    notice = validate_notice(notice_raw)
    for existing in store['notices']:
        if existing['notice_id'] == notice['notice_id']:
            if existing['withdrawn'] != notice['withdrawn']:
                raise DeliveryError('notice id already received with a '
                                    'different set: ' + notice['notice_id'])
            return store, False
    notices = sorted(store['notices'] + [notice],
                     key=lambda row: row['notice_id'])
    return {'version': 1, 'current': store['current'],
            'previous': store['previous'],
            'highest_release': store['highest_release'],
            'notices': notices}, True


def rollback_store(store_raw):
    """Roll back to the retained previous snapshot when it is fully servable.

    The highest release number and the accepted notices are never lost; a
    rejected rollback changes nothing, so the readable packages stay as they
    were.
    """
    store = validate_store(store_raw)
    previous = store['previous']
    if previous is None:
        raise DeliveryError('no rollback candidate')
    blocked = _blocked_set(store)
    for package in previous['packages']:
        bad = _package_blocked_by(package, blocked)
        if bad:
            raise DeliveryError(
                'rollback candidate references withdrawn evidence in package '
                '%s/%s' % (package['application_id'], package['locale']))
    return {'version': 1, 'current': previous, 'previous': None,
            'highest_release': store['highest_release'],
            'notices': store['notices']}


def store_status(store_raw):
    """Current identity, rollback candidate, highest release, readability."""
    store = validate_store(store_raw)
    blocked = _blocked_set(store)
    current = store['current']
    previous = store['previous']
    packages = []
    for package in current['packages']:
        bad = _package_blocked_by(package, blocked)
        packages.append({'application_id': package['application_id'],
                         'locale': package['locale'], 'readable': not bad,
                         'blocked_by': bad})
    rollback_allowed = previous is not None and not any(
        _package_blocked_by(package, blocked) for package in previous['packages'])
    return {'current': {'snapshot_id': current['snapshot_id'],
                        'release': current['release']},
            'previous': None if previous is None else
                        {'snapshot_id': previous['snapshot_id'],
                         'release': previous['release']},
            'highest_release': store['highest_release'],
            'rollback_allowed': rollback_allowed,
            'notices': store['notices'],
            'packages': packages}


def fetch_package(store_raw, application_id, locale):
    """Read one current package; withdrawn packages are not served."""
    store = validate_store(store_raw)
    key = (text(application_id), text(locale))
    blocked = _blocked_set(store)
    for package in store['current']['packages']:
        if _package_id(package) == key:
            bad = _package_blocked_by(package, blocked)
            if bad:
                raise DeliveryError(
                    'package is withdrawn by notice evidence: '
                    + ', '.join('%s#%d' % (item['translation_id'],
                                           item['review_seq'])
                                for item in bad))
            return package
    raise DeliveryError('unknown package: %s/%s' % key)
