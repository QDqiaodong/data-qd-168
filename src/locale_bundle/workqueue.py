"""Offline dispatch ledger (work queue) for the localization maintainer.

A single operator keeps the ledger in one local JSON file. ``sync`` consumes
the append-only review-history fact log (v2) layered with release-style
``components``/``applications`` (v3) and evaluates one explicitly selected
query cutoff: the application's used keys are the transitive closure of its
root components, the language scope is the non-default locales of
``requested``, and every (locale, key) slot is judged by that locale's own
review evidence even when the v1 fallback chain could serve another locale.

One task per (locale, key, source_id). ``assign``/``confirm`` are manual
operations recorded as audit events; they never modify review facts or
release bundles. Only successful syncs that change something record their
query and cutoff; an equivalent rerun leaves the ledger untouched.
"""
from .common import fields, text, integer, array
from .history import validate_fact_log, _current_sources, _slot_state
from .release import validate_component_graph, component_closure

SYNC_EXTRA_FIELDS = ('components', 'applications')
STATUSES = ('open', 'pending_confirm', 'done', 'archived')
REVIEW_STATES = ('missing', 'pending', 'rejected', 'approved', 'stale_source')
TASK_LIMIT = 100000
EVENT_LIMIT = 10000

_EVENT_FIELDS = {
    'created': 'seq type query_id at known_seq applications review_state',
    'scope_updated': 'seq type query_id at known_seq applications',
    'entered_scope': 'seq type query_id at known_seq applications',
    'left_scope': 'seq type query_id at known_seq',
    'approved': 'seq type query_id at known_seq translation_id review_seq',
    'approval_changed': 'seq type query_id at known_seq translation_id review_seq',
    'reopened': 'seq type query_id at known_seq review_state',
    'archived': 'seq type query_id at known_seq replaced_by',
    'assigned': 'seq type assignee',
    'confirmed': 'seq type confirmer translation_id review_seq',
}
_SYNC_EVENTS = ('created', 'scope_updated', 'entered_scope', 'left_scope',
                'approved', 'approval_changed', 'reopened', 'archived')


def empty_ledger():
    return {'version': 1, 'last_sync': None, 'syncs': [], 'tasks': []}


def _identifier_set(value):
    items = [text(item) for item in array(value)]
    if len(set(items)) != len(items) or items != sorted(items):
        raise ValueError('expected a sorted set of identifiers')
    return items


def _review_state(value):
    if value not in REVIEW_STATES:
        raise ValueError('invalid review state')
    return value


def _evidence(value):
    fields(value, 'translation_id review_seq')
    return {'translation_id': text(value['translation_id']),
            'review_seq': integer(value['review_seq'], 1)}


def _confirmation(value):
    fields(value, 'confirmer translation_id review_seq')
    return {'confirmer': text(value['confirmer']),
            'translation_id': text(value['translation_id']),
            'review_seq': integer(value['review_seq'], 1)}


def _event(value):
    if not isinstance(value, dict) or value.get('type') not in _EVENT_FIELDS:
        raise ValueError('invalid ledger event')
    kind = value['type']
    fields(value, _EVENT_FIELDS[kind])
    event = {'seq': integer(value['seq'], 1), 'type': kind}
    if kind in _SYNC_EVENTS:
        event['query_id'] = text(value['query_id'])
        event['at'] = integer(value['at'], 0)
        event['known_seq'] = integer(value['known_seq'], 1)
    if kind in ('created', 'scope_updated', 'entered_scope'):
        event['applications'] = _identifier_set(value['applications'])
    if kind in ('created', 'reopened'):
        event['review_state'] = _review_state(value['review_state'])
    if kind in ('approved', 'approval_changed'):
        event['translation_id'] = text(value['translation_id'])
        event['review_seq'] = integer(value['review_seq'], 1)
    if kind == 'archived':
        event['replaced_by'] = text(value['replaced_by'])
    if kind == 'assigned':
        event['assignee'] = text(value['assignee'])
    if kind == 'confirmed':
        event['confirmer'] = text(value['confirmer'])
        event['translation_id'] = text(value['translation_id'])
        event['review_seq'] = integer(value['review_seq'], 1)
    return event


