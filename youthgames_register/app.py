"""Prepare, preview and submit immutable applicants with isolated worker sessions."""

import asyncio
from collections import Counter
import json
from pathlib import Path
import shutil
import tempfile
import time

from . import VERSION
from . import browser as web
from .config import load, resolved, signature, input_snapshot, sha, validate_browser
from .domain import validate, option, uc_label, redact_tokens, classify
from .reports import export, output_folder
from .store import opened, import_batch, jobs, begin, intent, dispatched, finish, now, check_receipt, known_registrations, bind_completed_batch


def seal_snapshot(config, profile_path, copies, hashes):
    parent = resolved(profile_path, config['state']['database']).parent / 'youthgames_snapshots'
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / signature({'hashes': hashes, 'version': VERSION})
    if destination.exists():
        for key, (_, expected) in copies.items():
            p = destination / (key + copies[key][0].suffix)
            if not p.is_file() or sha(p.read_bytes()) != expected:
                raise ValueError('Sealed registration input snapshot changed')
        return destination
    staging = Path(tempfile.mkdtemp(prefix='.snapshot-', dir=parent))
    try:
        for key, (path, expected) in copies.items():
            target = staging / (key + path.suffix)
            shutil.copyfile(path, target)
            if sha(target.read_bytes()) != expected:
                raise ValueError('Snapshot copy hash mismatch')
        (staging / 'hashes.json').write_text(json.dumps(hashes, indent=2))
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


async def prepare_in(db, browser, config, profile_path, session=None, emit=None):
    if config['photos']['mode']=='original_sqlite_reference':
        raise ValueError('Original SQLite photos currently require the existing HTTP transport')
    with input_snapshot(config, profile_path, allow_empty=True) as (rows, origins, copies, hashes):
        if not rows:
            return {'batch': None, 'counts': {'total': 0}, 'empty_input': True}
        pairs = [(r, p) for r, p in zip(rows, origins) if not config['selection']['rolls'] or p['roll'] in config['selection']['rolls']]
        if not pairs:
            raise ValueError('Profile selection has no normalized applicants')
        known=known_registrations(db,config)
        completed=[known[data['BForm']] for data,_ in pairs if data['BForm'] in known]
        pairs=[(data,origin) for data,origin in pairs if data['BForm'] not in known]
        if emit and completed:
            emit('stage',message=f'Skipping {len(completed):,} permanently registered applicants before browser form validation')
        if not pairs:
            snapshot=seal_snapshot(config,profile_path,copies,hashes)
            batch=bind_completed_batch(db,config,hashes,snapshot,completed)
            counts=dict(Counter(row['status'] for row in completed));counts['total']=len(completed)
            return {'batch':batch,'counts':counts,'empty_input':True,'skipped_registered':len(completed)}
        catalog = await web.catalog(browser, config, pairs[0][0], session=session, emit=emit)
        catalog_hash = signature(catalog)
        snapshot = seal_snapshot(config, profile_path, copies, hashes)
        photo = snapshot / ('photo' + copies['photo'][0].suffix)
        applicants = []
        for data, origin in pairs:
            checked = validate(data, config, catalog)
            ids = {}
            for field in web.SELECTS:
                try:
                    choices = catalog['sports_by_gender'].get(data['Gender'], []) if field == 'Sport' else catalog['options'][field]
                    ids[field] = option(choices, uc_label(data, config) if field == 'UC' else data[field])
                except ValueError:
                    pass
            applicants.append({'roll': origin['roll'], 'data': data, 'origins': origin['origins'],
                'operator_confirmation': config['confirmation'], 'photo': {'path': str(photo), 'sha256': hashes['photo'], 'kind': config['photos']['mode']},
                'option_ids': ids, 'dob_rule': catalog['dob'], 'catalog_hash': catalog_hash, 'validation': checked})
            if origin.get('administrative_allocation'):
                applicants[-1]['administrative_allocation'] = origin['administrative_allocation']
            if origin.get('sport_allocation'):
                applicants[-1]['sport_allocation']=origin['sport_allocation']
        batch = import_batch(db, config, hashes, catalog, snapshot, applicants)
        (snapshot / ('catalog-' + catalog_hash + '.json')).write_text(json.dumps(catalog, indent=2))
        destination = resolved(profile_path, config['state']['output'])
        counts = export(db, config, destination)
        return {'batch': batch, 'counts': counts, 'catalog_hash': catalog_hash, 'catalog': catalog,'skipped_registered':len(completed)}


