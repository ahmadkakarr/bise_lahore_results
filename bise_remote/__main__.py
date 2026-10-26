"""Remote Inter 2024 collection: server job and independent laptop monitor."""
import argparse
import json
from pathlib import Path

from .common import load
from . import control, pilot


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('start','status','watch','logs','stop','download','prepare','pilot'))
    parser.add_argument('--job',choices=('collection','registration'),default=None)
    parser.add_argument('--profile',type=Path,default=pilot.ROOT/'remote_profiles/inter-2024-part2.json')
    parser.add_argument('--interval',type=float,default=5)
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args()
    if args.interval<2:parser.error('--interval must be at least 2 seconds')
    active=control.STATE/'active-task.json'
    job=args.job or (json.loads(active.read_text())['job'] if active.exists() else 'collection')
    if job=='registration':
        from . import registration
        if args.action=='prepare':registration.prepare();return
        if args.action in ('start','pilot'):registration.start(pilot_mode=args.action=='pilot');return
        config=registration.deployment()
        if args.action=='watch':control.watch(config,args.interval,registration.status)
        elif args.action=='status':
            value=registration.status(config)
            if args.json:print(json.dumps(value,indent=2))
            else:
                from rich.console import Console
                Console().print(control.panel(value))
        elif args.action=='download':raise ValueError('Registration snapshots use its dedicated state DB; collection download requires --job collection')
        else:getattr(registration,args.action)(config)
        return
    if args.action in ('prepare','pilot'):parser.error('Use --job registration for prepare/pilot')
    if args.action=='start':
        control.start(load(args.profile));return
    config=control.deployment()
    if args.action=='watch':control.watch(config,args.interval)
    elif args.action=='status':
        value=control.status(config)
        if args.json:print(json.dumps(value,indent=2))
        else:
            from rich.console import Console
            Console().print(control.panel(value))
    else:getattr(control,args.action)(config)


if __name__=='__main__':main()
