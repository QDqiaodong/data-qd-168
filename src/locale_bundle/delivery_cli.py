"""CLI for offline release delivery (contract v6).

Headquarters commands: ``build`` (fact log + component graph -> snapshot)
and ``delta`` (two snapshots -> one delta file). Store commands: ``init``
(first full snapshot), ``install`` (one delta), ``notice`` (one withdrawal
notice), ``rollback`` (back to the retained previous snapshot), ``status``
and ``fetch`` (read-only queries). Every store mutation validates fully and
then replaces the single store file atomically; failures exit 2 and leave
the old file and every existing output untouched.
"""
import argparse
import json
from pathlib import Path
import sys

from .common import read_json, publish
from .delivery import (DeliveryError, build_snapshot, build_delta, init_store,
                       install_delta, receive_notice, rollback_store,
                       store_status, fetch_package)
from .vendor import serialized_size


def _distinct_paths(paths):
    resolved = [Path(path).resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise DeliveryError('input and output paths must not coincide')


def _bounded_publish(path, value):
    if serialized_size(value) > 8_000_000:
        raise DeliveryError('output exceeds 8MB')
    publish(path, value)


def _print(value):
    json.dump(value, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write('\n')


def main():
    parser = argparse.ArgumentParser(description='离线发布交付')
    commands = parser.add_subparsers(dest='command', required=True)

    build = commands.add_parser('build', help='总部：从审核历史与组件图构建发布快照')
    build.add_argument('--source', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    build.add_argument('--query', required=True)
    build.add_argument('--release', type=int, required=True)

    delta = commands.add_parser('delta', help='总部：从基线与目标快照生成增量文件')
    delta.add_argument('--baseline', type=Path, required=True)
    delta.add_argument('--target', type=Path, required=True)
    delta.add_argument('--output', type=Path, required=True)

    init = commands.add_parser('init', help='门店：从完整快照初始化')
    init.add_argument('--store', type=Path, required=True)
    init.add_argument('--snapshot', type=Path, required=True)

    install = commands.add_parser('install', help='门店：以当前快照为基线安装增量')
    install.add_argument('--store', type=Path, required=True)
    install.add_argument('--delta', type=Path, required=True)

    notice = commands.add_parser('notice', help='门店：接收撤回通知')
    notice.add_argument('--store', type=Path, required=True)
    notice.add_argument('--notice', type=Path, required=True)

    rollback = commands.add_parser('rollback', help='门店：回退到保留的上一套快照')
    rollback.add_argument('--store', type=Path, required=True)

    status = commands.add_parser('status', help='门店：查询当前身份与包可读性')
    status.add_argument('--store', type=Path, required=True)

    fetch = commands.add_parser('fetch', help='门店：读取单个当前包')
    fetch.add_argument('--store', type=Path, required=True)
    fetch.add_argument('--application-id', required=True)
    fetch.add_argument('--locale', required=True)

    args = parser.parse_args()
    try:
        if args.command == 'build':
            _distinct_paths([args.source, args.output])
            snapshot = build_snapshot(read_json(args.source), args.query,
                                      args.release)
            _bounded_publish(args.output, snapshot)
            print('built release %d snapshot %s'
                  % (snapshot['release'], snapshot['snapshot_id'][:12]))
        elif args.command == 'delta':
            _distinct_paths([args.baseline, args.target, args.output])
            delta = build_delta(read_json(args.baseline), read_json(args.target))
            _bounded_publish(args.output, delta)
            print('wrote delta -> release %d' % delta['target_release'])
        elif args.command == 'init':
            _distinct_paths([args.store, args.snapshot])
            if args.store.exists() or args.store.is_symlink():
                raise DeliveryError('store already exists: ' + str(args.store))
            store = init_store(read_json(args.snapshot))
            _bounded_publish(args.store, store)
            print('initialized store at release %d' % store['highest_release'])
        elif args.command == 'install':
            _distinct_paths([args.store, args.delta])
            store = install_delta(read_json(args.store), read_json(args.delta))
            _bounded_publish(args.store, store)
            print('installed release %d' % store['highest_release'])
        elif args.command == 'notice':
            _distinct_paths([args.store, args.notice])
            store, changed = receive_notice(read_json(args.store),
                                            read_json(args.notice))
            if changed:
                _bounded_publish(args.store, store)
                print('notice accepted')
            else:
                print('notice already received; store unchanged')
        elif args.command == 'rollback':
            store = rollback_store(read_json(args.store))
            _bounded_publish(args.store, store)
            print('rolled back to release %d' % store['current']['release'])
        elif args.command == 'status':
            _print(store_status(read_json(args.store)))
        else:
            package = fetch_package(read_json(args.store), args.application_id,
                                    args.locale)
            _print(package)
    except (OSError, DeliveryError, ValueError, TypeError, KeyError,
            OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