def _task(value):
    fields(value, 'locale key source_id status in_scope applications assignee '
                 'review_state approval confirmation events')
    status = value['status']
    if status not in STATUSES:
        raise ValueError('invalid task status')
    in_scope = value['in_scope']
    if type(in_scope) is not bool:
        raise ValueError('in_scope must be a boolean')
    approval = value['approval']
    if approval is not None:
        approval = _evidence(approval)
    confirmation = value['confirmation']
    if confirmation is not None:
        confirmation = _confirmation(confirmation)
    assignee = value['assignee']
    if assignee is not None:
        assignee = text(assignee)
    events = [_event(row) for row in array(value['events'], EVENT_LIMIT)]
    if not events:
        raise ValueError('task must carry its creation event')
    seqs = [event['seq'] for event in events]
    if len(set(seqs)) != len(seqs) or seqs != sorted(seqs):
        raise ValueError('task events must have strictly increasing seqs')
    if status == 'open' and (approval is not None or confirmation is not None):
        raise ValueError('open task cannot hold approval or confirmation')
    if status == 'pending_confirm' and (approval is None or confirmation is not None):
        raise ValueError('pending_confirm task must hold approval only')
    if status == 'done':
        if approval is None or confirmation is None:
            raise ValueError('done task must hold approval and confirmation')
        done = (confirmation['translation_id'], confirmation['review_seq'])
        if done != (approval['translation_id'], approval['review_seq']):
            raise ValueError('done task confirmation must match its approval')
    review_state = _review_state(value['review_state'])
    if status == 'open' and review_state == 'approved':
        raise ValueError('open task cannot be in approved review state')
    if status in ('pending_confirm', 'done') and review_state != 'approved':
        raise ValueError('task awaiting or past confirmation must be approved')
    if status == 'archived' and in_scope:
        raise ValueError('archived task cannot be in scope')
    return {'locale': text(value['locale']), 'key': text(value['key']),
            'source_id': text(value['source_id']), 'status': status,
            'in_scope': in_scope, 'applications': _identifier_set(value['applications']),
            'assignee': assignee, 'review_state': review_state,
            'approval': approval, 'confirmation': confirmation, 'events': events}


def _sync_record(value):
    fields(value, 'query_id at known_seq')
    return {'query_id': text(value['query_id']), 'at': integer(value['at'], 0),
            'known_seq': integer(value['known_seq'], 1)}


def validate_ledger(raw):
    """Validate a ledger file and return a normalized, freely mutable copy."""
    fields(raw, 'version last_sync syncs tasks')
    if type(raw['version']) is not int or raw['version'] != 1:
        raise ValueError('unsupported ledger version')
    last_sync = raw['last_sync']
    if last_sync is not None:
        last_sync = _sync_record(last_sync)
    syncs = [_sync_record(row) for row in array(raw['syncs'])]
    for previous, current in zip(syncs, syncs[1:]):
        if current['at'] < previous['at'] or current['known_seq'] < previous['known_seq']:
            raise ValueError('ledger sync history is not monotonic')
    if bool(syncs) != (last_sync is not None) or (syncs and syncs[-1] != last_sync):
        raise ValueError('ledger last_sync does not match its sync history')
    tasks = [_task(row) for row in array(raw['tasks'], TASK_LIMIT)]
    identities = [(task['locale'], task['key'], task['source_id']) for task in tasks]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicate task identity')
    if identities != sorted(identities):
        raise ValueError('ledger tasks must be sorted by identity')
    seqs = [event['seq'] for task in tasks for event in task['events']]
    if len(set(seqs)) != len(seqs):
        raise ValueError('ledger event seqs must be globally unique')
    return {'version': 1, 'last_sync': last_sync, 'syncs': syncs, 'tasks': tasks}


def _find_task(ledger, locale, key, source_id):
    for task in ledger['tasks']:
        if (task['locale'], task['key'], task['source_id']) == (locale, key, source_id):
            return task
    raise ValueError('unknown task: %s/%s/%s' % (locale, key, source_id))


