import copy
import json
from pathlib import Path
import unittest

from locale_bundle.messages import render
from locale_bundle.vendor import (VendorError, build_package, empty_workspace,
                                  receive_response, validate_package,
                                  validate_workspace)

ROOT = Path(__file__).resolve().parents[1]


def fixture_workspace():
    return json.loads((ROOT / 'fixtures/vendor_workspace.json').read_text())


def fixture_selection():
    return json.loads((ROOT / 'fixtures/vendor_selection.json').read_text())


def group_for(package, context):
    groups = [g for g in package['request']['groups'] if g['context'] == context]
    assert len(groups) == 1
    return groups[0]


def response_from(package, group_id, texts):
    """Keep the source segment skeleton (same slots in order) but replace the
    literal text segments with ``texts`` in their original order."""
    group = next(g for g in package['request']['groups']
                 if g['group_id'] == group_id)
    cursor = 0
    segments = []
    for segment in group['segments']:
        if segment['kind'] == 'slot':
            segments.append(copy.deepcopy(segment))
        else:
            segments.append({'kind': 'text', 'text': texts[cursor]})
            cursor += 1
    assert cursor == len(texts)
    return {'batch_id': package['batch_id'],
            'results': [{'group_id': group_id, 'segments': segments}]}


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.workspace = fixture_workspace()
        self.selection = fixture_selection()

    def test_groups_merge_by_locale_context_template_and_variables(self):
        package = build_package(self.workspace, self.selection)
        groups = package['request']['groups']
        group_ids = [g['group_id'] for g in groups]
        self.assertEqual(group_ids, sorted(group_ids))
        contexts = sorted(g['context'] for g in groups)
        self.assertEqual(contexts,
                         ['ui/button', 'ui/plain', 'ui/slots', 'ui/zz-other'])
        button = group_for(package, 'ui/button')
        other = group_for(package, 'ui/zz-other')
        # Identical text, different reuse_context: separate groups.
        self.assertNotEqual(button['group_id'], other['group_id'])
        members = [m for m in package['local']['members']
                   if m['group_id'] == button['group_id']]
        self.assertEqual(sorted(m['key'] for m in members),
                         ['key_a', 'key_b', 'key_c'])
        # The request never carries keys, local revisions, drafts or receipts.
        request_blob = json.dumps(package['request'])
        for leaked in ('key_a', 'key_b', 'key_c', 'key_plain', 'key_slots',
                       'key_other', 'draft', 'revision', 'receipt'):
            self.assertNotIn(leaked, request_blob)
        # Source revision ids live only in the local association data.
        request_tokens = set(request_blob.replace('"', ' ').replace('{', ' ')
                             .replace('}', ' ').replace(',', ' ').split())
        self.assertTrue(request_tokens.isdisjoint(
            {m['source_revision'] for m in package['local']['members']}))

    def test_repeated_variables_get_distinct_slots_and_double_braces_are_literal(self):
        package = build_package(self.workspace, self.selection)
        slots_group = group_for(package, 'ui/slots')
        self.assertEqual(slots_group['variables'], [
            {'slot': 1, 'name': 'x', 'type': 'string'},
            {'slot': 2, 'name': 'x', 'type': 'string'},
            {'slot': 3, 'name': 'count', 'type': 'integer'}])
        kinds = [(s['kind'], s.get('text', s.get('slot')))
                 for s in slots_group['segments']]
        self.assertEqual(kinds, [
            ('text', 'Use '), ('slot', 1), ('text', ' and '), ('slot', 2),
            ('text', ' again with {brace} '), ('slot', 3)])

    def test_deterministic_equivalent_inputs(self):
        reordered = list(reversed(self.selection))
        package_a = build_package(self.workspace, self.selection)
        package_b = build_package(self.workspace, reordered)
        self.assertEqual(package_a, package_b)
        # Batch identity binds local source revisions and draft versions: a
        # new revision with identical text changes the batch id.
        changed = copy.deepcopy(self.workspace)
        changed['sources'][0]['source_revision'] = 'a2'
        package_c = build_package(changed, self.selection)
        self.assertNotEqual(package_c['batch_id'], package_a['batch_id'])
        saved = copy.deepcopy(self.workspace)
        self.assertEqual(self.workspace, saved)  # prepare never mutates inputs

    def test_selection_validation(self):
        bad = copy.deepcopy(self.selection)
        bad.append({'locale': 'de', 'key': 'key_a'})
        with self.assertRaises(VendorError):
            build_package(self.workspace, bad)
        with self.assertRaises(VendorError):
            build_package(self.workspace, [{'locale': 'en', 'key': 'key_a'}])
        with self.assertRaises(VendorError):
            build_package(self.workspace, [{'locale': 'de', 'key': 'nope'}])
        with self.assertRaises(VendorError):
            build_package(self.workspace, [])


