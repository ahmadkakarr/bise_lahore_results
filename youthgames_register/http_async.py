"""Concurrent HTTPX workers with one SQLite writer and one-shot applicant POSTs."""

import asyncio
import csv
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import signal
import shutil
import time
from urllib.parse import urljoin, urlparse

import httpx

from .config import load, resolved, input_snapshot, signature, sha
from .domain import classify
from .http_pilot import BASE, Response, prepare_form, prepare_applicants, multipart, receipt
from .app import attempt_folder
from .reports import output_folder
from .store import opened, import_batch, jobs, begin, intent, dispatched, finish, now, known_registrations, ready_job_refs, last_dispatch_time, authorize_uncertain_retries
from .http_recovery import recover_rate_limited, cooldown_remaining
from .name_recovery import recover_name_formatting
from youthgames_shared.names import name_changes

MAX_HTTP_WORKERS = 36
MAX_RESPONSE = 6 * 1024 * 1024
RATE_HEADERS = {'retry-after','ratelimit','ratelimit-policy','ratelimit-limit','ratelimit-remaining','ratelimit-reset',
                'x-ratelimit-limit','x-ratelimit-remaining','x-ratelimit-reset'}


@dataclass
class HttpResponse:
    status: int
    html: str
    retry_after: str | None = None
    rate_headers: dict | None = None
    hops: list | None = None


class Client:
    """One pooled connection per worker, isolated cookies, explicit safe redirects."""
    def __init__(self, transport=None, route=None):
        if transport is not None and route is not None:raise ValueError('Use one explicit transport or route')
        self.route,self.transport=route,transport
        self.client = self.make_client()
        self.sent = False
        self.needs_reset = False
        self.last_hops=[];self.request_stage={};self.route_generation=None

    def make_client(self):
        return httpx.AsyncClient(transport=self.route.transport() if self.route else self.transport, follow_redirects=False,trust_env=self.route is None,
            timeout=httpx.Timeout(connect=15, read=90, write=30, pool=10),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=60),
            headers={'User-Agent':'YouthGames-HTTP/2.0', 'Accept':'text/html'})

    async def reset(self):
        """Called between requests only; never replay the prior POST."""
        try:await self.client.aclose()
        except (httpx.TransportError,OSError):pass  # A dead socket must not prevent replacement.
        self.client=self.make_client()
        self.needs_reset=False

    async def __aenter__(self): return self
    async def __aexit__(self, *args): await self.client.aclose()

    async def request(self, method, url, body=None, headers=None):
        observed = {};hops=[];self.last_hops=hops
        async with asyncio.timeout(180 if method=='POST' else 60):
            for _ in range(6):
                parsed = urlparse(url)
                if (parsed.scheme, parsed.netloc) != ('https','youthgames.punjab.gov.pk'):
                    raise ValueError('Redirect left the official portal')
                self.request_stage={'method':method,'path':parsed.path or '/','phase':'waiting_for_headers'}
                async with self.client.stream(method, url, content=body, headers=headers) as response:
                    self.request_stage.update(status=response.status_code,phase='reading_body')
                    rate_headers={name:value[:500] for name,value in response.headers.items() if name in RATE_HEADERS}
                    observed.update(rate_headers)
                    hops.append({'method':method,'path':parsed.path or '/', 'status':response.status_code,'rate_headers':rate_headers})
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE: raise ValueError('Server response exceeds 6 MiB')
                    self.request_stage['phase']='response_received'
                    status = response.status_code
                    if status in (301,302,303,307,308) and response.headers.get('location'):
                        if method == 'POST' and status not in (301,302,303):
                            raise ValueError('POST-preserving redirect blocked; request will not be replayed')
                        url = urljoin(url, response.headers['location'])
                        method, body, headers = 'GET', None, None
                        continue
                    return HttpResponse(status, bytes(data).decode('utf-8'), observed.get('retry-after'), observed,hops)
            raise ValueError('Too many redirects')

    async def get(self):
        # Keep the TCP pool, start a fresh cookie/CSRF session for each applicant.
        self.client.cookies.clear(); self.sent = False
        response = await self.request('GET', BASE + '/')
        if response.status == 200 and not list(self.client.cookies.jar):
            raise ValueError('Portal did not establish a cookie session')
        return response

    async def post(self, body, content_type):
        if self.sent: raise ValueError('Applicant POST already sent; replay blocked')
        self.sent = True
        return await self.request('POST', BASE + '/applicant', body,
            {'Content-Type':content_type, 'Origin':BASE, 'Referer':BASE+'/'})


