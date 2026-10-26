"""Operator-run browser comparison with a dashboard and durable diagnostic logs."""

import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import signal
import time

from . import app
from .config import load, resolved, validate_browser
from .reports import output_folder
from .terminal import Dashboard


async def run(profile_path, command='submit', limit=18, rolls=(), captcha_mode=None, workers=None, headless=None):
    config=load(profile_path)
    if workers is not None:config['browser']['workers']=workers
    if headless is not None:config['browser']['headless']=headless
    if captcha_mode is not None:config['captcha']['mode']=captcha_mode
    validate_browser(config['browser'])
    if limit<0:raise ValueError('limit must be nonnegative')
    if command=='submit' and (config['browser']['headless'] or config['browser']['workers']>1) and config['captcha']['mode']!='auto':
        raise ValueError('Headless or multiple-worker submission requires --captcha-mode auto')
    destination=output_folder(resolved(profile_path,config['state']['output']),config['profile'])
    directory=destination/'Runs'/('Browser-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    directory.mkdir(parents=True,exist_ok=False)
    control=app.RunControl();counts=Counter();active=set();peak=0;interrupted=False
    started=time.monotonic();loop=asyncio.get_running_loop();handlers={};task=asyncio.current_task()
    transport='Headless Chromium' if config['browser']['headless'] else 'Chromium'
    with Dashboard(config['profile'],config['browser']['workers'],transport=transport) as ui:
        ui.attach(directory)
        def emit(phase,**values):
            nonlocal peak
            if phase=='awaiting_response':
                counts['dispatched_posts']+=1;active.add(values['worker']);peak=max(peak,len(active))
            elif phase in ('saving_result','result'):
                active.discard(values.get('worker'))
            elif phase=='browser_response' and values['status']==429:counts['http_429_responses']+=1
            elif phase=='browser_throttle':counts['form_throttle_refusals']+=1
            ui.event(phase,**values)
        def stop():
            nonlocal interrupted
            if interrupted:
                task.cancel();return
            interrupted=True;control.reason=control.reason or 'operator_stopped'
            control.stopped.set()
            emit('stage',message='Stopping new browser jobs; finishing in-flight receipts')
        for sig in (signal.SIGINT,signal.SIGTERM):
            try:
                handlers[sig]=signal.getsignal(sig);loop.add_signal_handler(sig,stop)
            except (ValueError,RuntimeError,NotImplementedError):handlers.pop(sig,None)
        emit('browser_settings',workers=config['browser']['workers'],headless=config['browser']['headless'],
             submission_spacing='per_worker',interval_seconds=config['pacing']['submission_interval_seconds'],
             limit=limit,global_dispatch_gate=False)
        try:
            result=await app.run(profile_path,command,limit,rolls,captcha_mode=captcha_mode,workers=workers,
                                 headless=headless,emit=emit,control=control)
        except asyncio.CancelledError:
            interrupted=True
            result={'blocked':'interrupted_after_cancellation','outcomes':[],
                    'note':'Inspect SQLite and receipts; dispatched unknown outcomes require reconciliation'}
        except Exception as exc:
            result={'blocked':'browser_run_failed','outcomes':[],'error_type':type(exc).__name__,
                    'error_message':str(exc),'note':'Inspect private Summary.json and receipts before retry'}
        finally:
            for sig,handler in handlers.items():
                loop.remove_signal_handler(sig);signal.signal(sig,handler)
        outcomes=result.get('outcomes',[])
        states=Counter(row['state'] for row in outcomes)
        blocked=result.get('blocked') or control.reason
        if not blocked and any(states[s] for s in ('uncertain','blocked_input','rejected')):
            blocked='Browser outcomes need attention; inspect saved receipts'
        result.update(status='stopped' if blocked or interrupted else 'complete',interrupted=interrupted,
            registered_now=states['registered'],already_registered_now=states['already_registered_external'],processed=sum(v for k,v in states.items() if k!='ready'),
            workers=config['browser']['workers'],headless=config['browser']['headless'],transport='playwright',
            queued=ui.total,submission_spacing='per_worker',interval_seconds=config['pacing']['submission_interval_seconds'],
            global_dispatch_gate=False,peak_in_flight=peak,
            diagnostics={key:counts[key] for key in ('dispatched_posts','http_429_responses','form_throttle_refusals')},
            elapsed_seconds=round(time.monotonic()-started,3),report=str(directory/'Summary.json'),log=str(directory/'events.jsonl'))
        if blocked:result['blocked']=blocked
        (directory/'Summary.json').write_text(json.dumps(result,indent=2)+'\n')
        emit('stage',message='Finished' if result['status']=='complete' else 'Stopped: '+str(blocked or 'operator stopped'))
    return result
