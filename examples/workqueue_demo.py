#!/usr/bin/env python3
"""Offline demo of the locale-bundle-work-queue dispatch ledger.

Runs the linked acceptance scenario against a throwaway ledger under
run/workqueue/ using only the local fixture and the CLI; no network, no
services. Run from the repository root with the project environment:

    .venv/bin/python examples/workqueue_demo.py
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures/workqueue_input.json'
WORK = ROOT / 'run' / 'workqueue'
LEDGER = WORK / 'ledger.json'
SOURCE = WORK / 'input.json'
APP_A = {'id': 'app-a', 'roots': ['checkout', 'catalog']}
APP_B = {'id': 'app-b', 'roots': ['b-ui']}


def cli(*args, expect=0):
    result = subprocess.run([sys.executable, '-m', 'locale_bundle.workqueue_cli', *args],
                            capture_output=True, text=True, cwd=ROOT)
    if result.returncode != expect:
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        raise SystemExit('step failed: %s (exit %s, wanted %s)'
                         % (' '.join(args), result.returncode, expect))
    return result


def sync(applications, query):
    data = json.loads(FIXTURE.read_text())
    data['applications'] = applications
    SOURCE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    cli('sync', '--source', str(SOURCE), '--ledger', str(LEDGER), '--query', query)


def show(label):
    view = json.loads(cli('query', '--ledger', str(LEDGER)).stdout)
    print('== ' + label)
    for task in view['tasks']:
        print('   %(locale)s/%(key)s/%(source_id)s %(status)-15s %(state)s' % {
            'locale': task['locale'], 'key': task['key'], 'source_id': task['source_id'],
            'status': task['status'] + ('' if task['in_scope'] else ' (out of scope)'),
            'state': task['review_state']})
        print('      apps=%s assignee=%s approval=%s confirmation=%s'
              % (task['applications'], task['assignee'], task['approval'],
                 task['confirmation']))
    print('   last_sync=%s' % (view['last_sync'],))


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)

    sync([APP_A], 'q1')
    show('1. sync q1, app-a only: one open task for fr/greeting/s1')
    cli('assign', '--ledger', str(LEDGER), '--locale', 'fr', '--key', 'greeting',
        '--source-id', 's1', '--translator', 'alice')
    show('2. assigned to alice')

    sync([APP_A, APP_B], 'q1')
    show('3. same cutoff, app-b joins: still one task, apps=[app-a, app-b]')
    before = LEDGER.read_bytes()
    sync([APP_A, APP_B], 'q1')
    assert LEDGER.read_bytes() == before, 'equivalent rerun must not change the ledger'
    print('   equivalent rerun left the ledger byte-identical')

    sync([APP_A, APP_B], 'q2')
    show('4. sync q2: translation approved, task awaits confirmation')
    cli('confirm', '--ledger', str(LEDGER), '--locale', 'fr', '--key', 'greeting',
        '--source-id', 's1', '--confirmer', 'lead',
        '--translation-id', 't1', '--review-seq', '3')
    show('5. confirmed by lead with evidence (t1, 3)')

    sync([APP_A, APP_B], 'q3')
    show('6. sync q3: approval revoked, same task reopens, old confirm kept')

    sync([], 'q3')
    show('7. both applications leave: task frozen out of scope')
    sync([APP_A], 'q3')
    show('8. app-a rejoins: same task, assignment preserved')

    sync([APP_A], 'q5')
    show('9. sync q5: s2 replaces s1; old task archived, new unassigned task')

    cli('assign', '--ledger', str(LEDGER), '--locale', 'fr', '--key', 'greeting',
        '--source-id', 's1', '--translator', 'bob', expect=2)
    print('10. assign on archived s1 rejected with exit 2, ledger preserved')
    print('demo OK')


if __name__ == '__main__':
    main()