class Control:
    def __init__(self, submission_interval=5.0, prepare_ahead=4, max_in_flight=4, form_max_age=30.0):
        self.stop = asyncio.Event()
        self.deadline = 0.0
        self.transport_deadline=0.0;self.transport_retry_round=0
        self.waiters=set()
        self.request_gate=self.network_generation=self.on_transport_error=self.on_transport_success=None
        self.reason = None
        # Only dispatch scheduling holds this lock. GETs and POST response waits
        # have independent bounds, while each worker retains its own cookie jar.
        self.submission_lock = asyncio.Lock()
        self.submission_interval = submission_interval
        self.next_submission = 0.0
        self.prepare_ahead = prepare_ahead
        self.max_in_flight = max_in_flight
        self.form_max_age = form_max_age
        self.preparation_slots = asyncio.Semaphore(prepare_ahead)
        self.post_slots = asyncio.Semaphore(max_in_flight)
        self.generation = 0
        self.preparing = self.in_flight = 0
        self.peak_preparing = self.peak_in_flight = 0
        self.refreshed_forms = 0
        self.auto_recover = True
        self.continue_on_uncertain = False
        self.database = self.portal = self.campaign = None
        self.connection_key = self.connection_name = None
        self.retry_round = self.throttle_round = 0
        self.retry_episode_until = 0.0
        self.safe_form_retries = 0

    def bind(self, db, portal, campaign, connection_key=None, connection_name=None, requested_interval=None):
        """Restore portal-wide pacing without altering any applicant state."""
        self.database, self.portal, self.campaign = db, portal, campaign
        self.connection_key,self.connection_name=connection_key,connection_name
        saved = db.execute('SELECT * FROM portal_pacing WHERE portal=? AND campaign=?', (portal, campaign)).fetchone()
        records=[saved] if saved else []
        if connection_key:
            scoped=db.execute('SELECT * FROM connection_pacing WHERE portal=? AND campaign=? AND connection_key=?',
                (portal,campaign,connection_key)).fetchone()
            if scoped:records.append(scoped)
        for saved in records:
            # An explicit operator baseline may replace an old non-throttle
            # baseline. Actual server-throttle spacing and all deadlines remain.
            if requested_interval is None or saved['throttle_round']:
                self.submission_interval = max(self.submission_interval, saved['dispatch_interval'])
            self.retry_round=max(self.retry_round,saved['retry_round']);self.throttle_round=max(self.throttle_round,saved['throttle_round'])
            remaining = max(0, (datetime.fromisoformat(saved['not_before']) - datetime.now(timezone.utc)).total_seconds())
            key=saved['connection_key'] if 'connection_key' in saved.keys() else ''
            typed=db.execute('SELECT * FROM recovery_delays WHERE portal=? AND campaign=? AND connection_key=?',(portal,campaign,key)).fetchone()
            if typed:
                def remaining_at(stamp):return max(0,(datetime.fromisoformat(stamp)-datetime.now(timezone.utc)).total_seconds()) if stamp else 0
                server=remaining_at(typed['server_not_before']);transport=remaining_at(typed['transport_not_before'])
                self.retry_episode_until=max(self.retry_episode_until,time.monotonic()+server)
                self.delay(server);self.transport_delay(transport)
                self.transport_retry_round=max(self.transport_retry_round,typed['transport_round'])
            else:
                # Legacy rows combined both kinds of wait. Even a transport
                # reason might include an earlier Retry-After; preserve it.
                self.retry_episode_until = max(self.retry_episode_until,time.monotonic()+remaining)
                self.delay(remaining)

    def backoff(self, seconds, reason, *, throttle=False, temporary_transport=False):
        """Coalesce simultaneous refusals; honor the server plus bounded backoff.

        Transport waits cap at 30 seconds and reset on recovery. Server
        backoff caps at five minutes; a longer server wait always wins.
        Spacing only becomes more conservative, including across restarts.
        """
        if not self.auto_recover:
            self.delay(seconds)
            return seconds
        current = time.monotonic()
        if temporary_transport and not throttle:
            if current>=self.transport_deadline:self.transport_retry_round+=1
            delay=max(seconds,min(30,5*2**min(max(0,self.transport_retry_round-1),3)),self.transport_deadline-current)
            self.transport_delay(delay)
            self.persist(reason)
            return delay
        new_episode = current >= self.retry_episode_until
        if new_episode:
            self.retry_round += 1
        if throttle and (new_episode or not self.throttle_round):
            self.throttle_round += 1
            self.submission_interval = max(self.submission_interval, min(60.0, 5.0 * 1.5 ** min(self.throttle_round, 7)))
        delay = max(seconds, min(300.0, 15.0 * 2 ** min(max(0, self.retry_round - 1), 5)),
                    self.retry_episode_until - current)
        self.retry_episode_until = current + delay
        self.delay(delay)
        self.persist(reason)
        return delay

    @property
    def effective_deadline(self):return max(self.deadline,self.transport_deadline)

    def persist(self,reason):
        if self.database is not None:
            clock=time.monotonic();utc=datetime.now(timezone.utc)
            def stamp(deadline):return (utc+timedelta(seconds=max(0,deadline-clock))).isoformat() if deadline>clock else None
            until=stamp(self.effective_deadline) or utc.isoformat()
            with self.database:
                if self.connection_key:
                    self.database.execute('''INSERT INTO connection_pacing VALUES (?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(portal,campaign,connection_key) DO UPDATE SET dispatch_interval=excluded.dispatch_interval,
                        retry_round=excluded.retry_round,throttle_round=excluded.throttle_round,
                        not_before=excluded.not_before,reason=excluded.reason,updated_at=excluded.updated_at''',
                        (self.portal,self.campaign,self.connection_key,self.submission_interval,self.retry_round,
                         self.throttle_round,until,reason,now()))
                else:self.database.execute('''INSERT INTO portal_pacing VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(portal,campaign) DO UPDATE SET dispatch_interval=excluded.dispatch_interval,
                    retry_round=excluded.retry_round,throttle_round=excluded.throttle_round,
                    not_before=excluded.not_before,reason=excluded.reason,updated_at=excluded.updated_at''',
                    (self.portal, self.campaign, self.submission_interval, self.retry_round,
                     self.throttle_round, until, reason, now()))
                self.database.execute('''INSERT INTO recovery_delays VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(portal,campaign,connection_key) DO UPDATE SET server_not_before=excluded.server_not_before,
                    transport_not_before=excluded.transport_not_before,transport_round=excluded.transport_round,updated_at=excluded.updated_at''',
                    (self.portal,self.campaign,self.connection_key or '',stamp(self.deadline),stamp(self.transport_deadline),self.transport_retry_round,now()))

    def transport_delay(self,value):
        if value>0:
            self.transport_deadline=max(self.transport_deadline,time.monotonic()+value)
            self.generation+=1;self.wake_waiters()

    def transport_recovered(self):
        if not self.transport_retry_round and not self.transport_deadline:return False
        self.transport_retry_round=0;self.transport_deadline=0
        self.wake_waiters();self.persist('transport_recovered')
        return True

    def delay(self, value):
        if value > 0:
            self.deadline = max(self.deadline, time.monotonic() + value)
            self.generation += 1  # Discard prepared forms across a server cooldown.
            self.wake_waiters()

    def wake_waiters(self):
        # Each waiter owns its signal: one resuming worker cannot clear a
        # shared Event before another worker has observed the notification.
        for signal in tuple(self.waiters):
            if not signal.done():signal.set_result(None)

    async def wait(self, seconds=0):
        until = time.monotonic() + seconds
        while not self.stop.is_set():
            delay = max(until, self.effective_deadline) - time.monotonic()
            if delay <= 0: return True
            signal=asyncio.get_running_loop().create_future()
            self.waiters.add(signal)
            waiting=[asyncio.create_task(self.stop.wait()),signal]
            try:await asyncio.wait(waiting,timeout=delay,return_when=asyncio.FIRST_COMPLETED)
            finally:
                self.waiters.discard(signal)
                for task in waiting:
                    if not task.done():task.cancel()
                await asyncio.gather(*waiting,return_exceptions=True)
        return False


