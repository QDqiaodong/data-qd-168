#!/usr/bin/env python3
"""Offline demo of the locale-bundle-vendor handoff (contract v5).

Reproduces the acceptance scenario with throwaway files under run/vendor/
using only the local CLI; no network, no vendor service. Run from the
repository root with the project environment:

    .venv/bin/python examples/vendor_demo.py

Scenario:
  * three keys with identical text and one reuse_context share one vendor
    group; a fourth key with identical text but another context gets its own
    group; a fifth key exercises repeated variables, slot reordering and
    literal braces;
  * before the response, one key gets a new source revision (identical text)
    while another key goes no-draft -> saved draft -> withdrawn (null) draft;
  * the response therefore only changes the third key, recording
    source_changed and draft_changed for the other two;
  * another group is delivered separately, then its earlier response is
    replayed (same verdict, no rewrite) and a conflicting replay is refused;
  * an overlapping batch prepared after a local edit cannot overwrite the
    newer draft when its older response arrives late;
  * one delivery mixing a valid new reply with an unknown group writes
    nothing: drafts and receipts stay atomically consistent.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'run' / 'vendor'
WORKSPACE = WORK / 'workspace.json'
SELECTION = WORK / 'selection.json'
PACKAGE = WORK / 'package.json'
RESPONSE = WORK / 'response.json'
REQUEST_COPY = WORK / 'request_for_vendor.json'
PACKAGE2 = WORK / 'package2.json'
RESPONSE2 = WORK / 'response2.json'

WORKSPACE_DOC = {
    'version': 1,
    'default_locale': 'en',
    'locales': ['de', 'ja'],
    'sources': [
        {'key': 'key_a', 'source_revision': 'a1', 'reuse_context': 'ui/button',
         'value': 'Save {name}', 'variables': {'name': 'string'}},
        {'key': 'key_b', 'source_revision': 'b1', 'reuse_context': 'ui/button',
         'value': 'Save {name}', 'variables': {'name': 'string'}},
        {'key': 'key_c', 'source_revision': 'c1', 'reuse_context': 'ui/button',
         'value': 'Save {name}', 'variables': {'name': 'string'}},
        {'key': 'key_other', 'source_revision': 'o1',
         'reuse_context': 'ui/zz-other',
         'value': 'Save {name}', 'variables': {'name': 'string'}},
        {'key': 'key_overlap', 'source_revision': 'v1',
         'reuse_context': 'ui/overlap',
         'value': 'Send {name}', 'variables': {'name': 'string'}},
        {'key': 'key_plain', 'source_revision': 'p1', 'reuse_context': 'ui/plain',
         'value': 'Done', 'variables': {}},
        {'key': 'key_slots', 'source_revision': 'k1', 'reuse_context': 'ui/slots',
         'value': 'Use {x} and {x} again with {{brace}} {count}',
         'variables': {'x': 'string', 'count': 'integer'}},
    ],
    'drafts': [
        {'locale': 'de', 'key': 'key_b', 'revision': 1,
         'message': {'value': 'Alt {name}', 'variables': {'name': 'string'}}},
    ],
    'receipts': [],
}
SELECTION_DOC = [
    {'locale': 'de', 'key': 'key_c'},
    {'locale': 'de', 'key': 'key_a'},
    {'locale': 'de', 'key': 'key_other'},
    {'locale': 'de', 'key': 'key_b'},
    {'locale': 'de', 'key': 'key_plain'},
    {'locale': 'de', 'key': 'key_slots'},
    {'locale': 'de', 'key': 'key_overlap'},
]


def cli(*args, expect=0):
    result = subprocess.run(
        [sys.executable, '-m', 'locale_bundle.vendor_cli', *args],
        capture_output=True, text=True, cwd=ROOT)
    if result.returncode != expect:
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        raise SystemExit('step failed: %s (exit %s, wanted %s)'
                         % (' '.join(args), result.returncode, expect))
    return result


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def group_by_context(package, context):
    return next(g for g in package['request']['groups']
                if g['context'] == context)


def replace_literals(group, texts):
    """Rebuild response segments preserving every slot, swapping literals."""
    cursor = 0
    segments = []
    for segment in group['segments']:
        if segment['kind'] == 'slot':
            segments.append(dict(segment))
        else:
            segments.append({'kind': 'text', 'text': texts[cursor]})
            cursor += 1
    assert cursor == len(texts)
    return segments


def show_drafts(label):
    ws = load(WORKSPACE)
    print('== ' + label)
    for draft in ws['drafts']:
        message = draft['message']
        value = None if message is None else message['value']
        print('   %s/%s rev=%s message=%r'
              % (draft['locale'], draft['key'], draft['revision'], value))
    for receipt in ws['receipts']:
        verdicts = ', '.join('%s/%s=%s' % (m['locale'], m['key'], m['result'])
                             for m in receipt['members'])
        print('   receipt seq=%s group=%s [%s]'
              % (receipt['seq'], receipt['group_id'], verdicts))


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    save(WORKSPACE, WORKSPACE_DOC)
    save(SELECTION, SELECTION_DOC)

    # 1. prepare: grouping is independent of selection order.
    cli('prepare', '--workspace', str(WORKSPACE), '--selection', str(SELECTION),
        '--package', str(PACKAGE))
    package = load(PACKAGE)
    contexts = sorted(g['context'] for g in package['request']['groups'])
    print('1. prepared batch', package['batch_id'][:12], 'groups=', contexts)
    button = group_by_context(package, 'ui/button')
    other = group_by_context(package, 'ui/zz-other')
    assert button['group_id'] != other['group_id'], 'context must split groups'
    members = sorted(m['key'] for m in package['local']['members']
                     if m['group_id'] == button['group_id'])
    assert members == ['key_a', 'key_b', 'key_c'], members
    # Only the request leaves the machine; the local association stays behind.
    save(REQUEST_COPY, package['request'])
    request_blob = json.dumps(package['request'])
    assert 'key_a' not in request_blob and 'revision' not in request_blob
    print('   handed', REQUEST_COPY.relative_to(ROOT),
        'to the vendor; local members stay in', PACKAGE.relative_to(ROOT))

    # 2. external editor: source revision bump (identical text) and the
    #    no-draft -> saved -> withdrawn lifecycle, expressed as workspace
    #    snapshots any external editor could produce under the same contract.
    ws = load(WORKSPACE)
    ws['sources'][0]['source_revision'] = 'a2'
    ws['drafts'][0]['revision'] = 2
    ws['drafts'][0]['message'] = {'value': 'Speichern {name}',
                                  'variables': {'name': 'string'}}
    save(WORKSPACE, ws)
    print('2a. key_a source a1 -> a2 (same text); key_b draft saved at rev 2')
    ws = load(WORKSPACE)
    ws['drafts'][0]['revision'] = 3
    ws['drafts'][0]['message'] = None
    save(WORKSPACE, ws)
    print('2b. key_b draft withdrawn at rev 3 (null message, row kept)')

    # 3. vendor response for the shared button group.
    response = {'batch_id': package['batch_id'],
                'results': [{'group_id': button['group_id'],
                             'segments': replace_literals(button, ['DE '])}]}
    save(RESPONSE, response)
    report = json.loads(cli('receive', '--workspace', str(WORKSPACE),
                            '--package', str(PACKAGE),
                            '--response', str(RESPONSE)).stdout)
    verdicts = {(m['key']): m['result'] for m in report['results'][0]['members']}
    assert verdicts == {'key_a': 'source_changed', 'key_b': 'draft_changed',
                        'key_c': 'applied'}, verdicts
    show_drafts('3. button group received: only key_c applied')
    ws = load(WORKSPACE)
    draft_c = next(d for d in ws['drafts'] if d['key'] == 'key_c')
    assert draft_c['revision'] == 1 and draft_c['message']['value'] == 'DE {name}'
    draft_b = next(d for d in ws['drafts'] if d['key'] == 'key_b')
    assert draft_b['revision'] == 3 and draft_b['message'] is None

    # 4. exact replay returns the stored verdict and does not rewrite.
    before = WORKSPACE.read_bytes()
    replay = cli('receive', '--workspace', str(WORKSPACE),
                 '--package', str(PACKAGE), '--response', str(RESPONSE))
    replay_report = json.loads(replay.stdout)
    assert replay_report['results'][0]['replayed'] is True
    assert WORKSPACE.read_bytes() == before
    print('4. identical replay: stored verdict returned, workspace unchanged')

    # 5. conflicting replay for the same group is refused with exit 2.
    conflict = json.loads(RESPONSE.read_text())
    conflict['results'][0]['segments'][0]['text'] = 'ANDERS '
    save(RESPONSE, conflict)
    refused = cli('receive', '--workspace', str(WORKSPACE),
                  '--package', str(PACKAGE), '--response', str(RESPONSE),
                  expect=2)
    assert 'different segments' in refused.stderr
    assert WORKSPACE.read_bytes() == before
    print('5. conflicting replay refused with exit 2, workspace preserved')

    # 6. partial delivery for the slots group with reordered slots and
    #    literal braces, then replaying that earlier partial response.
    slots = group_by_context(package, 'ui/slots')
    reordered = [
        {'kind': 'text', 'text': 'x2='}, slots['segments'][3],
        {'kind': 'text', 'text': ' x1='}, slots['segments'][1],
        {'kind': 'text', 'text': ' {literal} n='}, slots['segments'][5]]
    save(RESPONSE, {'batch_id': package['batch_id'],
                    'results': [{'group_id': slots['group_id'],
                                 'segments': reordered}]})
    cli('receive', '--workspace', str(WORKSPACE), '--package', str(PACKAGE),
        '--response', str(RESPONSE))
    show_drafts('6. slots group received with reordered slots')
    ws = load(WORKSPACE)
    slots_draft = next(d for d in ws['drafts'] if d['key'] == 'key_slots')
    from locale_bundle.messages import render
    rendered = render(slots_draft['message'], {'x': 'v', 'count': 7})
    assert rendered == 'x2=v x1=v {literal} n=7', rendered
    before = WORKSPACE.read_bytes()
    cli('receive', '--workspace', str(WORKSPACE), '--package', str(PACKAGE),
        '--response', str(RESPONSE))
    assert WORKSPACE.read_bytes() == before
    print('   replaying the earlier partial response changes nothing; render =',
          rendered)

    # 7. overlapping batches: key_overlap belongs to a group the main batch
    #    never received. A local edit advances its draft, a second overlapping
    #    batch is prepared against the new revision, and when the stale main
    #    batch's late response arrives it cannot overwrite the newer draft.
    overlap = group_by_context(package, 'ui/overlap')
    ws = load(WORKSPACE)
    ws['drafts'].append({
        'locale': 'de', 'key': 'key_overlap', 'revision': 1,
        'message': {'value': 'manual {name}',
                    'variables': {'name': 'string'}}})
    ws['drafts'].sort(key=lambda d: (d['locale'], d['key']))
    save(WORKSPACE, ws)
    print('7a. local draft for key_overlap saved at rev 1')

    save(SELECTION, [{'locale': 'de', 'key': 'key_overlap'}])
    cli('prepare', '--workspace', str(WORKSPACE), '--selection', str(SELECTION),
        '--package', str(PACKAGE2))
    package2 = load(PACKAGE2)
    assert package2['batch_id'] != package['batch_id']
    g2 = package2['request']['groups'][0]['group_id']
    save(RESPONSE2, {'batch_id': package2['batch_id'],
                     'results': [{'group_id': g2,
                                  'segments': [{'kind': 'text', 'text': 'new '},
                                               {'kind': 'slot', 'slot': 1}]}]})
    cli('receive', '--workspace', str(WORKSPACE), '--package', str(PACKAGE2),
        '--response', str(RESPONSE2))
    # The stale response from the first batch arrives last.
    stale = {'batch_id': package['batch_id'],
             'results': [{'group_id': overlap['group_id'],
                          'segments': [{'kind': 'text', 'text': 'old '},
                                       {'kind': 'slot', 'slot': 1}]}]}
    save(RESPONSE, stale)
    stale_report = json.loads(cli('receive', '--workspace', str(WORKSPACE),
                                  '--package', str(PACKAGE),
                                  '--response', str(RESPONSE)).stdout)
    assert stale_report['results'][0]['members'][0]['result'] == 'draft_changed'
    ws = load(WORKSPACE)
    draft_overlap = next(d for d in ws['drafts'] if d['key'] == 'key_overlap')
    assert draft_overlap['revision'] == 2
    assert draft_overlap['message']['value'] == 'new {name}'
    print('7b. overlapping batches: stale response recorded draft_changed,',
          'draft kept %r at rev 2' % draft_overlap['message']['value'])

    # 8. a delivery mixing a valid new reply with an unknown group must not
    #    partially write drafts or receipts.
    plain = group_by_context(package, 'ui/plain')
    before = WORKSPACE.read_bytes()
    receipts_before = len(load(WORKSPACE)['receipts'])
    save(RESPONSE, {'batch_id': package['batch_id'],
                    'results': [
                        {'group_id': plain['group_id'], 'segments': []},
                        {'group_id': 'g0000000000000000', 'segments': []}]})
    bad = cli('receive', '--workspace', str(WORKSPACE),
              '--package', str(PACKAGE), '--response', str(RESPONSE), expect=2)
    assert 'unknown group' in bad.stderr
    assert WORKSPACE.read_bytes() == before
    assert len(load(WORKSPACE)['receipts']) == receipts_before
    print('8. valid reply + unknown group in one delivery: no partial write')

    show_drafts('final workspace state')
    print('demo OK')


if __name__ == '__main__':
    main()
