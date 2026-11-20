import copy
import json
from pathlib import Path
import unittest

from locale_bundle.workqueue import (sync_ledger, assign_task, confirm_task,
                                     query_ledger, validate_ledger, empty_ledger)

ROOT = Path(__file__).resolve().parents[1]
APP_A = {'id': 'app-a', 'roots': ['checkout', 'catalog']}
APP_B = {'id': 'app-b', 'roots': ['b-ui']}


def sample():
    return json.loads((ROOT / 'fixtures/workqueue_input.json').read_text())


def step(applications, mutate=None):
    data = copy.deepcopy(sample())
    data['applications'] = copy.deepcopy(applications)
    if mutate is not None:
        mutate(data)
    return data


def only_task(ledger):
    assert len(ledger['tasks']) == 1
    return ledger['tasks'][0]


def task(ledger, locale, key, source_id):
    return next(t for t in ledger['tasks']
                if (t['locale'], t['key'], t['source_id']) == (locale, key, source_id))


def event_types(t):
    return [event['type'] for event in t['events']]


class AcceptanceTests(unittest.TestCase):
    """The linked dispatch scenario from the contract, end to end."""

    def test_linked_scenario(self):
        # app-a reaches the shared key through two paths; app-b is absent.
        ledger = sync_ledger(None, step([APP_A]), 'q1')
        t = only_task(ledger)
        self.assertEqual((t['locale'], t['key'], t['source_id']), ('fr', 'greeting', 's1'))
        self.assertEqual((t['status'], t['applications'], t['assignee']),
                         ('open', ['app-a'], None))
        self.assertEqual(ledger['last_sync'], {'query_id': 'q1', 'at': 0, 'known_seq': 2})

        ledger = assign_task(ledger, 'fr', 'greeting', 's1', 'alice')
        self.assertEqual(only_task(ledger)['assignee'], 'alice')

        # Same cutoff, app-b joins: still one task, affected set grows.
        ledger = sync_ledger(ledger, step([APP_A, APP_B]), 'q1')
        t = only_task(ledger)
        self.assertEqual(t['applications'], ['app-a', 'app-b'])
        self.assertEqual(t['assignee'], 'alice')

        # Equivalent reruns (including reordered input) append nothing.
        self.assertEqual(sync_ledger(ledger, step([APP_A, APP_B]), 'q1'), ledger)
        shuffled = step([APP_B, APP_A])
        for name in ('sources', 'translations', 'reviews', 'queries', 'components'):
            shuffled[name] = list(reversed(shuffled[name]))
        self.assertEqual(sync_ledger(ledger, shuffled, 'q1'), ledger)

        # Approval becomes visible: pending confirmation, then manual confirm.
        ledger = sync_ledger(ledger, step([APP_A, APP_B]), 'q2')
        t = only_task(ledger)
        self.assertEqual(t['status'], 'pending_confirm')
        self.assertEqual(t['approval'], {'translation_id': 't1', 'review_seq': 3})
        ledger = confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't1', 3)
        t = only_task(ledger)
        self.assertEqual(t['status'], 'done')
        self.assertEqual(t['confirmation'],
                         {'confirmer': 'lead', 'translation_id': 't1', 'review_seq': 3})

        # The revocation sync reopens the same task; the old confirmation stays.
        ledger = sync_ledger(ledger, step([APP_A, APP_B]), 'q3')
        t = only_task(ledger)
        self.assertEqual((t['status'], t['assignee'], t['confirmation']),
                         ('open', 'alice', None))
        confirmed = [e for e in t['events'] if e['type'] == 'confirmed']
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]['confirmer'], 'lead')

        # Both applications leave, then app-a rejoins: same assignment.
        ledger = sync_ledger(ledger, step([]), 'q3')
        self.assertIs(only_task(ledger)['in_scope'], False)
        ledger = sync_ledger(ledger, step([APP_A]), 'q3')
        t = only_task(ledger)
        self.assertIs(t['in_scope'], True)
        self.assertEqual(t['assignee'], 'alice')

        # s2 replaces s1 while the translation still points at s1: archive the
        # old task and open a fresh unassigned one for app-a only.
        ledger = sync_ledger(ledger, step([APP_A]), 'q5')
        self.assertEqual(len(ledger['tasks']), 2)
        old = task(ledger, 'fr', 'greeting', 's1')
        new = task(ledger, 'fr', 'greeting', 's2')
        self.assertEqual((old['status'], old['assignee']), ('archived', 'alice'))
        self.assertEqual([e['type'] for e in old['events'] if e['type'] == 'confirmed'],
                         ['confirmed'])
        self.assertEqual((new['status'], new['assignee'], new['applications'],
                          new['review_state']),
                         ('open', None, ['app-a'], 'stale_source'))

        # Operations against the archived revision are rejected.
        with self.assertRaises(ValueError):
            assign_task(ledger, 'fr', 'greeting', 's1', 'bob')
        with self.assertRaises(ValueError):
            confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't1', 5)

        self.assertEqual(event_types(old),
                         ['created', 'assigned', 'scope_updated', 'approved',
                          'confirmed', 'reopened', 'left_scope', 'entered_scope',
                          'archived'])
        seqs = [e['seq'] for t in ledger['tasks'] for e in t['events']]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))
        self.assertEqual([s['query_id'] for s in ledger['syncs']],
                         ['q1', 'q1', 'q2', 'q3', 'q3', 'q3', 'q5'])


