import json
from pathlib import Path
import unittest

from locale_bundle.delivery import build_snapshot
from locale_bundle.settlement import (SettlementError, init_books, import_batch,
                                      reconcile_month, close_month, get_bill,
                                      validate_books)

ROOT = Path(__file__).resolve().parents[1]


def snapshots():
    data = json.loads((ROOT / 'fixtures/delivery_input.json').read_text())
    return {'r1': build_snapshot(data, 'q1', 1),
            'r2': build_snapshot(data, 'q2', 2),
            'r3': build_snapshot(data, 'q3', 3)}


SNAPS = snapshots()


def dim(snapshot, application_id, key, count, locale='fr-CA'):
    return {'snapshot_id': snapshot['snapshot_id'],
            'application_id': application_id, 'locale': locale,
            'key': key, 'count': count}


def record(store_id, date, revision, counts):
    return {'store_id': store_id, 'date': date, 'revision': revision,
            'counts': counts}


def agreement(translation_id, review_seq, vendor='acme', price=30):
    return {'translation_id': translation_id, 'review_seq': review_seq,
            'vendor': vendor, 'unit_price': price}


def batch(snapshots=(), agreements=(), records=()):
    return {'version': 1, 'snapshots': list(snapshots),
            'agreements': list(agreements), 'records': list(records)}


def one_store():
    return init_books({'version': 1, 'stores': [
        {'store_id': 'store-1', 'enabled': '2026-05-30'}]})


def saga_books():
    """The acceptance saga up to the state just before the May closing:
    store-1 enabled 2026-05-30, one billable dimension at 10 on 05-31 and
    20 on 06-02, with 06-01 not yet reported."""
    books = one_store()
    books, report, changed = import_batch(books, batch(
        snapshots=[SNAPS['r1']], agreements=[agreement('t1', 4)],
        records=[record('store-1', '2026-05-31', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 10)]),
                 record('store-1', '2026-06-02', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 20)])]))
    assert changed and report['receipts_added'] == 2
    return books


def day(report, store_index, date):
    store = report['stores'][store_index]
    return next(day for day in store['days'] if day['date'] == date)


def entry(bill, service_month):
    return next(entry for entry in bill['entries']
                if entry['service_month'] == service_month)