def retry_seconds(value):
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 5.0
    except (TypeError,ValueError):
        try: return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError,ValueError,OverflowError): return 5.0


class FormUnavailable(Exception):
    """Safe GET failed; the applicant has no external submission intent."""
    def __init__(self, message, temporary=False):
        super().__init__(message)
        self.temporary = temporary


class PreflightStopped(Exception):
    """Operator stopped a safe preflight wait; still produce a run summary."""


async def get_form(client, control, emit, metadata):
    for retry in range(3):
        if control.request_gate is not None and not await control.request_gate():raise asyncio.CancelledError()
        if not await control.wait(): raise asyncio.CancelledError()
        if control.request_gate is not None and not await control.request_gate():raise asyncio.CancelledError()
        try:
            if getattr(client,'needs_reset',False):await client.reset()
            if control.network_generation is not None:
                generation=control.network_generation()
                if getattr(client,'route_generation',None) not in (None,generation):await client.reset()
                client.route_generation=generation
            response = await client.get()
            if response.rate_headers: emit('portal_limits',method='GET',headers=response.rate_headers,hops=getattr(response,'hops',None),**metadata)
            if response.status == 200:
                if control.on_transport_success is not None:control.on_transport_success()
                if control.transport_recovered():emit('recovery_reset',server_seconds=max(0,control.deadline-time.monotonic()),**metadata)
                return response
            if response.status not in (429,500,502,503,504):
                raise FormUnavailable(f'Form GET returned HTTP {response.status}')
            seconds = retry_seconds(response.retry_after) + 2 if response.retry_after else 2 ** (retry + 1)
            if control.auto_recover:
                seconds = control.backoff(seconds, f'temporary_form_get_http_{response.status}', throttle=response.status == 429)
                emit('cooldown', seconds=seconds, message=f'Temporary HTTP {response.status} on form GET; assigned workers wait', **metadata)
                emit('pacing_update', submission_interval_seconds=control.submission_interval, **metadata)
            else:
                control.delay(seconds)
        except (httpx.TransportError,TimeoutError) as error:
            if control.on_transport_error is not None:control.on_transport_error(type(error).__name__)
            if hasattr(client,'reset'):await client.reset()
            seconds = 2 ** (retry + 1)
            if control.auto_recover:
                seconds = control.backoff(seconds, 'temporary_form_get_transport_error',temporary_transport=True)
                emit('cooldown', seconds=seconds,kind='transport',error_type=type(error).__name__,message='Temporary form GET connection error; route retry after short wait', **metadata)
            else:
                control.delay(seconds)
        if retry == 2: raise FormUnavailable('Form GET failed after three safe attempts', temporary=True)
        emit('get_retry', attempt=retry+1, **metadata)


