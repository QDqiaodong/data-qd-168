import copy
import json
from pathlib import Path
import unittest

from locale_bundle.compiler import compile_bundle
from locale_bundle.messages import render
from locale_bundle.release import compile_release_bundles

ROOT = Path(__file__).resolve().parents[1]


def sample():
    return json.loads((ROOT / 'fixtures/release_input.json').read_text())


def minimal():
    return {
        'default_locale': 'en',
        'fallback': {'fr-CA': 'fr'},
        'catalogs': {
            'en': {'a': {'value': 'A', 'variables': {}},
                   'b': {'value': 'B', 'variables': {}},
                   's': {'value': 'S', 'variables': {}}},
            'fr': {'a': {'value': 'A-fr', 'variables': {}},
                   's': {'value': 'S-fr', 'variables': {}}},
            'fr-CA': {'b': {'value': 'B-ca', 'variables': {}}},
        },
        'requested': ['fr-CA'],
        'components': [{'id': 'shared', 'requires': [], 'keys': ['s']},
                       {'id': 'left', 'requires': ['shared'], 'keys': ['a']},
                       {'id': 'right', 'requires': ['shared'], 'keys': ['b']},
                       {'id': 'solo', 'requires': [], 'keys': ['b']}],
        'applications': [{'id': 'app-a', 'roots': ['left', 'right']},
                         {'id': 'app-b', 'roots': ['solo']}],
    }


def package(result, app_id):
    return next(p for p in result['applications'] if p['application_id'] == app_id)


