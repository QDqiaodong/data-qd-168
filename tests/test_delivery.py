import copy
import json
from pathlib import Path
import unittest

from locale_bundle.delivery import (DeliveryError, build_snapshot, build_delta,
                                    validate_snapshot, validate_delta,
                                    validate_store, init_store, install_delta,
                                    receive_notice, rollback_store,
                                    store_status, fetch_package,
                                    snapshot_identity, _hash)

ROOT = Path(__file__).resolve().parents[1]


def sample():
    return json.loads((ROOT / 'fixtures/delivery_input.json').read_text())


def v4_input():
    data = sample()
    for component in data['components']:
        if component['id'] == 'office':
            component['keys'] = ['cart', 'promo']
    return data


def releases():
    data = sample()
    return {'v1': build_snapshot(data, 'q1', 1),
            'v2': build_snapshot(data, 'q2', 2),
            'v3alt': build_snapshot(data, 'q3-alt', 3),
            'v3': build_snapshot(data, 'q3', 3),
            'v4': build_snapshot(v4_input(), 'q4', 4)}


def package(snapshot, application_id, locale='fr-CA'):
    return next(p for p in snapshot['packages']
                if p['application_id'] == application_id and p['locale'] == locale)


def evidence(message):
    return (message['translation_id'], message['review_seq'])


NOTICE = {'version': 1, 'notice_id': 'n1',
          'withdrawn': [{'translation_id': 't1', 'review_seq': 4}]}


class BuildTests(unittest.TestCase):
    def test_cutoff_visibility_and_evidence(self):
        rel = releases()
        greeting = package(rel['v1'], 'app-a')['messages'][0]
        self.assertEqual((greeting['source_locale'], greeting['source_id'],
                          evidence(greeting)), ('fr', 's1', ('t1', 4)))
        # cart's source is not yet visible at q1: the key is not packaged.
        self.assertEqual([m['key'] for m in package(rel['v1'], 'app-c')['messages']],
                         ['settings'])
        # q2 sees cart; its approved fr translation is an empty string and is
        # still a translation: fr-CA is served from fr, not from the default.
        cart = next(m for m in package(rel['v2'], 'app-c')['messages']
                    if m['key'] == 'cart')
        self.assertEqual((cart['value'], cart['source_locale'], evidence(cart)),
                         ('', 'fr', ('t2', 7)))
        # Default supply carries no evidence.
        settings = next(m for m in package(rel['v2'], 'app-c')['messages']
                        if m['key'] == 'settings')
        self.assertEqual((settings['source_locale'], evidence(settings)),
                         ('en', (None, None)))
        # q3 sees the re-approval: same text, new review seq.
        g3 = package(rel['v3'], 'app-a')['messages'][0]
        self.assertEqual((g3['value'], evidence(g3)), ('Bonjour {name}', ('t1', 10)))

    def test_identity_ignores_equivalent_reordering(self):
        data = sample()
        reordered = copy.deepcopy(data)
        for name in ('sources', 'translations', 'reviews', 'queries',
                     'components', 'applications'):
            reordered[name] = list(reversed(reordered[name]))
        reordered['components'][2]['keys'] = ['greeting']
        reordered['requested'] = ['fr-CA']
        a = build_snapshot(data, 'q3', 3)
        b = build_snapshot(reordered, 'q3', 3)
        self.assertEqual(a, b)
        self.assertEqual(a['snapshot_id'], b['snapshot_id'])
        # A different release number is a different release identity.
        other = build_snapshot(data, 'q3', 4)
        self.assertNotEqual(a['snapshot_id'], other['snapshot_id'])

    def test_build_rejections(self):
        data = sample()
        for bad in (0, -1, '3', True):
            with self.assertRaises(ValueError):
                build_snapshot(data, 'q1', bad)
        with self.assertRaises(DeliveryError):
            build_snapshot(data, 'nope', 1)
        # A query with no visible source revision cannot build a release.
        with self.assertRaises(DeliveryError):
            build_snapshot({'default_locale': 'en', 'locales': ['en'],
                            'fallback': {}, 'requested': ['en'],
                            'sources': [], 'translations': [], 'reviews': [],
                            'queries': [{'id': 'q0', 'at': 0, 'known_seq': 1}],
                            'components': [], 'applications': []}, 'q0', 1)
        # Component keys must be source keys declared in the log.
        bad_graph = sample()
        bad_graph['components'][2]['keys'] = ['greeting', 'ghost']
        with self.assertRaises(ValueError):
            build_snapshot(bad_graph, 'q1', 1)

    def test_snapshot_self_validation(self):
        rel = releases()
        tampered = copy.deepcopy(rel['v1'])
        tampered['packages'][0]['messages'][0]['value'] = 'Salut {name}'
        with self.assertRaises(DeliveryError):
            validate_snapshot(tampered)
        tampered_id = copy.deepcopy(rel['v1'])
        tampered_id['snapshot_id'] = '0' * 64
        with self.assertRaises(DeliveryError):
            validate_snapshot(tampered_id)
        # Non-default supply without evidence is invalid.
        bad = copy.deepcopy(rel['v1'])
        bad['packages'][0]['messages'][0]['review_seq'] = None
        with self.assertRaises(DeliveryError):
            validate_snapshot(bad)