class WorkspaceValidationTests(unittest.TestCase):
    def test_workspace_invariants(self):
        ws = fixture_workspace()
        validate_workspace(ws)
        bad = copy.deepcopy(ws)
        bad['locales'] = ['de', 'de']
        with self.assertRaises(VendorError):
            validate_workspace(bad)
        bad = copy.deepcopy(ws)
        bad['locales'] = ['en']
        with self.assertRaises(VendorError):
            validate_workspace(bad)
        bad = copy.deepcopy(ws)
        bad['sources'][0]['key'] = bad['sources'][1]['key']
        with self.assertRaises(VendorError):
            validate_workspace(bad)
        # Drafts whose locale or key left the current scope are retained as
        # history and remain valid against their own variable table.
        historical = copy.deepcopy(ws)
        historical['locales'] = ['ja']
        validate_workspace(historical)
        historical = copy.deepcopy(ws)
        historical['drafts'][0]['key'] = 'gone'  # source removed, draft kept
        validate_workspace(historical)
        # A draft is validated against its own variable table.
        bad = copy.deepcopy(ws)
        bad['drafts'][0]['message']['variables'] = {'name': 'string',
                                                     'extra': 'string'}
        with self.assertRaises((VendorError, ValueError)):
            validate_workspace(bad)
        # Null message keeps the row at a positive revision (withdrawal).
        withdrawn = copy.deepcopy(ws)
        withdrawn['drafts'][0]['message'] = None
        validate_workspace(withdrawn)

    def test_empty_workspace_constructor(self):
        ws = empty_workspace('en', ['de'])
        self.assertEqual(ws['receipts'], [])
        with self.assertRaises(VendorError):
            empty_workspace('en', ['en'])

    def test_corrupt_receipts_rejected(self):
        ws = fixture_workspace()
        package = build_package(ws, fixture_selection())
        group = group_for(package, 'ui/button')
        response = response_from(package, group['group_id'], ['x'])
        updated, _, _ = receive_response(ws, package, response)
        validate_workspace(copy.deepcopy(updated))
        # A stale segment hash is a corrupt record.
        bad = copy.deepcopy(updated)
        bad['receipts'][0]['segment_hash'] = '0' * 64
        with self.assertRaises(VendorError):
            validate_workspace(bad)
        # Duplicate (batch_id, group_id) receipt identity is rejected.
        bad = copy.deepcopy(updated)
        bad['receipts'].append(copy.deepcopy(bad['receipts'][0]))
        bad['receipts'][1]['seq'] = 2
        with self.assertRaises(VendorError):
            validate_workspace(bad)
        # And a corrupt workspace makes receive fail as a whole operation.
        with self.assertRaises(VendorError):
            receive_response(bad, package,
                             {'batch_id': package['batch_id'], 'results': []})


class ReceiveAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = fixture_workspace()
        self.package = build_package(self.workspace, fixture_selection())
        self.button = group_for(self.package, 'ui/button')
        self.slots = group_for(self.package, 'ui/slots')

    def result_row(self, report, group_id, locale, key):
        group = next(r for r in report['results'] if r['group_id'] == group_id)
        return next(m for m in group['members']
                    if m['locale'] == locale and m['key'] == key)

    def receive(self, workspace, response):
        updated, report, changed = receive_response(workspace, self.package, response)
        return updated, report, changed

    def test_acceptance_scenario_source_changed_draft_changed_applied(self):
        # Before the response: key_a gets a new source revision with the
        # identical text; key_b is saved (rev 2) then withdrawn (rev 3, null).
        workspace = copy.deepcopy(self.workspace)
        workspace['sources'][0]['source_revision'] = 'a2'
        workspace['drafts'][0]['revision'] = 2
        workspace['drafts'][0]['message'] = {
            'value': 'Speichern {name}', 'variables': {'name': 'string'}}
        workspace['drafts'][0]['revision'] = 3
        workspace['drafts'][0]['message'] = None
        response = response_from(self.package, self.button['group_id'], ['DE '])
        updated, report, changed = self.receive(workspace, response)
        self.assertTrue(changed)
        a = self.result_row(report, self.button['group_id'], 'de', 'key_a')
        b = self.result_row(report, self.button['group_id'], 'de', 'key_b')
        c = self.result_row(report, self.button['group_id'], 'de', 'key_c')
        self.assertEqual(a['result'], 'source_changed')
        self.assertEqual(b['result'], 'draft_changed')
        self.assertEqual(c['result'], 'applied')
        # Only key_c changed; key_a keeps its source, key_b keeps its null row.
        drafts = {(d['locale'], d['key']): d for d in updated['drafts']}
        self.assertNotIn(('de', 'key_a'), drafts)
        self.assertEqual(drafts[('de', 'key_b')]['message'], None)
        self.assertEqual(drafts[('de', 'key_b')]['revision'], 3)
        self.assertEqual(drafts[('de', 'key_c')]['revision'], 1)
        self.assertEqual(drafts[('de', 'key_c')]['message']['value'],
                         'DE {name}')
        # The applied translation renders through the existing render().
        self.assertEqual(render(drafts[('de', 'key_c')]['message'],
                                {'name': 'Ada'}), 'DE Ada')
        receipt = next(r for r in updated['receipts']
                       if r['group_id'] == self.button['group_id'])
        button_report = next(r for r in report['results']
                             if r['group_id'] == self.button['group_id'])
        self.assertEqual(receipt['members'], button_report['members'])

    def test_reordered_slots_with_literal_braces_render_safely(self):
        group = self.slots
        # Vendor reorders the slots and adds literal braces as text.
        segments = [
            {'kind': 'text', 'text': 'x2='}, group['segments'][3],
            {'kind': 'text', 'text': ' x1='}, group['segments'][1],
            {'kind': 'text', 'text': ' {literal} n='}, group['segments'][5]]
        response = {'batch_id': self.package['batch_id'],
                    'results': [{'group_id': group['group_id'],
                                 'segments': segments}]}
        updated, report, changed = self.receive(copy.deepcopy(self.workspace),
                                                response)
        self.assertTrue(changed)
        draft = next(d for d in updated['drafts']
                     if d['key'] == 'key_slots' and d['locale'] == 'de')
        # Literal vendor braces are escaped and never become variables.
        self.assertEqual(draft['message']['variables'],
                         {'x': 'string', 'count': 'integer'})
        rendered = render(draft['message'], {'x': 'v', 'count': 7})
        self.assertEqual(rendered, 'x2=v x1=v {literal} n=7')

    def test_empty_segments_is_a_valid_empty_translation(self):
        # Only slotless copy may answer with an empty segment array: it is a
        # valid empty translation, not an error.
        plain = group_for(self.package, 'ui/plain')
        response = {'batch_id': self.package['batch_id'],
                    'results': [{'group_id': plain['group_id'], 'segments': []}]}
        updated, report, _ = self.receive(copy.deepcopy(self.workspace), response)
        draft = next(d for d in updated['drafts']
                     if d['key'] == 'key_plain' and d['locale'] == 'de')
        self.assertEqual(draft['message'], {'value': '', 'variables': {}})
        self.assertEqual(render(draft['message'], {}), '')
        # A slotted group cannot drop its slots.
        bad = {'batch_id': self.package['batch_id'],
               'results': [{'group_id': self.button['group_id'], 'segments': []}]}
        with self.assertRaises(VendorError):
            self.receive(copy.deepcopy(self.workspace), bad)

    def test_partial_delivery_then_replay_returns_stored_verdict(self):
        group = self.slots
        first_segments = [
            {'kind': 'text', 'text': 'A '}, group['segments'][1],
            {'kind': 'text', 'text': ' B '}, group['segments'][3],
            {'kind': 'text', 'text': ' C '}, group['segments'][5]]
        response = {'batch_id': self.package['batch_id'],
                    'results': [{'group_id': group['group_id'],
                                 'segments': first_segments}]}
        workspace = copy.deepcopy(self.workspace)
        updated, report, changed = self.receive(workspace, response)
        self.assertTrue(changed)
        first_bytes = json.dumps(updated, sort_keys=True)
        # Replay the identical ordered segments: same verdict, no new receipt,
        # no workspace write.
        again, replay_report, changed_again = self.receive(updated, response)
        self.assertFalse(changed_again)
        self.assertTrue(replay_report['results'][0]['replayed'])
        self.assertEqual(replay_report['results'][0]['members'],
                         report['results'][0]['members'])
        self.assertEqual(json.dumps(again, sort_keys=True), first_bytes)
        self.assertEqual(len(again['receipts']), 1)
        # Different segments for an already received group fail the whole call.
        conflict = copy.deepcopy(response)
        conflict['results'][0]['segments'][-1] = {
            'kind': 'text', 'text': ' CHANGED '}
        conflict['results'][0]['segments'].append(group['segments'][5])
        with self.assertRaises(VendorError):
            self.receive(updated, conflict)

    def test_overlapping_batches_older_response_cannot_overwrite(self):
        selection = [{'locale': 'de', 'key': 'key_c'}]
        batch1 = build_package(copy.deepcopy(self.workspace), selection)
        # An editor advances the draft, then a second overlapping batch is
        # prepared against the new revision.
        workspace = copy.deepcopy(self.workspace)
        workspace['drafts'].append({
            'locale': 'de', 'key': 'key_c', 'revision': 1,
            'message': {'value': 'manual {name}',
                        'variables': {'name': 'string'}}})
        batch2 = build_package(copy.deepcopy(workspace), selection)
        self.assertNotEqual(batch1['batch_id'], batch2['batch_id'])
        g1 = batch1['request']['groups'][0]['group_id']
        g2 = batch2['request']['groups'][0]['group_id']
        resp2 = {'batch_id': batch2['batch_id'],
                 'results': [{'group_id': g2,
                              'segments': [{'kind': 'text', 'text': 'new '},
                                           {'kind': 'slot', 'slot': 1}]}]}
        workspace, report2, _ = receive_response(workspace, batch2, resp2)
        draft = next(d for d in workspace['drafts']
                     if d['key'] == 'key_c' and d['locale'] == 'de')
        self.assertEqual(draft['revision'], 2)
        # The stale batch1 response arrives later: draft_changed, no overwrite.
        resp1 = {'batch_id': batch1['batch_id'],
                 'results': [{'group_id': g1,
                              'segments': [{'kind': 'text', 'text': 'old '},
                                           {'kind': 'slot', 'slot': 1}]}]}
        workspace, report1, changed = receive_response(workspace, batch1, resp1)
        self.assertTrue(changed)  # the receipt is new even though nothing applied
        row = report1['results'][0]['members'][0]
        self.assertEqual(row['result'], 'draft_changed')
        draft = next(d for d in workspace['drafts']
                     if d['key'] == 'key_c' and d['locale'] == 'de')
        self.assertEqual(draft['revision'], 2)
        self.assertEqual(draft['message']['value'], 'new {name}')

    def test_all_members_skipped_still_records_the_receipt(self):
        workspace = copy.deepcopy(self.workspace)
        # The whole target language leaves the scope: every member is
        # out_of_scope, but a receipt is still written and no draft changes.
        # The old de draft stays in the file as history.
        workspace['locales'] = ['ja']
        response = response_from(self.package, self.button['group_id'], ['x'])
        updated, report, changed = self.receive(workspace, response)
        self.assertTrue(changed)
        self.assertTrue(all(m['result'] == 'out_of_scope'
                            for m in report['results'][0]['members']))
        self.assertEqual(len(updated['receipts']), 1)
        # No draft was written: the historical de row is untouched.
        self.assertEqual(updated['drafts'], workspace['drafts'])
        # A single source key leaving gives a mixed verdict and still no
        # draft row for that key.
        workspace = copy.deepcopy(self.workspace)
        workspace['sources'] = [s for s in workspace['sources']
                                if s['key'] != 'key_c']
        updated, report, _ = self.receive(workspace, response)
        drafts = {(d['locale'], d['key']): d for d in updated['drafts']}
        self.assertNotIn(('de', 'key_c'), drafts)
        verdicts = {m['key']: m['result']
                    for m in report['results'][0]['members']}
        self.assertEqual(verdicts['key_c'], 'out_of_scope')
        self.assertEqual(verdicts['key_a'], 'applied')

    def test_bad_group_or_conflict_in_one_delivery_writes_nothing(self):
        group = self.button
        good = {'group_id': group['group_id'],
                'segments': [{'kind': 'text', 'text': 'ok '},
                             {'kind': 'slot', 'slot': 1}]}
        # A valid new reply plus an unknown group: whole operation fails.
        response = {'batch_id': self.package['batch_id'],
                    'results': [good, {'group_id': 'gnope', 'segments': []}]}
        workspace = copy.deepcopy(self.workspace)
        before = json.dumps(workspace, sort_keys=True)
        with self.assertRaises(VendorError):
            self.receive(workspace, response)
        self.assertEqual(json.dumps(workspace, sort_keys=True), before)
        # A valid new reply plus a conflicting replay of another group: fail.
        slots_response = response_from(self.package, self.slots['group_id'],
                                       ['A ', ' B ', ' C '])
        workspace, _, _ = self.receive(workspace, slots_response)
        before = json.dumps(workspace, sort_keys=True)
        conflict = copy.deepcopy(slots_response)
        conflict['results'][0]['segments'][-2]['text'] = ' CHANGED '
        response = {'batch_id': self.package['batch_id'],
                    'results': [good, conflict['results'][0]]}
        with self.assertRaises(VendorError):
            self.receive(workspace, response)
        self.assertEqual(json.dumps(workspace, sort_keys=True), before)
        # Illegal segments: a missing slot fails before any write.
        illegal = {'batch_id': self.package['batch_id'],
                   'results': [{'group_id': self.slots['group_id'],
                                'segments': [{'kind': 'slot', 'slot': 1}]}]}
        with self.assertRaises(VendorError):
            self.receive(workspace, illegal)
        self.assertEqual(json.dumps(workspace, sort_keys=True), before)

    def test_response_validation(self):
        group = self.button
        good_segment = [{'kind': 'text', 'text': 'ok'}, {'kind': 'slot', 'slot': 1}]
        with self.assertRaises(VendorError):
            receive_response(self.workspace, self.package,
                             {'batch_id': 'other', 'results': []})
        dup = {'batch_id': self.package['batch_id'],
               'results': [{'group_id': group['group_id'], 'segments': good_segment},
                           {'group_id': group['group_id'], 'segments': good_segment}]}
        with self.assertRaises(VendorError):
            receive_response(self.workspace, self.package, dup)
        bad_slot = {'batch_id': self.package['batch_id'],
                    'results': [{'group_id': group['group_id'],
                                 'segments': [{'kind': 'slot', 'slot': 9}]}]}
        with self.assertRaises(VendorError):
            receive_response(self.workspace, self.package, bad_slot)

    def test_frozen_package_validates_against_its_own_content(self):
        package = build_package(self.workspace, fixture_selection())
        validate_package(copy.deepcopy(package))
        # Tamper with the frozen local association: batch id no longer binds.
        tampered = copy.deepcopy(package)
        tampered['local']['members'][0]['source_revision'] = 'zzz'
        with self.assertRaises(VendorError):
            validate_package(tampered)
        tampered = copy.deepcopy(package)
        tampered['request']['groups'][0]['context'] = 'changed'
        with self.assertRaises(VendorError):
            validate_package(tampered)


if __name__ == '__main__':
    unittest.main()