def attempt_folder(destination, job_id, attempt):
    folder = destination / 'Receipts' / f'job-{job_id:06d}' / f'attempt-{attempt:06d}'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


async def capture_receipt(page, folder, job, attempt, outcome, html, status, sent):
    body = folder / 'response.html'
    body.write_text(redact_tokens(html), encoding='utf-8')
    files = {'response.html': {'path': str(body.resolve()), 'sha256': sha(body.read_bytes())}}
    network = folder / 'browser-network.json'
    if network.exists():
        files['browser-network.json'] = {'path':str(network.resolve()),'sha256':sha(network.read_bytes())}
    screenshot_error = None
    try:
        path = folder / 'result.png'
        await page.screenshot(path=str(path), full_page=True, timeout=15000)
        files['result.png'] = {'path': str(path.resolve()), 'sha256': sha(path.read_bytes())}
    except Exception as exc:
        screenshot_error = type(exc).__name__
    receipt = {'application': 'youthgames_register', 'transport': 'playwright', 'job': job['id'], 'attempt': attempt,
               'payload_hash': job['payload_hash'], 'post_dispatched': sent, 'http_status': status,
               'outcome': outcome, 'files': files, 'screenshot_error': screenshot_error, 'captured_at': now()}
    temp = folder / 'receipt.tmp'
    temp.write_text(json.dumps(receipt, indent=2))
    target = folder / 'receipt.json'
    temp.replace(target)
    return target


class SubmissionStopped(Exception):
    """Another worker stopped the run before this attempt could submit."""


class RunControl:
    def __init__(self):
        self.stopped = asyncio.Event()
        self.reason = None

    def check(self):
        if self.stopped.is_set():
            raise SubmissionStopped('Run stopped by another worker; no new submission allowed')

    def outcome(self, state):
        if state in ('uncertain', 'blocked_input', 'ready'):
            self.reason = self.reason or state
            self.stopped.set()


