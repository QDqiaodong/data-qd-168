"""Offline vendor handoff (contract v5).

The localization maintainer keeps one workspace JSON file holding the
default locale, the target locales, the current source rows, the local
drafts and the receive receipts. ``build_package`` groups a selection of
(locale, key) rows into vendor-facing translation groups without leaking
keys, local revisions, drafts or receipts, and ``receive_response`` writes
accepted vendor translations back into local drafts only.

A response never becomes an approved translation and never touches a
release bundle: it can only create or advance a local draft row. Version
conflicts are valid business outcomes (recorded per member), while unknown
groups, batch mismatches, corrupt records and illegal segments fail the
whole operation before any byte is written.
"""
import copy
import hashlib
import json
from string import Formatter

from .common import fields, text, integer, array
from .messages import validate_message

SOURCE_LIMIT = 10000
DRAFT_LIMIT = 100000
RECEIPT_LIMIT = 10000
SELECTION_LIMIT = 10000
RESULT_LIMIT = 10000
SEGMENT_LIMIT = 10000
TEXT_LIMIT = 10000
MEMBER_RESULTS = ('applied', 'out_of_scope', 'source_changed', 'draft_changed')


class VendorError(ValueError):
    """A validation or conflict error that makes the whole command fail."""


def _canonical(value):
    """Deterministic JSON bytes used for identity hashes and replay compares."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _bounded_text(value):
    if not isinstance(value, str) or len(value) > TEXT_LIMIT:
        raise VendorError('bounded text segment required')
    return value


# --------------------------------------------------------------------------
# Source template segmentation
# --------------------------------------------------------------------------

def split_segments(value):
    """Split a validated source template into text/slot segments.

    Double braces are literal text: Formatter unescapes ``{{``/``}}`` into
    single braces and may emit adjacent literal chunks around them, so
    neighboring text chunks are merged. Every variable occurrence gets its
    own slot number, including repeats.
    """
    segments = []
    occurrences = []
    slot = 0
    for literal, name, spec, conversion in Formatter().parse(value):
        if literal:
            if segments and segments[-1]['kind'] == 'text':
                segments[-1]['text'] += literal
            else:
                segments.append({'kind': 'text', 'text': literal})
        if name is not None:
            if spec or conversion or not name.isidentifier():
                raise VendorError('only simple named placeholders are supported')
            slot += 1
            occurrences.append((slot, name))
            segments.append({'kind': 'slot', 'slot': slot})
    return segments, occurrences


def build_group_segments(value, variables):
    """Return (segments, slot variables) for one source template."""
    segments, occurrences = split_segments(value)
    slot_variables = [{'slot': slot, 'name': name, 'type': variables[name]}
                      for slot, name in occurrences]
    return segments, slot_variables


def reassemble(group, segments):
    """Rebuild a renderable template from a vendor response.

    Vendor text is literal: every brace is doubled so Formatter can never
    read a vendor-supplied brace as a new variable. Slot segments are turned
    back into the original named placeholders.
    """
    names = {entry['slot']: entry['name'] for entry in group['variables']}
    parts = []
    for segment in segments:
        if segment['kind'] == 'text':
            parts.append(segment['text'].replace('{', '{{').replace('}', '}}'))
        else:
            parts.append('{' + names[segment['slot']] + '}')
    value = ''.join(parts)
    variables = {}
    for entry in group['variables']:
        variables[entry['name']] = entry['type']
    return validate_message({'value': value, 'variables': variables})


# --------------------------------------------------------------------------
# Workspace (sources + drafts + receipts in one atomic file)
# --------------------------------------------------------------------------

def _source_row(row):
    fields(row, 'key source_revision reuse_context value variables')
    message = validate_message({'value': row['value'], 'variables': row['variables']})
    return {'key': text(row['key']), 'source_revision': text(row['source_revision']),
            'reuse_context': text(row['reuse_context']),
            'value': message['value'], 'variables': message['variables']}


def _draft_row(row):
    fields(row, 'locale key revision message')
    message = row['message']
    if message is not None:
        message = validate_message(message)
    return {'locale': text(row['locale']), 'key': text(row['key']),
            'revision': integer(row['revision'], 1), 'message': message}


def _segment(row):
    if not isinstance(row, dict) or row.get('kind') not in ('text', 'slot'):
        raise VendorError('invalid segment')
    if row['kind'] == 'text':
        fields(row, 'kind text')
        return {'kind': 'text', 'text': _bounded_text(row['text'])}
    fields(row, 'kind slot')
    return {'kind': 'slot', 'slot': integer(row['slot'], 1)}


def _receipt_row(row):
    fields(row, 'seq batch_id group_id segments segment_hash members')
    seq = integer(row['seq'], 1)
    batch_id = text(row['batch_id'])
    group_id = text(row['group_id'])
    segments = [_segment(item) for item in array(row['segments'], SEGMENT_LIMIT)]
    segment_hash = text(row['segment_hash'])
    if segment_hash != _hash(segments):
        raise VendorError('receipt segment hash does not match its segments')
    members = []
    for item in array(row['members'], SELECTION_LIMIT):
        fields(item, 'locale key result revision_after')
        result = item['result']
        if result not in MEMBER_RESULTS:
            raise VendorError('invalid member result')
        members.append({'locale': text(item['locale']), 'key': text(item['key']),
                        'result': result,
                        'revision_after': integer(item['revision_after'], 0)})
    identities = [(item['locale'], item['key']) for item in members]
    if len(set(identities)) != len(identities) or identities != sorted(identities):
        raise VendorError('receipt members must be unique and sorted')
    return {'seq': seq, 'batch_id': batch_id, 'group_id': group_id,
            'segments': segments, 'segment_hash': segment_hash, 'members': members}


def validate_workspace(raw):
    """Validate a workspace file and return a normalized mutable copy."""
    fields(raw, 'version default_locale locales sources drafts receipts')
    if type(raw['version']) is not int or raw['version'] != 1:
        raise VendorError('unsupported workspace version')
    default_locale = text(raw['default_locale'])
    locales = [text(item) for item in array(raw['locales'], 100)]
    if not locales or len(set(locales)) != len(locales):
        raise VendorError('target locales must be present and unique')
    if default_locale in locales:
        raise VendorError('a target locale cannot be the default locale')
    sources = [_source_row(row) for row in array(raw['sources'], SOURCE_LIMIT)]
    keys = [row['key'] for row in sources]
    if len(set(keys)) != len(keys) or keys != sorted(keys):
        raise VendorError('source keys must be unique and sorted')
    drafts = [_draft_row(row) for row in array(raw['drafts'], DRAFT_LIMIT)]
    draft_ids = [(row['locale'], row['key']) for row in drafts]
    if len(set(draft_ids)) != len(draft_ids) or draft_ids != sorted(draft_ids):
        raise VendorError('draft identity must be unique and sorted')
    # Drafts for locales or keys that have left the current scope are kept as
    # history and validated only against their own variable table.
    receipts = [_receipt_row(row) for row in array(raw['receipts'], RECEIPT_LIMIT)]
    seqs = [row['seq'] for row in receipts]
    if len(set(seqs)) != len(seqs) or seqs != sorted(seqs):
        raise VendorError('receipt seqs must be unique and sorted')
    receipt_ids = [(row['batch_id'], row['group_id']) for row in receipts]
    if len(set(receipt_ids)) != len(receipt_ids):
        raise VendorError('a group can only be received once per batch')
    return {'version': 1, 'default_locale': default_locale, 'locales': locales,
            'sources': sources, 'drafts': drafts, 'receipts': receipts}


def empty_workspace(default_locale, locales):
    """Constructor for tests and external editors following the contract."""
    return validate_workspace({'version': 1, 'default_locale': default_locale,
                               'locales': list(locales), 'sources': [],
                               'drafts': [], 'receipts': []})


# --------------------------------------------------------------------------
# Frozen package validation
# --------------------------------------------------------------------------

def _request_group(row):
    fields(row, 'group_id locale context variables segments')
    group_id = text(row['group_id'])
    locale = text(row['locale'])
    context = text(row['context'])
    variables = []
    for item in array(row['variables'], SEGMENT_LIMIT):
        fields(item, 'slot name type')
        name = item['name']
        if not isinstance(name, str) or not name.isidentifier():
            raise VendorError('invalid variable name')
        if item['type'] not in ('string', 'integer'):
            raise VendorError('invalid variable type')
        variables.append({'slot': integer(item['slot'], 1), 'name': name,
                          'type': item['type']})
    slots = [entry['slot'] for entry in variables]
    if len(set(slots)) != len(slots) or slots != sorted(slots):
        raise VendorError('group slots must be unique and sorted')
    segments = [_segment(item) for item in array(row['segments'], SEGMENT_LIMIT)]
    used = [item['slot'] for item in segments if item['kind'] == 'slot']
    if used != slots:
        raise VendorError('group segments must use every declared slot exactly once')
    content = {'locale': locale, 'context': context,
               'variables': variables, 'segments': segments}
    if group_id != 'g' + _hash(content)[:16]:
        raise VendorError('group id does not match its frozen content')
    return {'group_id': group_id, 'locale': locale, 'context': context,
            'variables': variables, 'segments': segments}, content


def _package_member(row):
    fields(row, 'group_id locale key source_revision source_value source_variables '
                 'draft_revision')
    message = validate_message({'value': row['source_value'],
                                'variables': row['source_variables']})
    return {'group_id': text(row['group_id']), 'locale': text(row['locale']),
            'key': text(row['key']), 'source_revision': text(row['source_revision']),
            'source_value': message['value'], 'source_variables': message['variables'],
            'draft_revision': integer(row['draft_revision'], 0)}


def validate_package(raw):
    """Validate a frozen package against its own content, not current drafts."""
    fields(raw, 'version batch_id request local')
    if type(raw['version']) is not int or raw['version'] != 1:
        raise VendorError('unsupported package version')
    batch_id = text(raw['batch_id'])
    request = raw['request']
    local = raw['local']
    if not isinstance(request, dict) or not isinstance(local, dict):
        raise VendorError('request and local must be objects')
    fields(request, 'batch_id groups')
    fields(local, 'batch_id members')
    if text(request['batch_id']) != batch_id or text(local['batch_id']) != batch_id:
        raise VendorError('batch id is inconsistent inside the package')
    groups = []
    contents = {}
    for row in array(request['groups'], RESULT_LIMIT):
        group, content = _request_group(row)
        if group['group_id'] in contents:
            raise VendorError('duplicate group id')
        groups.append(group)
        contents[group['group_id']] = content
    group_ids = [group['group_id'] for group in groups]
    if group_ids != sorted(group_ids):
        raise VendorError('package groups must be sorted by group id')
    members = [_package_member(row) for row in array(local['members'], SELECTION_LIMIT)]
    identities = [(row['locale'], row['key']) for row in members]
    if not identities or len(set(identities)) != len(identities) \
            or identities != sorted(identities):
        raise VendorError('package members must be nonempty, unique and sorted')
    groups_by_id = {group['group_id']: group for group in groups}
    for member in members:
        group = groups_by_id.get(member['group_id'])
        if group is None:
            raise VendorError('package member references an unknown group')
        if group['locale'] != member['locale']:
            raise VendorError('package member locale differs from its group locale')
        segments, slot_variables = build_group_segments(
            member['source_value'], member['source_variables'])
        if segments != group['segments'] or slot_variables != group['variables']:
            raise VendorError('package member source does not match its group')
    if batch_id != _hash({'groups': groups, 'members': members}):
        raise VendorError('batch id does not match the frozen package content')
    return {'version': 1, 'batch_id': batch_id,
            'request': {'batch_id': batch_id, 'groups': groups},
            'local': {'batch_id': batch_id, 'members': members}}


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------

def validate_selection(raw, workspace):
    rows = array(raw, SELECTION_LIMIT)
    if not rows:
        raise VendorError('selection must be nonempty')
    selection = []
    for row in rows:
        fields(row, 'locale key')
        selection.append((text(row['locale']), text(row['key'])))
    if len(set(selection)) != len(selection):
        raise VendorError('selection contains a duplicate (locale, key) row')
    locale_set = set(workspace['locales'])
    source_by_key = {row['key']: row for row in workspace['sources']}
    for locale, key in selection:
        if locale not in locale_set:
            raise VendorError('selection locale is not a current target locale')
        if key not in source_by_key:
            raise VendorError('selection key is not a current source key')
    return sorted(selection)


def build_package(workspace_raw, selection_raw):
    """Build a deterministic, vendor-safe package; the workspace is untouched."""
    workspace = validate_workspace(workspace_raw)
    selection = validate_selection(selection_raw, workspace)
    source_by_key = {row['key']: row for row in workspace['sources']}
    draft_revision = {(row['locale'], row['key']): row['revision']
                      for row in workspace['drafts']}
    grouped = {}
    order = []
    for locale, key in selection:
        source = source_by_key[key]
        segments, slot_variables = build_group_segments(
            source['value'], source['variables'])
        content = {'locale': locale, 'context': source['reuse_context'],
                   'variables': slot_variables, 'segments': segments}
        group_id = 'g' + _hash(content)[:16]
        if group_id not in grouped:
            grouped[group_id] = {
                'group_id': group_id, 'locale': locale,
                'context': source['reuse_context'],
                'variables': slot_variables, 'segments': segments}
            order.append(group_id)
        grouped.setdefault('members:' + group_id, []).append({
            'group_id': group_id, 'locale': locale, 'key': key,
            'source_revision': source['source_revision'],
            'source_value': source['value'],
            'source_variables': source['variables'],
            'draft_revision': draft_revision.get((locale, key), 0)})
    groups = [grouped[group_id] for group_id in sorted(order)]
    members = []
    for group_id in sorted(order):
        members.extend(grouped['members:' + group_id])
    members.sort(key=lambda row: (row['locale'], row['key']))
    batch_id = _hash({'groups': groups, 'members': members})
    return validate_package({'version': 1, 'batch_id': batch_id,
                             'request': {'batch_id': batch_id, 'groups': groups},
                             'local': {'batch_id': batch_id, 'members': members}})


# --------------------------------------------------------------------------
# receive
# --------------------------------------------------------------------------

def _response_segments(raw, group):
    segments = [_segment(item) for item in array(raw, SEGMENT_LIMIT)]
    declared = [entry['slot'] for entry in group['variables']]
    used = [item['slot'] for item in segments if item['kind'] == 'slot']
    if sorted(used) != sorted(declared) or len(used) != len(set(used)):
        raise VendorError('every original slot must appear exactly once')
    return segments


def validate_response(raw, package):
    fields(raw, 'batch_id results')
    batch_id = text(raw['batch_id'])
    if batch_id != package['batch_id']:
        raise VendorError('response batch id does not match the package')
    groups_by_id = {group['group_id']: group
                    for group in package['request']['groups']}
    results = []
    seen = set()
    for row in array(raw['results'], RESULT_LIMIT):
        fields(row, 'group_id segments')
        group_id = text(row['group_id'])
        if group_id not in groups_by_id:
            raise VendorError('response references an unknown group: ' + group_id)
        if group_id in seen:
            raise VendorError('response repeats a group in the same delivery')
        seen.add(group_id)
        segments = _response_segments(row['segments'], groups_by_id[group_id])
        results.append({'group_id': group_id, 'segments': segments})
    return results


def receive_response(workspace_raw, package_raw, response_raw):
    """Process one response delivery.

    Returns (new_workspace, report, changed). The inputs are never mutated.
    All structural validation and replay-conflict checks finish before any
    draft or receipt is produced, so a failure leaves the original file as
    the only possible state.
    """
    workspace = validate_workspace(workspace_raw)
    package = validate_package(package_raw)
    response = validate_response(response_raw, package)
    groups_by_id = {group['group_id']: group
                    for group in package['request']['groups']}
    members_by_group = {}
    for member in package['local']['members']:
        members_by_group.setdefault(member['group_id'], []).append(member)
    receipts_by_id = {(row['batch_id'], row['group_id']): row
                      for row in workspace['receipts']}

    # Replay conflicts fail the whole call before anything is judged.
    for row in response:
        prior = receipts_by_id.get((package['batch_id'], row['group_id']))
        if prior is not None and _canonical(prior['segments']) \
                != _canonical(row['segments']):
            raise VendorError('group already received with different segments: '
                              + row['group_id'])

    # Every translation is rebuilt and validated before any state is touched,
    # so a malformed group can never leave a partially mutated workspace.
    translated_by_group = {
        row['group_id']: reassemble(groups_by_id[row['group_id']],
                                    row['segments'])
        for row in response
        if (package['batch_id'], row['group_id']) not in receipts_by_id}

    new_workspace = copy.deepcopy(workspace)
    source_by_key = {row['key']: row for row in new_workspace['sources']}
    locale_set = set(new_workspace['locales'])
    next_seq = max((row['seq'] for row in new_workspace['receipts']), default=0) + 1
    report = {'batch_id': package['batch_id'], 'results': []}
    changed = False

    for row in sorted(response, key=lambda item: item['group_id']):
        group_id = row['group_id']
        prior = receipts_by_id.get((package['batch_id'], group_id))
        if prior is not None:
            # Exact replay: return the stored verdict, rejudge nothing.
            report['results'].append(
                {'group_id': group_id, 'replayed': True,
                 'members': copy.deepcopy(prior['members'])})
            continue
        translated = translated_by_group[group_id]
        outcomes = []
        for member in sorted(members_by_group[group_id],
                             key=lambda item: (item['locale'], item['key'])):
            locale, key = member['locale'], member['key']
            draft = next((item for item in new_workspace['drafts']
                          if item['locale'] == locale and item['key'] == key), None)
            current_revision = draft['revision'] if draft is not None else 0
            if locale not in locale_set or key not in source_by_key:
                result = 'out_of_scope'
            else:
                source = source_by_key[key]
                if source['source_revision'] != member['source_revision'] \
                        or source['value'] != member['source_value'] \
                        or source['variables'] != member['source_variables']:
                    result = 'source_changed'
                elif current_revision != member['draft_revision']:
                    result = 'draft_changed'
                else:
                    result = 'applied'
                    new_revision = member['draft_revision'] + 1
                    if draft is None:
                        new_workspace['drafts'].append(
                            {'locale': locale, 'key': key, 'revision': new_revision,
                             'message': copy.deepcopy(translated)})
                        new_workspace['drafts'].sort(
                            key=lambda item: (item['locale'], item['key']))
                    else:
                        draft['revision'] = new_revision
                        draft['message'] = copy.deepcopy(translated)
                    current_revision = new_revision
            outcomes.append({'locale': locale, 'key': key, 'result': result,
                             'revision_after': current_revision})
        receipt = {'seq': next_seq, 'batch_id': package['batch_id'],
                   'group_id': group_id, 'segments': copy.deepcopy(row['segments']),
                   'segment_hash': _hash(row['segments']), 'members': outcomes}
        next_seq += 1
        new_workspace['receipts'].append(receipt)
        receipts_by_id[(package['batch_id'], group_id)] = receipt
        report['results'].append({'group_id': group_id, 'replayed': False,
                                  'members': outcomes})
        changed = True

    report['results'].sort(key=lambda item: item['group_id'])
    return new_workspace, report, changed


def serialized_size(value):
    """Byte length of the on-disk representation, including the trailing newline."""
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          allow_nan=False, indent=2).encode('utf-8')) + 1
