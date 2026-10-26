"""Normalize published BISE CSV/XLSX using editable, repeatable JSON profiles."""

import argparse
import json
from pathlib import Path
import sys

from .app import execute, inspect_source
from .config import initialize, load, resolve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect', help='List master headers, institutions and student sheets')
    inspect.add_argument('--source', type=Path, required=True)
    inspect.add_argument('--template', type=Path)
    init = commands.add_parser('init', help='Create a profile once; preserve existing edits')
    init.add_argument('--profile', required=True)
    init.add_argument('--config', type=Path)
    init.add_argument('--source', type=Path, required=True)
    init.add_argument('--template', type=Path)
    init.add_argument('--output', type=Path)
    for name in ('validate', 'preview', 'run', 'audit', 'status'):
        command = commands.add_parser(name)
        command.add_argument('--config', type=Path, required=True)
        if name == 'preview':
            command.add_argument('--limit', type=int, default=20)
        if name == 'status':
            command.add_argument('--bform')
            command.add_argument('--roll')
    catalog = commands.add_parser('catalog', help='Refresh official options in a visible browser; applicant POST is blocked')
    catalog.add_argument('--refresh', action='store_true', required=True)
    catalog.add_argument('--output', type=Path, default=Path('data/youthgames_catalog.json'))
    args = parser.parse_args()
    try:
        if args.command == 'catalog':
            import asyncio
            from youthgames_shared.catalog import refresh
            result = asyncio.run(refresh(args.output))
        elif args.command == 'status':
            from .state import status
            from .engine import identifier
            config = load(args.config)
            if args.bform and not identifier(args.bform):
                raise ValueError('BForm must have 13 digits')
            result = status(resolve(args.config, config['portal']['normalization_database']), config['profile'], identifier(args.bform) if args.bform else None, args.roll)
        elif args.command == 'inspect':
            result = inspect_source(args.source, args.template)
        elif args.command == 'init':
            if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- ' for c in args.profile) or not args.profile.strip():
                raise ValueError('Profile name may contain letters, numbers, spaces, hyphens and underscores')
            path = args.config or Path('normalization_profiles') / (args.profile.lower().replace(' ', '_') + '.json')
            created = initialize(path, args.profile, args.source, args.template, args.output)
            result = {'status': 'created' if created else 'profile_preserved', 'config': str(path.resolve())}
        else:
            if args.command == 'preview' and args.limit < 1:
                raise ValueError('Preview limit must be positive')
            result = execute(args.config, args.command, getattr(args, 'limit', 20))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.command == 'run':
            print('Already up to date.' if result['status'] == 'already_up_to_date' else 'Normalization complete.')
            target = Path(result['output']) / ('Normalized.xlsx' if (Path(result['output']) / 'Normalized.xlsx').exists() else 'Normalized.csv')
            print('Open: ' + str(target))
        return 0
    except KeyboardInterrupt:
        print('Stopped; previously published output is preserved.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