async def process_one(db, browser, config, job, destination, preview=False, answer_provider=None, session=None, control=None, worker=None, emit=None):
    if control and control.stopped.is_set():
        return {'job': job['id'], 'state': 'ready', 'post_dispatched': False}
    destination = output_folder(destination, config['profile'])
    item = json.loads(job['payload'])
    meta = {'job':job['id'],'worker':worker,'roll':job['roll'],
            'uc':item.get('administrative_allocation',{}).get('uc_number')}
    evidence = None
    owned = session is None
    session = session or web.FormSession(browser)
    page, gate = await session.start_attempt()
    if control and control.stopped.is_set():
        session.disarm()
        if owned:
            await session.close()
        return {'job': job['id'], 'state': 'ready', 'post_dispatched': False}
    attempt = None
    armed_intent = False
    if not preview:
        attempt = begin(db, job)
    folder = (destination / 'Previews' / f"job-{job['id']:06d}") if preview else attempt_folder(destination, job['id'], attempt)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if emit:
            from .browser_evidence import PageEvidence
            evidence = PageEvidence(page,emit,meta)
            emit('filling',**meta)
        else:print(json.dumps({'job': job['id'], 'worker': worker, 'phase': 'filling', 'preview': preview}), flush=True)
        form, expected, comparison = await web.fill(page, config, item)
        comparison['values'].pop('captcha', None)
        (folder / 'filled-comparison.json').write_text(json.dumps(comparison, indent=2))
        await page.screenshot(path=str(folder / 'filled.png'), full_page=True)
        if preview:
            if evidence:evidence.close(folder)
            return {'job': job['id'], 'state': 'preview_verified', 'post_dispatched': False}
        with db:
            db.execute("UPDATE jobs SET state='filled',updated_at=? WHERE id=?", (now(), job['id']))
        provider = answer_provider or (web.automatic_answer if config['captcha']['mode'] == 'auto' else web.assisted_answer)
        if emit:emit('reading_captcha',**meta)
        answer = await web.automatic_answer(form, job['id'],log=False) if emit and provider is web.automatic_answer else await provider(form, job['id'])
        if answer is None:
            if control:
                control.outcome('ready')
            finish(db, job['id'], attempt, 'ready', 'Operator skipped before submission')
            return {'job': job['id'], 'state': 'ready', 'post_dispatched': False}

        def commit_intent():
            nonlocal armed_intent
            if control:
                control.check()
            intent(db, job['id'], attempt)
            armed_intent = True
            if emit:emit('submitting',**meta)

        def commit_dispatch():
            if control:
                control.check()
            dispatched(db, job['id'], attempt)
            if emit:emit('awaiting_response',**meta)

        outcome, html, status = await web.send(page, form, expected, gate, commit_intent,
            commit_dispatch, item['photo']['sha256'])
        # A strong HTML receipt is not accepted unless this exact attempt sent its
        # verified POST; a cached/forged success view cannot register a draft.
        if not gate.sent:
            raise ValueError('No verified applicant POST was dispatched')
        if evidence:evidence.close(folder)
        if outcome.get('rate_limited'):
            if control:
                control.reason = 'server_rate_limit_detected'
                control.stopped.set()
            if emit:emit('browser_throttle',message=outcome['detail'],**meta)
        if control:
            control.outcome(outcome['state'])
        if emit:emit('saving_result',**meta)
        receipt = await capture_receipt(page, folder, job, attempt, outcome, html, status, gate.sent)
        finish(db, job['id'], attempt, outcome['state'], outcome['detail'], receipt, outcome['tracking'])
        return {'job': job['id'], 'state': outcome['state'], 'post_dispatched': True, 'receipt': str(receipt),
                'retryable_captcha': outcome.get('retryable_captcha', False),'rate_limited':outcome.get('rate_limited',False),
                'tracking':outcome.get('tracking')}
    except BaseException as exc:
        if not preview and attempt is not None:
            if evidence:evidence.close(folder)
            state = 'uncertain' if gate.sent or (armed_intent and not gate.error) else 'ready' if isinstance(exc, (SubmissionStopped, asyncio.CancelledError, KeyboardInterrupt)) and not armed_intent else 'blocked_input'
            if control:
                control.outcome(state)
            detail = gate.error or (type(exc).__name__ + ': ' + str(exc))
            # Logs and public summaries use job IDs; detailed website errors and
            # private local evidence stay in the database/receipt.
            try:
                html = await page.content()
            except BaseException:
                html = ''
            outcome = {'state': state, 'tracking': None, 'detail': detail}
            receipt = await capture_receipt(page, folder, job, attempt, outcome, html, 0, gate.sent)
            finish(db, job['id'], attempt, state, detail, receipt)
            if state == 'blocked_input' and not gate.sent:
                from .browser_recovery import recover_stopped_dispatches
                if recover_stopped_dispatches(db, config, {job['id']}):
                    state = 'ready'
                    receipt = db.execute('SELECT receipt FROM jobs WHERE id=?', (job['id'],)).fetchone()['receipt']
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            return {'job': job['id'], 'state': state, 'post_dispatched': gate.sent, 'receipt': str(receipt), 'error': type(exc).__name__}
        raise
    finally:
        if evidence:evidence.close()
        session.disarm()
        if owned:
            await session.close()
        if not preview:
            export(db, config, destination)