def _event_seq(ledger):
    seqs = [event['seq'] for task in ledger['tasks'] for event in task['events']]
    return max(seqs, default=0) + 1


def sync_ledger(ledger, data, query_id):
    """Sync the ledger against one query cutoff; return the new ledger.

    ledger is raw ledger data (or None for the first sync); it is validated
    before use and never mutated. Any rollback below the last accepted
    cutoff, invalid input or an invalid ledger raises ValueError and leaves
    the caller's ledger untouched.
    """
    ledger = empty_ledger() if ledger is None else validate_ledger(ledger)
    ctx = validate_fact_log(data, SYNC_EXTRA_FIELDS)
    query_id = text(query_id)
    queries = {query['id']: query for query in ctx['queries']}
    if query_id not in queries:
        raise ValueError('unknown query: ' + query_id)
    query = queries[query_id]
    at, known_seq = query['at'], query['known_seq']
    last = ledger['last_sync']
    if last is not None and (at < last['at'] or known_seq < last['known_seq']):
        raise ValueError('cutoff rollback rejected: last accepted is '
                         '%(query_id)s (at=%(at)d, known_seq=%(known_seq)d)' % last)
    components, applications, by_id = validate_component_graph(
        data['components'], data['applications'], set(ctx['sources_by_key']),
        'source facts')
    del components  # only the application closures and the id index are used

    current = _current_sources(ctx['sources_by_key'], at, known_seq)
    if not current:
        raise ValueError('query has no visible source revision: ' + query_id)

    # Scope: keys actually used per application, restricted to revisions in
    # effect at the cutoff; keys not yet effective are not dispatched.
    key_apps = {}
    for application in applications:
        keys = set()
        for component_id in component_closure(application, by_id):
            keys.update(by_id[component_id]['keys'])
        for key in keys:
            if key in current:
                key_apps.setdefault(key, set()).add(application['id'])
    locales = sorted(locale for locale in ctx['requested'] if locale != ctx['default'])
    scope = {(locale, key) for locale in locales for key in key_apps}

    def state_of(locale, key):
        return _slot_state(ctx['translations_by_slot'], ctx['reviews_by_translation'],
                           locale, key, current[key]['id'], at, known_seq)

    changed = False
    next_seq = [_event_seq(ledger)]

    def record(task, **event):
        event['seq'] = next_seq[0]
        next_seq[0] += 1
        task['events'].append(event)
        return event

    def sync_event(task, kind, **details):
        return record(task, type=kind, query_id=query_id, at=at, known_seq=known_seq,
                      **details)

    tasks = []
    for task in ledger['tasks']:
        key = task['key']
        source = current.get(key)
        if task['status'] != 'archived' and source is not None \
                and task['source_id'] != source['id']:
            # Source revision replaced: archive even when already out of scope.
            task['status'] = 'archived'
            task['in_scope'] = False
            sync_event(task, 'archived', replaced_by=source['id'])
            changed = True
        if task['status'] != 'archived':
            if (task['locale'], key) in scope:
                apps = sorted(key_apps[key])
                if not task['in_scope']:
                    task['in_scope'] = True
                    task['applications'] = apps
                    sync_event(task, 'entered_scope', applications=apps)
                    changed = True
                elif task['applications'] != apps:
                    task['applications'] = apps
                    sync_event(task, 'scope_updated', applications=apps)
                    changed = True
                row = state_of(task['locale'], key)
                if row['state'] == 'approved':
                    evidence = {'translation_id': row['translation_id'],
                                'review_seq': row['review_seq']}
                    if task['status'] == 'open':
                        task['status'] = 'pending_confirm'
                        task['review_state'] = 'approved'
                        task['approval'] = evidence
                        sync_event(task, 'approved', **evidence)
                        changed = True
                    elif task['status'] == 'pending_confirm':
                        if task['approval'] != evidence:
                            task['approval'] = evidence
                            sync_event(task, 'approval_changed', **evidence)
                            changed = True
                    elif task['status'] == 'done':
                        confirmed = (task['confirmation']['translation_id'],
                                     task['confirmation']['review_seq'])
                        if confirmed != (evidence['translation_id'], evidence['review_seq']):
                            # Same slot, different evidence: the old
                            # confirmation cannot keep this task complete.
                            task['status'] = 'pending_confirm'
                            task['confirmation'] = None
                            task['approval'] = evidence
                            sync_event(task, 'approval_changed', **evidence)
                            changed = True
                else:
                    if task['status'] in ('pending_confirm', 'done'):
                        task['status'] = 'open'
                        task['review_state'] = row['state']
                        task['approval'] = None
                        task['confirmation'] = None
                        sync_event(task, 'reopened', review_state=row['state'])
                        changed = True
                    elif task['review_state'] != row['state']:
                        task['review_state'] = row['state']
                        changed = True
            elif task['in_scope']:
                task['in_scope'] = False
                sync_event(task, 'left_scope')
                changed = True
        tasks.append(task)

    existing = {(task['locale'], task['key'], task['source_id']) for task in tasks}
    for locale in locales:
        for key in sorted(key_apps):
            source = current[key]
            if (locale, key, source['id']) in existing:
                continue
            row = state_of(locale, key)
            if row['state'] == 'approved':
                continue  # only revisions not yet approved get a new task
            task = {'locale': locale, 'key': key, 'source_id': source['id'],
                    'status': 'open', 'in_scope': True,
                    'applications': sorted(key_apps[key]), 'assignee': None,
                    'review_state': row['state'], 'approval': None,
                    'confirmation': None, 'events': []}
            sync_event(task, 'created', applications=task['applications'],
                       review_state=row['state'])
            tasks.append(task)
            changed = True

    if changed:
        record_sync = {'query_id': query_id, 'at': at, 'known_seq': known_seq}
        ledger['syncs'].append(record_sync)
        ledger['last_sync'] = record_sync
    ledger['tasks'] = sorted(tasks, key=lambda task: (task['locale'], task['key'],
                                                      task['source_id']))
    return ledger


