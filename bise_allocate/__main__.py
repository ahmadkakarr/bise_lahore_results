"""Allocate frozen eligible cohorts, normalize them, and verify bounded live pilots."""
import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

from . import app


def main(alarm_default=False):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('apply','normalize','sync','status','pilot','http-pilot','http-submit','sports','private-normalize','private-pilot','participation'):
        command=commands.add_parser(name)
        command.add_argument('--config',type=Path,required=True)
        command.add_argument('--json',action='store_true',help='Print the full structured result instead of the terminal summary')
        if name=='participation':command.add_argument('--subcategory',required=True,choices=('Open Category','Private'))
        if name=='private-normalize':command.add_argument('--group',required=True,choices=('raiwind','model_town'))
        if name=='private-pilot':command.add_argument('--connections',type=Path)
        if name=='sports':command.add_argument('--ratios',type=Path,required=True,help='Approved gender-wise sports ratio JSON; assigns only never-dispatched applicants and syncs input')
        if name in ('http-submit','pilot','http-pilot'):
            from run_alarm import add_arguments
            add_arguments(command,default=alarm_default)
        if name=='http-submit':
            command.add_argument('--connections',type=Path,help='Fixed Wi-Fi/USB interfaces; dynamic IP recovery, independent pacing, shared registration ledger')
            command.add_argument('--pakistan-proxy',action='store_true',help='Opt in to the fixed Pakistan proxy as an additional independently monitored route; requires --connections')
            command.add_argument('--proxy-config',type=Path,default=None,help='Proxy JSON (default connection_profiles/pakistan-proxy.json); requires --pakistan-proxy')
            command.add_argument('--retry-uncertain',action='store_true',
                help='Retry uncertain applicants with preserved history; unresolved rows are quarantined while others continue (with auto recovery)')
            command.add_argument('--uncertain-retries',type=int,choices=range(0,6),default=2,
                help='Extra attempts per uncertain applicant during this run (default 2); then retain uncertain and continue')
            worker_options=command.add_mutually_exclusive_group()
            worker_options.add_argument('--workers',type=int,choices=range(1,37),default=None,help='Total workers (default 18)')
            worker_options.add_argument('--workers-per-connection',type=int,choices=range(1,37),
                help='Workers for EACH configured connection; e.g. 6 means 12 total on Wi-Fi + USB (maximum 36 total)')
            command.add_argument('--limit',type=int,default=0,help='0 processes all remaining Ready students')
            command.add_argument('--refresh-input',action='store_true',help='Refresh normalization from source SQLite; default resumes the existing pinned input')
            command.add_argument('--prepare-ahead',type=int,choices=range(1,9),default=4,help='Bounded preparation window (default 4); per route with --connections')
            command.add_argument('--in-flight',type=int,choices=range(1,37),default=4,help='Maximum POST response waits (default 4); per route with --connections')
            command.add_argument('--auto-recover',action=argparse.BooleanOptionalAction,default=True,
                help='Default on: keep retrying proven temporary refusals/GET failures with durable shared backoff')
            command.add_argument('--dispatch-interval',type=float,default=None,
                help='Explicit POST baseline, at least 5 seconds; per route; replaces old configured baseline but preserves server-throttle spacing and cooldowns')
        if name=='status':command.add_argument('--roll')
        if name=='pilot':
            command.add_argument('--workers',type=int,choices=range(1,19),default=18)
            command.add_argument('--headless',action='store_true')
    args=parser.parse_args()
    from run_alarm import guarded
    return guarded(args,_execute,'Allocation '+args.command+' / '+args.config.stem)