class MonthlySagaTests(unittest.TestCase):
    """The acceptance scenario: 10 / 14 / 20 at the three midnights, the
    middle one first missing, then late-reported, corrected to 16 before
    the June closing and corrected back to 14 before the July closing."""

    def test_full_saga(self):
        books = saga_books()
        # May reconciliation: only 05-30 is complete (10 renders, 300分);
        # 05-31 misses its right boundary and cannot be smeared from 10->20.
        report = reconcile_month(books, '2026-05')
        first = day(report, 0, '2026-05-30')
        self.assertEqual((first['status'], first['left_revision'],
                          first['right_revision']), ('complete', 0, 1))
        self.assertEqual([(u['count'], u['amount']) for u in first['usage']],
                         [(10, 300)])
        self.assertEqual(day(report, 0, '2026-05-31'),
                         {'date': '2026-05-31', 'status': 'missing',
                          'missing': 'right'})
        books, may_bill, changed = close_month(books, '2026-05')
        self.assertTrue(changed)
        self.assertEqual(may_bill['vendor_totals'],
                         [{'vendor': 'acme', 'amount': 300}])
        current = entry(may_bill, '2026-05')
        self.assertEqual((current['kind'], current['recognized'],
                          current['booked_before'], current['amount']),
                         ('current', 300, 0, 300))
        self.assertEqual([(l['translation_id'], l['review_seq'], l['usage'],
                           l['unit_price'], l['amount'])
                          for l in current['lines']], [('t1', 4, 10, 30, 300)])
        # The frozen basis shows the covered day and the gap.
        basis = may_bill['basis'][0]
        self.assertEqual([d['date'] for d in basis['complete_days']],
                         ['2026-05-30'])
        self.assertEqual(basis['missing_days'],
                         [{'date': '2026-05-31', 'missing': 'right'}])

        # The 06-01 midnight arrives late: May gains 4, June gains 6 in
        # real time; the closed May bill does not move.
        books, _, _ = import_batch(books, batch(records=[
            record('store-1', '2026-06-01', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 14)])]))
        self.assertEqual([u['count'] for u in
                          day(reconcile_month(books, '2026-05'), 0, '2026-05-31')['usage']],
                         [4])
        self.assertEqual([u['count'] for u in
                          day(reconcile_month(books, '2026-06'), 0, '2026-06-01')['usage']],
                         [6])
        self.assertEqual(get_bill(books, '2026-05'), may_bill)

        # Before the June closing the 06-01 midnight is corrected 14 -> 16.
        books, _, _ = import_batch(books, batch(records=[
            record('store-1', '2026-06-01', 2, [dim(SNAPS['r1'], 'app-a', 'greeting', 16)])]))
        books, june_bill, _ = close_month(books, '2026-06')
        adjustment = entry(june_bill, '2026-05')
        self.assertEqual((adjustment['kind'], adjustment['recognized'],
                          adjustment['booked_before'], adjustment['amount']),
                         ('adjustment', 480, 300, 180))
        self.assertEqual([(l['usage'], l['unit_price'], l['amount'])
                          for l in adjustment['lines']], [(16, 30, 480)])
        current = entry(june_bill, '2026-06')
        self.assertEqual((current['kind'], current['recognized'],
                          current['booked_before'], current['amount']),
                         ('current', 120, 0, 120))
        self.assertEqual([l['usage'] for l in current['lines']], [4])

        # The same midnight is corrected back to 14 with a higher revision.
        books, _, _ = import_batch(books, batch(records=[
            record('store-1', '2026-06-01', 3, [dim(SNAPS['r1'], 'app-a', 'greeting', 14)])]))
        books, july_bill, _ = close_month(books, '2026-07')
        offset = entry(july_bill, '2026-05')
        self.assertEqual((offset['kind'], offset['recognized'],
                          offset['booked_before'], offset['amount']),
                         ('offset', 420, 480, -60))
        self.assertEqual([(l['usage'], l['amount'])
                          for l in offset['lines']], [(14, 420)])
        adjustment = entry(july_bill, '2026-06')
        self.assertEqual((adjustment['kind'], adjustment['recognized'],
                          adjustment['booked_before'], adjustment['amount']),
                         ('adjustment', 180, 120, 60))
        self.assertEqual(july_bill['vendor_totals'],
                         [{'vendor': 'acme', 'amount': 0}])
        self.assertEqual(len(july_bill['entries']), 2)  # July itself has no usage

        # The May and June bills stay exactly as they were handed out.
        self.assertEqual(get_bill(books, '2026-05'), may_bill)
        self.assertEqual(get_bill(books, '2026-06'), june_bill)
        # Months that did not change are not adjusted again.
        books, august_bill, _ = close_month(books, '2026-08')
        self.assertEqual(august_bill['entries'], [])
        self.assertEqual(august_bill['vendor_totals'], [])
        # Re-closing returns the original bill without changing state.
        again, replayed, changed = close_month(books, '2026-06')
        self.assertFalse(changed)
        self.assertEqual(replayed, june_bill)
        self.assertEqual(again, books)

    def test_closing_order_is_enforced(self):
        books = saga_books()
        with self.assertRaises(ValueError):
            close_month(books, '2026-07')  # skips May and June
        with self.assertRaises(ValueError):
            close_month(books, '2026-04')  # before the earliest enable month
        books, _, _ = close_month(books, '2026-05')
        with self.assertRaises(ValueError):
            close_month(books, '2026-07')  # skips June
        with self.assertRaises(ValueError):
            get_bill(books, '2026-06')     # not closed yet