async def preflight_form(factory, control, emit):
    """Safe GET outages can wait unattended before any applicant is claimed."""
    while not control.stop.is_set():
        try:
            async with factory(0) as client:
                return await get_form(client, control, emit, {})
        except FormUnavailable as error:
            if not error.temporary or not control.auto_recover:
                raise
            delay = control.backoff(0, 'temporary_preflight_get_failure',temporary_transport=True)
            emit('cooldown', seconds=delay,kind='transport',message='Temporary form GET failure; automatically retrying after backoff')
            if not await control.wait(): break
        except asyncio.CancelledError:
            if control.stop.is_set(): raise PreflightStopped() from None
            raise
    raise PreflightStopped()


def reschedule_uncertain(db,config,directory,outcome,control,retries,maximum,emit):
    """Explicit opt-in: bounded retries on this route; retain unresolved history."""
    if outcome['state']!='uncertain' or not control.continue_on_uncertain or control.stop.is_set():return False
    job=outcome['job'];retries[job]+=1
    if retries[job]<=maximum:
        authorized=authorize_uncertain_retries(db,config,directory,job_ids=[job])
        if authorized:
            seconds=control.backoff(0,'uncertain_response_retry',temporary_transport=True)
            emit('uncertain_retry',**outcome,retry=retries[job],maximum=maximum,seconds=seconds)
            emit('cooldown',connection=control.connection_name,seconds=seconds,kind='transport',
                message='Response uncertain; fresh attempt after cooldown on the same connection')
            return True
    emit('uncertain_quarantined',**outcome,retries=max(0,retries[job]-1),
        reason='Unresolved response retained in SQLite; continuing other applicants')
    return False


