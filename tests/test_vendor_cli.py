import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'locale_bundle.vendor_cli'
WORKSPACE_FIXTURE = ROOT / 'fixtures/vendor_workspace.json'
SELECTION_FIXTURE = ROOT / 'fixtures/vendor_selection.json'


class VendorCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.workspace = self.dir / 'workspace.json'
        self.selection = self.dir / 'selection.json'
        self.package = self.dir / 'package.json'
        self.response = self.dir / 'response.json'
        self.workspace.write_text(WORKSPACE_FIXTURE.read_text())
        self.selection.write_text(SELECTION_FIXTURE.read_text())

    def invoke(self, *args):
        return subprocess.run([sys.executable, '-m', MODULE, *args],
                              capture_output=True, text=True, timeout=10, cwd=ROOT,
                              env={'PYTHONPATH': str(ROOT / 'src')})

    def prepare(self):
        return self.invoke('prepare', '--workspace', str(self.workspace),
                           '--selection', str(self.selection),
                           '--package', str(self.package))

    def receive(self):
        return self.invoke('receive', '--workspace', str(self.workspace),
                           '--package', str(self.package),
                           '--response', str(self.response))

    def test_prepare_and_receive_flow_and_replay(self):
        r = self.prepare()
        self.assertEqual(r.returncode, 0, r.stderr)
        package = json.loads(self.package.read_text())
        group = next(g for g in package['request']['groups']
                     if g['context'] == 'ui/button')
        self.response.write_text(json.dumps({
            'batch_id': package['batch_id'],
            'results': [{'group_id': group['group_id'],
                         'segments': [{'kind': 'text', 'text': 'DE '},
                                      {'kind': 'slot', 'slot': 1}]}]}))
        before = self.workspace.read_bytes()
        r = self.receive()
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(len(report['results']), 1)
        workspace = json.loads(self.workspace.read_text())
        draft = next(d for d in workspace['drafts']
                     if d['locale'] == 'de' and d['key'] == 'key_c')
        self.assertEqual(draft['revision'], 1)
        self.assertEqual(draft['message']['value'], 'DE {name}')
        # Exact replay: identical workspace, replayed flag, no new receipt.
        replay = self.receive()
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertTrue(json.loads(replay.stdout)['results'][0]['replayed'])
        self.assertNotEqual(self.workspace.read_bytes(), before)
        replayed_workspace = self.workspace.read_bytes()
        self.assertEqual(self.receive().returncode, 0)
        self.assertEqual(self.workspace.read_bytes(), replayed_workspace)

    def test_conflicting_replay_fails_and_preserves_workspace(self):
        self.assertEqual(self.prepare().returncode, 0)
        package = json.loads(self.package.read_text())
        group = next(g for g in package['request']['groups']
                     if g['context'] == 'ui/button')
        payload = {'batch_id': package['batch_id'],
                   'results': [{'group_id': group['group_id'],
                                'segments': [{'kind': 'text', 'text': 'A '},
                                             {'kind': 'slot', 'slot': 1}]}]}
        self.response.write_text(json.dumps(payload))
        self.assertEqual(self.receive().returncode, 0)
        accepted = self.workspace.read_bytes()
        payload['results'][0]['segments'][0]['text'] = 'DIFFERENT '
        self.response.write_text(json.dumps(payload))
        r = self.receive()
        self.assertEqual(r.returncode, 2)
        self.assertIn('different segments', r.stderr)
        self.assertEqual(self.workspace.read_bytes(), accepted)

    def test_unknown_group_fails_without_touching_workspace(self):
        self.assertEqual(self.prepare().returncode, 0)
        package = json.loads(self.package.read_text())
        self.response.write_text(json.dumps(
            {'batch_id': package['batch_id'],
             'results': [{'group_id': 'gdeadbeef', 'segments': []}]}))
        before = self.workspace.read_bytes()
        r = self.receive()
        self.assertEqual(r.returncode, 2)
        self.assertIn('unknown group', r.stderr)
        self.assertEqual(self.workspace.read_bytes(), before)

    def test_prepare_never_modifies_workspace(self):
        before = self.workspace.read_bytes()
        self.assertEqual(self.prepare().returncode, 0)
        self.assertEqual(self.workspace.read_bytes(), before)

    def test_path_collisions_rejected(self):
        r = self.invoke('prepare', '--workspace', str(self.workspace),
                        '--selection', str(self.workspace),
                        '--package', str(self.package))
        self.assertEqual(r.returncode, 2)
        r = self.invoke('receive', '--workspace', str(self.workspace),
                        '--package', str(self.workspace),
                        '--response', str(self.response))
        self.assertEqual(r.returncode, 2)

    def test_symlink_workspace_preserved_on_failure(self):
        target = self.dir / 'target.json'
        target.write_text('{"broken": true}')
        link = self.dir / 'link.json'
        link.symlink_to(target)
        r = self.invoke('receive', '--workspace', str(link),
                        '--package', str(self.package),
                        '--response', str(self.response))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(target.read_text(), '{"broken": true}')

    def test_json_safety_conventions(self):
        self.assertEqual(self.prepare().returncode, 0)
        package = json.loads(self.package.read_text())
        for raw in ('{', '{"a":1,"a":2}', '{"a":NaN}'):
            self.response.write_text(raw)
            self.assertEqual(self.receive().returncode, 2)
        # Wrong batch id is rejected.
        self.response.write_text(json.dumps(
            {'batch_id': 'nope', 'results': []}))
        self.assertEqual(self.receive().returncode, 2)


if __name__ == '__main__':
    unittest.main()