class SameDaySnapshotsTests(unittest.TestCase):
    """One business day spanning two snapshots: fallback supply, default
    supply and a re-approval are counted separately by approval and rate."""

    def test_fallback_default_and_reapproval_rates(self):
        books = init_books({'version': 1, 'stores': [
            {'store_id': 'store-2', 'enabled': '2026-06-01'}]})
        books, _, _ = import_batch(books, batch(
            snapshots=[SNAPS['r1'], SNAPS['r3']],
            agreements=[agreement('t1', 4, price=30),
                        agreement('t1', 10, price=50),
                        agreement('t2', 7, price=20)],
            records=[record('store-2', '2026-06-02', 1, [
                dim(SNAPS['r1'], 'app-a', 'greeting', 5),
                dim(SNAPS['r3'], 'app-a', 'greeting', 7),
                dim(SNAPS['r1'], 'app-c', 'settings', 3),
                dim(SNAPS['r3'], 'app-c', 'cart', 4)])]))
        report = reconcile_month(books, '2026-06')
        usage = {u['key'] + '@' + u['snapshot_id'][:4]: u
                 for u in day(report, 0, '2026-06-01')['usage']}
        # fr-CA greeting actually served from fr under (t1, 4) at 30分.
        old = usage['greeting@' + SNAPS['r1']['snapshot_id'][:4]]
        self.assertEqual((old['source_locale'], old['translation_id'],
                          old['review_seq'], old['count'], old['amount']),
                         ('fr', 't1', 4, 5, 150))
        # The re-approved greeting (t1, 10) has its own rate of 50分.
        new = usage['greeting@' + SNAPS['r3']['snapshot_id'][:4]]
        self.assertEqual((new['translation_id'], new['review_seq'],
                          new['count'], new['amount']), ('t1', 10, 7, 350))
        # Default-locale supply records usage but no fee.
        settings = usage['settings@' + SNAPS['r1']['snapshot_id'][:4]]
        self.assertEqual((settings['source_locale'], settings['translation_id'],
                          settings['vendor'], settings['count'],
                          settings['amount']), ('en', None, None, 3, 0))
        # The approved fr translation of cart is the empty string and still
        # counts and bills normally.
        cart = usage['cart@' + SNAPS['r3']['snapshot_id'][:4]]
        self.assertEqual((cart['translation_id'], cart['review_seq'],
                          cart['count'], cart['amount']), ('t2', 7, 4, 80))
        books, june_bill, _ = close_month(books, '2026-06')
        current = entry(june_bill, '2026-06')
        self.assertEqual(current['recognized'], 580)
        self.assertEqual([(l['translation_id'], l['review_seq'], l['usage'],
                           l['unit_price'], l['amount'])
                          for l in current['lines']],
                         [('t1', 4, 5, 30, 150), ('t1', 10, 7, 50, 350),
                          ('t2', 7, 4, 20, 80)])

    def test_vendors_settle_independently(self):
        # Release 2 serves greeting under (t1, 4) and cart under (t2, 7);
        # the two approvals are billed to different vendors.
        books = init_books({'version': 1, 'stores': [
            {'store_id': 'store-1', 'enabled': '2026-05-30'}]})
        books, _, _ = import_batch(books, batch(
            snapshots=[SNAPS['r2']],
            agreements=[agreement('t1', 4, vendor='acme', price=30),
                        agreement('t2', 7, vendor='babel', price=20)],
            records=[record('store-1', '2026-05-31', 1,
                            [dim(SNAPS['r2'], 'app-a', 'greeting', 10),
                             dim(SNAPS['r2'], 'app-c', 'cart', 4)])]))
        books, may_bill, _ = close_month(books, '2026-05')
        self.assertEqual([(e['vendor'], e['amount']) for e in may_bill['entries']],
                         [('acme', 300), ('babel', 80)])
        self.assertEqual(may_bill['vendor_totals'],
                         [{'vendor': 'acme', 'amount': 300},
                          {'vendor': 'babel', 'amount': 80}])
        # A correction that only touches acme's dimension leaves babel out
        # of the next bill entirely.
        books, _, _ = import_batch(books, batch(records=[
            record('store-1', '2026-05-31', 2,
                   [dim(SNAPS['r2'], 'app-a', 'greeting', 12),
                    dim(SNAPS['r2'], 'app-c', 'cart', 4)])]))
        books, june_bill, _ = close_month(books, '2026-06')
        self.assertEqual([(e['service_month'], e['vendor'], e['kind'], e['amount'])
                          for e in june_bill['entries']],
                         [('2026-05', 'acme', 'adjustment', 60)])
        self.assertEqual(june_bill['vendor_totals'],
                         [{'vendor': 'acme', 'amount': 60}])