class CutoffTests(unittest.TestCase):
    def test_rollback_of_either_dimension_rejected_and_ledger_untouched(self):
        ledger = sync_ledger(None, step([APP_A]), 'q3')
        before = copy.deepcopy(ledger)

        def with_query(qid, at, known_seq):
            def mutate(data):
                data['queries'].append({'id': qid, 'at': at, 'known_seq': known_seq})
            return mutate
        # at decreases while known_seq increases, and vice versa.
        with self.assertRaisesRegex(ValueError, 'rollback'):
            sync_ledger(ledger, step([APP_A], with_query('qa', 11, 5)), 'qa')
        with self.assertRaisesRegex(ValueError, 'rollback'):
            sync_ledger(ledger, step([APP_A], with_query('qb', 13, 3)), 'qb')
        self.assertEqual(ledger, before)
        # Equal cutoffs and jointly increasing cutoffs are accepted.
        self.assertEqual(sync_ledger(ledger, step([APP_A]), 'q3'), ledger)
        advanced = sync_ledger(ledger, step([APP_A]), 'q4')
        self.assertEqual(advanced['last_sync'],
                         {'query_id': 'q4', 'at': 14, 'known_seq': 5})

    def test_unchanged_sync_does_not_advance_watermark(self):
        ledger = sync_ledger(None, step([APP_A]), 'q1')
        # A new fact about an undeclared key changes nothing task-relevant.
        def mutate(data):
            data['sources'].append({'id': 's9', 'seq': 7, 'effective': 0,
                                    'key': 'other', 'value': 'Other', 'variables': {}})
            data['queries'].append({'id': 'q9', 'at': 0, 'known_seq': 7})
        self.assertEqual(sync_ledger(ledger, step([APP_A], mutate), 'q9'), ledger)
        # The recorded watermark still allows a lower-but-equal cutoff afterwards.
        self.assertEqual(sync_ledger(ledger, step([APP_A]), 'q1'), ledger)


