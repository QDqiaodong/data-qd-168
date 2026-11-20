import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'locale_bundle.history_cli'
FIXTURE = ROOT / 'fixtures/history_input.json'


def batch_with_failing_second_query():
    data = json.loads(FIXTURE.read_text())
    data['sources'][0]['effective'] = 5
    data['queries'] = [{'id': 'ok', 'at': 10, 'known_seq': 6},
                       {'id': 'too-early', 'at': 0, 'known_seq': 6}]
    return json.dumps(data)


class HistoryCliTests(unittest.TestCase):
    def invoke(self, source, output):
        return subprocess.run([sys.executable, '-m', MODULE, '--source', str(source),
                               '--output', str(output)], capture_output=True, text=True, timeout=10)

    def test_valid_report_and_repeat(self):
        with tempfile.TemporaryDirectory() as t:
            report = Path(t) / 'history.json'
            r = self.invoke(FIXTURE, report)
            self.assertEqual(r.returncode, 0, r.stderr)
            first = report.read_bytes()
            views = json.loads(first)['views']
            self.assertEqual([v['query_id'] for v in views],
                             ['q1', 'q2', 'q3', 'q4', 'q5', 'q6'])
            self.assertEqual(views[0]['catalogs']['fr-CA']['g']['translation_id'], 't1')
            self.assertEqual(views[3]['catalogs']['fr-CA']['g']['translation_id'], 't2')
            self.assertEqual(self.invoke(FIXTURE, report).returncode, 0)
            self.assertEqual(first, report.read_bytes())

    def test_invalid_inputs_preserve_report(self):
        with tempfile.TemporaryDirectory() as t:
            report = Path(t) / 'history.json'
            source = Path(t) / 'input.json'
            report.write_text('old')
            bad = ('{', '{"a":1,"a":2}', '{"a":NaN}', '{}', batch_with_failing_second_query())
            for raw in bad:
                with self.subTest(raw=raw[:40]):
                    source.write_text(raw)
                    r = self.invoke(source, report)
                    self.assertEqual(r.returncode, 2, r.stderr)
                    self.assertEqual(report.read_text(), 'old')

    def test_output_collision_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            source = Path(t) / 'input.json'
            source.write_bytes(FIXTURE.read_bytes())
            before = source.read_bytes()
            self.assertEqual(self.invoke(source, source).returncode, 2)
            self.assertEqual(before, source.read_bytes())

    def test_symlink_target_preserved(self):
        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / 'target'
            target.write_text('old')
            link = Path(t) / 'link'
            link.symlink_to(target)
            self.assertEqual(self.invoke(FIXTURE, link).returncode, 2)
            self.assertEqual(target.read_text(), 'old')

    def test_missing_source_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.invoke(Path(t) / 'missing', Path(t) / 'report').returncode, 2)


if __name__ == '__main__':
    unittest.main()