async def process(db, job, client, config, destination, source_rows, control, emit, worker):
    item = json.loads(job['payload'])
    allocation = item.get('administrative_allocation', {})
    meta = {'worker':worker, 'job':job['id'], 'roll':job['roll'], 'uc':allocation.get('uc_number'),'group':item['data'].get('Tehsil','')}
    if control.connection_name:meta['connection']=control.connection_name
    attempt = None; sent = False; committed = False; post_slot = False; post_started = None
    order=getattr(control,'dispatch_order',None)
    if order:order.enqueue(job['id'])
    if control.stop.is_set(): return None
    attempt = begin(db, job)
    folder = attempt_folder(destination, job['id'], attempt)
    try:
        if signature(item) != job['payload_hash'] or item['data'] != source_rows.get(item['data']['BForm']):
            raise ValueError('Prepared applicant differs from the pinned input snapshot')
        from .photos import matches_profile
        if item['operator_confirmation'] != config['confirmation'] or not matches_profile(config,item['photo']):
            raise ValueError('Prepared applicant confirmation/photo differs from the pinned profile')
        emit('waiting_for_preparation', **meta)
        # This bounded window owns complete sessions until dispatch, rather than
        # preparing all queued applicants and letting their challenges go stale.
        async with control.preparation_slots:
            for refresh in range(4):
                if not await control.wait(): raise asyncio.CancelledError()
                generation = control.generation
                fetched_at = time.monotonic()  # Conservative age includes GET time.
                emit('fetching_form', **meta)
                control.preparing += 1
                control.peak_preparing = max(control.peak_preparing, control.preparing)
                try:
                    form = await get_form(client, control, emit, meta)
                finally:
                    control.preparing -= 1
                fields, catalog_hash = prepare_form(form.html, config, item)
                body, content_type = multipart(fields, item['photo'], item['photo']['sha256'])
                emit('prepared', form_seconds=round(time.monotonic()-fetched_at,3), **meta)
                with db: db.execute("UPDATE jobs SET state='filled',updated_at=? WHERE id=?", (now(),job['id']))
                emit('waiting_for_portal_slot', **meta)
                if order:await order.wait(job['id'],control.stop)
                await control.post_slots.acquire()
                post_slot = True
                async with control.submission_lock:
                    if not await control.wait(max(0, control.next_submission-time.monotonic())):
                        raise asyncio.CancelledError()
                    if control.request_gate is not None and not await control.request_gate():raise asyncio.CancelledError()
                    age = time.monotonic()-fetched_at
                    if generation != control.generation or age >= control.form_max_age:
                        control.post_slots.release(); post_slot = False
                        control.refreshed_forms += 1
                        emit('refreshing_form', reason='cooldown' if generation != control.generation else 'age', **meta)
                        continue
                    proof = {'transport':'http', 'client':'httpx', 'method':'POST', 'endpoint':BASE+'/applicant',
                        'multipart_sha256':sha(body), 'payload_hash':job['payload_hash'], 'photo_sha256':item['photo']['sha256'],
                        'live_catalog_sha256':catalog_hash, 'field_names':sorted(fields), 'maximum_post_attempts':1,
                        'captcha_read_from_fresh_form':True, 'form_age_seconds':round(age,3), 'worker':worker,
                        'name_formatting':name_changes(item['data'])}
                    if item.get('provisional_dob'):proof['provisional_dob']=item['provisional_dob']
                    if item['data']['SubCategory']=='Private':
                        proof['institute_submission']={'mode':'private_manual','institution_id':fields['institution_id'],'institute_name':fields['institute_name']}
                    if control.connection_key:
                        proof.update(connection=control.connection_name,connection_key=control.connection_key)
                    intent(db,job['id'],attempt)
                    if order:dispatched(db,job['id'],attempt,control.connection_key,control.connection_name,ordering=order)
                    elif control.connection_key:dispatched(db,job['id'],attempt,control.connection_key,control.connection_name)
                    else:dispatched(db,job['id'],attempt)
                    control.next_submission = time.monotonic() + control.submission_interval
                    sent = True
                break
            else:
                raise FormUnavailable('Prepared form repeatedly aged out; no POST dispatched', temporary=True)
        # Release the preparation window immediately. Only the independent POST
        # semaphore remains held until this response and its receipt are saved.
        emit('submitting', **meta)
        control.in_flight += 1
        control.peak_in_flight = max(control.peak_in_flight, control.in_flight)
        post_started = time.monotonic()
        emit('awaiting_response', **meta)
        # Work between durable intent and the socket write must not consume the
        # minimum spacing between actual POST starts.
        control.next_submission=max(control.next_submission,time.monotonic()+control.submission_interval)
        response = await client.post(body,content_type)
        if response.rate_headers: emit('portal_limits',method='POST',headers=response.rate_headers,hops=getattr(response,'hops',None),**meta)
        proof['response_rate_headers'] = response.rate_headers or {}
        proof['response_hops'] = getattr(response,'hops',None) or []
        if response.retry_after and response.status in (429,503): control.delay(retry_seconds(response.retry_after))
        outcome = classify(response.html,response.status)
        emit('saving_result', **meta)
        path = receipt(folder,job,attempt,outcome,response,proof)
        finish(db,job['id'],attempt,outcome['state'],outcome['detail'],path,outcome.get('tracking'))
        committed = True
        detail = None
        if outcome.get('rate_limited'):
            recovered = recover_rate_limited(db, config, {job['id']})
            if not recovered:
                raise ValueError('Rate-limit receipt could not be verified for recovery')
            delay = control.backoff(recovered[0]['wait_seconds'], 'explicit_server_throttle', throttle=True)
            detail = outcome['detail']  # Strictly matched public throttle message.
            emit('cooldown', seconds=round(delay, 1), message=detail, **meta)
            emit('pacing_update', submission_interval_seconds=control.submission_interval, **meta)
        elif outcome['state']=='rejected' and control.auto_recover:
            detail=outcome['detail']
            emit('quarantined',reason=detail,receipt=str(path),**meta)
        elif outcome['state']=='uncertain' and control.continue_on_uncertain:
            emit('uncertain_saved',reason=outcome['detail'],receipt=str(path),**meta)
        elif outcome['state'] not in ('registered', 'already_registered_external'):
            control.reason = outcome['state'] + ': portal outcome requires review of saved receipt'
            control.stop.set()
        return {**meta,'state':outcome['state'],'tracking':outcome.get('tracking'),'post_dispatched':True,
                'rate_limited':outcome.get('rate_limited', False), 'detail':detail,
                'post_seconds':round(time.monotonic()-post_started,3)}
    except BaseException as error:
        if committed:
            # A renderer/audit I/O error must never rewrite a committed server
            # receipt or turn a confirmed registration into an unknown outcome.
            control.reason = 'Local post-processing error: ' + type(error).__name__
            control.stop.set()
            saved = db.execute('SELECT state,tracking FROM jobs WHERE id=?', (job['id'],)).fetchone()
            return {**meta,'state':saved['state'],'tracking':saved['tracking'],'post_dispatched':True,'error':type(error).__name__}
        # Even cancellation after dispatch retains uncertainty and its receipt.
        state = 'uncertain' if sent else 'ready' if isinstance(error,(asyncio.CancelledError,FormUnavailable)) else 'blocked_input'
        if sent:
            if isinstance(error,(httpx.TransportError,TimeoutError)) and hasattr(client,'reset'):
                client.needs_reset=True  # Replace before a fresh GET; never replay this POST.
            outcome = {'state':'uncertain','tracking':None,'detail':'HTTP interrupted after durable dispatch: '+type(error).__name__}
            proof['transport_error'] = type(error).__name__
            proof['response_hops']=getattr(client,'last_hops',[])[:]
            proof['interrupted_request']=getattr(client,'request_stage',{})
            proof['post_seconds']=round(time.monotonic()-post_started,3) if post_started is not None else None
            path = receipt(folder,job,attempt,outcome,Response(0,''),proof)
        else:
            path = None
        finish(db,job['id'],attempt,state,type(error).__name__+': '+str(error)[:2000],path)
        if (sent and isinstance(error,(httpx.TransportError,TimeoutError))
                and control.continue_on_uncertain and not control.stop.is_set()):
            emit('uncertain_saved',reason=type(error).__name__,receipt=str(path),**meta)
            return {**meta,'state':'uncertain','tracking':None,'post_dispatched':True,
                'error':type(error).__name__,'detail':'Response not captured; durable uncertain receipt preserved'}
        if (not sent and isinstance(error, FormUnavailable) and error.temporary
                and control.auto_recover and not control.stop.is_set()):
            control.safe_form_retries += 1
            delay = control.backoff(0, 'temporary_undispatched_form_failure',temporary_transport=True)
            emit('cooldown', seconds=delay,kind='transport', message='Temporary form failure before dispatch; applicant stays Ready', **meta)
            return {**meta,'state':'ready','tracking':None,'post_dispatched':False,'retryable_form':True}
        if not sent and state=='blocked_input' and order and control.auto_recover:
            emit('quarantined',reason=str(error)[:500],**meta)
            return {**meta,'state':state,'tracking':None,'post_dispatched':False,'error':type(error).__name__}
        control.reason = control.reason or ('uncertain_submission_requires_reconciliation' if sent else type(error).__name__)
        control.stop.set()
        if isinstance(error,(KeyboardInterrupt,SystemExit)): raise
        return {**meta,'state':state,'tracking':None,'post_dispatched':sent,'error':type(error).__name__}
    finally:
        if order:order.release(job['id'])
        if post_slot:
            control.post_slots.release()
        if post_started is not None:
            control.in_flight -= 1