class RevisionRuleTests(unittest.TestCase):
    def test_equivalent_reupload_adds_no_fact(self):
        books = saga_books()
        again, report, changed = import_batch(books, batch(records=[
            record('store-1', '2026-05-31', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 10)])]))
        self.assertFalse(changed)
        self.assertEqual(report['records_replayed'], 1)
        self.assertEqual(again['receipts'], books['receipts'])
        # An explicit zero is equivalent to an omitted dimension.
        counts = [dim(SNAPS['r1'], 'app-a', 'greeting', 10),
                  dim(SNAPS['r1'], 'app-a', 'settings', 0)]
        again, report, changed = import_batch(books, batch(records=[
            record('store-1', '2026-05-31', 1, counts)]))
        self.assertFalse(changed)
        self.assertEqual(report['records_replayed'], 1)

    def test_conflicting_revision_rejects_even_when_superseded(self):
        books = saga_books()
        books, _, _ = import_batch(books, batch(records=[
            record('store-1', '2026-06-01', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 14)]),
            record('store-1', '2026-06-01', 2, [dim(SNAPS['r1'], 'app-a', 'greeting', 16)])]))
        # Revision 1 was superseded by revision 2, yet re-uploading it with
        # different content rejects the whole batch.
        with self.assertRaises(ValueError):
            import_batch(books, batch(records=[
                record('store-1', '2026-06-01', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 15)])]))
        # The same conflict inside one batch is also rejected.
        with self.assertRaises(ValueError):
            import_batch(books, batch(records=[
                record('store-1', '2026-06-03', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 20)]),
                record('store-1', '2026-06-03', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 21)])]))

    def test_unseen_lower_revision_is_kept_but_does_not_displace(self):
        books = saga_books()
        books, _, _ = import_batch(books, batch(records=[
            record('store-1', '2026-06-01', 2, [dim(SNAPS['r1'], 'app-a', 'greeting', 16)])]))
        before = len(books['receipts'])
        books, report, changed = import_batch(books, batch(records=[
            record('store-1', '2026-06-01', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 14)])]))
        self.assertTrue(changed)
        self.assertEqual(report['receipts_added'], 1)
        self.assertEqual(len(books['receipts']), before + 1)
        late = books['receipts'][-1]
        self.assertEqual((late['date'], late['revision']), ('2026-06-01', 1))
        # The current vector still comes from revision 2.
        usage = day(reconcile_month(books, '2026-05'), 0, '2026-05-31')['usage']
        self.assertEqual([u['count'] for u in usage], [6])  # 16 - 10


class MonotonicityTests(unittest.TestCase):
    def test_joint_correction_of_two_midnights(self):
        books = saga_books()
        books, _, _ = import_batch(books, batch(records=[
            record('store-1', '2026-06-01', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 14)])]))
        # Correcting 05-31 to 15 alone would temporarily exceed the 14 of
        # 06-01; the batch corrects both midnights and is judged by its
        # final merged state.
        books, report, changed = import_batch(books, batch(records=[
            record('store-1', '2026-05-31', 2, [dim(SNAPS['r1'], 'app-a', 'greeting', 15)]),
            record('store-1', '2026-06-01', 2, [dim(SNAPS['r1'], 'app-a', 'greeting', 16)])]))
        self.assertTrue(changed)
        self.assertEqual([u['count'] for u in
                          day(reconcile_month(books, '2026-05'), 0, '2026-05-31')['usage']],
                         [1])

    def test_regression_across_a_gap_is_rejected(self):
        books = saga_books()
        # 05-31 = 10 and 06-02 = 20 with 06-01 missing; a report of 25 for
        # the missing midnight would regress against the 20 of 06-02.
        with self.assertRaises(ValueError):
            import_batch(books, batch(records=[
                record('store-1', '2026-06-01', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 25)])]))
        # A regression inside one batch's final state is rejected as well.
        with self.assertRaises(ValueError):
            import_batch(books, batch(records=[
                record('store-1', '2026-06-01', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 30)]),
                record('store-1', '2026-06-01', 2, [dim(SNAPS['r1'], 'app-a', 'greeting', 5)])]))


class AllOrNothingTests(unittest.TestCase):
    def test_illegal_record_voids_legal_registrations(self):
        books = one_store()
        legal = batch(
            snapshots=[SNAPS['r1'], SNAPS['r3']],
            agreements=[agreement('t1', 4), agreement('t1', 10, price=50)],
            records=[record('store-1', '2026-05-31', 1,
                            [dim(SNAPS['r1'], 'app-a', 'greeting', 10)])])
        # Same batch, plus a record whose dimension has no billing agreement
        # (t2#7 of the r3 cart is not registered): everything is void.
        poisoned = batch(
            snapshots=[SNAPS['r1'], SNAPS['r3']],
            agreements=[agreement('t1', 4), agreement('t1', 10, price=50)],
            records=[record('store-1', '2026-05-31', 1,
                            [dim(SNAPS['r1'], 'app-a', 'greeting', 10)]),
                     record('store-1', '2026-06-01', 1,
                            [dim(SNAPS['r3'], 'app-c', 'cart', 1)])])
        with self.assertRaises(ValueError):
            import_batch(books, poisoned)
        self.assertEqual(books['snapshots'], [])
        self.assertEqual(books['agreements'], [])
        self.assertEqual(books['receipts'], [])
        # The legal batch alone still imports cleanly afterwards.
        books, report, changed = import_batch(books, legal)
        self.assertTrue(changed)
        self.assertEqual((report['snapshots_registered'],
                          report['agreements_registered'],
                          report['receipts_added']), (2, 2, 1))