class EvidenceTests(unittest.TestCase):
    def test_old_evidence_cannot_confirm_reapproved_translation(self):
        ledger = sync_ledger(None, step([APP_A]), 'q2')
        ledger = sync_ledger(ledger, step([APP_A]), 'q3')
        ledger = sync_ledger(ledger, step([APP_A]), 'q4')
        t = only_task(ledger)
        self.assertEqual(t['status'], 'pending_confirm')
        self.assertEqual(t['approval'], {'translation_id': 't1', 'review_seq': 5})
        with self.assertRaisesRegex(ValueError, 'current approval'):
            confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't1', 3)
        ledger = confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't1', 5)
        self.assertEqual(only_task(ledger)['status'], 'done')
        # Repeating the still-current confirmation appends no event.
        self.assertEqual(confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't1', 5),
                         ledger)

    def test_replaced_approval_evidence_breaks_completion_without_rejection(self):
        def mutate(data):
            data['translations'].append(
                {'id': 't2', 'seq': 7, 'effective': 16, 'locale': 'fr', 'key': 'greeting',
                 'source_id': 's1', 'value': 'Salut {name}', 'variables': {'name': 'string'}})
            data['reviews'].append({'seq': 8, 'effective': 16, 'translation_id': 't2',
                                    'approved': True})
            data['queries'].append({'id': 'q6', 'at': 16, 'known_seq': 8})
        ledger = sync_ledger(None, step([APP_A]), 'q1')
        ledger = sync_ledger(ledger, step([APP_A]), 'q2')
        ledger = confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't1', 3)
        self.assertEqual(only_task(ledger)['status'], 'done')
        # t2 becomes the approved translation of the same source revision;
        # no rejection of t1 is ever visible between the two syncs.
        ledger = sync_ledger(ledger, step([APP_A], mutate), 'q6')
        t = only_task(ledger)
        self.assertEqual(t['status'], 'pending_confirm')
        self.assertEqual(t['approval'], {'translation_id': 't2', 'review_seq': 8})
        self.assertIn('approval_changed', event_types(t))
        with self.assertRaisesRegex(ValueError, 'current approval'):
            confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't1', 3)
        ledger = confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't2', 8)
        self.assertEqual(only_task(ledger)['status'], 'done')

    def test_done_task_reentering_scope_is_rejudged(self):
        ledger = sync_ledger(None, step([APP_A]), 'q1')
        ledger = sync_ledger(ledger, step([APP_A]), 'q2')
        ledger = confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't1', 3)
        ledger = sync_ledger(ledger, step([]), 'q2')
        self.assertIs(only_task(ledger)['in_scope'], False)
        # Evidence unchanged on re-entry: completion is maintained.
        ledger = sync_ledger(ledger, step([APP_A]), 'q2')
        self.assertEqual(only_task(ledger)['status'], 'done')
        # Revoked while out of scope: re-entry reopens the same task.
        ledger = sync_ledger(ledger, step([]), 'q3')
        ledger = sync_ledger(ledger, step([APP_A]), 'q3')
        t = only_task(ledger)
        self.assertEqual(t['status'], 'open')
        self.assertIn('reopened', event_types(t))


class ScopeTests(unittest.TestCase):
    def test_fallback_locale_needs_its_own_approval(self):
        def mutate(data):
            data['locales'] = ['en', 'fr', 'fr-CA']
            data['fallback'] = {'fr-CA': 'fr'}
            data['requested'] = ['fr-CA']
        # fr is approved at q2 and fr-CA could fall back to it, yet the
        # fr-CA slot itself has no translation at all.
        ledger = sync_ledger(None, step([APP_A], mutate), 'q2')
        t = only_task(ledger)
        self.assertEqual((t['locale'], t['review_state'], t['status']),
                         ('fr-CA', 'missing', 'open'))

    def test_language_scope_follows_requested(self):
        def wider(data):
            data['locales'] = ['en', 'fr', 'fr-CA']
            data['fallback'] = {'fr-CA': 'fr'}
            data['requested'] = ['fr', 'fr-CA']
        ledger = sync_ledger(None, step([APP_A], wider), 'q1')
        self.assertEqual({t['locale'] for t in ledger['tasks']}, {'fr', 'fr-CA'})
        # fr-CA leaves the language scope; the fr task is untouched.
        ledger = sync_ledger(ledger, step([APP_A]), 'q1')
        self.assertIs(task(ledger, 'fr-CA', 'greeting', 's1')['in_scope'], False)
        self.assertIs(task(ledger, 'fr', 'greeting', 's1')['in_scope'], True)
        with self.assertRaisesRegex(ValueError, 'out of scope'):
            assign_task(ledger, 'fr-CA', 'greeting', 's1', 'alice')

    def test_default_only_requested_dispatches_nothing(self):
        ledger = sync_ledger(None, step([APP_A], lambda d: d.update(requested=['en'])), 'q1')
        self.assertEqual(ledger['tasks'], [])
        self.assertIsNone(ledger['last_sync'])

    def test_key_not_yet_effective_is_not_dispatched(self):
        def mutate(data):
            data['sources'].append({'id': 's9', 'seq': 7, 'effective': 50,
                                    'key': 'future-key', 'value': 'Future',
                                    'variables': {}})
            data['components'].append({'id': 'future', 'requires': [],
                                       'keys': ['future-key']})
            data['applications'] = [{'id': 'app-a',
                                     'roots': ['checkout', 'catalog', 'future']}]
            data['queries'].append({'id': 'q6', 'at': 50, 'known_seq': 7})
        ledger = sync_ledger(None, step(None, mutate), 'q1')
        self.assertEqual([t['key'] for t in ledger['tasks']], ['greeting'])
        ledger = sync_ledger(ledger, step(None, mutate), 'q6')
        future = [t for t in ledger['tasks'] if t['key'] == 'future-key']
        self.assertEqual(len(future), 1)
        self.assertEqual((future[0]['status'], future[0]['source_id']), ('open', 's9'))

    def test_approved_revision_creates_no_task(self):
        ledger = sync_ledger(None, step([APP_A]), 'q2')
        self.assertEqual(ledger['tasks'], [])

    def test_archived_even_when_out_of_scope(self):
        ledger = sync_ledger(None, step([APP_A]), 'q1')
        ledger = sync_ledger(ledger, step([]), 'q1')
        self.assertIs(only_task(ledger)['in_scope'], False)
        # s2 becomes current while the task is out of scope: still archived.
        ledger = sync_ledger(ledger, step([]), 'q5')
        old = only_task(ledger)
        self.assertEqual((old['status'], old['in_scope']), ('archived', False))
        self.assertEqual([e['type'] for e in old['events']],
                         ['created', 'left_scope', 'archived'])
        # app-a rejoins at the new revision: a fresh unassigned task appears.
        ledger = sync_ledger(ledger, step([APP_A]), 'q5')
        new = task(ledger, 'fr', 'greeting', 's2')
        self.assertEqual((new['status'], new['assignee'], new['review_state']),
                         ('open', None, 'stale_source'))

    def test_empty_applications_is_a_valid_empty_scope(self):
        ledger = sync_ledger(None, step([]), 'q1')
        self.assertEqual(ledger, empty_ledger())


