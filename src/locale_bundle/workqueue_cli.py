import argparse
import json
from pathlib import Path
import sys
from .common import read_json, publish
from .workqueue import sync_ledger, assign_task, confirm_task, query_ledger, validate_ledger


def main():
    parser = argparse.ArgumentParser(description='离线派工台账')
    commands = parser.add_subparsers(dest='command', required=True)
    sync = commands.add_parser('sync', help='按选定查询截止同步台账')
    sync.add_argument('--source', type=Path, required=True)
    sync.add_argument('--ledger', type=Path, required=True)
    sync.add_argument('--query', required=True)
    assign = commands.add_parser('assign', help='分派任务给译者')
    assign.add_argument('--ledger', type=Path, required=True)
    assign.add_argument('--locale', required=True)
    assign.add_argument('--key', required=True)
    assign.add_argument('--source-id', required=True)
    assign.add_argument('--translator', required=True)
    confirm = commands.add_parser('confirm', help='负责人确认任务完成')
    confirm.add_argument('--ledger', type=Path, required=True)
    confirm.add_argument('--locale', required=True)
    confirm.add_argument('--key', required=True)
    confirm.add_argument('--source-id', required=True)
    confirm.add_argument('--confirmer', required=True)
    confirm.add_argument('--translation-id', required=True)
    confirm.add_argument('--review-seq', type=int, required=True)
    query = commands.add_parser('query', help='查询台账')
    query.add_argument('--ledger', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'sync':
            if args.source.resolve() == args.ledger.resolve():
                raise ValueError('source and ledger must differ')
            data = read_json(args.source)
            ledger = validate_ledger(read_json(args.ledger)) if args.ledger.exists() else None
            updated = sync_ledger(ledger, data, args.query)
            if updated != ledger:
                publish(args.ledger, updated)
            print('synced ' + str(args.ledger))
        elif args.command == 'assign':
            ledger = validate_ledger(read_json(args.ledger))
            updated = assign_task(ledger, args.locale, args.key, args.source_id,
                                  args.translator)
            if updated != ledger:
                publish(args.ledger, updated)
            print('assigned %s/%s/%s to %s' % (args.locale, args.key, args.source_id,
                                               args.translator))
        elif args.command == 'confirm':
            ledger = validate_ledger(read_json(args.ledger))
            updated = confirm_task(ledger, args.locale, args.key, args.source_id,
                                   args.confirmer, args.translation_id, args.review_seq)
            if updated != ledger:
                publish(args.ledger, updated)
            print('confirmed %s/%s/%s by %s' % (args.locale, args.key, args.source_id,
                                                args.confirmer))
        else:
            ledger = validate_ledger(read_json(args.ledger))
            json.dump(query_ledger(ledger), sys.stdout, ensure_ascii=False,
                      sort_keys=True, indent=2)
            sys.stdout.write('\n')
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
