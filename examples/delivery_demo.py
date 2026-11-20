#!/usr/bin/env python3
"""Offline demo of the locale-bundle-delivery flow (contract v6).

Reproduces the synthetic acceptance scenario with throwaway files under
run/delivery/ using only the local CLI; no network, no services. Run from
the repository root with the project environment:

    .venv/bin/python examples/delivery_demo.py

Scenario:
  * one approved fr translation (t1, review_seq 4) falls back into the two
    fr-CA packages app-a/app-b, while app-c is independent;
  * the store initializes from the release 1 snapshot and installs the
    release 2 delta (key cart appears, served by an approved empty fr
    translation), then the withdrawal notice for (t1, 4) arrives;
  * app-a/app-b become unreadable immediately, app-c keeps serving, and
    rollback to release 1 is refused because it carries the same approval;
  * a delta that only touches unrelated keys cannot install, while the
    delta that moves both packages to the re-approved evidence (t1, 10)
    installs even though the greeting text is literally unchanged;
  * release 4 deletes settings and adds promo in one release; after it
    installs, rollback to release 3 succeeds, and re-installing release 4
    from the exact v3 baseline fails because 4 is not newer any more.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'run' / 'delivery'
STORE = WORK / 'store.json'
FIXTURE = ROOT / 'fixtures' / 'delivery_input.json'

NOTICE = {'version': 1, 'notice_id': 'n1',
          'withdrawn': [{'translation_id': 't1', 'review_seq': 4}]}


def cli(*args, expect=0):
    result = subprocess.run(
        [sys.executable, '-m', 'locale_bundle.delivery_cli', *args],
        capture_output=True, text=True, cwd=ROOT)
    if result.returncode != expect:
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        raise SystemExit('step failed: %s (exit %s, wanted %s)'
                         % (' '.join(args), result.returncode, expect))
    return result


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def build(source, query, release, name):
    cli('build', '--source', str(source), '--output', str(WORK / name),
        '--query', query, '--release', str(release))
    return load(WORK / name)


def delta(baseline, target, name):
    cli('delta', '--baseline', str(WORK / baseline),
        '--target', str(WORK / target), '--output', str(WORK / name))
    return load(WORK / name)


def install(name, expect=0):
    return cli('install', '--store', str(STORE), '--delta', str(WORK / name),
               expect=expect)


def status():
    return json.loads(cli('status', '--store', str(STORE)).stdout)


def readability(report):
    return {p['application_id']: ('readable' if p['readable'] else 'BLOCKED')
            for p in report['packages']}


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    base = load(FIXTURE)
    save(WORK / 'input.json', base)
    v4_input = json.loads(json.dumps(base))
    for component in v4_input['components']:
        if component['id'] == 'office':
            component['keys'] = ['cart', 'promo']
    save(WORK / 'input_v4.json', v4_input)
    save(WORK / 'notice.json', NOTICE)

    print('== headquarters: build the release line from the fact log')
    snaps = {}
    snaps[1] = build(WORK / 'input.json', 'q1', 1, 'snap1.json')
    snaps[2] = build(WORK / 'input.json', 'q2', 2, 'snap2.json')
    build(WORK / 'input.json', 'q3-alt', 3, 'snap3alt.json')
    snaps[3] = build(WORK / 'input.json', 'q3', 3, 'snap3.json')
    snaps[4] = build(WORK / 'input_v4.json', 'q4', 4, 'snap4.json')
    for release, snapshot in snaps.items():
        print('   release %d: %s' % (release, snapshot['snapshot_id'][:12]))
    delta('snap1.json', 'snap2.json', 'delta12.json')
    delta('snap2.json', 'snap3alt.json', 'delta23alt.json')
    d23 = delta('snap2.json', 'snap3.json', 'delta23.json')
    d34 = delta('snap3.json', 'snap4.json', 'delta34.json')

    print('1. store: init at release 1, install the release 2 delta')
    cli('init', '--store', str(STORE), '--snapshot', str(WORK / 'snap1.json'))
    install('delta12.json')
    report = status()
    print('   current=%s highest=%s %s'
          % (report['current']['release'], report['highest_release'],
             readability(report)))

    print('2. withdrawal notice for (t1, 4) arrives after the install')
    cli('notice', '--store', str(STORE), '--notice', str(WORK / 'notice.json'))
    report = status()
    print('   %s rollback_allowed=%s'
          % (readability(report), report['rollback_allowed']))
    refused = cli('fetch', '--store', str(STORE), '--application-id', 'app-a',
                  '--locale', 'fr-CA', expect=2)
    print('   fetch app-a: exit 2 (%s)' % refused.stderr.strip())
    served = json.loads(cli('fetch', '--store', str(STORE),
                            '--application-id', 'app-c',
                            '--locale', 'fr-CA').stdout)
    print('   fetch app-c: keys=%s' % [m['key'] for m in served['messages']])
    cli('rollback', '--store', str(STORE), expect=2)
    print('   rollback refused: release 1 still serves the withdrawn approval')

    print('3. an unrelated-changes-only delta cannot install')
    before = STORE.read_bytes()
    install('delta23alt.json', expect=2)
    assert STORE.read_bytes() == before
    print('   release 3 candidate with only the settings edit: refused,',
          'store untouched')

    print('4. the re-approval delta installs (same text, new evidence)')
    upsert = next(c['keys_upserted'][0] for c in d23['packages_changed']
                  if c['application_id'] == 'app-a')
    print('   greeting value=%r review_seq=%s'
          % (upsert['value'], upsert['review_seq']))
    install('delta23.json')
    report = status()
    print('   %s rollback_allowed=%s (previous is the stopped release 2)'
          % (readability(report), report['rollback_allowed']))
    cli('rollback', '--store', str(STORE), expect=2)

    print('5. release 4 deletes settings and adds promo in one release')
    change = d34['packages_changed'][0]
    print('   app-c: removed=%s added=%s (cart inherited, not resent)'
          % (change['keys_removed'],
             [m['key'] for m in change['keys_upserted']]))
    install('delta34.json')
    served = json.loads(cli('fetch', '--store', str(STORE),
                            '--application-id', 'app-c',
                            '--locale', 'fr-CA').stdout)
    print('   app-c now serves keys=%s' % [m['key'] for m in served['messages']])

    print('6. rollback to release 3, then release 4 is no longer newer')
    cli('rollback', '--store', str(STORE))
    report = status()
    print('   current=%s highest=%s previous=%s'
          % (report['current']['release'], report['highest_release'],
             report['previous']))
    refused = install('delta34.json', expect=2)
    print('   reinstall release 4: exit 2 (%s)' % refused.stderr.strip())
    print('demo OK')


if __name__ == '__main__':
    main()
