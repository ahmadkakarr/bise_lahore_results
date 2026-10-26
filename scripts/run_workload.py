#!/usr/bin/env python3
"""Run a collector or registration command in its own bounded systemd scope."""
import argparse
import json
import os
from pathlib import Path
import shutil
import uuid

ROOT=Path(__file__).resolve().parents[1]
COMMANDS={'collect':('bise_results','collect'),'register':('bise_allocate','http-submit')}


def command(kind,arguments,*,unit=None):
    module,action=COMMANDS[kind]
    properties=['CPUWeight=25','IOWeight=25','TasksMax=1024',
                'MemorySwapMax=512M','MemoryHigh=4G' if kind=='collect' else 'MemoryHigh=768M',
                'MemoryMax=6G' if kind=='collect' else 'MemoryMax=1536M',
                'CPUQuota=400%' if kind=='collect' else 'CPUQuota=200%']
    return ['systemd-run','--user','--scope','--quiet','--collect','--same-dir','--expand-environment=no',
            '--slice=bise-background.slice','--unit='+(unit or f'bise-{kind}-{uuid.uuid4().hex[:12]}'),
            *['--property='+p for p in properties],'--',
            'nice','-n','5',str(ROOT/'.venv/bin/python'),'-m',module,action,*arguments]


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true',help='Print the exact scope command; no processes or submissions')
    parser.add_argument('kind',choices=tuple(COMMANDS))
    parser.add_argument('arguments',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
    arguments=args.arguments[1:] if args.arguments[:1]==['--'] else args.arguments
    if not arguments:parser.error('Provide the original collect/http-submit arguments after --')
    result=command(args.kind,arguments)
    if args.dry_run:
        print(json.dumps({'command':result,'working_directory':str(ROOT),'applicant_posts':0},indent=2));return 0
    if not shutil.which('systemd-run') or not shutil.which('nice'):
        parser.error('This bounded workload launcher requires Linux systemd and coreutils nice')
    # Only this new child scope is controlled; never restart or stop other jobs.
    # systemd-run --scope preserves the terminal and forwards Ctrl+C normally.
    os.chdir(ROOT)
    # Replace this launcher so there is no extra Python parent that might kill
    # systemd-run before the application's Ctrl+C cleanup has finished.
    os.execvpe(result[0],result,os.environ)


if __name__=='__main__':raise SystemExit(main())
