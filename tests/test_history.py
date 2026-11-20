import json
from pathlib import Path
import unittest
from locale_bundle.history import compile_reviewed_history
from locale_bundle.messages import render

ROOT = Path(__file__).resolve().parents[1]


def sample():
    return json.loads((ROOT / 'fixtures/history_input.json').read_text())


def minimal():
    return {
        'default_locale': 'en',
        'locales': ['en', 'fr'],
        'fallback': {},
        'requested': ['fr'],
        'sources': [{'id': 's1', 'seq': 1, 'effective': 0, 'key': 'g',
                     'value': 'Hello', 'variables': {}}],
        'translations': [{'id': 't1', 'seq': 2, 'effective': 0, 'locale': 'fr', 'key': 'g',
                          'source_id': 's1', 'value': 'Bonjour', 'variables': {}}],
        'reviews': [{'seq': 3, 'effective': 0, 'translation_id': 't1', 'approved': True}],
        'queries': [{'id': 'q', 'at': 0, 'known_seq': 3}],
    }


def view(result, query_id):
    return next(v for v in result['views'] if v['query_id'] == query_id)


def state_row(v, locale, key):
    return next(r for r in v['translation_states'] if r['locale'] == locale and r['key'] == key)


def without_query_id(v):
    return {k: val for k, val in v.items() if k != 'query_id'}


def derived(v):
    return {k: v[k] for k in ('default_locale', 'catalogs', 'fallback_messages', 'translation_states')}


class AcceptanceTests(unittest.TestCase):
    """The linked approve/update/revoke scenario from the contract."""

    def check(self, v, message, fr_state, fallback_source):
        resolved = v['catalogs']['fr-CA']['g']
        self.assertEqual({k: resolved[k] for k in
                          ('value', 'source_locale', 'source_id', 'translation_id', 'review_seq')},
                         dict(zip(('value', 'source_locale', 'source_id', 'translation_id', 'review_seq'),
                                  message)))
        self.assertEqual({k: state_row(v, 'fr', 'g')[k] for k in
                          ('translation_id', 'review_seq', 'state')},
                         dict(zip(('translation_id', 'review_seq', 'state'), fr_state)))
        self.assertEqual(state_row(v, 'fr-CA', 'g'),
                         {'locale': 'fr-CA', 'key': 'g', 'translation_id': None,
                          'review_seq': None, 'state': 'missing'})
        self.assertEqual(v['fallback_messages'],
                         [{'locale': 'fr-CA', 'key': 'g', 'source_locale': fallback_source}])

    def test_six_linked_queries(self):
        result = compile_reviewed_history(sample())
        self.assertEqual([v['query_id'] for v in result['views']],
                         ['q1', 'q2', 'q3', 'q4', 'q5', 'q6'])
        for v in result['views']:
            self.assertEqual(set(v), {'query_id', 'at', 'known_seq', 'default_locale',
                                      'catalogs', 'fallback_messages', 'translation_states'})
            self.assertEqual(v['default_locale'], 'en')
            self.assertEqual(list(v['catalogs']), ['fr-CA'])
            pairs = [(r['locale'], r['key']) for r in v['translation_states']]
            self.assertEqual(pairs, sorted(pairs))
        self.check(view(result, 'q1'), ('Bonjour {name}', 'fr', 's1', 't1', 3),
                   ('t1', 3, 'approved'), 'fr')
        self.check(view(result, 'q2'), ('Hi {name}', 'en', 's2', None, None),
                   ('t1', 3, 'stale_source'), 'en')
        self.check(view(result, 'q3'), ('Hi {name}', 'en', 's2', None, None),
                   ('t2', None, 'pending'), 'en')
        self.check(view(result, 'q4'), ('Salut {name}', 'fr', 's2', 't2', 6),
                   ('t2', 6, 'approved'), 'fr')
        self.check(view(result, 'q5'), ('Hi {name}', 'en', 's2', None, None),
                   ('t2', 7, 'rejected'), 'en')
        self.check(view(result, 'q6'), ('Salut {name}', 'fr', 's2', 't2', 6),
                   ('t2', 6, 'approved'), 'fr')

    def test_resolved_message_renders(self):
        message = view(compile_reviewed_history(sample()), 'q1')['catalogs']['fr-CA']['g']
        self.assertEqual(render(message, {'name': 'Ada'}), 'Bonjour Ada')

    def test_late_revocation_with_earlier_effective_only_shifts_newer_cutoffs(self):
        data = sample()
        baseline = compile_reviewed_history(data)
        # Arrives late (seq 8) but is effective at the same instant as the approval.
        data['reviews'].append({'seq': 8, 'effective': 10, 'translation_id': 't2', 'approved': False})
        data['queries'].append({'id': 'q7', 'at': 10, 'known_seq': 8})
        extended = compile_reviewed_history(data)
        for before, after in zip(baseline['views'], extended['views']):
            self.assertEqual(before, after)
        q7 = view(extended, 'q7')
        resolved = q7['catalogs']['fr-CA']['g']
        self.assertEqual((resolved['value'], resolved['source_locale'], resolved['source_id']),
                         ('Hi {name}', 'en', 's2'))
        self.assertEqual({k: state_row(q7, 'fr', 'g')[k] for k in ('translation_id', 'review_seq', 'state')},
                         {'translation_id': 't2', 'review_seq': 8, 'state': 'rejected'})

    def test_fact_order_and_duplicate_queries_are_deterministic(self):
        baseline = compile_reviewed_history(sample())
        for permute in (reversed, lambda rows: rows[1:] + rows[:1]):
            shuffled = sample()
            for name in ('sources', 'translations', 'reviews'):
                shuffled[name] = list(permute(shuffled[name]))
            self.assertEqual(compile_reviewed_history(shuffled), baseline)
        data = sample()
        data['queries'].append({'id': 'q4-again', 'at': 10, 'known_seq': 6})
        result = compile_reviewed_history(data)
        self.assertEqual(without_query_id(view(result, 'q4-again')),
                         without_query_id(view(result, 'q4')))


