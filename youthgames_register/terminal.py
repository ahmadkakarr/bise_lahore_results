"""Compact terminal dashboard and private structured run events."""

from collections import Counter, deque
from math import ceil as math_ceil
import json
from pathlib import Path
import time

from rich.console import Console, Group
from rich.columns import Columns
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text
from .progress import BatchProgress,atomic_json,atomic_current
from datetime import datetime,timezone


class Dashboard:
    def __init__(self, title, workers=0, enabled=True, console=None, transport='Direct HTTPS'):
        self.console = console or Console()
        self.title, self.workers, self.enabled = title, workers, enabled
        self.transport = transport
        self.stage = 'Preparing'
        self.total = self.completed = 0
        self.counts, self.uc = Counter(), Counter()
        self.retries = 0
        self.throttles = 0
        self.cooldown_until = 0
        self.active, self.recent = {}, deque(maxlen=5)
        self.started = time.monotonic()
        self.run_started_at=datetime.now(timezone.utc).isoformat()
        self.run_started = None
        self.startup_seconds = 0
        self.success_times = deque()
        self.pipeline = {}
        self.connections = {}
        self.connection_counts={}
        self.batch=BatchProgress();self.batch_provider=None
        self.run_status='preparing';self.run_directory=None;self.current_progress_path=None
        self.progress_error=None;self.batch_error=None
        self.log = self.live = None
        self.last_plain = 0
        self.progress = Progress(TextColumn('[bold cyan]Run processed'), BarColumn(),
            TextColumn('{task.completed:.0f}/{task.total:.0f}'), TextColumn('{task.percentage:>5.1f}%'),
            TimeElapsedColumn(), TextColumn('{task.fields[eta]}'), auto_refresh=False)
        self.task = self.progress.add_task('HTTP', total=0, eta='ETA calculating…')

    def __enter__(self):
        if self.enabled and self.console.is_terminal:
            self.live = Live(get_renderable=self.render, console=self.console, refresh_per_second=4, vertical_overflow='ellipsis')
            self.live.start()
        return self

    def attach(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.log = (Path(directory) / 'events.jsonl').open('a', buffering=1)
        self.run_directory=Path(directory)
        if self.run_directory.parent.name=='Runs':
            self.current_progress_path=self.run_directory.parent.parent/'Current Progress.json'

    def refresh_batch(self):
        if self.batch_provider:
            try:self.event('batch_snapshot',**self.batch_provider());self.batch_error=None
            except Exception as error:
                # A monitoring read must not turn a committed registration
                # into an error or stop unrelated workers.
                self.batch_error=type(error).__name__

    def progress_snapshot(self):
        return {'schema_version':1,'profile':self.title,'updated_at':datetime.now(timezone.utc).isoformat(),
            'run_started_at':self.run_started_at,
            'status':self.run_status,'stage':self.stage,
            'run_selected_at_start':self.total,'run_processed':self.completed,
            'run_left_to_process':max(0,self.total-self.completed),
            'registered_this_run':self.counts['registered'],
            'already_registered_this_run':self.counts['already_registered_external'],
            'batch':self.batch.summary(),'counts_source':'SQLite at startup/shutdown; committed outcome events while running',
            'run_directory':str(self.run_directory) if self.run_directory else None,
            'connections':{name:{'label':row.get('label',name),'workers':row.get('workers',0),
                'state':row.get('health','checking'),'counts':dict(self.connection_counts.get(name,{}))}
                for name,row in tuple(self.connections.items())}}

    def persist_progress(self):
        if self.run_directory is None:return
        value=self.progress_snapshot()
        try:
            atomic_json(self.run_directory/'Progress.json',value)
            if self.current_progress_path:atomic_current(self.current_progress_path,value)
            self.progress_error=None
        except OSError as error:self.progress_error=type(error).__name__

    def finish(self,result,refresh=True):
        if refresh:self.refresh_batch()
        self.run_status=result.get('status','stopped')
        result['run_selected_at_start']=result.get('queued',self.total)
        if self.batch.summary():result['batch_progress']=self.batch.summary()
        self.persist_progress()
        if result.get('report'):
            # Runner writes transport details first; persist the final merged
            # batch counters too, rather than adding them only to stdout.
            try:atomic_json(Path(result['report']),result)
            except OSError as error:self.progress_error=type(error).__name__

    def event(self, phase, **values):
        if phase == 'queue':
            self.total = values['total']
            self.run_started = time.monotonic()
            self.startup_seconds = self.run_started - self.started
            self.progress.reset(self.task, total=self.total, completed=0, eta='ETA calculating…')
            self.stage = 'Running' if self.total else 'Already up to date — no new submissions'
            self.run_status='running'
        elif phase=='batch_snapshot':
            self.batch.seed(values)
        elif phase == 'pipeline':
            self.pipeline = values
        elif phase=='connections':
            for route in values['routes']:self.connections.setdefault(route['connection'],{}).update(route)
        elif phase=='connection_health':
            row=self.connections.setdefault(values['connection'],{})
            row.update(health=values['state'],health_reason=values.get('reason',''))
            name=row.get('label',values['connection'])
            self.recent.append(f"{name} · {values['state']} · {values.get('reason','')}")
        elif phase=='connection_pacing':
            row=self.connections.setdefault(values['connection'],{})
            row.update(values)
            row[values.get('kind','server')+'_until']=time.monotonic()+values.get('cooldown_seconds',0)
            row['cooldown_until']=max(row.get('server_until',0),row.get('transport_until',0))
        elif phase=='recovery_reset':
            if values.get('connection'):
                row=self.connections.setdefault(values['connection'],{})
                row['transport_until']=0
                row['server_until']=time.monotonic()+values.get('server_seconds',0)
                row['cooldown_until']=row['server_until']
            else:self.cooldown_until=time.monotonic()+values.get('server_seconds',0)
        elif phase == 'stage':
            self.stage = values['message']
        elif phase == 'result':
            if values['state'] != 'ready':
                self.completed += 1
            self.counts[values['state']] += 1
            if values.get('connection'):
                self.connection_counts.setdefault(values['connection'],Counter())[values['state']]+=1
            if values['state'] == 'registered':
                current = time.monotonic()
                self.success_times.append(current)
                while self.success_times and self.success_times[0] <= current-60:
                    self.success_times.popleft()
            if values.get('uc') and values['state'] == 'registered':
                self.uc[values['uc']] += 1
            self.recent.append(f"UC {values.get('uc', '—')} · roll {values.get('roll', '—')} · {values['state']}" +
                (f" · {values['tracking']}" if values.get('tracking') else ''))
            self.progress.update(self.task, completed=self.completed)
        elif phase == 'get_retry':
            self.retries += 1
        elif phase == 'cooldown':
            if values.get('connection'):
                row=self.connections.setdefault(values['connection'],{})
                key=values.get('kind','server')+'_until'
                row[key]=max(row.get(key,0),time.monotonic()+values['seconds'])
                row['cooldown_until']=max(row.get('server_until',0),row.get('transport_until',0))
            else:
                self.cooldown_until = max(self.cooldown_until, time.monotonic() + values['seconds'])
                self.stage = 'Temporary connection retry; records preserved' if values.get('kind')=='transport' else 'Portal requested a shared cooldown; records preserved'
        elif phase == 'throttle_retry':
            self.throttles += 1
            self.recent.append(f"UC {values.get('uc', '—')} · roll {values.get('roll', '—')} · {values.get('detail') or 'Portal throttle; retry after cooldown'}")
        elif phase == 'form_retry':
            self.retries += 1
            self.recent.append(f"UC {values.get('uc', '—')} · roll {values.get('roll', '—')} · temporary form failure; retry scheduled")
        elif phase=='uncertain_retry':
            if values.get('connection'):
                self.connection_counts.setdefault(values['connection'],Counter())['uncertain_retries']+=1
            self.recent.append(f"Roll {values.get('roll','—')} · response uncertain · retry {values['retry']}/{values['maximum']} after cooldown")
        elif phase == 'pacing_update':
            if values.get('connection'):
                self.connections.setdefault(values['connection'],{})['submission_interval_seconds']=values['submission_interval_seconds']
            elif self.pipeline:
                self.pipeline = {**self.pipeline,'submission_interval_seconds':values['submission_interval_seconds']}
        elif phase == 'browser_throttle':
            self.throttles += 1
            self.stage = 'Server throttle detected; finishing in-flight browser attempts'
            self.recent.append(values['message'])
        elif phase == 'submitting':
            if values.get('connection'):
                self.connection_counts.setdefault(values['connection'],Counter())['submitted']+=1
            if self.cooldown_until and time.monotonic() >= self.cooldown_until:self.stage='Running after portal cooldown'
        state=values.get('state') if phase=='result' else {'uncertain_saved':'uncertain','uncertain_retry':'ready',
            'quarantined':'rejected','form_retry':'ready'}.get(phase)
        if state and values.get('job') is not None:self.batch.transition(values['job'],state)
        if phase in ('queue','batch_snapshot','result','uncertain_saved','uncertain_retry','quarantined','form_retry','stage','stopping'):
            self.persist_progress()
        if values.get('worker') and phase not in ('portal_limits','browser_response','browser_request_failed','recovery_reset'):
            self.active[values['worker']] = {'phase': phase, **values}
        if self.log:
            # Deliberately exclude names, DOB, identity numbers, tokens and photo bytes.
            logged={k:v for k,v in values.items() if phase!='batch_snapshot' or k!='jobs'}
            self.log.write(json.dumps({'time': time.time(), 'phase': phase, **logged}) + '\n')
        if self.live:
            self.live.update(self.render())
        elif self.enabled and (phase in ('queue', 'stage', 'stopping', 'cooldown') or time.monotonic() - self.last_plain >= 5):
            self.last_plain = time.monotonic()
            batch=self.batch.summary()
            overview=(f"Batch {batch['total']:,} | Completed {batch['completed']:,} | REMAINING {batch['remaining']:,} | Held {batch['held']:,} | " if batch else '')
            self.console.print(Text(overview+f"{self.stage} | Run processed {self.completed:,}/{self.total:,} | "
                f"{self.counts['registered']:,} registered | {self.workers} workers" +
                (f" | {values.get('message','Temporary wait; automatically retrying')}" if phase == 'cooldown' else '')))

    def recent_rate(self):
        current = time.monotonic()
        # Rich refreshes on its own thread. Render from snapshots; event() is
        # the sole writer, including when a cooldown ages out every success.
        recent = sum(instant > current-60 for instant in tuple(self.success_times))
        elapsed = current-self.run_started if self.run_started is not None else 0
        return recent*60/min(60,elapsed) if elapsed > 0 else 0

    def render(self):
        rate = self.recent_rate()
        if len(self.success_times) >= 3 and rate > 0:
            remaining = int(max(0, self.total-self.completed) / (rate/60))
            eta = f'ETA {remaining//3600:02d}:{remaining%3600//60:02d}:{remaining%60:02d}'
        else:
            eta = 'ETA calculating…'
        self.progress.update(self.task, eta=eta)
        header = Text(f'{self.title}  •  {self.transport}  •  {self.workers} workers', style='bold cyan')
        failures = sum(self.counts[k] for k in ('rejected', 'uncertain', 'blocked_input'))
        cooldown = max(0, int(self.cooldown_until - time.monotonic() + 0.999))
        stats = Text(f"{self.stage}   ✓ {self.counts['registered']:,} saved   "
            f"! {failures:,} need attention   {self.counts['already_registered_external']:,} already registered   {self.counts['ready']:,} deferred   "
            f"↻ {self.retries} GET retries · {self.throttles} throttled   {rate:.1f} saved/min (last 60s)" +
            (f'   WAIT {cooldown}s (all workers)' if cooldown else ''), style='yellow' if cooldown or failures else 'green')
        table = Table(expand=True, box=None, padding=(0, 1))
        for title in (('Worker','Connection','UC','Roll','Activity') if self.connections else ('Worker','UC','Roll','Activity')): table.add_column(title)
        activity = Counter(item.get('phase') for item in tuple(self.active.values()))
        preparing = sum(activity[p] for p in ('fetching_form','get_retry','refreshing_form','filling','reading_captcha'))
        prepared = sum(activity[p] for p in ('prepared','waiting_for_portal_slot'))
        awaiting = sum(activity[p] for p in ('submitting','awaiting_response'))
        saving = activity['saving_result']
        working = preparing + prepared + awaiting + saving
        pipeline = Text(f'Preparing {preparing} · Prepared {prepared} · Awaiting response {awaiting} · Saving {saving} · '
            f'Waiting/idle {max(0,self.workers-working)}', style='cyan')
        if self.pipeline:
            pipeline.append(f"   Limits: {self.pipeline['prepare_ahead']} prepare-ahead / {self.pipeline['max_in_flight']} in-flight / "
                f"{self.pipeline['submission_interval_seconds']:g}s dispatch spacing", style='dim')
            if self.pipeline.get('scope')=='per_connection':pipeline.append(' (per connection)',style='bold')
        if self.run_started is not None:
            pipeline.append(f'   Startup {self.startup_seconds:.0f}s', style='dim')
        maximum = max(4, self.console.height - (23 if self.batch.totals else 21))
        for worker in range(1, min(self.workers, maximum) + 1):
            item = self.active.get(worker, {})
            state = str(item.get('state') or item.get('phase', 'waiting'))
            style = 'green' if state == 'registered' else 'red' if state in ('uncertain','blocked_input','rejected') else 'yellow' if state in ('get_retry','ready') else 'cyan'
            cells=[str(worker)]
            if self.connections:
                name=item.get('connection','—');cells.append(str(self.connections.get(name,{}).get('label',name)))
            table.add_row(*cells,str(item.get('uc', '—')),str(item.get('roll', '—')),Text(state.replace('_', ' '),style=style))
        if self.workers > maximum: table.add_row('…',*(['']*(3 if self.connections else 2)),f'{self.workers - maximum} more workers')
        uc = Table(box=None, padding=(0, 1))
        uc.add_column('UC'); uc.add_column('Saved now', justify='right')
        for number, count in sorted(self.uc.items()): uc.add_row(str(number), str(count))
        if not self.uc: uc.add_row('—', '0')
        recent = '\n'.join(tuple(self.recent)) or 'Waiting for confirmed registration receipts…'
        routes=Text(' · '.join(f"{row.get('label',name)}: {row.get('submission_interval_seconds',row.get('dispatch_interval_seconds',0)):g}s / "
            + ('offline; waiting for reconnect' if row.get('health')=='offline' else
               'unreachable; rechecking egress' if row.get('health')=='unreachable' else
               'paused; check route configuration' if row.get('health')=='paused' else
               'checking route availability' if row.get('health')=='checking' else
               f"server cooldown {max(0,math_ceil(row['server_until']-time.monotonic()))}s" if row.get('server_until',0)>time.monotonic() else
               f"retry in {max(0,math_ceil(row['transport_until']-time.monotonic()))}s" if row.get('transport_until',0)>time.monotonic() else
               'degraded; rechecking egress' if row.get('health')=='degraded' else 'ready')
            for name,row in tuple(self.connections.items())),style='cyan')
        batch=self.batch.summary()
        current=Text()
        if batch:
            current.append(f"Batch total {batch['total']:,}   Completed {batch['completed']:,}   ",style='bold')
            current.append(f"REMAINING {batch['remaining']:,}",style='bold yellow')
            current.append(f"   Held {batch['held']:,}\n",style='bold')
            current.append(f"Completed before this run {batch['completed_before_run']:,}   Completed this run {batch['completed_this_run']:,}   ",style='cyan')
        current.append(f"Run selected at start {self.total:,}   Left to process this run {max(0,self.total-self.completed):,}",style='cyan')
        if self.batch_error:current.append('   Batch refresh unavailable: '+self.batch_error,style='yellow')
        if self.progress_error:current.append('   Progress file update failed: '+self.progress_error,style='yellow')
        panels=[Panel(Group(header,current,stats,pipeline,routes,self.progress), border_style='cyan')]
        if self.connections:
            by_route=Table(expand=True,box=None)
            for label in ('Connection','Workers','POSTs','Saved','Already registered','Uncertain','Retries','Spacing','State'):
                by_route.add_column(label)
            for name,row in tuple(self.connections.items()):
                counts=self.connection_counts.get(name,{})
                by_route.add_row(str(row.get('label',name)),str(row.get('workers','—')),
                    *(str(counts.get(key,0)) for key in ('submitted','registered','already_registered_external','uncertain','uncertain_retries')),
                    f"{row.get('submission_interval_seconds',row.get('dispatch_interval_seconds',0)):g}s",
                    Text(row['health'] if row.get('health') in ('offline','unreachable','paused','checking') else 'server wait' if row.get('server_until',0)>time.monotonic() else
                        'retrying' if row.get('transport_until',0)>time.monotonic() else row.get('health','checking'),
                        style='green' if row.get('health')=='online' and row.get('cooldown_until',0)<=time.monotonic() else 'yellow'))
            panels.append(Panel(by_route,title='Connections · this run',border_style='cyan'))
        return Group(*panels,Columns([Panel(table, title='Worker activity'), Panel(uc, title='UC registrations')], expand=True),
            Panel(Text(recent), title='Recent results', border_style='green'),
            Text('Ctrl-C: stop new work; finish in-flight requests. Receipts and tracking IDs stay in SQLite.', style='dim'))

    def __exit__(self, *args):
        if self.live: self.live.stop()
        if self.log: self.log.close()


def print_summary(result, console=None, show_ucs=False):
    console = console or Console()
    values = {**result, **(result.get('summary', {}) if isinstance(result.get('summary'), dict) else {})}
    table = Table(title='Youth Games · Results', show_header=False, box=None)
    if result.get('batch_progress'):
        batch=result['batch_progress']
        for label,key in [('Batch total','total'),('Completed before this run','completed_before_run'),('Completed this run','completed_this_run'),
            ('Completed in batch (incl. portal duplicates)','completed'),('REMAINING in batch','remaining'),('Held in batch','held')]:
            table.add_row(label,f"{batch[key]:,}")
    if 'queued' in result:table.add_row('Run selected at start',f"{result['queued']:,}")
    for label, key in [('Status','status'),('Assigned students','assigned'),('Processed now','processed'),('Registered now','registered_now'),('Already registered (skipped)','already_registered_now'),
                       ('Registered in allocation','registered'),('Ready remaining','ready_remaining'),
                       ('Normalized students','normalized'),('Age eligible','age_eligible'),('Ready to register','ready_to_register'),
                       ('Ready input records','ready_records'),('Previously completed (SQLite skip)','skipped_registered'),
                       ('Prior uncertain queued for retry','uncertain_requeued'),
                       ('Uncertain response retries','uncertain_retry_attempts'),
                       ('Quarantined records','needs_attention'),('Disabled-route retries deferred','deferred_disabled_connections'),('Duplicate/invalid records','normalization_blocked'),('Workers','workers'),('Internet connections','connections'),('Pacing scope','pacing_scope')]:
        if key in values: table.add_row(label, f'{values[key]:,}' if type(values[key]) is int else str(values[key]))
    if 'global_submission_interval_seconds' in values:
        table.add_row('Portal-wide submission spacing', str(values['global_submission_interval_seconds']) + ' seconds')
    if 'throttle_refusals' in values:
        table.add_row('Explicit throttle refusals (resumable)', str(values['throttle_refusals']))
    if 'auto_recover' in values:
        table.add_row('Automatic temporary-error recovery', 'On' if values['auto_recover'] else 'Off (bounded retries)')
    if 'safe_form_retries' in values:
        table.add_row('Undispatched form retries', str(values['safe_form_retries']))
    for label,key in [('Preparation window','prepare_ahead'),('Maximum in-flight POSTs','max_in_flight'),
                      ('Peak parallel form GETs','peak_preparing'),('Peak in-flight POSTs','peak_in_flight'),('Refreshed forms','refreshed_forms')]:
        if key in values: table.add_row(label,str(values[key]))
    for label,key in [('Browser POSTs dispatched','dispatched_posts'),('HTTP 429 responses','http_429_responses'),
                      ('Form throttle refusals','form_throttle_refusals')]:
        if key in values.get('diagnostics',{}):table.add_row(label,str(values['diagnostics'][key]))
    if 'pilot_passed' in result:
        table.add_row('UC pilot', 'Verified' if result['pilot_passed'] else 'Needs attention')
        table.add_row('New submissions', str(result.get('attempted_now', 0)))
    console.print(Panel(table, border_style='yellow' if result.get('blocked') or result.get('status') in ('stopped','blocked') else 'green'))
    if result.get('connection_results'):
        routes=Table('Connection','Workers','POSTs','Saved','Already registered','Uncertain','Retries',title='Connections · this run')
        for name,counts in result['connection_results'].items():
            routes.add_row(result.get('connection_labels',{}).get(name,name),str(result.get('workers_by_connection',{}).get(name,'—')),
                *(str(counts.get(key,0)) for key in ('submitted','registered','already_registered_external','uncertain','uncertain_retries')))
        console.print(routes)
    if result.get('blocked'):console.print(Text('Stopped: '+result['blocked'],style='yellow'))
    if show_ucs and result.get('ucs'):
        uc=Table('UC','Assigned','Registered','Ready','Blocked')
        for row in result['ucs']:uc.add_row(str(row['uc_number']),str(row['assigned']),str(row['registered']),str(row['ready_remaining']),str(row['normalization_blocked']))
        console.print(uc)
    for label,key in [('Details','report'),('Needs attention','attention_report'),('Run log','log'),('Output','output')]:
        if result.get(key): console.print(Text(f'{label}: {result[key]}', style='dim'))