async def run(profile_path, command, limit=1, rolls=(), answer_provider=None, captcha_mode=None, workers=None, headless=None, emit=None, control=None):
    config = load(profile_path)
    if captcha_mode is not None:
        if captcha_mode not in ('auto', 'assist'):
            raise ValueError('CAPTCHA mode must be auto or assist')
        config['captcha']['mode'] = captcha_mode
    if workers is not None:
        config['browser']['workers'] = workers
    if headless is not None:
        config['browser']['headless'] = headless
    validate_browser(config['browser'])
    if limit < 0:
        raise ValueError('limit must be nonnegative')
    worker_count = config['browser']['workers']
    if command == 'submit' and config['captcha']['mode'] != 'auto' and (config['browser']['headless'] or worker_count > 1):
        raise ValueError('Headless or multiple-worker submission requires --captcha-mode auto')
    database = resolved(profile_path, config['state']['database'])
    destination = resolved(profile_path, config['state']['output'])
    control = control or RunControl()
    with opened(database) as db:
        pending=db.execute("SELECT count(*) FROM jobs WHERE portal=? AND campaign=? AND state='uncertain'",
            (config['portal'].split('#')[0].rstrip('/'),config['campaign'])).fetchone()[0]
        if command=='submit' and pending:
            return {'blocked':'uncertain_submission_requires_reconciliation','attempted_now':0,'outcomes':[],
                    'counts':{'uncertain':pending},'output':str(destination)}
        async with web.visible_browser(headless=config['browser']['headless']) as browser, web.form_session(browser, reuse=not config['browser']['fresh_context_per_applicant']) as session:
            if emit:emit('stage',message='Reading live browser catalog and preparing input')
            prepared = await prepare_in(db, browser, config, profile_path, session=session, emit=emit)
            if not emit:print(json.dumps({'profile': config['profile'], 'counts': prepared['counts'], 'headless': config['browser']['headless'], 'workers': worker_count, 'captcha_mode': config['captcha']['mode'], 'reuse_tab': not config['browser']['fresh_context_per_applicant'], 'submission_spacing': 'per_worker'}), flush=True)
            if prepared.get('empty_input'):
                return {'status': 'nothing_to_register', 'attempted_now': 0, 'outcomes': [], 'counts': prepared['counts'], 'output': str(destination),
                        'skipped_registered':prepared.get('skipped_registered',0)}
            if command in ('inspect', 'prepare'):
                return {k: v for k, v in prepared.items() if k != 'catalog'}
            if command == 'submit' and jobs(db, config['profile'], states=('uncertain',)):
                return {'blocked': 'uncertain_submission_requires_reconciliation', 'attempted_now': 0,
                        'outcomes': [], 'counts': prepared['counts'], 'output': str(destination)}
            if command == 'submit':
                from .browser_recovery import recover_browser_jobs
                recovered = recover_browser_jobs(db, config)
                total_recovered = sum(len(value) for key,value in recovered.items() if key.startswith('recovered_'))
                if emit and total_recovered:
                    emit('stage', message=f'Recovered {total_recovered} verified temporary refusals/undispatched stops; original receipts retained')
                delay = recovered['cooldown_seconds']
                if delay > 0:
                    if emit: emit('cooldown', seconds=delay, message='Honoring saved server cooldown before any browser submission')
                    try:
                        await asyncio.wait_for(control.stopped.wait(), timeout=delay)
                    except TimeoutError:
                        pass
            selected = jobs(db, config['profile'], states=('ready',), rolls=rolls)
            if limit:
                selected = selected[:limit]
            outcomes = []
            if emit:
                emit('queue',total=len(selected))
                emit('stage',message=f'Browser workers run independently; {config["pacing"]["submission_interval_seconds"]}s per-worker spacing; no global dispatch gate')
            queue = asyncio.Queue()
            for job in selected:
                queue.put_nowait(job)

            async def consume(worker_id, worker_session):
                last_dispatch = None

                async def spacing():
                    if last_dispatch is not None and command == 'submit':
                        delay = config['pacing']['submission_interval_seconds'] - (time.monotonic() - last_dispatch)
                        if delay > 0:
                            try:
                                await asyncio.wait_for(control.stopped.wait(), timeout=delay)
                            except TimeoutError:
                                pass
                    return not control.stopped.is_set()

                while not control.stopped.is_set():
                    if not await spacing():
                        break
                    try:
                        job = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    retries = 0
                    while True:
                        result = await process_one(db, browser, config, job, destination, command == 'preview', answer_provider, session=worker_session, control=control, worker=worker_id,emit=emit)
                        if result['post_dispatched']:
                            last_dispatch = time.monotonic()
                        control.outcome(result['state'])
                        if not (command == 'submit' and result.get('retryable_captcha') and retries < config['captcha']['max_explicit_rejection_retries'] and not control.stopped.is_set()):
                            break
                        # Only a captured explicit CAPTCHA-only rejection can retry.
                        # Never replay a timeout, unknown response, or interrupted intent.
                        retries += 1
                        if not await spacing():
                            break
                        with db:
                            db.execute("UPDATE jobs SET state='ready',detail='Retry after explicit CAPTCHA rejection' WHERE id=? AND state='rejected'", (job['id'],))
                        job = db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
                    result['worker'] = worker_id
                    outcomes.append(result)
                    if emit:
                        item=json.loads(job['payload'])
                        emit('result',roll=job['roll'],uc=item.get('administrative_allocation',{}).get('uc_number'),**result)
                    else:print(json.dumps(result), flush=True)

            async def worker(worker_id):
                try:
                    if worker_id == 1:
                        await consume(worker_id, session)
                    else:
                        async with web.form_session(browser, reuse=not config['browser']['fresh_context_per_applicant']) as worker_session:
                            await consume(worker_id, worker_session)
                except BaseException:
                    control.stopped.set()
                    raise

            tasks = [asyncio.create_task(worker(i + 1)) for i in range(min(worker_count, len(selected)))]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                control.stopped.set()
                for task in tasks:
                    # Cancelling gather already cancels its children. A second
                    # cancellation interrupts their receipt/context cleanup.
                    if not task.done() and not task.cancelling():
                        task.cancel()
                # Finish cancellation receipts/DB transitions before closing the DB.
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            finally:
                export(db, config, destination)
            return {'attempted_now': len(outcomes), 'outcomes': outcomes, 'counts': dict(Counter(r['state'] for r in jobs(db, config['profile']))),
                    'output': str(destination),'stop_reason':control.reason,'submission_spacing':'per_worker',
                    'skipped_registered':prepared.get('skipped_registered',0)}