class HistorySemanticsTests(unittest.TestCase):
    def test_same_effective_is_decided_by_seq(self):
        data = minimal()
        data['sources'] = [
            {'id': 's1', 'seq': 1, 'effective': 0, 'key': 'g', 'value': 'one', 'variables': {}},
            {'id': 's2', 'seq': 2, 'effective': 0, 'key': 'g', 'value': 'two', 'variables': {}}]
        data['translations'] = [
            {'id': 't1', 'seq': 3, 'effective': 0, 'locale': 'fr', 'key': 'g',
             'source_id': 's1', 'value': 'un', 'variables': {}},
            {'id': 't2', 'seq': 4, 'effective': 0, 'locale': 'fr', 'key': 'g',
             'source_id': 's2', 'value': 'deux', 'variables': {}}]
        data['reviews'] = [
            {'seq': 5, 'effective': 0, 'translation_id': 't1', 'approved': True},
            {'seq': 6, 'effective': 0, 'translation_id': 't2', 'approved': True}]
        data['queries'] = [{'id': 'q', 'at': 0, 'known_seq': 6}]
        v = view(compile_reviewed_history(data), 'q')
        self.assertEqual((v['catalogs']['fr']['g']['value'], v['catalogs']['fr']['g']['translation_id']),
                         ('deux', 't2'))
        self.assertEqual(state_row(v, 'fr', 'g')['state'], 'approved')

    def test_source_type_evolution_invalidates_old_translation_but_not_input(self):
        data = minimal()
        data['sources'] = [
            {'id': 's1', 'seq': 1, 'effective': 0, 'key': 'g',
             'value': 'Hello {name}', 'variables': {'name': 'string'}},
            {'id': 's2', 'seq': 4, 'effective': 10, 'key': 'g',
             'value': 'Count {count}', 'variables': {'count': 'integer'}}]
        data['translations'] = [
            {'id': 't1', 'seq': 2, 'effective': 0, 'locale': 'fr', 'key': 'g',
             'source_id': 's1', 'value': 'Bonjour {name}', 'variables': {'name': 'string'}}]
        data['reviews'] = [{'seq': 3, 'effective': 0, 'translation_id': 't1', 'approved': True}]
        data['queries'] = [{'id': 'before', 'at': 0, 'known_seq': 3},
                           {'id': 'after', 'at': 10, 'known_seq': 4}]
        result = compile_reviewed_history(data)
        before = view(result, 'before')['catalogs']['fr']['g']
        self.assertEqual((before['value'], before['source_locale']), ('Bonjour {name}', 'fr'))
        after_view = view(result, 'after')
        after = after_view['catalogs']['fr']['g']
        self.assertEqual((after['value'], after['source_locale'], after['source_id']),
                         ('Count {count}', 'en', 's2'))
        self.assertEqual(state_row(after_view, 'fr', 'g')['state'], 'stale_source')
        self.assertEqual(render(after, {'count': 3}), 'Count 3')

    def test_unapproved_new_translation_does_not_resurrect_old_approval(self):
        def base():
            data = minimal()
            data['locales'] = ['en', 'fr', 'fr-CA']
            data['fallback'] = {'fr-CA': 'fr'}
            data['requested'] = ['fr-CA']
            data['sources'] = [
                {'id': 's1', 'seq': 1, 'effective': 0, 'key': 'g', 'value': 'Hello', 'variables': {}},
                {'id': 's2', 'seq': 4, 'effective': 10, 'key': 'g', 'value': 'Hi', 'variables': {}}]
            data['translations'] = [
                {'id': 't1', 'seq': 2, 'effective': 0, 'locale': 'fr', 'key': 'g',
                 'source_id': 's1', 'value': 'Bonjour', 'variables': {}}]
            data['reviews'] = [{'seq': 3, 'effective': 0, 'translation_id': 't1', 'approved': True}]
            return data

        # Pending successor: fr-CA must fall to en, never back to approved t1.
        data = base()
        data['translations'].append(
            {'id': 't2', 'seq': 5, 'effective': 10, 'locale': 'fr', 'key': 'g',
             'source_id': 's2', 'value': 'Salut', 'variables': {}})
        data['queries'] = [{'id': 'q', 'at': 10, 'known_seq': 5}]
        v = view(compile_reviewed_history(data), 'q')
        self.assertEqual((v['catalogs']['fr-CA']['g']['value'], v['catalogs']['fr-CA']['g']['source_locale']),
                         ('Hi', 'en'))
        self.assertEqual(state_row(v, 'fr', 'g')['state'], 'pending')

        # Rejected successor.
        data = base()
        data['translations'].append(
            {'id': 't2', 'seq': 5, 'effective': 10, 'locale': 'fr', 'key': 'g',
             'source_id': 's2', 'value': 'Salut', 'variables': {}})
        data['reviews'].append({'seq': 6, 'effective': 10, 'translation_id': 't2', 'approved': False})
        data['queries'] = [{'id': 'q', 'at': 10, 'known_seq': 6}]
        v = view(compile_reviewed_history(data), 'q')
        self.assertEqual(v['catalogs']['fr-CA']['g']['source_locale'], 'en')
        self.assertEqual(state_row(v, 'fr', 'g')['state'], 'rejected')

        # Stale successor still bound to the old source revision.
        data = base()
        data['translations'].append(
            {'id': 't2', 'seq': 5, 'effective': 10, 'locale': 'fr', 'key': 'g',
             'source_id': 's1', 'value': 'Salut', 'variables': {}})
        data['queries'] = [{'id': 'q', 'at': 10, 'known_seq': 5}]
        v = view(compile_reviewed_history(data), 'q')
        self.assertEqual(v['catalogs']['fr-CA']['g']['source_locale'], 'en')
        self.assertEqual(state_row(v, 'fr', 'g')['state'], 'stale_source')

    def test_empty_values_are_valid_and_empty_approved_translation_blocks_fallback(self):
        data = minimal()
        data['locales'] = ['en', 'fr', 'fr-CA']
        data['fallback'] = {'fr-CA': 'fr'}
        data['requested'] = ['fr-CA']
        data['sources'] = [
            {'id': 's1', 'seq': 1, 'effective': 0, 'key': 'g', 'value': '', 'variables': {}},
            {'id': 's2', 'seq': 2, 'effective': 0, 'key': 'h', 'value': 'Hello', 'variables': {}}]
        data['translations'] = [
            {'id': 't1', 'seq': 3, 'effective': 0, 'locale': 'fr', 'key': 'g',
             'source_id': 's1', 'value': '', 'variables': {}},
            {'id': 't2', 'seq': 4, 'effective': 0, 'locale': 'fr', 'key': 'h',
             'source_id': 's2', 'value': '', 'variables': {}}]
        data['reviews'] = [
            {'seq': 5, 'effective': 0, 'translation_id': 't1', 'approved': True},
            {'seq': 6, 'effective': 0, 'translation_id': 't2', 'approved': True}]
        data['queries'] = [{'id': 'q', 'at': 0, 'known_seq': 6}]
        v = view(compile_reviewed_history(data), 'q')
        self.assertEqual((v['catalogs']['fr-CA']['g']['value'], v['catalogs']['fr-CA']['g']['source_locale']),
                         ('', 'fr'))
        # The approved empty fr translation stops the chain before the nonempty default.
        self.assertEqual((v['catalogs']['fr-CA']['h']['value'], v['catalogs']['fr-CA']['h']['source_locale']),
                         ('', 'fr'))

    def test_cutoffs_hide_future_facts(self):
        data = minimal()
        data['sources'].append(
            {'id': 's2', 'seq': 4, 'effective': 10, 'key': 'g', 'value': 'Hi', 'variables': {}})
        data['reviews'].append({'seq': 5, 'effective': 0, 'translation_id': 't1', 'approved': False})
        data['reviews'].append({'seq': 6, 'effective': 99, 'translation_id': 't1', 'approved': False})
        data['queries'] = [{'id': 'q1', 'at': 0, 'known_seq': 3},
                           {'id': 'q2', 'at': 0, 'known_seq': 4},
                           {'id': 'q3', 'at': 0, 'known_seq': 5},
                           {'id': 'q4', 'at': 0, 'known_seq': 6}]
        result = compile_reviewed_history(data)
        q1, q2, q3, q4 = (view(result, qid) for qid in ('q1', 'q2', 'q3', 'q4'))
        self.assertEqual(derived(q1), derived(q2))
        self.assertEqual(q1['catalogs']['fr']['g']['source_locale'], 'fr')
        self.assertEqual(derived(q3), derived(q4))
        self.assertEqual((q3['catalogs']['fr']['g']['source_locale'], state_row(q3, 'fr', 'g')['state']),
                         ('en', 'rejected'))

    def test_invisible_referenced_source_does_not_leak_into_view(self):
        data = minimal()
        data['sources'].append(
            {'id': 's2', 'seq': 4, 'effective': 20, 'key': 'g', 'value': 'Future', 'variables': {}})
        data['translations'] = [
            {'id': 't2', 'seq': 5, 'effective': 10, 'locale': 'fr', 'key': 'g',
             'source_id': 's2', 'value': 'Salut', 'variables': {}}]
        data['reviews'] = [{'seq': 6, 'effective': 10, 'translation_id': 't2', 'approved': True}]
        data['queries'] = [{'id': 'q', 'at': 15, 'known_seq': 6}]
        v = view(compile_reviewed_history(data), 'q')
        # s2 is not visible at 15, so the approved t2 is stale against s1 and cannot leak s2's text.
        self.assertEqual((v['catalogs']['fr']['g']['value'], v['catalogs']['fr']['g']['source_id']),
                         ('Hello', 's1'))
        self.assertEqual(state_row(v, 'fr', 'g')['state'], 'stale_source')

    def test_translation_states_cover_unrequested_locales(self):
        data = minimal()
        data['locales'] = ['en', 'fr', 'de']
        v = view(compile_reviewed_history(data), 'q')
        self.assertEqual([(r['locale'], r['key'], r['state']) for r in v['translation_states']],
                         [('de', 'g', 'missing'), ('fr', 'g', 'approved')])


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, mutate):
        data = minimal()
        mutate(data)
        with self.assertRaises(ValueError):
            compile_reviewed_history(data)

    def test_top_level_fields_are_exact(self):
        self.assert_invalid(lambda d: d.update(extra=1))
        self.assert_invalid(lambda d: d.pop('reviews'))

    def test_row_fields_are_exact(self):
        self.assert_invalid(lambda d: d['sources'][0].update(extra=1))
        self.assert_invalid(lambda d: d['sources'][0].pop('key'))
        self.assert_invalid(lambda d: d['translations'][0].pop('source_id'))
        self.assert_invalid(lambda d: d['reviews'][0].update(id='r1'))
        self.assert_invalid(lambda d: d['queries'][0].pop('at'))

    def test_scalar_typing_rules(self):
        self.assert_invalid(lambda d: d['reviews'][0].update(approved=1))
        self.assert_invalid(lambda d: d['reviews'][0].update(approved='true'))
        self.assert_invalid(lambda d: d['sources'][0].update(seq=True))
        self.assert_invalid(lambda d: d['sources'][0].update(seq=0))
        self.assert_invalid(lambda d: d['sources'][0].update(seq=10**9 + 1))
        self.assert_invalid(lambda d: d['sources'][0].update(effective=-1))
        self.assert_invalid(lambda d: d['queries'][0].update(at=-1))
        self.assert_invalid(lambda d: d['queries'][0].update(known_seq=0))
        self.assert_invalid(lambda d: d['queries'][0].update(known_seq=True))

    def test_locale_and_requested_rules(self):
        self.assert_invalid(lambda d: d.update(locales=['en', 'en']))
        self.assert_invalid(lambda d: d.update(locales=['en', 'fr'], default_locale='de'))
        self.assert_invalid(lambda d: d.update(locales=['en'] + ['x%d' % i for i in range(100)]))
        self.assert_invalid(lambda d: d.update(requested=[]))
        self.assert_invalid(lambda d: d.update(requested=['fr', 'fr']))
        self.assert_invalid(lambda d: d.update(requested=['de']))
        self.assert_invalid(lambda d: d.update(queries=[]))
        self.assert_invalid(lambda d: d.update(queries=[{'id': 'q%d' % i, 'at': 0, 'known_seq': 3}
                                                        for i in range(101)]))

    def test_duplicate_ids_and_seqs_rejected(self):
        def dup_source_id(d):
            d['sources'].append({'id': 's1', 'seq': 9, 'effective': 0, 'key': 'g',
                                 'value': 'x', 'variables': {}})
        def cross_kind_id(d):
            d['translations'][0]['id'] = 's1'
        def dup_query_id(d):
            d['queries'].append({'id': 'q', 'at': 0, 'known_seq': 3})
        def dup_seq(d):
            d['reviews'][0]['seq'] = 1
        def dup_seq_same_kind(d):
            d['sources'].append({'id': 's2', 'seq': 1, 'effective': 0, 'key': 'g',
                                 'value': 'x', 'variables': {}})
        for mutate in (dup_source_id, cross_kind_id, dup_query_id, dup_seq, dup_seq_same_kind):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(mutate)

    def test_bad_references_rejected(self):
        def unknown_source(d):
            d['translations'][0]['source_id'] = 'nope'
        def later_source(d):
            d['sources'].append({'id': 's2', 'seq': 5, 'effective': 0, 'key': 'g',
                                 'value': 'x', 'variables': {}})
            d['translations'][0]['source_id'] = 's2'
        def unknown_translation(d):
            d['reviews'][0]['translation_id'] = 'nope'
        def later_translation(d):
            d['translations'].append({'id': 't2', 'seq': 4, 'effective': 0, 'locale': 'fr',
                                      'key': 'g', 'source_id': 's1', 'value': 'x', 'variables': {}})
            d['reviews'][0]['translation_id'] = 't2'
        def key_mismatch(d):
            d['translations'][0]['key'] = 'other'
        def variables_mismatch(d):
            d['translations'][0].update(value='Bonjour {name}', variables={'name': 'string'})
        def default_locale_translation(d):
            d['translations'][0]['locale'] = 'en'
        def undeclared_locale_translation(d):
            d['translations'][0]['locale'] = 'de'
        for mutate in (unknown_source, later_source, unknown_translation, later_translation,
                       key_mismatch, variables_mismatch, default_locale_translation,
                       undeclared_locale_translation):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(mutate)

    def test_fallback_cycle_rejected_even_when_unrequested(self):
        def cycle(d):
            d.update(locales=['en', 'fr', 'a', 'b'], requested=['fr'],
                     fallback={'a': 'b', 'b': 'a'})
        def self_loop(d):
            d.update(locales=['en', 'fr', 'a'], requested=['fr'], fallback={'a': 'a'})
        def default_parent(d):
            d.update(fallback={'en': 'fr'})
        def unknown_edge(d):
            d.update(fallback={'fr': 'de'})
        for mutate in (cycle, self_loop, default_parent, unknown_edge):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(mutate)

    def test_unsafe_messages_rejected(self):
        for value in ('{name.upper}', '{name[0]}', '{}', '{name!r}', '{name:03}', '{'):
            with self.subTest(value=value):
                self.assert_invalid(lambda d: d['sources'][0].update(value=value))
            with self.subTest(value=value, kind='translation'):
                self.assert_invalid(lambda d: d['translations'][0].update(value=value))
        self.assert_invalid(lambda d: d['sources'][0].update(value='Hi {name}'))
        self.assert_invalid(lambda d: d['sources'][0].update(variables={'name': 'float'}))

    def test_query_without_visible_source_fails(self):
        def future_only(d):
            d['sources'][0]['effective'] = 5
            d['translations'][0]['effective'] = 5
            d['reviews'][0]['effective'] = 5
        self.assert_invalid(future_only)

    def test_later_query_failure_fails_whole_batch(self):
        data = minimal()
        for name in ('sources', 'translations', 'reviews'):
            for row in data[name]:
                row['effective'] = 5
        data['queries'] = [{'id': 'ok', 'at': 10, 'known_seq': 3},
                           {'id': 'too-early', 'at': 0, 'known_seq': 3}]
        with self.assertRaisesRegex(ValueError, 'no visible source'):
            compile_reviewed_history(data)


if __name__ == '__main__':
    unittest.main()