class ManualOperationTests(unittest.TestCase):
    def setUp(self):
        self.ledger = sync_ledger(None, step([APP_A]), 'q1')

    def test_assign_requires_incomplete_in_scope_task(self):
        with self.assertRaisesRegex(ValueError, 'unknown task'):
            assign_task(self.ledger, 'fr', 'greeting', 's9', 'alice')
        ledger = assign_task(self.ledger, 'fr', 'greeting', 's1', 'alice')
        self.assertEqual(only_task(ledger)['assignee'], 'alice')
        # Reassignment of an incomplete task is a new manual act.
        ledger = assign_task(ledger, 'fr', 'greeting', 's1', 'bob')
        self.assertEqual(only_task(ledger)['assignee'], 'bob')
        # Assigning is also possible while awaiting confirmation.
        ledger = sync_ledger(ledger, step([APP_A]), 'q2')
        ledger = assign_task(ledger, 'fr', 'greeting', 's1', 'alice')
        ledger = confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't1', 3)
        with self.assertRaisesRegex(ValueError, 'completed'):
            assign_task(ledger, 'fr', 'greeting', 's1', 'bob')

    def test_confirm_requires_current_approval(self):
        with self.assertRaisesRegex(ValueError, 'no approval'):
            confirm_task(self.ledger, 'fr', 'greeting', 's1', 'lead', 't1', 3)
        with self.assertRaisesRegex(ValueError, 'unknown task'):
            confirm_task(self.ledger, 'fr', 'absent', 's1', 'lead', 't1', 3)

    def test_manual_operations_never_touch_facts(self):
        before = step([APP_A])
        ledger = assign_task(self.ledger, 'fr', 'greeting', 's1', 'alice')
        ledger = sync_ledger(ledger, step([APP_A]), 'q2')
        ledger = confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't1', 3)
        self.assertEqual(step([APP_A]), before)