class GapSemanticsTests(unittest.TestCase):
    def test_spanning_difference_proves_no_middle_day(self):
        books = saga_books()  # 10 on 05-31, 20 on 06-02, 06-01 missing
        report = reconcile_month(books, '2026-06')
        self.assertEqual(day(report, 0, '2026-06-01'),
                         {'date': '2026-06-01', 'status': 'missing',
                          'missing': 'left'})
        # Even a zero spanning difference fills nothing: replace 06-02 with
        # a vector equal to 05-31; the days in between stay missing.
        books = one_store()
        books, _, _ = import_batch(books, batch(
            snapshots=[SNAPS['r1']], agreements=[agreement('t1', 4)],
            records=[record('store-1', '2026-05-31', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 10)]),
                     record('store-1', '2026-06-03', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 10)])]))
        report = reconcile_month(books, '2026-06')
        self.assertEqual(day(report, 0, '2026-06-01')['missing'], 'both')
        self.assertEqual(day(report, 0, '2026-06-02')['missing'], 'left')
        books, _, _ = close_month(books, '2026-05')
        books, june_bill, _ = close_month(books, '2026-06')
        self.assertEqual(june_bill['entries'], [])
        basis = next(row for row in june_bill['basis'] if row['month'] == '2026-06')
        self.assertEqual(basis['complete_days'], [])

    def test_one_late_report_completes_two_adjacent_days(self):
        books = saga_books()
        books, _, _ = import_batch(books, batch(records=[
            record('store-1', '2026-06-01', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 14)])]))
        self.assertEqual(day(reconcile_month(books, '2026-05'), 0, '2026-05-31')['status'],
                         'complete')
        self.assertEqual(day(reconcile_month(books, '2026-06'), 0, '2026-06-01')['status'],
                         'complete')

    def test_missing_endpoint_names(self):
        books = one_store()
        report = reconcile_month(books, '2026-05')
        # Enable day: the left boundary is the fixed zero vector.
        self.assertEqual(day(report, 0, '2026-05-30')['missing'], 'right')
        self.assertEqual(day(report, 0, '2026-05-31')['missing'], 'both')


class RegistrationTests(unittest.TestCase):
    def test_snapshot_replay_uses_only_snapshot_id_content(self):
        books = saga_books()
        # Re-registering the same snapshot is a no-op, even with a different
        # query header: only the content bound by snapshot_id participates.
        restamped = dict(SNAPS['r1'], query_id='q9', at=99, known_seq=99)
        again, report, changed = import_batch(books, batch(snapshots=[restamped]))
        self.assertFalse(changed)
        self.assertEqual(report['snapshots_replayed'], 1)
        self.assertEqual(again['snapshots'], books['snapshots'])

    def test_agreement_replay_and_immutable_terms(self):
        books = saga_books()
        again, report, changed = import_batch(
            books, batch(agreements=[agreement('t1', 4)]))
        self.assertFalse(changed)
        self.assertEqual(report['agreements_replayed'], 1)
        with self.assertRaises(ValueError):
            import_batch(books, batch(agreements=[agreement('t1', 4, price=31)]))
        with self.assertRaises(ValueError):
            import_batch(books, batch(agreements=[agreement('t1', 4, vendor='other')]))
        # A new approval of the same translation gets its own rate.
        books, report, changed = import_batch(
            books, batch(agreements=[agreement('t1', 10, price=50)]))
        self.assertTrue(changed)
        self.assertEqual(report['agreements_registered'], 1)

    def test_zero_priced_agreement_must_be_registered(self):
        books = one_store()
        with self.assertRaises(ValueError):
            import_batch(books, batch(
                snapshots=[SNAPS['r1']],
                records=[record('store-1', '2026-05-31', 1,
                                [dim(SNAPS['r1'], 'app-a', 'greeting', 1)])]))
        books, _, _ = import_batch(books, batch(
            snapshots=[SNAPS['r1']], agreements=[agreement('t1', 4, price=0)],
            records=[record('store-1', '2026-05-31', 1,
                            [dim(SNAPS['r1'], 'app-a', 'greeting', 1)])]))
        books, may_bill, _ = close_month(books, '2026-05')
        self.assertEqual(may_bill['entries'], [])  # zero price books nothing
        usage = may_bill['basis'][0]['complete_days'][0]['usage']
        self.assertEqual([(u['count'], u['amount']) for u in usage], [(1, 0)])