def reconcile(db, config, job_id):
    selected = [r for r in jobs(db, config['profile']) if r['id'] == job_id]
    if len(selected) != 1 or selected[0]['state'] != 'uncertain':
        raise ValueError('Reconcile requires an uncertain job in this profile')
    job = selected[0]
    if not check_receipt(job['receipt']):
        raise ValueError('Receipt artifacts are missing/damaged; official operator reconciliation is required')
    receipt = json.loads(Path(job['receipt']).read_text())
    if receipt['payload_hash'] != job['payload_hash'] or not receipt['post_dispatched']:
        raise ValueError('Receipt is not bound to the dispatched applicant snapshot')
    html = Path(receipt['files']['response.html']['path']).read_text()
    result = classify(html, receipt['http_status'])
    if result['state'] != 'registered':
        return {'job': job_id, 'state': 'uncertain', 'detail': 'No confirmed captured server receipt; no resubmission performed'}
    # Preserve the original conservative classification and create an immutable
    # derived receipt bound to the same dispatched request and response bytes.
    original = Path(job['receipt'])
    derived = {**receipt, 'outcome': result, 'reconciled_at': now(),
               'files': {**receipt['files'], 'original-receipt.json': {'path': str(original.resolve()), 'sha256': sha(original.read_bytes())}}}
    target = original.with_name('reconciled-' + signature(result)[:16] + '.json')
    if not target.exists():
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(derived, indent=2))
        temporary.replace(target)
    finish(db, job_id, receipt['attempt'], 'registered', 'Reconciled captured server receipt', target, result['tracking'])
    return {'job': job_id, 'state': 'registered'}
