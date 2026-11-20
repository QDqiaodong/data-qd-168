"""CLI for offline usage reconciliation and settlement (contract v7).

Five subcommands: ``init`` (create the books from the store registry),
``import`` (fold one batch of snapshots, billing agreements and counter
records into the books), ``reconcile`` (real-time per-day status of one
month), ``close`` (close one service month in order, or read back the
original bill when it is already closed) and ``bill`` (read back one closed
bill). Every mutation validates the next state completely and then replaces
the single books file atomically; failures exit 2 and leave the old file
and every existing output untouched.
"""
import argparse
import json
from pathlib import Path
import sys

from .common import read_json, publish
from .settlement import (SettlementError, init_books, import_batch,
                         reconcile_month, close_month, get_bill)
from .vendor import serialized_size


def _distinct_paths(paths):
    resolved = [Path(path).resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise SettlementError('input and output paths must not coincide')


def _bounded_publish(path, value):
    if serialized_size(value) > 8_000_000:
        raise SettlementError('output exceeds 8MB')
    publish(path, value)


def _print(value):
    json.dump(value, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write('\n')


def main():
    parser = argparse.ArgumentParser(description='离线用量对账与结算')
    commands = parser.add_subparsers(dest='command', required=True)

    init = commands.add_parser('init', help='总部：登记门店及启用计数日期，创建账簿')
    init.add_argument('--books', type=Path, required=True)
    init.add_argument('--stores', type=Path, required=True)

    load = commands.add_parser('import', help='总部：导入一批快照、计费约定与计数记录')
    load.add_argument('--books', type=Path, required=True)
    load.add_argument('--batch', type=Path, required=True)

    reconcile = commands.add_parser('reconcile', help='总部：查询指定月份的逐日对账状态')
    reconcile.add_argument('--books', type=Path, required=True)
    reconcile.add_argument('--month', required=True)
    reconcile.add_argument('--store')

    close = commands.add_parser('close', help='财务：按顺序封存一个服务月份')
    close.add_argument('--books', type=Path, required=True)
    close.add_argument('--month', required=True)

    bill = commands.add_parser('bill', help='财务：读回某月已封账单')
    bill.add_argument('--books', type=Path, required=True)
    bill.add_argument('--month', required=True)

    args = parser.parse_args()
    try:
        if args.command == 'init':
            _distinct_paths([args.books, args.stores])
            if args.books.exists() or args.books.is_symlink():
                raise SettlementError('books already exist: ' + str(args.books))
            books = init_books(read_json(args.stores))
            _bounded_publish(args.books, books)
            print('initialized books with %d store(s)' % len(books['stores']))
        elif args.command == 'import':
            _distinct_paths([args.books, args.batch])
            books, report, changed = import_batch(read_json(args.books),
                                                  read_json(args.batch))
            if changed:
                _bounded_publish(args.books, books)
            _print(report)
        elif args.command == 'reconcile':
            _print(reconcile_month(read_json(args.books), args.month,
                                   args.store))
        elif args.command == 'close':
            books, bill, changed = close_month(read_json(args.books), args.month)
            if changed:
                _bounded_publish(args.books, books)
            _print(bill)
        else:
            _print(get_bill(read_json(args.books), args.month))
    except (OSError, SettlementError, ValueError, TypeError, KeyError,
            OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
