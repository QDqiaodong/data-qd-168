import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'locale_bundle.settlement_cli'
DELIVERY_MODULE = 'locale_bundle.delivery_cli'
FIXTURE = ROOT / 'fixtures/delivery_input.json'


class SettlementCliTests(unittest.TestCase):
    """The acceptance saga end to end through the CLI, plus the file and
    failure protections shared with the other commands."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.books = self.dir / 'books.json'
        # Headquarters builds the release line the stores count against.
        for query, release in (('q1', 1), ('q2', 2), ('q3', 3)):
            self.invoke_delivery(
                'build', '--source', str(FIXTURE),
                '--output', str(self.dir / ('snap%d.json' % release)),
                '--query', query, '--release', str(release))
        self.snap = {release: self._read('snap%d.json' % release)
                     for release in (1, 2, 3)}
        self._write('stores.json', {'version': 1, 'stores': [
            {'store_id': 'store-1', 'enabled': '2026-05-30'},
            {'store_id': 'store-2', 'enabled': '2026-06-01'}]})
        self.invoke('init', '--books', str(self.books),
                    '--stores', str(self.dir / 'stores.json'))

    def _write(self, name, value):
        path = self.dir / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        return path

    def _read(self, name):
        return json.loads((self.dir / name).read_text())

    def _run(self, module, *args, expect=0):
        result = subprocess.run([sys.executable, '-m', module, *args],
                                capture_output=True, text=True, timeout=10,
                                cwd=ROOT, env={'PYTHONPATH': str(ROOT / 'src')})
        self.assertEqual(result.returncode, expect,
                         '%s -> %s\n%s' % (' '.join(args), result.returncode,
                                           result.stderr))
        return result

    def invoke(self, *args, expect=0):
        return self._run(MODULE, *args, expect=expect)

    def invoke_delivery(self, *args, expect=0):
        return self._run(DELIVERY_MODULE, *args, expect=expect)

    def _dim(self, release, application_id, key, count):
        return {'snapshot_id': self.snap[release]['snapshot_id'],
                'application_id': application_id, 'locale': 'fr-CA',
                'key': key, 'count': count}

    @staticmethod
    def _record(store_id, date, revision, counts):
        return {'store_id': store_id, 'date': date, 'revision': revision,
                'counts': counts}

    @staticmethod
    def _agreement(translation_id, review_seq, price):
        return {'translation_id': translation_id, 'review_seq': review_seq,
                'vendor': 'acme', 'unit_price': price}

    def _import(self, name, snapshots=(), agreements=(), records=(), expect=0):
        self._write(name, {'version': 1, 'snapshots': list(snapshots),
                           'agreements': list(agreements),
                           'records': list(records)})
        result = self.invoke('import', '--books', str(self.books),
                             '--batch', str(self.dir / name), expect=expect)
        return json.loads(result.stdout) if expect == 0 else result

    def _reconcile(self, month, store=None):
        args = ['reconcile', '--books', str(self.books), '--month', month]
        if store is not None:
            args += ['--store', store]
        return json.loads(self.invoke(*args).stdout)

    def _close(self, month, expect=0):
        result = self.invoke('close', '--books', str(self.books),
                             '--month', month, expect=expect)
        return json.loads(result.stdout) if expect == 0 else result

    def _bill(self, month, expect=0):
        result = self.invoke('bill', '--books', str(self.books),
                             '--month', month, expect=expect)
        return json.loads(result.stdout) if expect == 0 else result

    @staticmethod
    def _day(report, store_id, date):
        store = next(row for row in report['stores']
                     if row['store_id'] == store_id)
        return next(day for day in store['days'] if day['date'] == date)

    def test_monthly_saga_through_the_cli(self):
        # store-1 counts one billable dimension: 10 / (missing) / 20.
        report = self._import(
            'b1.json', snapshots=[self.snap[1]],
            agreements=[self._agreement('t1', 4, 30)],
            records=[self._record('store-1', '2026-05-31', 1, [self._dim(1, 'app-a', 'greeting', 10)]),
                     self._record('store-1', '2026-06-02', 1, [self._dim(1, 'app-a', 'greeting', 20)])])
        self.assertEqual((report['snapshots_registered'],
                          report['agreements_registered'],
                          report['receipts_added']), (1, 1, 2))
        may = self._reconcile('2026-05', 'store-1')
        first = self._day(may, 'store-1', '2026-05-30')
        self.assertEqual((first['status'], first['usage'][0]['count']), ('complete', 10))
        self.assertEqual(self._day(may, 'store-1', '2026-05-31')['missing'], 'right')
        may_bill = self._close('2026-05')
        self.assertEqual(may_bill['vendor_totals'], [{'vendor': 'acme', 'amount': 300}])

        # The 06-01 midnight arrives late (14), together with the second
        # store's first vector spanning two snapshots on one business day.
        report = self._import(
            'b2.json', snapshots=[self.snap[2], self.snap[3]],
            agreements=[self._agreement('t1', 10, 50), self._agreement('t2', 7, 20)],
            records=[self._record('store-1', '2026-06-01', 1, [self._dim(1, 'app-a', 'greeting', 14)]),
                     self._record('store-2', '2026-06-02', 1,
                                  [self._dim(1, 'app-a', 'greeting', 5),
                                   self._dim(3, 'app-a', 'greeting', 7),
                                   self._dim(1, 'app-c', 'settings', 3),
                                   self._dim(3, 'app-c', 'cart', 4)])])
        self.assertEqual(report['receipts_added'], 2)
        may = self._reconcile('2026-05', 'store-1')
        june = self._reconcile('2026-06')
        self.assertEqual(self._day(may, 'store-1', '2026-05-31')['usage'][0]['count'], 4)
        self.assertEqual(self._day(june, 'store-1', '2026-06-01')['usage'][0]['count'], 6)
        second = self._day(june, 'store-2', '2026-06-01')
        by_key = {(u['snapshot_id'], u['key']): u for u in second['usage']}
        self.assertEqual(by_key[(self.snap[1]['snapshot_id'], 'greeting')]['count'], 5)
        self.assertEqual(by_key[(self.snap[3]['snapshot_id'], 'greeting')]['count'], 7)
        self.assertEqual(by_key[(self.snap[1]['snapshot_id'], 'settings')]['amount'], 0)
        self.assertEqual(by_key[(self.snap[3]['snapshot_id'], 'cart')]['count'], 4)
        self.assertEqual(sum(u['amount'] for u in second['usage']), 580)

        # Correct the 06-01 midnight to 16 before closing June.
        self._import('b3.json',
                     records=[self._record('store-1', '2026-06-01', 2, [self._dim(1, 'app-a', 'greeting', 16)])])
        june_bill = self._close('2026-06')
        by_month = {entry['service_month']: entry for entry in june_bill['entries']}
        self.assertEqual((by_month['2026-05']['kind'], by_month['2026-05']['amount']),
                         ('adjustment', 180))
        self.assertEqual((by_month['2026-06']['kind'], by_month['2026-06']['amount']),
                         ('current', 700))

        # Correct it back to 14; July settles May -2 and June +2 renders.
        self._import('b4.json',
                     records=[self._record('store-1', '2026-06-01', 3, [self._dim(1, 'app-a', 'greeting', 14)])])
        july_bill = self._close('2026-07')
        by_month = {entry['service_month']: entry for entry in july_bill['entries']}
        self.assertEqual((by_month['2026-05']['kind'], by_month['2026-05']['amount']),
                         ('offset', -60))
        self.assertEqual((by_month['2026-06']['kind'], by_month['2026-06']['amount']),
                         ('adjustment', 60))

        # The handed-out bills read back exactly as closed.
        self.assertEqual(self._bill('2026-05'), may_bill)
        self.assertEqual(self._bill('2026-06'), june_bill)
        # Re-closing an already closed month prints the original bill and
        # does not rewrite the books.
        before = self.books.read_bytes()
        self.assertEqual(self._close('2026-06'), june_bill)
        self.assertEqual(self.books.read_bytes(), before)
        # Nothing changed since July: August books no differences.
        self.assertEqual(self._close('2026-08')['entries'], [])

    def test_close_order_and_readback_errors(self):
        self._import('b1.json', snapshots=[self.snap[1]],
                     agreements=[self._agreement('t1', 4, 30)],
                     records=[self._record('store-1', '2026-05-31', 1,
                                           [self._dim(1, 'app-a', 'greeting', 10)])])
        before = self.books.read_bytes()
        skipped = self._close('2026-06', expect=2)
        self.assertIn('skip', skipped.stderr)
        self.assertEqual(self.books.read_bytes(), before)
        self._close('2026-04', expect=2)  # before the earliest enable month
        self._bill('2026-05', expect=2)   # not closed yet
        self.assertEqual(self.books.read_bytes(), before)
        self._close('2026-05')
        self._bill('2026-05')

    def test_failed_batch_leaves_books_untouched(self):
        self._import('b1.json', snapshots=[self.snap[1]],
                     agreements=[self._agreement('t1', 4, 30)],
                     records=[self._record('store-1', '2026-05-31', 1,
                                           [self._dim(1, 'app-a', 'greeting', 10)])])
        before = self.books.read_bytes()
        # A legal record and an illegal one (no agreement for t2#7) in the
        # same batch: nothing commits.
        self._import('bad.json', snapshots=[self.snap[3]],
                     agreements=[self._agreement('t1', 10, 50)],
                     records=[self._record('store-1', '2026-06-01', 1,
                                           [self._dim(1, 'app-a', 'greeting', 14)]),
                              self._record('store-1', '2026-06-02', 1,
                                           [self._dim(3, 'app-c', 'cart', 1)])],
                     expect=2)
        self.assertEqual(self.books.read_bytes(), before)
        # A revision conflict rejects the batch even when superseded.
        self._import('b2.json',
                     records=[self._record('store-1', '2026-06-01', 1,
                                           [self._dim(1, 'app-a', 'greeting', 14)]),
                              self._record('store-1', '2026-06-01', 2,
                                           [self._dim(1, 'app-a', 'greeting', 16)])])
        before = self.books.read_bytes()
        conflict = self._import('conflict.json',
                                records=[self._record('store-1', '2026-06-01', 1,
                                                      [self._dim(1, 'app-a', 'greeting', 15)])],
                                expect=2)
        self.assertIn('conflicts', conflict.stderr)
        self.assertEqual(self.books.read_bytes(), before)
        # Corrupt batch JSON.
        (self.dir / 'broken.json').write_text('{')
        self.invoke('import', '--books', str(self.books),
                    '--batch', str(self.dir / 'broken.json'), expect=2)
        self.assertEqual(self.books.read_bytes(), before)

    def test_books_file_protections(self):
        # init refuses to overwrite an existing books file.
        self.invoke('init', '--books', str(self.books),
                    '--stores', str(self.dir / 'stores.json'), expect=2)
        # A symlinked books file is never replaced.
        target = self.dir / 'target.json'
        target.write_text('old')
        link = self.dir / 'link.json'
        link.symlink_to(target)
        self.invoke('init', '--books', str(link),
                    '--stores', str(self.dir / 'stores.json'), expect=2)
        self.assertEqual(target.read_text(), 'old')
        # Paths of one invocation must not coincide.
        self.invoke('init', '--books', str(self.dir / 'stores.json'),
                    '--stores', str(self.dir / 'stores.json'), expect=2)
        self._import('b1.json', snapshots=[self.snap[1]],
                     agreements=[self._agreement('t1', 4, 30)])
        self.invoke('import', '--books', str(self.books),
                    '--batch', str(self.books), expect=2)
        # Corrupt books: every command fails and preserves the file.
        self.books.write_text('{"version": 2}')
        before = self.books.read_bytes()
        self.invoke('reconcile', '--books', str(self.books),
                    '--month', '2026-05', expect=2)
        self.invoke('close', '--books', str(self.books),
                    '--month', '2026-05', expect=2)
        self.invoke('bill', '--books', str(self.books),
                    '--month', '2026-05', expect=2)
        self._import('b2.json', records=[], expect=2)
        self.assertEqual(self.books.read_bytes(), before)
        # Missing books file is an error for every command but init.
        missing = self.dir / 'missing.json'
        for args in (('reconcile', '--month', '2026-05'),
                     ('close', '--month', '2026-05'),
                     ('bill', '--month', '2026-05')):
            self.invoke(args[0], '--books', str(missing), *args[1:], expect=2)
        # Unknown store filter and malformed months are rejected.
        ok = self.dir / 'ok.json'
        self.invoke('init', '--books', str(ok),
                    '--stores', str(self.dir / 'stores.json'))
        self.invoke('reconcile', '--books', str(ok), '--month', '2026-05',
                    '--store', 'nope', expect=2)
        self.invoke('reconcile', '--books', str(ok), '--month', '2026-13',
                    expect=2)
        self.invoke('close', '--books', str(ok), '--month', '2026-5', expect=2)

    def test_produced_state_stays_readable(self):
        self._import('b1.json', snapshots=[self.snap[1]],
                     agreements=[self._agreement('t1', 4, 30)],
                     records=[self._record('store-1', '2026-05-31', 1,
                                           [self._dim(1, 'app-a', 'greeting', 10)])])
        self._close('2026-05')
        # The state written by import and close reads back under the same
        # rules for every following command.
        report = self._reconcile('2026-05')
        self.assertEqual(report['month'], '2026-05')
        bill = self._bill('2026-05')
        self.assertEqual(bill['month'], '2026-05')
        again = json.loads(self.books.read_text())
        self.assertEqual(again['bills'][0], bill)


if __name__ == '__main__':
    unittest.main()