class DeltaTests(unittest.TestCase):
    def test_delta_covers_whole_release_without_unchanged_content(self):
        rel = releases()
        delta = build_delta(rel['v3'], rel['v4'])
        self.assertEqual(delta['target_release'], 4)
        self.assertEqual(delta['baseline_id'], rel['v3']['snapshot_id'])
        self.assertEqual(delta['target_id'], rel['v4']['snapshot_id'])
        self.assertEqual(delta['packages_removed'], [])
        self.assertEqual(delta['packages_added'], [])
        self.assertEqual(len(delta['packages_changed']), 1)
        change = delta['packages_changed'][0]
        self.assertEqual((change['application_id'], change['locale']),
                         ('app-c', 'fr-CA'))
        self.assertEqual(change['keys_removed'], ['settings'])
        self.assertEqual([m['key'] for m in change['keys_upserted']], ['promo'])
        # The unchanged cart entry (empty text, same evidence) is not resent.
        blob = json.dumps(delta)
        self.assertNotIn('cart', blob)
        self.assertNotIn('t2', blob)

    def test_evidence_only_change_is_transmitted(self):
        rel = releases()
        delta = build_delta(rel['v2'], rel['v3'])
        changed = {c['application_id']: c for c in delta['packages_changed']}
        upsert = changed['app-a']['keys_upserted'][0]
        self.assertEqual(upsert['key'], 'greeting')
        self.assertEqual(upsert['value'], 'Bonjour {name}')
        self.assertEqual(evidence(upsert), ('t1', 10))

    def test_package_add_and_remove(self):
        rel = releases()
        v2 = copy.deepcopy(rel['v2'])
        # Forge a snapshot without app-b and with an extra app-d package by
        # rebuilding through the public identity rules.
        v2['packages'] = [p for p in v2['packages']
                          if p['application_id'] != 'app-b']
        extra = copy.deepcopy(v2['packages'][0])
        extra['application_id'] = 'app-d'
        v2['packages'].append(extra)
        v2['packages'].sort(key=lambda p: (p['application_id'], p['locale']))
        v2['snapshot_id'] = _hash(snapshot_identity(
            v2['release'], v2['default_locale'], v2['packages']))
        delta = build_delta(rel['v2'], v2)
        self.assertEqual(delta['packages_removed'],
                         [{'application_id': 'app-b', 'locale': 'fr-CA'}])
        self.assertEqual([p['application_id'] for p in delta['packages_added']],
                         ['app-d'])

    def test_delta_integrity(self):
        rel = releases()
        delta = build_delta(rel['v1'], rel['v2'])
        tampered = copy.deepcopy(delta)
        tampered['packages_changed'][0]['keys_upserted'][0]['value'] = 'x'
        with self.assertRaises(DeliveryError):
            validate_delta(tampered)
        with self.assertRaises(DeliveryError):
            validate_delta({**delta, 'version': 2})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.rel = releases()
        self.d12 = build_delta(self.rel['v1'], self.rel['v2'])
        self.d23 = build_delta(self.rel['v2'], self.rel['v3'])
        self.d23alt = build_delta(self.rel['v2'], self.rel['v3alt'])
        self.d34 = build_delta(self.rel['v3'], self.rel['v4'])

    def test_corrupt_store_rejected(self):
        store = init_store(self.rel['v1'])
        raw = copy.deepcopy(store)
        raw['highest_release'] = 0
        with self.assertRaises(ValueError):
            validate_store(raw)
        raw = copy.deepcopy(store)
        raw['current']['release'] = 9
        with self.assertRaises(ValueError):
            validate_store(raw)

    def test_init_anchors_highest_release(self):
        store = init_store(self.rel['v2'])
        self.assertEqual(store['highest_release'], 2)
        self.assertIsNone(store['previous'])
        # Even a well-formed delta to an older release cannot lower it.
        with self.assertRaises(DeliveryError):
            install_delta(store, self.d12)

    def test_notice_semantics(self):
        store = init_store(self.rel['v1'])
        store, changed = receive_notice(store, NOTICE)
        self.assertTrue(changed)
        # Same id, same set: no change.
        again, changed = receive_notice(store, NOTICE)
        self.assertFalse(changed)
        self.assertEqual(again, store)
        # Same id, different set: rejected as a whole.
        with self.assertRaises(DeliveryError):
            receive_notice(store, {'version': 1, 'notice_id': 'n1',
                                   'withdrawn': []})
        # Evidence never installed is accepted and blocks nothing new.
        store, changed = receive_notice(
            store, {'version': 1, 'notice_id': 'n2',
                    'withdrawn': [{'translation_id': 't9', 'review_seq': 99}]})
        self.assertTrue(changed)
        status = store_status(store)
        by_app = {p['application_id']: p for p in status['packages']}
        self.assertEqual(by_app['app-a']['blocked_by'],
                         [{'translation_id': 't1', 'review_seq': 4}])
        self.assertTrue(by_app['app-c']['readable'])
        # The old notice never blocks the re-approval (t1, 10): jumping
        # straight from v1 to the re-approved v3 is servable.
        d13 = build_delta(self.rel['v1'], self.rel['v3'])
        store = install_delta(store, d13)
        self.assertTrue(all(p['readable'] for p in store_status(store)['packages']))

    def test_notice_arriving_before_release_blocks_install(self):
        store = init_store(self.rel['v1'])
        store, _ = receive_notice(store, NOTICE)
        # v2 still serves (t1, 4): the install is refused as a whole.
        with self.assertRaises(DeliveryError):
            install_delta(store, self.d12)

    def test_install_requires_current_baseline_and_newer_release(self):
        store = init_store(self.rel['v1'])
        store = install_delta(store, self.d12)
        # The old delta cannot overwrite the newer install.
        with self.assertRaises(DeliveryError):
            install_delta(store, self.d12)
        # A delta based on v1 while current is v2 is not applicable.
        d13 = build_delta(self.rel['v1'], self.rel['v3'])
        with self.assertRaises(DeliveryError):
            install_delta(store, d13)

    def test_restored_identity_is_checked(self):
        store = init_store(self.rel['v1'])
        store = install_delta(store, self.d12)
        crafted = copy.deepcopy(self.d23)
        crafted['packages_changed'][0]['keys_upserted'][0]['value'] = 'Forged {name}'
        # Re-seal the crafted delta so integrity passes but identity fails.
        content = {k: crafted[k] for k in
                   ('target_release', 'baseline_id', 'target_id', 'target_header',
                    'packages_removed', 'packages_added', 'packages_changed')}
        crafted['delta_id'] = _hash(content)
        with self.assertRaises(DeliveryError) as ctx:
            install_delta(store, crafted)
        self.assertIn('target identity', str(ctx.exception))

    def test_rollback_never_loses_highest_or_notices(self):
        store = init_store(self.rel['v1'])
        store = install_delta(store, self.d12)
        store = install_delta(store, self.d23)
        store = install_delta(store, self.d34)
        store, _ = receive_notice(
            store, {'version': 1, 'notice_id': 'n9',
                    'withdrawn': [{'translation_id': 't9', 'review_seq': 1}]})
        store = rollback_store(store)
        self.assertEqual(store['current']['release'], 3)
        self.assertIsNone(store['previous'])
        self.assertEqual(store['highest_release'], 4)
        self.assertEqual([n['notice_id'] for n in store['notices']], ['n9'])
        with self.assertRaises(DeliveryError):
            rollback_store(store)

    def test_status_and_fetch(self):
        store = init_store(self.rel['v1'])
        store = install_delta(store, self.d12)
        store, _ = receive_notice(store, NOTICE)
        status = store_status(store)
        self.assertEqual(status['current']['release'], 2)
        self.assertEqual(status['previous']['release'], 1)
        self.assertEqual(status['highest_release'], 2)
        self.assertFalse(status['rollback_allowed'])
        by_app = {p['application_id']: p for p in status['packages']}
        self.assertFalse(by_app['app-a']['readable'])
        self.assertEqual(by_app['app-a']['blocked_by'],
                         [{'translation_id': 't1', 'review_seq': 4}])
        self.assertTrue(by_app['app-c']['readable'])
        with self.assertRaises(DeliveryError):
            fetch_package(store, 'app-a', 'fr-CA')
        with self.assertRaises(DeliveryError):
            fetch_package(store, 'app-z', 'fr-CA')
        package_c = fetch_package(store, 'app-c', 'fr-CA')
        self.assertEqual([m['key'] for m in package_c['messages']],
                         ['cart', 'settings'])


if __name__ == '__main__':
    unittest.main()