class ValidationTests(unittest.TestCase):
    def test_sync_input_validation(self):
        def assert_invalid(mutate, pattern=''):
            data = step([APP_A])
            mutate(data)
            with self.assertRaisesRegex(ValueError, pattern):
                sync_ledger(None, data, 'q1')
        assert_invalid(lambda d: d.update(extra=1), 'expected fields')
        assert_invalid(lambda d: d.pop('components'), 'expected fields')
        assert_invalid(lambda d: d.pop('applications'), 'expected fields')
        assert_invalid(lambda d: d['reviews'][0].update(approved=1), 'boolean')
        assert_invalid(lambda d: d['sources'][0].update(seq=2), 'distinct')
        assert_invalid(lambda d: d['components'][0].update(keys=['ghost']),
                       'source facts')
        assert_invalid(lambda d: d['components'][2].update(requires=['checkout']),
                       'cycle')
        assert_invalid(lambda d: d['applications'][0].update(roots=['ghost']),
                       'unknown root')
        assert_invalid(lambda d: d.update(requested=['de']), 'requested')

    def test_unknown_query_and_invisible_source(self):
        with self.assertRaisesRegex(ValueError, 'unknown query'):
            sync_ledger(None, step([APP_A]), 'nope')

        def mutate(data):
            for row in data['sources']:
                row['effective'] = 99
            data['queries'].append({'id': 'qx', 'at': 5, 'known_seq': 6})
        with self.assertRaisesRegex(ValueError, 'no visible source'):
            sync_ledger(None, step([APP_A], mutate), 'qx')

    def test_invalid_ledger_rejected(self):
        good = sync_ledger(None, step([APP_A]), 'q1')
        good = assign_task(good, 'fr', 'greeting', 's1', 'alice')
        good = sync_ledger(good, step([APP_A]), 'q5')
        # good now holds the archived s1 task and the open s2 task.

        def assert_bad(mutate):
            ledger = copy.deepcopy(good)
            mutate(ledger)
            with self.assertRaises(ValueError):
                validate_ledger(ledger)
        assert_bad(lambda l: l.update(version=2))
        assert_bad(lambda l: l.pop('syncs'))
        assert_bad(lambda l: l.update(last_sync=None))
        assert_bad(lambda l: l.update(syncs=[]))
        assert_bad(lambda l: l['syncs'].append(
            {'query_id': 'q0', 'at': 0, 'known_seq': 1}))
        assert_bad(lambda l: l['tasks'][0].update(status='weird'))
        assert_bad(lambda l: l['tasks'][0].update(in_scope='yes'))
        assert_bad(lambda l: l['tasks'][1].update(
            approval={'translation_id': 't1', 'review_seq': 3}))
        assert_bad(lambda l: l['tasks'][1].update(applications=['app-b', 'app-a']))
        assert_bad(lambda l: l['tasks'][0]['events'].append(
            {'seq': 1, 'type': 'assigned', 'assignee': 'bob'}))
        assert_bad(lambda l: l['tasks'][0]['events'].append(
            {'seq': 99, 'type': 'teleported'}))
        assert_bad(lambda l: l['tasks'].append(copy.deepcopy(l['tasks'][0])))
        assert_bad(lambda l: l['tasks'].reverse())

    def test_done_task_invariants(self):
        ledger = sync_ledger(None, step([APP_A]), 'q1')
        ledger = sync_ledger(ledger, step([APP_A]), 'q2')
        ledger = confirm_task(ledger, 'fr', 'greeting', 's1', 'lead', 't1', 3)

        def assert_bad(mutate):
            broken = copy.deepcopy(ledger)
            mutate(broken)
            with self.assertRaises(ValueError):
                validate_ledger(broken)
        assert_bad(lambda l: l['tasks'][0].update(confirmation=None))
        assert_bad(lambda l: l['tasks'][0]['confirmation'].update(review_seq=4))
        assert_bad(lambda l: l['tasks'][0].update(approval=None))


class QueryTests(unittest.TestCase):
    def test_query_view_exposes_ledger(self):
        ledger = sync_ledger(None, step([APP_A]), 'q1')
        ledger = assign_task(ledger, 'fr', 'greeting', 's1', 'alice')
        view = query_ledger(ledger)
        self.assertEqual(view['last_sync'], {'query_id': 'q1', 'at': 0, 'known_seq': 2})
        t = view['tasks'][0]
        for name in ('locale', 'key', 'source_id', 'status', 'in_scope',
                     'applications', 'assignee', 'review_state', 'approval',
                     'confirmation', 'events'):
            self.assertIn(name, t)
        self.assertEqual((t['assignee'], t['review_state']), ('alice', 'pending'))
        with self.assertRaises(ValueError):
            query_ledger({'version': 1})


if __name__ == '__main__':
    unittest.main()