class RecordValidationTests(unittest.TestCase):
    def setUp(self):
        self.books = one_store()
        self.books, _, _ = import_batch(self.books, batch(
            snapshots=[SNAPS['r1']], agreements=[agreement('t1', 4)]))

    def reject(self, rec):
        with self.assertRaises(ValueError):
            import_batch(self.books, batch(records=[rec]))

    def test_record_validation(self):
        good = dim(SNAPS['r1'], 'app-a', 'greeting', 1)
        # Dates must be after the enable date.
        self.reject(record('store-1', '2026-05-30', 1, [good]))
        self.reject(record('store-1', '2026-05-29', 1, [good]))
        self.reject(record('store-1', 'not-a-date', 1, [good]))
        # Unknown store, snapshot, application, locale and key.
        self.reject(record('store-9', '2026-05-31', 1, [good]))
        self.reject(record('store-1', '2026-05-31', 1,
                           [dict(good, snapshot_id='0' * 64)]))
        self.reject(record('store-1', '2026-05-31', 1,
                           [dict(good, application_id='app-z')]))
        self.reject(record('store-1', '2026-05-31', 1, [dict(good, locale='fr')]))
        self.reject(record('store-1', '2026-05-31', 1, [dict(good, key='nope')]))
        # Bad counts, bad revisions, duplicate dimensions.
        self.reject(record('store-1', '2026-05-31', 1,
                           [dict(good, count=-1)]))
        self.reject(record('store-1', '2026-05-31', 0, [good]))
        self.reject(record('store-1', '2026-05-31', 1, [good, good]))
        # A default-locale dimension needs no agreement but the entry must
        # exist in the snapshot; app-c/settings is default-served in r1.
        self.reject(record('store-1', '2026-05-31', 1,
                           [dim(SNAPS['r1'], 'app-c', 'cart', 1)]))

    def test_default_supply_records_usage_without_a_fee(self):
        books, _, _ = import_batch(self.books, batch(records=[
            record('store-1', '2026-05-31', 1,
                   [dim(SNAPS['r1'], 'app-c', 'settings', 3)])]))
        usage = day(reconcile_month(books, '2026-05'), 0, '2026-05-30')['usage']
        self.assertEqual([(u['source_locale'], u['vendor'], u['amount'])
                          for u in usage], [('en', None, 0)])


class StateIntegrityTests(unittest.TestCase):
    def test_produced_state_is_readable_and_tampering_is_caught(self):
        books = saga_books()
        books, _, _ = close_month(books, '2026-05')
        books, _, _ = import_batch(books, batch(records=[
            record('store-1', '2026-06-01', 1, [dim(SNAPS['r1'], 'app-a', 'greeting', 14)])]))
        books, _, _ = close_month(books, '2026-06')
        # JSON round-trip: the state we produce reads back identically.
        reread = validate_books(json.loads(json.dumps(books)))
        self.assertEqual(reread, books)

        def tamper(path, value):
            copy = json.loads(json.dumps(books))
            cursor = copy
            for key in path[:-1]:
                cursor = cursor[key]
            cursor[path[-1]] = value
            with self.assertRaises(ValueError):
                validate_books(copy)

        tamper(['receipts', 0, 'counts', 0, 'count'], 11)      # frozen basis breaks
        tamper(['bills', 0, 'entries', 0, 'amount'], 301)      # entry arithmetic
        tamper(['bills', 0, 'vendor_totals', 0, 'amount'], 0)  # totals
        tamper(['bills', 1, 'receipts_through'], 1)            # horizon
        tamper(['agreements', 0, 'unit_price'], 31)            # immutable terms
        tamper(['receipts', 0, 'revision'], 9)                 # pinned revision

    def test_corrupt_books_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_books({'version': 2, 'stores': [], 'snapshots': [],
                            'agreements': [], 'receipts': [], 'bills': []})
        with self.assertRaises(ValueError):
            init_books({'version': 1, 'stores': []})
        with self.assertRaises(ValueError):
            init_books({'version': 1, 'stores': [
                {'store_id': 'a', 'enabled': '2026-05-30'},
                {'store_id': 'a', 'enabled': '2026-06-01'}]})


if __name__ == '__main__':
    unittest.main()
