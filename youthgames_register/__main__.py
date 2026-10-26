"""Prepare, preview, submit and recover Youth Games jobs with isolated workers."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from .app import run, reconcile
from .config import load, resolved
from .reports import export
from .store import opened, jobs
from . import MAX_WORKERS


def main(alarm_default=False):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    sync = commands.add_parser('sync', help='Refresh the BISE ready master and pin it as games input; no submissions')
    sync.add_argument('--config', type=Path, default=Path('registration_profiles/master.json'))
    sync.add_argument('--normalization-config', type=Path, default=Path('normalization_profiles/combined.json'))
    sync.add_argument('--settings', type=Path, default=Path('registration_profiles/lahore.json'), help='Existing operator settings used only when creating the master profile')
    for name in ('inspect', 'prepare', 'preview', 'submit', 'status', 'export', 'reconcile', 'recover'):
        command = commands.add_parser(name)
        command.add_argument('--config', type=Path, required=True)
        if name in ('preview', 'submit'):
            command.add_argument('--limit', type=int, default=1, help='0 means all ready; registered/uncertain/ineligible are skipped')
            command.add_argument('--workers', type=int, choices=range(1, MAX_WORKERS + 1), default=None, help=f'Override profile worker count (1–{MAX_WORKERS}); limit is shared by all workers')
            command.add_argument('--roll', action='append', default=[])
            command.add_argument('--diagnostics','--debug',action=argparse.BooleanOptionalAction,default=name=='submit',
                help='Browser dashboard and network diagnostics; enabled by default for submit; --no-diagnostics disables it')
        if name in ('inspect', 'prepare', 'preview', 'submit'):
            visibility = command.add_mutually_exclusive_group()
            visibility.add_argument('--headless', action='store_true', dest='headless', default=None)
            visibility.add_argument('--headed', action='store_false', dest='headless')
        if name == 'submit':
            from run_alarm import add_arguments
            add_arguments(command,default=alarm_default)
            command.add_argument('--captcha-mode', choices=['auto', 'assist'], default=None, help='Override profile mode; auto solves visible addition without a terminal prompt')
        if name == 'reconcile':
            command.add_argument('--job', type=int, required=True)
    args = parser.parse_args()
    from run_alarm import guarded
    return guarded(args,_execute,'Youth Games '+args.command+' / '+args.config.stem)


def _execute(args):
    try:
        if hasattr(args, 'limit') and args.limit < 0:
            raise ValueError('limit must be nonnegative')
        if args.command == 'sync':
            from .handoff import sync
            result = sync(args.config, args.normalization_config, args.settings)
        elif args.command in ('status', 'export', 'reconcile', 'recover'):
            config = load(args.config)
            with opened(resolved(args.config, config['state']['database'])) as db:
                if args.command == 'recover':
                    from .browser_recovery import recover_browser_jobs
                    result = recover_browser_jobs(db, config)
                elif args.command == 'status':
                    from collections import Counter
                    result = {'counts': dict(Counter(r['state'] for r in jobs(db, config['profile'])))}
                else:
                    result = reconcile(db, config, args.job) if args.command == 'reconcile' else {}
                    result['counts'] = export(db, config, resolved(args.config, config['state']['output']))
        elif getattr(args,'diagnostics',False):
            from .browser_run import run as diagnostic_run
            result=asyncio.run(diagnostic_run(args.config,args.command,args.limit,args.roll,
                captcha_mode=getattr(args,'captcha_mode',None),workers=args.workers,headless=args.headless))
        else:
            result = asyncio.run(run(args.config, args.command, getattr(args, 'limit', 1), getattr(args, 'roll', ()), captcha_mode=getattr(args, 'captcha_mode', None), workers=getattr(args, 'workers', None), headless=getattr(args, 'headless', None)))
        if getattr(args,'diagnostics',False):
            from .terminal import print_summary
            print_summary(result)
        else:print(json.dumps(result, indent=2))
        if args.command in ('prepare', 'preview', 'submit', 'export'):
            config = load(args.config)
            if config['state'].get('write_reports', False):
                print('Open: ' + str(resolved(args.config, config['state']['output']) / 'Registration Results.xlsx'))
            else:
                print('Registration state and tracking IDs saved in SQLite.')
        if result.get('interrupted'):return 130
        if args.command == 'submit' and (result.get('blocked') or any(o['state'] in ('uncertain', 'blocked_input', 'rejected') for o in result.get('outcomes', []))):
            return 2
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        print('Stopped. Committed results preserved; possible dispatched POSTs require reconciliation.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(alarm_default=True))
