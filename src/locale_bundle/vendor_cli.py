"""CLI for the offline vendor handoff (contract v5).

Two subcommands: ``prepare`` builds a vendor package from a workspace and a
selection without touching the workspace; ``receive`` folds one vendor
response into local drafts and receipts, replacing the workspace atomically
in place. Responses never become approved translations and never modify an
existing release bundle.
"""
import argparse
import json
from pathlib import Path
import sys

from .common import read_json, publish
from .vendor import (VendorError, build_package, receive_response,
                     serialized_size)


def _distinct_paths(paths):
    resolved = [Path(path).resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise VendorError('input and output paths must not coincide')


def _bounded_publish(path, value):
    if serialized_size(value) > 8_000_000:
        raise VendorError('output exceeds 8MB')
    publish(path, value)


def main():
    parser = argparse.ArgumentParser(description='离线供应商交接')
    commands = parser.add_subparsers(dest='command', required=True)

    prepare = commands.add_parser('prepare', help='归并选择行并生成供应商包')
    prepare.add_argument('--workspace', type=Path, required=True)
    prepare.add_argument('--selection', type=Path, required=True)
    prepare.add_argument('--package', type=Path, required=True)

    receive = commands.add_parser('receive', help='接收一批供应商回件并写入本地草稿')
    receive.add_argument('--workspace', type=Path, required=True)
    receive.add_argument('--package', type=Path, required=True)
    receive.add_argument('--response', type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            _distinct_paths([args.workspace, args.selection, args.package])
            workspace = read_json(args.workspace)
            selection = read_json(args.selection)
            package = build_package(workspace, selection)
            _bounded_publish(args.package, package)
            print('prepared ' + str(args.package))
        else:
            _distinct_paths([args.workspace, args.package, args.response])
            workspace = read_json(args.workspace)
            package = read_json(args.package)
            response = read_json(args.response)
            updated, report, changed = receive_response(workspace, package, response)
            if changed:
                # receive may only replace the workspace it read, in place.
                _bounded_publish(args.workspace, updated)
            json.dump(report, sys.stdout, ensure_ascii=False,
                      sort_keys=True, indent=2)
            sys.stdout.write('\n')
    except (OSError, VendorError, ValueError, TypeError, KeyError,
            OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
