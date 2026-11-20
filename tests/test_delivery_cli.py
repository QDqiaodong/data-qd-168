import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'locale_bundle.delivery_cli'
FIXTURE = ROOT / 'fixtures/delivery_input.json'

NOTICE = {'version': 1, 'notice_id': 'n1',
          'withdrawn': [{'translation_id': 't1', 'review_seq': 4}]}


class DeliveryCliAcceptanceTests(unittest.TestCase):
    """The synthetic acceptance scenario, end to end through the CLI.

    One approved fr translation (t1, review_seq 4) falls back into the two
    fr-CA packages app-a/app-b; app-c is independent. The store installs
    release 2, then the withdrawal notice for (t1, 4) arrives.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.dir / 'store.json'
        base = json.loads(FIXTURE.read_text())
        self._write('input.json', base)
        v4 = json.loads(json.dumps(base))
        for component in v4['components']:
            if component['id'] == 'office':
                component['keys'] = ['cart', 'promo']
        self._write('input_v4.json', v4)
        self._write('notice.json', NOTICE)
        # Headquarters builds the release line.
        self._build('input.json', 'q1', 1, 'snap1.json')
        self._build('input.json', 'q2', 2, 'snap2.json')
        self._build('input.json', 'q3-alt', 3, 'snap3alt.json')
        self._build('input.json', 'q3', 3, 'snap3.json')
        self._build('input_v4.json', 'q4', 4, 'snap4.json')
        self._delta('snap1.json', 'snap2.json', 'delta12.json')
        self._delta('snap2.json', 'snap3alt.json', 'delta23alt.json')
        self._delta('snap2.json', 'snap3.json', 'delta23.json')
        self._delta('snap3.json', 'snap4.json', 'delta34.json')

    def _write(self, name, value):
        path = self.dir / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        return path

    def _read(self, name):
        return json.loads((self.dir / name).read_text())

    def invoke(self, *args, expect=0):
        result = subprocess.run([sys.executable, '-m', MODULE, *args],
                                capture_output=True, text=True, timeout=10,
                                cwd=ROOT, env={'PYTHONPATH': str(ROOT / 'src')})
        self.assertEqual(result.returncode, expect,
                         '%s -> %s\n%s' % (' '.join(args), result.returncode,
                                           result.stderr))
        return result

    def _build(self, source, query, release, output):
        self.invoke('build', '--source', str(self.dir / source),
                    '--output', str(self.dir / output),
                    '--query', query, '--release', str(release))

    def _delta(self, baseline, target, output):
        self.invoke('delta', '--baseline', str(self.dir / baseline),
                    '--target', str(self.dir / target),
                    '--output', str(self.dir / output))

    def _install(self, delta, expect=0):
        return self.invoke('install', '--store', str(self.store),
                           '--delta', str(self.dir / delta), expect=expect)

    def _status(self):
        return json.loads(self.invoke('status', '--store', str(self.store)).stdout)

    def _fetch(self, application_id, expect=0):
        result = self.invoke('fetch', '--store', str(self.store),
                             '--application-id', application_id,
                             '--locale', 'fr-CA', expect=expect)
        return json.loads(result.stdout) if expect == 0 else result

    def _notice(self, notice_file='notice.json', expect=0):
        return self.invoke('notice', '--store', str(self.store),
                           '--notice', str(self.dir / notice_file), expect=expect)

    def _rollback(self, expect=0):
        return self.invoke('rollback', '--store', str(self.store), expect=expect)

    def test_full_acceptance_scenario(self):
        # Initialize from the complete v1 snapshot, then install v2.
        self.invoke('init', '--store', str(self.store),
                    '--snapshot', str(self.dir / 'snap1.json'))
        status = self._status()
        self.assertEqual(status['highest_release'], 1)
        self.assertIsNone(status['previous'])
        self._install('delta12.json')
        status = self._status()
        self.assertEqual((status['current']['release'],
                          status['previous']['release'],
                          status['highest_release']), (2, 1, 2))
        self.assertTrue(all(p['readable'] for p in status['packages']))
        # The empty fr translation of cart is served from fr, not dropped.
        cart = next(m for m in self._fetch('app-c')['messages']
                    if m['key'] == 'cart')
        self.assertEqual((cart['value'], cart['source_locale'],
                          cart['translation_id'], cart['review_seq']),
                         ('', 'fr', 't2', 7))

        # The withdrawal notice arrives after release 2 was installed.
        self._notice()
        status = self._status()
        by_app = {p['application_id']: p for p in status['packages']}
        self.assertFalse(by_app['app-a']['readable'])
        self.assertFalse(by_app['app-b']['readable'])
        self.assertEqual(by_app['app-a']['blocked_by'],
                         [{'translation_id': 't1', 'review_seq': 4}])
        self.assertTrue(by_app['app-c']['readable'])
        blocked = self._fetch('app-a', expect=2)
        self.assertIn('t1#4', blocked.stderr)
        self.assertEqual([m['key'] for m in self._fetch('app-c')['messages']],
                         ['cart', 'settings'])

        # The previous release still carries the same approval: no rollback.
        refused = self._rollback(expect=2)
        self.assertIn('withdrawn', refused.stderr)
        self.assertTrue(self._status()['packages'][2]['readable'])

        # A delta touching only unrelated keys still restores a target that
        # inherits the withdrawn evidence: refused, nothing changes.
        before = self.store.read_bytes()
        refused = self._install('delta23alt.json', expect=2)
        self.assertIn('withdrawn', refused.stderr)
        self.assertEqual(self.store.read_bytes(), before)

        # The delta that moves both packages to the re-approved evidence
        # installs, even though the greeting text is literally unchanged.
        delta23 = self._read('delta23.json')
        upserts = {c['application_id']: c['keys_upserted']
                   for c in delta23['packages_changed']}
        self.assertEqual(upserts['app-a'][0]['value'], 'Bonjour {name}')
        self.assertEqual(upserts['app-a'][0]['review_seq'], 10)
        self._install('delta23.json')
        status = self._status()
        self.assertEqual(status['current']['release'], 3)
        self.assertTrue(all(p['readable'] for p in status['packages']))
        # The previous slot still holds the stopped release 2: no rollback.
        self.assertEqual(status['previous']['release'], 2)
        self.assertFalse(status['rollback_allowed'])
        self._rollback(expect=2)

        # Release 4 deletes settings and adds promo in one release; the
        # unchanged cart entry is not retransmitted inside the delta.
        delta34 = self._read('delta34.json')
        self.assertNotIn('cart', json.dumps(delta34))
        self._install('delta34.json')
        keys = [m['key'] for m in self._fetch('app-c')['messages']]
        self.assertEqual(keys, ['cart', 'promo'])
        status = self._status()
        self.assertEqual((status['current']['release'],
                          status['previous']['release']), (4, 3))
        self.assertTrue(status['rollback_allowed'])

        # Rollback to release 3 succeeds; the highest release stays 4.
        self._rollback()
        status = self._status()
        self.assertEqual((status['current']['release'],
                          status['highest_release']), (3, 4))
        self.assertIsNone(status['previous'])
        self.assertEqual([m['key'] for m in self._fetch('app-c')['messages']],
                         ['cart', 'settings'])

        # Re-installing release 4 from the exact v3 baseline fails: the
        # release number is not newer than the historical highest.
        before = self.store.read_bytes()
        refused = self._install('delta34.json', expect=2)
        self.assertIn('not newer', refused.stderr)
        self.assertEqual(self.store.read_bytes(), before)

    def test_notice_replay_conflict_and_late_evidence(self):
        self.invoke('init', '--store', str(self.store),
                    '--snapshot', str(self.dir / 'snap1.json'))
        self._install('delta12.json')
        self._notice()
        # Same id and same set: accepted again, state byte-identical.
        before = self.store.read_bytes()
        self._notice()
        self.assertEqual(self.store.read_bytes(), before)
        # Same id with a different set: the whole receipt is refused.
        conflict = {'version': 1, 'notice_id': 'n1',
                    'withdrawn': [{'translation_id': 't1', 'review_seq': 10}]}
        self._write('conflict.json', conflict)
        refused = self._notice('conflict.json', expect=2)
        self.assertIn('different set', refused.stderr)
        self.assertEqual(self.store.read_bytes(), before)
        # A notice naming evidence nothing has installed is accepted and
        # takes effect for future installs: v3alt inherits (t1, 4) and stays
        # refused, while the re-approved v3 installs.
        unknown = {'version': 1, 'notice_id': 'n2',
                   'withdrawn': [{'translation_id': 't9', 'review_seq': 99}]}
        self._write('unknown.json', unknown)
        self._notice('unknown.json')
        status = self._status()
        self.assertEqual([n['notice_id'] for n in status['notices']],
                         ['n1', 'n2'])
        self._install('delta23alt.json', expect=2)
        self._install('delta23.json')
        self.assertTrue(all(p['readable'] for p in self._status()['packages']))

    def test_wrong_baseline_and_corrupt_delta_leave_store_untouched(self):
        self.invoke('init', '--store', str(self.store),
                    '--snapshot', str(self.dir / 'snap1.json'))
        self._install('delta12.json')
        before = self.store.read_bytes()
        # A stale delta whose baseline is v1 cannot overwrite the v2 install.
        refused = self._install('delta12.json', expect=2)
        self.assertIn('baseline', refused.stderr)
        self.assertEqual(self.store.read_bytes(), before)
        # Corrupted JSON.
        (self.dir / 'broken.json').write_text('{')
        refused = self._install('broken.json', expect=2)
        self.assertEqual(self.store.read_bytes(), before)
        # Integrity seal broken.
        tampered = self._read('delta23.json')
        tampered['packages_changed'][0]['keys_upserted'][0]['review_seq'] = 11
        self._write('tampered.json', tampered)
        refused = self._install('tampered.json', expect=2)
        self.assertIn('delta id', refused.stderr)
        self.assertEqual(self.store.read_bytes(), before)
        # Well-sealed but the restored target is not the pinned identity.
        crafted = self._read('delta23.json')
        crafted['target_id'] = '0' * 64
        content = {k: crafted[k] for k in
                   ('target_release', 'baseline_id', 'target_id',
                    'target_header', 'packages_removed', 'packages_added',
                    'packages_changed')}
        crafted['delta_id'] = hashlib.sha256(json.dumps(
            content, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()
        self._write('crafted.json', crafted)
        refused = self._install('crafted.json', expect=2)
        self.assertIn('target identity', refused.stderr)
        self.assertEqual(self.store.read_bytes(), before)
        # The store still answers with its current identity for a rebuild.
        status = self._status()
        self.assertEqual(status['current']['release'], 2)
        self.assertEqual(status['current']['snapshot_id'],
                         self._read('snap2.json')['snapshot_id'])

    def test_store_file_protections(self):
        self.invoke('init', '--store', str(self.store),
                    '--snapshot', str(self.dir / 'snap1.json'))
        # A second init over an existing store is refused.
        self.invoke('init', '--store', str(self.store),
                    '--snapshot', str(self.dir / 'snap2.json'), expect=2)
        # Corrupt store: every operation fails and preserves the file.
        self.store.write_text('{"version": 2}')
        before = self.store.read_bytes()
        self.invoke('status', '--store', str(self.store), expect=2)
        self._install('delta23.json', expect=2)
        self._notice(expect=2)
        self._rollback(expect=2)
        self._fetch('app-c', expect=2)
        self.assertEqual(self.store.read_bytes(), before)
        # Missing store is an error for every store operation.
        missing = self.dir / 'missing.json'
        self.invoke('status', '--store', str(missing), expect=2)
        self.invoke('install', '--store', str(missing),
                    '--delta', str(self.dir / 'delta12.json'), expect=2)
        # Store and delta paths must not coincide.
        self.invoke('install', '--store', str(self.store),
                    '--delta', str(self.store), expect=2)
        # A symlinked store is never replaced.
        target = self.dir / 'target.json'
        target.write_text('old')
        link = self.dir / 'link.json'
        link.symlink_to(target)
        self.invoke('init', '--store', str(link),
                    '--snapshot', str(self.dir / 'snap1.json'), expect=2)
        self.assertEqual(target.read_text(), 'old')

    def test_build_and_delta_rejections(self):
        # Unknown query, bad release numbers and an invalid fact log.
        self.invoke('build', '--source', str(self.dir / 'input.json'),
                    '--output', str(self.dir / 'x.json'),
                    '--query', 'nope', '--release', '9', expect=2)
        self.assertFalse((self.dir / 'x.json').exists())
        self.invoke('build', '--source', str(self.dir / 'input.json'),
                    '--output', str(self.dir / 'x.json'),
                    '--query', 'q1', '--release', '0', expect=2)
        self._write('bad_input.json', {'default_locale': 'en'})
        self.invoke('build', '--source', str(self.dir / 'bad_input.json'),
                    '--output', str(self.dir / 'x.json'),
                    '--query', 'q1', '--release', '1', expect=2)
        # A tampered snapshot cannot serve as a delta endpoint.
        snap = self._read('snap1.json')
        snap['release'] = 7
        self._write('tampered_snap.json', snap)
        self.invoke('delta', '--baseline', str(self.dir / 'tampered_snap.json'),
                    '--target', str(self.dir / 'snap2.json'),
                    '--output', str(self.dir / 'x.json'), expect=2)
        # init with a tampered snapshot is refused and creates nothing.
        self.invoke('init', '--store', str(self.dir / 's.json'),
                    '--snapshot', str(self.dir / 'tampered_snap.json'), expect=2)
        self.assertFalse((self.dir / 's.json').exists())


if __name__ == '__main__':
    unittest.main()