def _execute(args):
    try:
        if args.command=='http-submit':
            from youthgames_register.http_async import run
            from youthgames_register.terminal import Dashboard
            if args.limit<0:raise ValueError('limit must be nonnegative')
            if args.dispatch_interval is not None and (not math.isfinite(args.dispatch_interval) or args.dispatch_interval<5):
                raise ValueError('--dispatch-interval must be finite and at least five seconds')
            if args.proxy_config and not args.pakistan_proxy:raise ValueError('--proxy-config requires --pakistan-proxy')
            if args.pakistan_proxy and not args.connections:raise ValueError('--pakistan-proxy requires --connections')
            proxy_config=(args.proxy_config or Path('connection_profiles/pakistan-proxy.json')) if args.pakistan_proxy else None
            config=app.load(args.config);paths=app.paths(config,args.config)
            if args.connections:
                from youthgames_register.connections import load_routes
                routes=load_routes(args.connections,proxy_config)
            if args.workers_per_connection is not None:
                if not args.connections:raise ValueError('--workers-per-connection requires --connections')
                workers=args.workers_per_connection*len(routes)
                if workers>36:raise ValueError('Combined worker count must not exceed 36')
            else:workers=args.workers or 18
            if args.connections and workers<len(routes):raise ValueError('Use at least one worker per enabled connection')
            args.workers=workers
            from .participation import current as participation_policy
            participation=participation_policy(paths['allocation_database'],config['scheme'])
            title=config['scheme']+(' / '+participation['subcategory'] if participation else '')
            with Dashboard(title,workers=args.workers,enabled=not args.json) as ui:
                ui.batch_provider=lambda:app.batch_progress(args.config)
                ui.refresh_batch()
                from .resume import prepare
                preparation=prepare(args.config,refresh=args.refresh_input,emit=ui.event)
                result=asyncio.run(run(paths['registration_profile'],workers=args.workers,limit=args.limit,ui=ui,
                                      prepare_ahead=args.prepare_ahead,max_in_flight=args.in_flight,
                                      auto_recover=args.auto_recover,dispatch_interval=args.dispatch_interval,connections=args.connections,
                                      retry_uncertain=args.retry_uncertain,uncertain_retries=args.uncertain_retries,proxy_config=proxy_config))
                result.update(preparation)
                result.update({key:value for key,value in app.progress(args.config).items()
                               if key in ('assigned','registered','ready_remaining','normalization_blocked','completed_total','remaining_total','held_total')})
                ui.event('stage',message='Finished; review held or deferred rows' if result['status']=='complete_with_issues' else 'Finished' if result['status']=='complete' else 'Stopped: ' + result.get('blocked', 'committed results preserved'))
                ui.finish(result,refresh=False)
        elif args.command=='participation':
            from .participation import apply
            result=apply(args.config,args.subcategory)
        elif args.command=='private-pilot':
            from .private_pilot import run
            result=asyncio.run(run(args.config,args.connections))
        elif args.command=='private-normalize':
            from .private_pool import run
            result=run(args.config,args.group,emit=None if args.json else lambda message:print(message,flush=True))
        elif args.command=='sports':
            from .sports import apply
            result=apply(args.config,args.ratios)
            result['handoff']=app.sync(args.config)
        elif args.command=='http-pilot':
            from .http import run
            from youthgames_register.terminal import Dashboard
            with Dashboard('UC HTTP pilot verification',workers=1,enabled=not args.json) as ui:
                result=run(args.config,emit=ui.event)
        elif args.command=='pilot':result=asyncio.run(app.pilot(args.config,args.workers,args.headless))
        elif args.command=='status':result=app.progress(args.config,args.roll)
        else:result=getattr(app,args.command)(args.config)
        if args.json:print(json.dumps(result,indent=2,ensure_ascii=False))
        else:
            from youthgames_register.terminal import print_summary
            print_summary(result,show_ucs=args.command=='status')
            if args.command=='private-normalize':
                print('Ready master: '+result['ready_master'])
                print(result['note'])
        if args.command=='http-submit' and result.get('interrupted'):return 130
        if args.command=='http-submit' and (result.get('blocked') or result['status']=='stopped'):return 2
        return 2 if args.command in ('pilot','http-pilot','private-pilot') and not result['pilot_passed'] else 0
    except (KeyboardInterrupt,asyncio.CancelledError):
        print('Stopped. Assignments and committed registration receipts are preserved; rerun safely.',file=sys.stderr);return 130
    except Exception as exc:
        print(f'Error: {exc}',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main(alarm_default=True))
