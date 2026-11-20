#!/usr/bin/env python3
"""Offline demo of the locale-bundle-settlement flow (contract v7).

Reproduces the synthetic acceptance scenario with throwaway files under
run/settlement/ using only the local CLIs; no network, no services. Run
from the repository root with the project environment:

    .venv/bin/python examples/settlement_demo.py

Scenario:
  * store-1 starts counting on 2026-05-30; one billable dimension (the
    fr-CA greeting of app-a, actually served from fr under (t1, 4) at
    30分/render) reads 10 at 05-31, 14 at 06-01 and 20 at 06-02, but the
    06-01 midnight is missing at first;
  * the May closing therefore confirms only 05-30's 10 renders and cannot
    derive the other two days from the 10 -> 20 span;
  * the 06-01 midnight is late-reported as 14 (May +4, June +6 in real
    time), then corrected to 16 before the June closing: the June bill
    books May's remaining 6 renders and June's 4 renders;
  * the same midnight is corrected back to 14 with a higher revision: the
    July bill offsets 2 May renders and books 2 June renders, while the
    original May and June bills stay untouched;
  * store-2 shows one business day spanning two snapshots: fallback supply
    (fr-CA from fr), default-locale supply (no fee) and a re-approval with
    its own rate are counted separately.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'run' / 'settlement'
BOOKS = WORK / 'books.json'
FIXTURE = ROOT / 'fixtures' / 'delivery_input.json'

SNAP = {}


def cli(module, *args, expect=0):
    result = subprocess.run([sys.executable, '-m', module, *args],
                            capture_output=True, text=True, cwd=ROOT)
    if result.returncode != expect:
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        raise SystemExit('step failed: %s (exit %s, wanted %s)'
                         % (' '.join(args), result.returncode, expect))
    return result


def settlement(*args, expect=0):
    return cli('locale_bundle.settlement_cli', *args, expect=expect)


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def dim(release, application_id, key, count):
    return {'snapshot_id': SNAP[release]['snapshot_id'],
            'application_id': application_id, 'locale': 'fr-CA',
            'key': key, 'count': count}


def record(store_id, date, revision, counts):
    return {'store_id': store_id, 'date': date, 'revision': revision,
            'counts': counts}


def agreement(translation_id, review_seq, price):
    return {'translation_id': translation_id, 'review_seq': review_seq,
            'vendor': 'acme', 'unit_price': price}


def import_batch(name, snapshots=(), agreements=(), records=()):
    save(WORK / name, {'version': 1, 'snapshots': list(snapshots),
                       'agreements': list(agreements),
                       'records': list(records)})
    report = json.loads(settlement('import', '--books', str(BOOKS),
                                   '--batch', str(WORK / name)).stdout)
    print('   import %s: %s' % (name, report))
    return report


def reconcile(month, store_id):
    report = json.loads(settlement('reconcile', '--books', str(BOOKS),
                                   '--month', month,
                                   '--store', store_id).stdout)
    store = report['stores'][0]
    pending = None  # (first date, last date, endpoint) of a missing run

    def flush():
        if pending is not None:
            first, last, endpoint = pending
            span = first if first == last else '%s..%s' % (first, last)
            print('   %s %s: MISSING %s endpoint' % (store_id, span, endpoint))

    for day in store['days']:
        if day['status'] == 'missing':
            if pending is None or day['missing'] != pending[2]:
                flush()
                pending = (day['date'], day['date'], day['missing'])
            else:
                pending = (pending[0], day['date'], pending[2])
            continue
        flush()
        pending = None
        usage = ', '.join('%s x%d -> %s分' % (u['key'], u['count'], u['amount'])
                          for u in day['usage']) or '(no renders)'
        print('   %s %s: complete r%d->r%d  %s'
              % (store_id, day['date'], day['left_revision'],
                 day['right_revision'], usage))
    flush()


def close(month):
    bill = json.loads(settlement('close', '--books', str(BOOKS),
                                 '--month', month).stdout)
    print('   close %s: vendor totals %s' % (month, bill['vendor_totals']))
    for entry in bill['entries']:
        print('     %s %-10s %s: recognized=%d booked_before=%d amount=%+d'
              % (entry['service_month'], entry['kind'], entry['vendor'],
                 entry['recognized'], entry['booked_before'], entry['amount']))
        for line in entry['lines']:
            print('       %s#%d  usage=%d x %d分 = %d分'
                  % (line['translation_id'], line['review_seq'],
                     line['usage'], line['unit_price'], line['amount']))
    return bill


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)

    print('== headquarters: build the release line from the fact log')
    for query, release in (('q1', 1), ('q2', 2), ('q3', 3)):
        cli('locale_bundle.delivery_cli', 'build', '--source', str(FIXTURE),
            '--output', str(WORK / ('snap%d.json' % release)),
            '--query', query, '--release', str(release))
        SNAP[release] = load(WORK / ('snap%d.json' % release))
        print('   release %d: %s' % (release, SNAP[release]['snapshot_id'][:12]))

    print('0. init: register the stores and their enable dates')
    save(WORK / 'stores.json', {'version': 1, 'stores': [
        {'store_id': 'store-1', 'enabled': '2026-05-30'},
        {'store_id': 'store-2', 'enabled': '2026-06-01'}]})
    settlement('init', '--books', str(BOOKS), '--stores',
               str(WORK / 'stores.json'))

    print('1. store-1 reports 10 at 05-31 and 20 at 06-02; 06-01 is missing')
    import_batch('batch1.json', snapshots=[SNAP[1]],
                 agreements=[agreement('t1', 4, 30)],
                 records=[record('store-1', '2026-05-31', 1, [dim(1, 'app-a', 'greeting', 10)]),
                          record('store-1', '2026-06-02', 1, [dim(1, 'app-a', 'greeting', 20)])])
    print('2. reconcile May: only 05-30 is complete; the 10 -> 20 span'
          ' proves nothing about 05-31')
    reconcile('2026-05', 'store-1')
    print('3. close May: only the 10 renders of 05-30 are booked')
    close('2026-05')

    print('4. late report: 06-01 was 14; store-2 sends its first vector,')
    print('   spanning release 1 and release 3 on one business day')
    import_batch('batch2.json', snapshots=[SNAP[2], SNAP[3]],
                 agreements=[agreement('t1', 10, 50), agreement('t2', 7, 20)],
                 records=[record('store-1', '2026-06-01', 1, [dim(1, 'app-a', 'greeting', 14)]),
                          record('store-2', '2026-06-02', 1,
                                 [dim(1, 'app-a', 'greeting', 5),
                                  dim(3, 'app-a', 'greeting', 7),
                                  dim(1, 'app-c', 'settings', 3),
                                  dim(3, 'app-c', 'cart', 4)])])
    print('5. real-time reconciliation: May +4, June +6 for store-1;')
    print('   store-2 counts fallback, default and re-approved supply apart')
    reconcile('2026-05', 'store-1')
    reconcile('2026-06', 'store-1')
    reconcile('2026-06', 'store-2')

    print('6. before the June closing, 06-01 is corrected 14 -> 16 (rev 2)')
    import_batch('batch3.json',
                 records=[record('store-1', '2026-06-01', 2, [dim(1, 'app-a', 'greeting', 16)])])
    print('7. close June: May gets its remaining 6 renders, June books 4')
    june_bill = close('2026-06')

    print('8. 06-01 is corrected back to 14 (rev 3)')
    import_batch('batch4.json',
                 records=[record('store-1', '2026-06-01', 3, [dim(1, 'app-a', 'greeting', 14)])])
    print('9. close July: May is offset by 2 renders, June gets 2 more')
    close('2026-07')

    print('10. the handed-out May and June bills read back unchanged')
    may_bill = json.loads(settlement('bill', '--books', str(BOOKS),
                                     '--month', '2026-05').stdout)
    print('    May bill totals: %s (basis: %d complete day(s), gap %s)'
          % (may_bill['vendor_totals'],
             len(may_bill['basis'][0]['complete_days']),
             may_bill['basis'][0]['missing_days']))
    reread = json.loads(settlement('bill', '--books', str(BOOKS),
                                   '--month', '2026-06').stdout)
    assert reread == june_bill
    print('    June bill reads back exactly as closed')

    print('11. nothing changed since July: August books no differences')
    close('2026-08')

    print('12. a revision conflict rejects the whole batch, state untouched')
    before = BOOKS.read_bytes()
    save(WORK / 'conflict.json',
         {'version': 1, 'snapshots': [], 'agreements': [],
          'records': [record('store-1', '2026-06-01', 3,
                             [dim(1, 'app-a', 'greeting', 15)])]})
    refused = settlement('import', '--books', str(BOOKS),
                         '--batch', str(WORK / 'conflict.json'), expect=2)
    assert BOOKS.read_bytes() == before
    print('    exit 2 (%s)' % refused.stderr.strip())
    print('demo OK')


if __name__ == '__main__':
    main()
