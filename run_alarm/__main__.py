"""Stop, inspect or briefly test local run-end alarms."""

import argparse
import json
import os
from pathlib import Path

from .core import DEFAULT_DIRECTORY,acknowledge,watch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=DEFAULT_DIRECTORY)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('stop',help='Silence ringing alarms; repeat safely')
    commands.add_parser('status',help='Show watchers and alarm state')
    test=commands.add_parser('test',help='Play a bounded audible test in this terminal')
    test.add_argument('--seconds',type=float,default=3)
    args=parser.parse_args()
    if args.command=='stop':
        print(f'Alarm acknowledgment sent: {acknowledge(args.directory)}');return 0
    if args.command=='status':
        for path in args.directory.glob('*.json'):
            try:state=json.loads(path.read_text())
            except (ValueError,OSError):continue
            if state.get('status') not in ('watching','alarming'):continue
            print(json.dumps(state,ensure_ascii=False))
        return 0
    if not 0<args.seconds<=60:parser.error('--seconds must be greater than zero and at most 60')
    import uuid
    args.directory.mkdir(parents=True,exist_ok=True)
    path=args.directory/(uuid.uuid4().hex+'.json')
    read_fd,write_fd=os.pipe();os.close(write_fd)
    try:watch(read_fd,path,'Alarm audio test',os.getpid(),duration=args.seconds)
    except KeyboardInterrupt:
        acknowledge(args.directory);return 130
    return 0


if __name__=='__main__':raise SystemExit(main())
