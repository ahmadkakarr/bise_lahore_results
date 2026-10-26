"""Fixed approved routes, separate pacing, one shared idempotent registration queue."""

import asyncio
from collections import Counter
import csv
from datetime import datetime,timezone
import json
import math
import shutil
import signal
import time

from . import connections as network
from . import http_async as single
from .config import load,resolved,input_snapshot
from .reports import output_folder
from .store import opened,known_registrations,import_batch,ready_job_refs,last_dispatch_time,authorize_uncertain_retries,retain_connection_history
from .http_recovery import recover_rate_limited,cooldown_remaining
from .name_recovery import recover_name_formatting
from .route_health import RouteHealth


async def run(profile_path,connections,workers=6,limit=0,ui=None,client_factory=None,install_signals=True,
              prepare_ahead=2,max_in_flight=2,auto_recover=True,dispatch_interval=None,retry_uncertain=False,uncertain_retries=2,proxy_config=None):
    if type(retry_uncertain) is not bool:raise ValueError('retry_uncertain must be boolean')
    if type(uncertain_retries) is not int or not 0<=uncertain_retries<=5:raise ValueError('uncertain_retries must be 0–5')
    if type(workers) is not int or not 1<=workers<=36:raise ValueError('Use 1–36 workers total across connections')
    if type(limit) is not int or limit<0:raise ValueError('limit must be nonnegative')
    if type(prepare_ahead) is not int or not 1<=prepare_ahead<=8:raise ValueError('prepare_ahead must be 1–8 per connection')
    if type(max_in_flight) is not int or not 1<=max_in_flight<=36:raise ValueError('in_flight must be 1–36 per connection')
    if type(auto_recover) is not bool:raise ValueError('auto_recover must be boolean')
    if dispatch_interval is not None and (type(dispatch_interval) not in (float,int) or not math.isfinite(dispatch_interval) or dispatch_interval<5):
        raise ValueError('dispatch_interval must be at least five seconds')
    routes=network.load_routes(connections,proxy_config)
    if workers<len(routes):raise ValueError('Use at least one worker per configured connection')
    config=load(profile_path)
    if config['captcha']['mode']!='auto':raise ValueError('Direct HTTP requires automatic CAPTCHA mode')
    emit=ui.event if ui else lambda *args,**values:None
    database=resolved(profile_path,config['state']['database'])
    destination=output_folder(resolved(profile_path,config['state']['output']),config['profile'])
    directory=destination/'Runs'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory.mkdir(parents=True,exist_ok=False)
    if ui:ui.attach(directory)
    emit('connections',routes=[route.public() for route in routes])
    emit('pipeline',prepare_ahead=prepare_ahead,max_in_flight=max_in_flight,
        submission_interval_seconds=dispatch_interval or max(route.interval for route in routes),scope='per_connection')
    stop=asyncio.Event();controls={};tasks=[];interrupted=False;watcher=None
    def request_stop():
        nonlocal interrupted
        if stop.is_set():
            for task in tasks:task.cancel()
        interrupted=True;stop.set();emit('stage',message='Stopping all routes; preserving in-flight receipts')
    loop=asyncio.get_running_loop();handlers={}
    if install_signals:
        for sig in (signal.SIGINT,signal.SIGTERM):
            try:handlers[sig]=signal.getsignal(sig);loop.add_signal_handler(sig,request_stop)
            except (ValueError,RuntimeError,NotImplementedError):handlers.pop(sig,None)
    def healthy_disk():return all(shutil.disk_usage(path).free>=2*1024**3 for path in (destination,database.parent))
    counts=Counter();route_counts={route.name:Counter() for route in routes};retry_counts=Counter();unknown_retries=Counter();result={};uncertain_requeued=[]
    assignment={number:routes[(number-1)%len(routes)] for number in range(1,workers+1)}
    health=RouteHealth(routes,stop,emit,recover=auto_recover)
    def reconnected(name):
        ctl=controls.get(name)
        if ctl:
            ctl.generation+=1  # Prepared sessions from before the link change are stale.
            ctl.transport_recovered()
            emit('recovery_reset',connection=name,server_seconds=max(0,ctl.deadline-time.monotonic()),
                message='Verified reconnect; transport retry reset; server cooldown retained')
    health.on_reconnect=reconnected
    preflight_route=routes[0]
    def factory(number):
        return client_factory(number) if client_factory else single.Client(route=preflight_route if number==0 else assignment[number])
    try:
        emit('stage',message=f'Checking {len(routes)} configured routes and current egress; dynamic public IPs accepted; no applicant POSTs')
        routes=await health.initial()
        if routes is None:raise single.PreflightStopped()
        if workers<len(routes):raise ValueError('Use at least one worker per configured connection')
        assignment={number:routes[(number-1)%len(routes)] for number in range(1,workers+1)}
        preflight_route=next(r for r in routes if health.online[r.name])
        watcher=asyncio.create_task(health.watch())
        emit('connections',routes=[{**route.public(),'workers':sum(r==route for r in assignment.values())} for route in routes])
        if not healthy_disk():
            result={'status':'stopped','blocked':'low_disk_space','processed':0,'registered_now':0}
        else:
            with opened(database) as db,input_snapshot(config,profile_path,allow_empty=True) as (rows,origins,copies,hashes):
                recover_name_formatting(db,config)
                for route in routes:
                    retain_connection_history(db,single.BASE,config['campaign'],route)
                    count=sum(assigned==route for assigned in assignment.values())
                    baseline=dispatch_interval if dispatch_interval is not None else max(route.interval,config['pacing']['submission_interval_seconds'])
                    ctl=single.Control(submission_interval=max(5,baseline),
                        prepare_ahead=min(count,prepare_ahead),max_in_flight=min(count,max_in_flight))
                    ctl.stop=stop;ctl.auto_recover=auto_recover
                    ctl.continue_on_uncertain=retry_uncertain and auto_recover
                    ctl.bind(db,single.BASE,config['campaign'],route.key,route.name,requested_interval=dispatch_interval)
                    ctl.request_gate=lambda name=route.name:health.wait(name)
                    ctl.network_generation=lambda name=route.name:health.generation[name]
                    ctl.on_transport_error=lambda error,name=route.name:health.invalidate(name,'Portal transport error '+error+'; rechecking configured interface')
                    ctl.on_transport_success=lambda name=route.name:health.portal_succeeded(name)
                    controls[route.name]=ctl
                    emit('connection_pacing',connection=route.name,submission_interval_seconds=ctl.submission_interval,
                        cooldown_seconds=max(0,ctl.effective_deadline-time.monotonic()),
                        kind='transport' if ctl.transport_deadline>ctl.deadline else 'server')
                uncertain=db.execute("SELECT count(*) FROM jobs WHERE portal=? AND campaign=? AND state='uncertain'",(single.BASE,config['campaign'])).fetchone()[0]
                if uncertain and not retry_uncertain:
                    result={'status':'blocked','blocked':'uncertain_submission_requires_reconciliation','processed':0,'registered_now':0}
                else:
                    rolls=set(config['selection']['rolls'])
                    pairs=[(row,origin) for row,origin in zip(rows,origins) if not rolls or origin['roll'] in rolls]
                    source={row['BForm']:row for row,_ in pairs}
                    if len(source)!=len(pairs):raise ValueError('Pinned input has duplicate BForms')
                    known=known_registrations(db,config)
                    skipped=sum(row['BForm'] in known for row,_ in pairs)
                    pairs=[(row,origin) for row,origin in pairs if row['BForm'] not in known]
                    selected=[]
                    if pairs:
                        form=None
                        while form is None and not stop.is_set():
                            # Only a safe form GET is needed for preflight. A
                            # failed uplink must not hold up a healthy peer.
                            candidates=sorted(routes,key=lambda r:not r.interface.startswith('wl'))
                            for candidate in candidates:
                                ctl=controls[candidate.name]
                                if not health.online[candidate.name] or ctl.effective_deadline>time.monotonic():continue
                                preflight_route=health.current[candidate.name]
                                try:
                                    async with factory(0) as client:response=await client.get()
                                except (single.httpx.TransportError,TimeoutError) as error:
                                    health.invalidate(candidate.name,'Preflight form connection failed')
                                    if not auto_recover:raise
                                    delay=ctl.backoff(0,'temporary_preflight_transport_error',temporary_transport=True)
                                    emit('cooldown',connection=candidate.name,seconds=delay,kind='transport',error_type=type(error).__name__)
                                    continue
                                if response.status==200:
                                    health.portal_succeeded(candidate.name)
                                    if ctl.transport_recovered():emit('recovery_reset',connection=candidate.name,server_seconds=max(0,ctl.deadline-time.monotonic()))
                                    form=response;break
                                if response.status in (429,500,502,503,504) and auto_recover:
                                    seconds=single.retry_seconds(response.retry_after)+2 if response.retry_after else 2
                                    delay=ctl.backoff(seconds,'temporary_preflight_response',throttle=response.status==429)
                                    emit('cooldown',connection=candidate.name,seconds=delay,message='Temporary form response; other online connections may continue')
                                    continue
                                raise single.FormUnavailable(f'Form GET returned HTTP {response.status}')
                            if form is None:
                                try:await asyncio.wait_for(stop.wait(),timeout=2)
                                except TimeoutError:pass
                        if form is None:raise single.PreflightStopped()
                        catalog,applicants,snapshot=single.prepare_applicants(config,profile_path,pairs,copies,hashes,form)
                        import_batch(db,config,hashes,catalog,snapshot,applicants);del applicants
                        if retry_uncertain:
                            uncertain_requeued=authorize_uncertain_retries(db,config,directory)
                            emit('stage',message=f'Operator opted into retry for {len(uncertain_requeued)} prior uncertain applicants; original attempts retained')
                        recover_rate_limited(db,config)
                        legacy_last=last_dispatch_time(db,single.BASE,config['campaign'])
                        for route in routes:
                            ctl=controls[route.name]
                            ctl.delay(cooldown_remaining(db,single.BASE,config['campaign'],route.key))
                            saved=db.execute('SELECT dispatched_at FROM connection_dispatch_clock WHERE portal=? AND campaign=? AND connection_key=?',
                                (single.BASE,config['campaign'],route.key)).fetchone()
                            stamp=saved[0] if saved else legacy_last
                            if stamp:
                                age=(datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds()
                                ctl.next_submission=time.monotonic()+max(0,ctl.submission_interval-age)
                        selected=ready_job_refs(db,config,limit)
                    queues={route.name:asyncio.Queue() for route in routes};fresh=asyncio.Queue()
                    # New work is shared by online routes. Previously dispatched
                    # identities and all retries stay on their recorded route.
                    names={r.key:r.name for r in routes}
                    affinities={r['job']:r['connection_key'] for r in db.execute('''
                        SELECT a.job,c.connection_key FROM attempts a JOIN attempt_connections c ON c.attempt=a.id
                        JOIN jobs j ON j.id=a.job WHERE j.portal=? AND j.campaign=? AND j.state='ready'
                        AND a.id=(SELECT max(x.id) FROM attempts x JOIN attempt_connections y ON y.attempt=x.id WHERE x.job=a.job)
                        ''',(single.BASE,config['campaign']))}
                    # Disabling a proxy (or removing a device) must not move a
                    # previously dispatched retry to another egress route.
                    deferred=sum(reference['id'] in affinities and affinities[reference['id']] not in names for reference in selected)
                    selected=[reference for reference in selected if reference['id'] not in affinities or affinities[reference['id']] in names]
                    for reference in selected:
                        queue=queues[names[affinities[reference['id']]]] if reference['id'] in affinities else fresh
                        queue.put_nowait(reference)
                    emit('queue',total=len(selected))
                    from .progress import refresh_ui
                    refresh_ui(ui)
                    emit('stage',message=f'Running {len(routes)} fixed connections; separate pacing/cooldowns, one shared registration ledger')
                    if deferred:emit('stage',message=f'{deferred} previously dispatched applicants deferred: their recorded connection is disabled; other work continues')

                    async def consume(number):
                        route=assignment[number];ctl=controls[route.name];retry_queue=queues[route.name]
                        def scoped_emit(phase,**values):
                            values.setdefault('connection',route.name);emit(phase,**values)
                        async with factory(number) as client:
                            while not stop.is_set():
                                needed=lambda:not retry_queue.empty() or not fresh.empty()
                                if not await health.wait(route.name,needed=needed):break
                                if not await ctl.wait():break
                                if not await health.wait(route.name,needed=needed):break
                                if not healthy_disk():ctl.reason='low_disk_space';stop.set();break
                                queue=retry_queue if not retry_queue.empty() else fresh
                                try:reference=queue.get_nowait()
                                except asyncio.QueueEmpty:break
                                try:
                                    job=db.execute("""SELECT j.* FROM jobs j WHERE j.id=? AND state='ready' AND NOT EXISTS(
                                        SELECT 1 FROM registered_applicants r WHERE r.portal=j.portal AND r.campaign=j.campaign AND r.applicant_key=j.applicant_key)""",(reference['id'],)).fetchone()
                                    if job is None:continue
                                    outcome=await single.process(db,job,client,config,destination,source,ctl,scoped_emit,number)
                                    if not outcome:continue
                                    if outcome['state'] in ('registered','already_registered_external'):
                                        health.portal_succeeded(route.name)
                                    if outcome.get('post_dispatched'):route_counts[route.name]['submitted']+=1
                                    if single.reschedule_uncertain(db,config,directory,outcome,ctl,unknown_retries,uncertain_retries,scoped_emit):
                                        route_counts[route.name]['uncertain_retries']+=1
                                        health.invalidate(route.name,'Uncertain response; checking connection before retry')
                                        retry_queue.put_nowait(reference);continue
                                    if outcome.get('rate_limited') or outcome.get('retryable_form'):
                                        if outcome.get('rate_limited'):
                                            retry_counts[job['id']]+=1;scoped_emit('throttle_retry',**outcome)
                                            if not auto_recover and retry_counts[job['id']]>config['captcha']['max_explicit_rejection_retries']:
                                                ctl.reason='Repeated explicit throttling; resume later';stop.set();continue
                                        else:scoped_emit('form_retry',**outcome)
                                        if outcome.get('retryable_form'):health.invalidate(route.name)
                                        retry_queue.put_nowait(reference)  # Same connection, never evade its cooldown.
                                    else:
                                        counts[outcome['state']]+=1;route_counts[route.name][outcome['state']]+=1
                                        scoped_emit('result',**outcome)
                                finally:queue.task_done()

                    async def worker(number):
                        try:await consume(number)
                        except BaseException:
                            stop.set();raise
                    tasks=[asyncio.create_task(worker(number)) for number in assignment]
                    try:outcomes=await asyncio.gather(*tasks,return_exceptions=True)
                    except BaseException:
                        stop.set()
                        for task in tasks:
                            if not task.done():task.cancel()
                        await asyncio.gather(*tasks,return_exceptions=True);raise
                    failures=[outcome for outcome in outcomes if isinstance(outcome,BaseException)]
                    blocked=next((ctl.reason for ctl in controls.values() if ctl.reason),None)
                    if failures:blocked=blocked or 'worker_error_requires_review'
                    if health.error:blocked='network_configuration_requires_attention'
                    if counts['uncertain'] and not (retry_uncertain and auto_recover):blocked='uncertain_submission_requires_reconciliation'
                    attention=db.execute('''SELECT j.id AS job,j.roll,j.state,j.detail AS reason,j.receipt FROM jobs j
                        JOIN members m ON m.job=j.id JOIN profiles p ON p.batch=m.batch WHERE p.name=?
                        AND j.state IN ('rejected','blocked_input','uncertain') ORDER BY j.roll,j.id''',(config['profile'],)).fetchall()
                    report=directory/'Needs Attention.csv'
                    with report.open('w',encoding='utf-8-sig',newline='') as stream:
                        writer=csv.DictWriter(stream,fieldnames=['job','roll','state','reason','receipt']);writer.writeheader()
                        writer.writerows(dict(row) for row in attention)
                    result={'status':'stopped' if stop.is_set() or blocked else 'complete_with_issues' if attention or deferred else 'complete',
                        'processed':sum(counts.values())-counts['ready'],'registered_now':counts['registered'],
                        'already_registered_now':counts['already_registered_external'],'skipped_registered':skipped,
                        'outcomes':dict(counts),'queued':len(selected),'needs_attention':len(attention),'attention_report':str(report),
                        'deferred_disabled_connections':deferred,
                        'throttle_refusals':sum(retry_counts.values()),'auto_recover':auto_recover,
                        'connection_results':{name:dict(value) for name,value in route_counts.items()}}
                    if blocked:result['blocked']=blocked
    except single.PreflightStopped:
        result={'status':'stopped','blocked':'network_configuration_requires_attention' if health.error else 'operator_stopped_before_submission',
            'processed':0,'registered_now':0}
    finally:
        health.on_reconnect=None
        if watcher:
            watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
        for sig,handler in handlers.items():loop.remove_signal_handler(sig);signal.signal(sig,handler)
    result.update(uncertain_retry_enabled=retry_uncertain,uncertain_requeued=len(uncertain_requeued),
        uncertain_retries_per_applicant=uncertain_retries,
        uncertain_retry_attempts=sum(value['uncertain_retries'] for value in route_counts.values()),
        workers_by_connection={r.name:sum(a==r for a in assignment.values()) for r in assignment.values()},
        connection_labels={r.name:r.label for r in assignment.values()},
        transport='httpx',pacing_scope='per_connection',connections=len({r.name for r in assignment.values()}),workers=workers,
        interrupted=interrupted,browser_launched=False,report=str(directory/'Summary.json'),log=str(directory/'events.jsonl'))
    result['connection_pacing']={name:{'interval_seconds':ctl.submission_interval,'retry_round':ctl.retry_round,
        'transport_retry_round':ctl.transport_retry_round,
        'server_wait_seconds':max(0,ctl.deadline-time.monotonic()),
        'transport_wait_seconds':max(0,ctl.transport_deadline-time.monotonic()),
        'peak_in_flight':ctl.peak_in_flight} for name,ctl in controls.items()}
    result['connection_online']=dict(health.online)
    finish=getattr(ui,'finish',None)
    if callable(finish):finish(result)
    (directory/'Summary.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