class AcceptanceTests(unittest.TestCase):
    def test_fixture_diamond_and_isolation(self):
        result = compile_release_bundles(sample())
        a = package(result, 'app-a')['catalogs']['fr-CA']
        self.assertEqual(a['key-x']['source_locale'], 'fr')
        self.assertEqual(a['key-x']['included_by'], ['shared'])
        self.assertEqual(a['button']['included_by'], ['catalog', 'checkout'])
        self.assertNotIn('key-x', package(result, 'app-b')['catalogs']['fr-CA'])
        self.assertEqual([row for row in package(result, 'app-a')['fallback_messages']
                          if row['key'] == 'key-x'],
                         [{'locale': 'fr-CA', 'key': 'key-x', 'source_locale': 'fr',
                           'included_by': ['shared']}])

    def test_diamond_shared_key_resolved_once_from_fr(self):
        result = compile_release_bundles(minimal())
        a = package(result, 'app-a')
        self.assertEqual(sorted(a['catalogs']['fr-CA']), ['a', 'b', 's'])
        key_x = a['catalogs']['fr-CA']['s']
        self.assertEqual(key_x['value'], 'S-fr')
        self.assertEqual(key_x['source_locale'], 'fr')
        self.assertEqual(key_x['included_by'], ['shared'])
        self.assertEqual([row for row in a['fallback_messages'] if row['key'] == 's'],
                         [{'locale': 'fr-CA', 'key': 's', 'source_locale': 'fr',
                           'included_by': ['shared']}])
        # app-b never reaches shared, so key-x/s stays out of its package.
        self.assertNotIn('s', package(result, 'app-b')['catalogs']['fr-CA'])

    def test_keys_do_not_leak_between_applications(self):
        data = minimal()
        data['applications'] = [{'id': 'app-a', 'roots': ['left', 'right']},
                                {'id': 'app-b', 'roots': ['solo']}]
        result = compile_release_bundles(data)
        b = package(result, 'app-b')['catalogs']['fr-CA']
        self.assertEqual(sorted(b), ['b'])
        self.assertNotIn('s', b)
        self.assertNotIn('a', b)
        a = package(result, 'app-a')['catalogs']['fr-CA']
        self.assertEqual(sorted(a), ['a', 'b', 's'])

    def test_included_by_dedupes_multi_path_reachability(self):
        data = minimal()
        # both roots depend on shared directly; still one component id.
        data['components'] = [
            {'id': 'shared', 'requires': [], 'keys': ['s']},
            {'id': 'left', 'requires': ['shared'], 'keys': ['a']},
            {'id': 'right', 'requires': ['shared'], 'keys': ['b']},
            {'id': 'leaf', 'requires': ['left', 'right', 'shared'], 'keys': []},
        ]
        data['applications'] = [{'id': 'app-a', 'roots': ['leaf']}]
        result = compile_release_bundles(data)
        self.assertEqual(package(result, 'app-a')['catalogs']['fr-CA']['s']['included_by'],
                         ['shared'])

    def test_multiple_declaring_components_sorted(self):
        data = minimal()
        data['components'].append({'id': 'ada', 'requires': [], 'keys': ['a']})
        data['applications'] = [{'id': 'app-a', 'roots': ['left', 'right', 'ada']}]
        result = compile_release_bundles(data)
        self.assertEqual(package(result, 'app-a')['catalogs']['fr-CA']['a']['included_by'],
                         ['ada', 'left'])

    def test_new_dependency_only_affects_that_application(self):
        data = minimal()
        baseline = compile_release_bundles(minimal())
        data['components'].append({'id': 'extra', 'requires': [], 'keys': ['b']})
        data['applications'][0]['roots'].append('extra')
        result = compile_release_bundles(data)
        a = package(result, 'app-a')
        self.assertEqual(a['catalogs']['fr-CA']['b']['included_by'], ['extra', 'right'])
        # app-b unchanged; v1 compiler unchanged because catalogs did not move.
        self.assertEqual(package(result, 'app-b'),
                         package(baseline, 'app-b'))

    def test_empty_translation_is_present_and_blocks_fallback(self):
        data = minimal()
        data['catalogs']['fr']['s'] = {'value': '', 'variables': {}}
        result = compile_release_bundles(data)
        message = package(result, 'app-a')['catalogs']['fr-CA']['s']
        self.assertEqual(message['value'], '')
        self.assertEqual(message['source_locale'], 'fr')
        self.assertEqual([row for row in package(result, 'app-a')['fallback_messages']
                          if row['key'] == 's'],
                         [{'locale': 'fr-CA', 'key': 's', 'source_locale': 'fr',
                           'included_by': ['shared']}])

    def test_application_without_roots_is_empty(self):
        data = minimal()
        data['applications'] = [{'id': 'empty', 'roots': []}]
        result = compile_release_bundles(data)
        self.assertEqual(result['applications'],
                         [{'application_id': 'empty',
                           'catalogs': {'fr-CA': {}}, 'fallback_messages': []}])

    def test_output_ordering(self):
        result = compile_release_bundles(minimal())
        self.assertEqual([p['application_id'] for p in result['applications']],
                         ['app-a', 'app-b'])
        for p in result['applications']:
            for messages in p['catalogs'].values():
                keys = list(messages)
                self.assertEqual(keys, sorted(keys))
            self.assertEqual(p['fallback_messages'],
                             sorted(p['fallback_messages'],
                                    key=lambda row: (row['locale'], row['key'])))

    def test_only_requested_locales_present(self):
        data = minimal()
        data['requested'] = ['en', 'fr-CA']
        result = compile_release_bundles(data)
        for p in result['applications']:
            self.assertEqual(set(p['catalogs']), {'en', 'fr-CA'})
        self.assertEqual(package(result, 'app-a')['catalogs']['en']['a']['source_locale'], 'en')


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, mutate, pattern):
        data = minimal()
        mutate(data)
        with self.assertRaisesRegex(ValueError, pattern):
            compile_release_bundles(data)

    def test_unknown_dependency(self):
        self.assert_invalid(lambda d: d['components'][1]['requires'].append('ghost'),
                            'unknown component reference')

    def test_unknown_root(self):
        self.assert_invalid(lambda d: d['applications'][0]['roots'].append('ghost'),
                            'unknown root component')

    def test_cycle_rejected_even_unused(self):
        def mutate(d):
            d['components'][1]['requires'] = ['right']
            d['components'][2]['requires'] = ['left']
            d['applications'] = [{'id': 'only', 'roots': []}]
        self.assert_invalid(mutate, 'cycle')

    def test_self_cycle(self):
        self.assert_invalid(lambda d: d['components'][0]['requires'].append('shared'), 'cycle')

    def test_duplicate_component_id(self):
        self.assert_invalid(lambda d: d['components'].append(copy.deepcopy(d['components'][0])),
                            'duplicate component id')

    def test_duplicate_application_id(self):
        self.assert_invalid(lambda d: d['applications'].append(copy.deepcopy(d['applications'][1])),
                            'duplicate application id')

    def test_duplicate_dependency(self):
        self.assert_invalid(lambda d: d['components'][1]['requires'].append('shared'),
                            'duplicate dependency')

    def test_duplicate_component_key(self):
        self.assert_invalid(lambda d: d['components'][1]['keys'].append('a'), 'duplicate key')

    def test_duplicate_root(self):
        self.assert_invalid(lambda d: d['applications'][1]['roots'].append('solo'),
                            'duplicate root component')

    def test_missing_default_key(self):
        self.assert_invalid(lambda d: d['components'][2]['keys'].append('ghost'), 'default catalog')

    def test_translation_key_must_exist_in_default(self):
        self.assert_invalid(lambda d: d['catalogs']['fr'].update(
            {'ghost': {'value': 'x', 'variables': {}}}), 'unknown key')

    def test_invalid_message(self):
        self.assert_invalid(lambda d: d['catalogs']['en']['a'].update(
            {'value': 'A {x}'}), 'placeholder')

    def test_translation_variable_types_must_match(self):
        self.assert_invalid(lambda d: d['catalogs']['en']['a'].update(
            {'value': 'A {n}', 'variables': {'n': 'string'}}), 'types')

    def test_invalid_fallback_edge(self):
        self.assert_invalid(lambda d: d['fallback'].update({'de': 'en'}), 'fallback edge')

    def test_fallback_cycle(self):
        self.assert_invalid(lambda d: d['fallback'].update({'fr': 'fr-CA'}), 'fallback cycle')

    def test_unknown_requested_locale(self):
        self.assert_invalid(lambda d: d.update({'requested': ['de']}), 'requested')

    def test_exact_component_fields(self):
        self.assert_invalid(lambda d: d['components'][0].update({'extra': 1}), 'expected fields')

    def test_exact_application_fields(self):
        self.assert_invalid(lambda d: d['applications'][0].update({'extra': 1}), 'expected fields')

    def test_requires_must_be_array_of_identifiers(self):
        self.assert_invalid(lambda d: d['components'][1].update({'requires': 'shared'}),
                            'bounded array')

    def test_message_fields_exposed_and_render(self):
        data = minimal()
        data['catalogs']['en']['a'] = {'value': 'Hi {name}', 'variables': {'name': 'string'}}
        data['catalogs']['fr']['a'] = {'value': 'Salut {name}', 'variables': {'name': 'string'}}
        result = compile_release_bundles(data)
        message = package(result, 'app-a')['catalogs']['fr-CA']['a']
        self.assertEqual(render(message, {'name': 'Ada'}), 'Salut Ada')
        self.assertEqual(message['source_locale'], 'fr')
        self.assertEqual(message['included_by'], ['left'])


class RegressionTests(unittest.TestCase):
    def test_v1_compiler_unchanged(self):
        source = json.loads((ROOT / 'fixtures/input.json').read_text())
        result = compile_bundle(source)
        self.assertEqual(result['catalogs']['fr-CA']['greeting']['source_locale'], 'fr')
        self.assertEqual(len(result['fallback_messages']), 1)