def assign_task(ledger, locale, key, source_id, translator):
    """Assign an in-scope, incomplete task to a translator."""
    ledger = validate_ledger(ledger)
    task = _find_task(ledger, text(locale), text(key), text(source_id))
    translator = text(translator)
    if task['status'] == 'archived':
        raise ValueError('task is archived')
    if not task['in_scope']:
        raise ValueError('task is out of scope')
    if task['status'] not in ('open', 'pending_confirm'):
        raise ValueError('task is already completed')
    task['assignee'] = translator
    task['events'].append({'seq': _event_seq(ledger), 'type': 'assigned',
                           'assignee': translator})
    return ledger


def confirm_task(ledger, locale, key, source_id, confirmer, translation_id, review_seq):
    """Confirm completion with the task's current approval evidence.

    Repeating a confirmation with the still-current evidence is a no-op;
    stale evidence, archived tasks and out-of-scope tasks are rejected.
    """
    ledger = validate_ledger(ledger)
    task = _find_task(ledger, text(locale), text(key), text(source_id))
    confirmer = text(confirmer)
    translation_id = text(translation_id)
    review_seq = integer(review_seq, 1)
    if task['status'] == 'archived':
        raise ValueError('task is archived')
    if not task['in_scope']:
        raise ValueError('task is out of scope')
    if task['status'] not in ('pending_confirm', 'done'):
        raise ValueError('task has no approval to confirm')
    approval = task['approval']
    if (approval['translation_id'], approval['review_seq']) != (translation_id, review_seq):
        raise ValueError('confirmation evidence does not match the current approval')
    if task['status'] == 'done':
        return ledger
    task['status'] = 'done'
    task['confirmation'] = {'confirmer': confirmer, 'translation_id': translation_id,
                            'review_seq': review_seq}
    task['events'].append({'seq': _event_seq(ledger), 'type': 'confirmed',
                           'confirmer': confirmer, 'translation_id': translation_id,
                           'review_seq': review_seq})
    return ledger


def query_ledger(ledger):
    """Return the query view: tasks, evidence, statuses and full audit trail."""
    return validate_ledger(ledger)
