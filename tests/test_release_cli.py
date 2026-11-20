import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'locale_bundle.release_cli'
SOURCE = ROOT / 'fixtures/release_input.json'


class ReleaseCliTests(unittest.TestCase):
    def invoke(self, source, output):
        return subprocess.run([sys.executable, '-m', MODULE, '--source', str(source),
                               '--output', str(output)], capture_output=True, text=True,
                              timeout=10, cwd=ROOT,
                              env={'PYTHONPATH': str(ROOT / 'src')})

    def test_valid_report_and_repeat(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'release.json'
            r = self.invoke(SOURCE, output)
            self.assertEqual(r.returncode, 0, r.stderr)
            first = output.read_bytes()
            report = json.loads(first)
            self.assertEqual([p['application_id'] for p in report['applications']],
                             ['app-a', 'app-b'])
            app_a = report['applications'][0]
            self.assertEqual(app_a['catalogs']['fr-CA']['key-x']['source_locale'], 'fr')
            self.assertEqual(app_a['catalogs']['fr-CA']['key-x']['included_by'], ['shared'])
            self.assertNotIn('key-x', report['applications'][1]['catalogs']['fr-CA'])
            self.assertEqual(self.invoke(SOURCE, output).returncode, 0)
            self.assertEqual(first, output.read_bytes())

    def test_invalid_inputs_preserve_report(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'release.json'
            source = Path(t) / 'bad.json'
            output.write_text('old')
            good = json.loads(SOURCE.read_text())
            broken = dict(good)
            broken['components'] = good['components'] + [good['components'][0]]
            for raw in ('{', '{"a":1,"a":2}', '{"a":NaN}', '{}', json.dumps(broken)):
                source.write_text(raw)
                r = self.invoke(source, output)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(output.read_text(), 'old')

    def test_cycle_and_missing_key_fail(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / 'release.json'
            source = Path(t) / 'bad.json'
            output.write_text('old')
            good = json.loads(SOURCE.read_text())
            cyclic = json.loads(SOURCE.read_text())
            cyclic['components'][2]['requires'] = ['checkout']
            missing = json.loads(SOURCE.read_text())
            missing['components'][1]['keys'].append('absent')
            for data in (cyclic, missing):
                source.write_text(json.dumps(data))
                self.assertEqual(self.invoke(source, output).returncode, 2)
                self.assertEqual(output.read_text(), 'old')

    def test_output_collision_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / 'source.json'
            path.write_bytes(SOURCE.read_bytes())
            before = path.read_bytes()
            self.assertEqual(self.invoke(path, path).returncode, 2)
            self.assertEqual(before, path.read_bytes())

    def test_symlink_target_preserved(self):
        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / 'target'
            target.write_text('old')
            link = Path(t) / 'link'
            link.symlink_to(target)
            self.assertEqual(self.invoke(SOURCE, link).returncode, 2)
            self.assertEqual(target.read_text(), 'old')

    def test_missing_source_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.invoke(Path(t) / 'missing',
                                         Path(t) / 'out').returncode, 2)