async def run(profile_path, workers=18, limit=0, ui=None, client_factory=None, control=None, install_signals=True,
              prepare_ahead=4, max_in_flight=4, auto_recover=True, dispatch_interval=None, connections=None, retry_uncertain=False, uncertain_retries=2, proxy_config=None):
    if type(retry_uncertain) is not bool:raise ValueError('retry_uncertain must be boolean')
    if type(uncertain_retries) is not int or not 0<=uncertain_retries<=5:raise ValueError('uncertain_retries must be 0–5')
    if proxy_config is not None and connections is None:raise ValueError('Optional proxy requires explicit --connections')
    if connections is not None:
        if control is not None:raise ValueError('Multi-connection mode manages its own connection controls')
        from .http_multi import run as multi_run
        return await multi_run(profile_path,connections,workers=workers,limit=limit,ui=ui,
            client_factory=client_factory,install_signals=install_signals,prepare_ahead=prepare_ahead,
            max_in_flight=max_in_flight,auto_recover=auto_recover,dispatch_interval=dispatch_interval,retry_uncertain=retry_uncertain,
            uncertain_retries=uncertain_retries,proxy_config=proxy_config)
    if type(workers) is not int or not 1 <= workers <= MAX_HTTP_WORKERS:
        raise ValueError(f'HTTP workers must be 1–{MAX_HTTP_WORKERS}')
    if type(limit) is not int or limit < 0: raise ValueError('limit must be a nonnegative integer')
    if type(prepare_ahead) is not int or not 1 <= prepare_ahead <= 8:
        raise ValueError('prepare_ahead must be 1–8')
    if type(max_in_flight) is not int or not 1 <= max_in_flight <= MAX_HTTP_WORKERS:
        raise ValueError(f'max_in_flight must be 1–{MAX_HTTP_WORKERS}')
    if type(auto_recover) is not bool:raise ValueError('auto_recover must be boolean')
    if dispatch_interval is not None and (type(dispatch_interval) not in (int,float) or not math.isfinite(dispatch_interval) or dispatch_interval < 5):
        raise ValueError('dispatch_interval must be finite and at least five seconds')
    config = load(profile_path)
    if config['captcha']['mode'] != 'auto':
        raise ValueError('Direct HTTP submission requires automatic CAPTCHA mode')
    factory = client_factory or (lambda worker:Client())
    baseline=dispatch_interval if dispatch_interval is not None else config['pacing']['submission_interval_seconds']
    control = control or Control(submission_interval=max(5.0, baseline),
        prepare_ahead=min(workers,prepare_ahead), max_in_flight=min(workers,max_in_flight))
    control.auto_recover = auto_recover
    control.continue_on_uncertain = retry_uncertain and auto_recover
    emit = ui.event if ui else lambda *args,**kwargs:None
    database = resolved(profile_path,config['state']['database'])
    destination = output_folder(resolved(profile_path,config['state']['output']),config['profile'])
    directory = destination/'Runs'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory.mkdir(parents=True,exist_ok=False)
    if ui: ui.attach(directory)
    emit('pipeline', prepare_ahead=control.prepare_ahead, max_in_flight=control.max_in_flight,
         submission_interval_seconds=control.submission_interval, form_max_age_seconds=control.form_max_age)
    if any(shutil.disk_usage(path).free < 2*1024**3 for path in (destination,database.parent)):
        result={'status':'stopped','blocked':'low_disk_space','processed':0,'registered_now':0,'workers':workers,
            'transport':'httpx','browser_launched':False,'report':str(directory/'Summary.json'),'log':str(directory/'events.jsonl')}
        emit('stage',message='Stopping before input copy: less than 2 GiB free')
        (directory/'Summary.json').write_text(json.dumps(result,indent=2)+'\n')
        return result
    counts=Counter(); peak=active=0; interrupted=False; tasks=[];uncertain_requeued=[]
    loop=asyncio.get_running_loop(); old_handlers={}
    def stop():
        nonlocal interrupted
        if control.stop.is_set():
            for task in tasks: task.cancel()
        interrupted=True;control.stop.set()
        emit('stage',message='Stopping new work; preserving in-flight receipts')
    if install_signals:
        for sig in (signal.SIGINT,signal.SIGTERM):
            try:
                old_handlers[sig]=signal.getsignal(sig);loop.add_signal_handler(sig,stop)
            except (ValueError,NotImplementedError,RuntimeError):old_handlers.pop(sig,None)
    try:
        with opened(database) as db, input_snapshot(config,profile_path,allow_empty=True) as (rows,origins,copies,hashes):
            repaired=recover_name_formatting(db,config)
            if repaired:emit('stage',message=f'Recovered {len(repaired)} receipt-proven name-format rejections')
            control.bind(db, BASE, config['campaign'],requested_interval=dispatch_interval)
            emit('pipeline', prepare_ahead=control.prepare_ahead, max_in_flight=control.max_in_flight,
                submission_interval_seconds=control.submission_interval, form_max_age_seconds=control.form_max_age)
            saved_wait=max(0,control.effective_deadline-time.monotonic())
            if saved_wait:
                emit('cooldown',seconds=saved_wait,message='Restored portal backoff from SQLite; resuming when it expires')
            uncertain=db.execute("""SELECT count(*) FROM jobs j WHERE portal=? AND campaign=? AND state='uncertain'
                AND EXISTS(SELECT 1 FROM members m JOIN profiles p ON p.batch=m.batch WHERE m.job=j.id AND p.name=?)""",
                (BASE,config['campaign'],config['profile'])).fetchone()[0]
            if uncertain and not retry_uncertain:
                result={'status':'blocked','blocked':'uncertain_submission_requires_reconciliation','processed':0,'registered_now':0,'workers':workers}
            else:
                rolls=set(config['selection']['rolls'])
                pairs=[(r,p) for r,p in zip(rows,origins) if not rolls or p['roll'] in rolls]
                source={r['BForm']:r for r,_ in pairs}
                if len(source)!=len(pairs):raise ValueError('Pinned HTTP source has duplicate BForms')
                registered=known_registrations(db, config)
                skipped_registered=sum(r['BForm'] in registered for r,_ in pairs)
                pairs=[(r,p) for r,p in pairs if r['BForm'] not in registered]
                if pairs and not control.stop.is_set():
                    emit('stage',message=f'Validating {len(pairs):,} eligible students and sealing input')
                    form=await preflight_form(factory,control,emit)
                    catalog,applicants,snapshot=prepare_applicants(config,profile_path,pairs,copies,hashes,form)
                    import_batch(db,config,hashes,catalog,snapshot,applicants)
                    del applicants
                    if retry_uncertain:
                        uncertain_requeued=authorize_uncertain_retries(db,config,directory)
                        emit('stage',message=f'Operator opted into retry for {len(uncertain_requeued)} prior uncertain applicants; original attempts retained')
                    recovered = recover_rate_limited(db, config)
                    if recovered: emit('stage', message=f'Recovered {len(recovered)} verified throttle refusals; original receipts retained')
                    cooldown = cooldown_remaining(db, BASE, config['campaign'])
                    control.delay(cooldown)
                    last = last_dispatch_time(db, BASE, config['campaign'])
                    if last:
                        age = (datetime.now(timezone.utc)-datetime.fromisoformat(last)).total_seconds()
                        control.next_submission = time.monotonic() + max(0, control.submission_interval-age)
                    if config['selection'].get('dispatch_pattern'):
                        from .alternating import Alternating
                        order=Alternating(db,config,ready_job_refs(db,config,0),limit)
                        selected=order.references;control.dispatch_order=order
                    else:selected=ready_job_refs(db,config,limit)
                else:selected=[]
                emit('queue',total=len(selected))
                from .progress import refresh_ui
                refresh_ui(ui)
                if selected:
                    emit('stage', message=f'Running: {control.prepare_ahead} prepare-ahead · {control.max_in_flight} in-flight · shared {control.submission_interval:g}s dispatch spacing')
                    if cooldown:
                        emit('cooldown',seconds=round(cooldown,1),message='Honoring saved server cooldown from the previous run')
                queue=asyncio.Queue()
                for job in selected:queue.put_nowait(job)
                retry_counts = Counter();unknown_retries=Counter()
                async def consume(number):
                    nonlocal active,peak
                    async with factory(number) as client:
                        while not control.stop.is_set():
                            if not await control.wait():break
                            if any(shutil.disk_usage(path).free < 2*1024**3 for path in (destination,database.parent)):
                                control.reason='low_disk_space';control.stop.set()
                                emit('stage',message='Stopping: less than 2 GiB free; pending records preserved')
                                break
                            try:reference=queue.get_nowait()
                            except asyncio.QueueEmpty:break
                            job=db.execute("""SELECT j.* FROM jobs j WHERE j.id=? AND j.state='ready'
                                AND NOT EXISTS(SELECT 1 FROM registered_applicants r WHERE r.portal=j.portal
                                    AND r.campaign=j.campaign AND r.applicant_key=j.applicant_key)""",(reference['id'],)).fetchone()
                            if job is None:
                                queue.task_done();continue
                            active+=1;peak=max(peak,active)
                            try:
                                outcome=await process(db,job,client,config,destination,source,control,emit,number)
                                if outcome:
                                    if reschedule_uncertain(db,config,directory,outcome,control,unknown_retries,uncertain_retries,emit):
                                        queue.put_nowait(reference);continue
                                    if outcome.get('rate_limited'):
                                        retry_counts[job['id']] += 1
                                        emit('throttle_retry', **outcome)
                                        if auto_recover or retry_counts[job['id']] <= config['captcha']['max_explicit_rejection_retries']:
                                            queue.put_nowait(reference)
                                        else:
                                            control.reason='Repeated explicit server throttling; pending records preserved for resume'
                                            control.stop.set()
                                    elif outcome.get('retryable_form'):
                                        emit('form_retry', **outcome)
                                        queue.put_nowait(reference)
                                    else:
                                        counts[outcome['state']]+=1;emit('result',**outcome)
                            finally:active-=1;queue.task_done()
                async def worker(number):
                    try:await consume(number)
                    except BaseException:
                        control.stop.set()
                        raise
                tasks=[asyncio.create_task(worker(n+1)) for n in range(min(workers,len(selected)))]
                try:
                    results=await asyncio.gather(*tasks,return_exceptions=True)
                except BaseException:
                    control.stop.set()
                    for task in tasks:
                        if not task.done() and not task.cancelling():task.cancel()
                    # Keep SQLite open until every in-flight receipt has committed.
                    await asyncio.gather(*tasks,return_exceptions=True)
                    raise
                failures=[r for r in results if isinstance(r,BaseException)]
                if failures:control.stop.set()
                result={'status':'stopped' if control.stop.is_set() else 'complete','processed':sum(counts.values())-counts['ready'],
                    'registered_now':counts['registered'],'already_registered_now':counts['already_registered_external'],
                    'skipped_registered':skipped_registered,'auto_recover':auto_recover,
                    'safe_form_retries':control.safe_form_retries,'retry_round':control.retry_round,
                    'outcomes':dict(counts),'queued':len(selected),'workers':workers,
                    'peak_active':peak,'interrupted':interrupted,'remaining_this_run':len(selected)-counts['registered']-counts['already_registered_external'],
                    'throttle_refusals':sum(retry_counts.values()), 'global_submission_interval_seconds':control.submission_interval}
                result.update(prepare_ahead=control.prepare_ahead, max_in_flight=control.max_in_flight,
                    peak_preparing=control.peak_preparing, peak_in_flight=control.peak_in_flight,
                    refreshed_forms=control.refreshed_forms)
                if failures:result['blocked']='worker_error_requires_review'
                if counts['uncertain'] and not control.continue_on_uncertain:result['blocked']='uncertain_submission_requires_reconciliation'
                if control.reason:result['blocked']=control.reason
                attention=db.execute('''SELECT j.id AS job,j.roll,j.state,j.detail AS reason,j.receipt FROM jobs j
                    JOIN members m ON m.job=j.id JOIN profiles p ON p.batch=m.batch
                    WHERE p.name=? AND j.state IN ('rejected','blocked_input','uncertain') ORDER BY j.roll,j.id''',
                    (config['profile'],)).fetchall()
                report=directory/'Needs Attention.csv'
                with report.open('w',encoding='utf-8-sig',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=['job','roll','state','reason','receipt'])
                    writer.writeheader();writer.writerows(dict(row) for row in attention)
                result.update(needs_attention=len(attention),attention_report=str(report))
                if attention and result['status']=='complete':result['status']='complete_with_issues'
    except PreflightStopped:
        result={'status':'stopped','blocked':'operator_stopped_before_submission','processed':0,
            'registered_now':0,'workers':workers,'interrupted':interrupted,'auto_recover':auto_recover}
    except asyncio.CancelledError:
        control.stop.set()
        for task in tasks:
            if not task.done() and not task.cancelling():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        raise
    finally:
        for sig,handler in old_handlers.items():
            loop.remove_signal_handler(sig);signal.signal(sig,handler)
    result.update(transport='httpx',browser_launched=False,report=str(directory/'Summary.json'),log=str(directory/'events.jsonl'))
    result.update(uncertain_retry_enabled=retry_uncertain,uncertain_requeued=len(uncertain_requeued))
    result['uncertain_retries_per_applicant']=uncertain_retries
    finish=getattr(ui,'finish',None)
    if callable(finish):finish(result)
    (directory/'Summary.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
