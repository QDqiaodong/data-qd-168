import argparse
from pathlib import Path
import sys
from .common import read_json, publish
from .release import compile_release_bundles


def main():
    parser = argparse.ArgumentParser(description='按应用依赖闭包构建发布资源包')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('source and output must differ')
        report = compile_release_bundles(read_json(args.source))
        publish(args.output, report)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print('wrote ' + str(args.output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
