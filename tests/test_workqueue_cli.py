import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'locale_bundle.workqueue_cli'
FIXTURE = ROOT / 'fixtures/workqueue_input.json'
APP_A = {'id': 'app-a', 'roots': ['checkout', 'catalog']}
APP_B = {'id': 'app-b', 'roots': ['b-ui']}


def input_with(applications):
    data = json.loads(FIXTURE.read_text())
    data['applications'] = applications
    return json.dumps(data)


class WorkQueueCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.ledger = self.dir / 'ledger.json'
        self.source = self.dir / 'input.json'

    def invoke(self, *args):
        return subprocess.run([sys.executable, '-m', MODULE, *args],
                              capture_output=True, text=True, timeout=10, cwd=ROOT,
                              env={'PYTHONPATH': str(ROOT / 'src')})

    def write_source(self, applications, mutate=None):
        data = json.loads(FIXTURE.read_text())
        data['applications'] = applications
        if mutate is not None:
            mutate(data)
        self.source.write_text(json.dumps(data))

    def sync(self, query):
        return self.invoke('sync', '--source', str(self.source),
                           '--ledger', str(self.ledger), '--query', query)

    def assign(self, source_id='s1', translator='alice'):
        return self.invoke('assign', '--ledger', str(self.ledger), '--locale', 'fr',
                           '--key', 'greeting', '--source-id', source_id,
                           '--translator', translator)

    def confirm(self, source_id='s1', translation_id='t1', review_seq='3'):
        return self.invoke('confirm', '--ledger', str(self.ledger), '--locale', 'fr',
                           '--key', 'greeting', '--source-id', source_id,
                           '--confirmer', 'lead', '--translation-id', translation_id,
                           '--review-seq', review_seq)

    def query(self):
        return self.invoke('query', '--ledger', str(self.ledger))

    def test_full_offline_flow(self):
        self.write_source([APP_A])
        r = self.sync('q1')
        self.assertEqual(r.returncode, 0, r.stderr)
        first = self.ledger.read_bytes()
        tasks = json.loads(first)['tasks']
        self.assertEqual([(t['locale'], t['key'], t['source_id'], t['status'])
                          for t in tasks],
                         [('fr', 'greeting', 's1', 'open')])
        # Equivalent rerun leaves the file byte-identical.
        self.assertEqual(self.sync('q1').returncode, 0)
        self.assertEqual(self.ledger.read_bytes(), first)

        self.assertEqual(self.assign().returncode, 0)
        self.assertEqual(json.loads(self.ledger.read_text())['tasks'][0]['assignee'],
                         'alice')

        self.write_source([APP_A, APP_B])
        self.assertEqual(self.sync('q2').returncode, 0)
        self.assertEqual(self.confirm().returncode, 0)
        r = self.query()
        self.assertEqual(r.returncode, 0, r.stderr)
        view = json.loads(r.stdout)
        task = view['tasks'][0]
        self.assertEqual(task['status'], 'done')
        self.assertEqual(task['confirmation'],
                         {'confirmer': 'lead', 'translation_id': 't1', 'review_seq': 3})
        self.assertEqual(view['last_sync'], {'query_id': 'q2', 'at': 10, 'known_seq': 3})
        # Repeating the same confirmation is a no-op.
        before = self.ledger.read_bytes()
        self.assertEqual(self.confirm().returncode, 0)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_rejected_operations_preserve_ledger(self):
        self.write_source([APP_A])
        self.assertEqual(self.sync('q1').returncode, 0)
        before = self.ledger.read_bytes()
        # Confirm before any approval exists.
        r = self.confirm()
        self.assertEqual(r.returncode, 2)
        self.assertIn('no approval', r.stderr)
        # Cutoff rollback: q1 is (0, 2); a lower known_seq than the accepted
        # q2 watermark must fail after q2 is accepted.
        self.assertEqual(self.sync('q2').returncode, 0)
        accepted = self.ledger.read_bytes()
        self.assertEqual(self.sync('q1').returncode, 2)
        self.assertEqual(self.ledger.read_bytes(), accepted)
        # Operations on an unknown task fail and preserve the file.
        r = self.invoke('assign', '--ledger', str(self.ledger), '--locale', 'fr',
                        '--key', 'greeting', '--source-id', 's9', '--translator', 'bob')
        self.assertEqual(r.returncode, 2)
        self.assertIn('unknown task', r.stderr)
        self.assertEqual(self.ledger.read_bytes(), accepted)
        self.assertGreater(len(before), 0)

    def test_invalid_sync_input_creates_no_ledger(self):
        for raw in ('{', '{"a":1,"a":2}', '{"a":NaN}', '{}'):
            self.source.write_text(raw)
            r = self.sync('q1')
            self.assertEqual(r.returncode, 2, raw)
            self.assertFalse(self.ledger.exists())
        # Unknown query id.
        self.write_source([APP_A])
        self.assertEqual(self.sync('nope').returncode, 2)
        self.assertFalse(self.ledger.exists())

    def test_corrupt_ledger_rejected_and_preserved(self):
        self.ledger.write_text('{"version": 2, "last_sync": null, "syncs": [], "tasks": []}')
        before = self.ledger.read_bytes()
        self.assertEqual(self.query().returncode, 2)
        self.assertEqual(self.assign().returncode, 2)
        self.assertEqual(self.confirm().returncode, 2)
        self.write_source([APP_A])
        self.assertEqual(self.sync('q1').returncode, 2)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_missing_ledger_rejected_for_manual_operations(self):
        self.assertEqual(self.assign().returncode, 2)
        self.assertEqual(self.confirm().returncode, 2)
        self.assertEqual(self.query().returncode, 2)
        self.assertFalse(self.ledger.exists())

    def test_source_ledger_collision_rejected(self):
        self.write_source([APP_A])
        before = self.source.read_bytes()
        r = self.invoke('sync', '--source', str(self.source),
                        '--ledger', str(self.source), '--query', 'q1')
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.source.read_bytes(), before)

    def test_symlink_ledger_preserved(self):
        target = self.dir / 'target'
        target.write_text('old')
        link = self.dir / 'link'
        link.symlink_to(target)
        self.write_source([APP_A])
        r = self.invoke('sync', '--source', str(self.source),
                        '--ledger', str(link), '--query', 'q1')
        self.assertEqual(r.returncode, 2)
        self.assertEqual(target.read_text(), 'old')


if __name__ == '__main__':
    unittest.main()
