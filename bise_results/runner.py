"""Parallel visible/headless workers with isolated sessions and durable commits."""

import asyncio
from datetime import datetime
import hashlib
import json
import os
import shutil
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, Error as BrowserError

from .captcha import recognize
from .parser import Document, ReviewRequired, parse_result
from .store import import_batch, locked_store, now, prepare_jobs
from . import MAX_WORKERS
from .exams import exam_type_for_grade, exam_details, course_for_grade
from .resources import CollectionResources,configure_ocr_children


class CaptchaRejected(RuntimeError):
    pass


class NeedsOperator(RuntimeError):
    pass


class SharedCooldown:
    """A server error pauses all workers without serializing healthy requests."""

    def __init__(self):
        self.until = 0

    def backoff(self, seconds):
        self.until = max(self.until, time.monotonic() + seconds)


class Pacer:
    """One instance per worker; only error cooldown is shared."""

    def __init__(self, seconds, cooldown=None):
        self.seconds = seconds
        self.next_at = 0
        self.cooldown = cooldown if cooldown is not None else SharedCooldown()

    async def wait(self):
        while True:
            delay = max(self.next_at, self.cooldown.until) - time.monotonic()
            if delay <= 0:
                self.next_at = time.monotonic() + self.seconds
                return
            # Recheck after waking: another worker may have extended cooldown.
            await asyncio.sleep(delay)

    def backoff(self, seconds):
        self.cooldown.backoff(seconds)


def atomic_write(path, content):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content if isinstance(content, bytes) else content.encode())
    temporary.replace(path)


async def capture(page, folder, name="card"):
    atomic_write(folder / f"{name}.html", await page.content())
    await page.screenshot(path=str(folder / f"{name}.tmp.png"), full_page=True)
    (folder / f"{name}.tmp.png").replace(folder / f"{name}.png")


async def close_safely(resource):
    try:
        await resource.close()
    except BrowserError:
        # Closing the window/driver or Ctrl+C can already have closed this
        # resource. Cleanup must not prevent export of committed results.
        print('Browser already closed; committed results will still be exported.', flush=True)


async def lookup(page, job, folder, args, pacer, operator_lock, progress=None):
    progress = progress or (lambda phase: None)
    progress('opening_form')
    response = await page.goto("http://result.biselahore.com/", wait_until="domcontentloaded", timeout=60000)
    if response is None or not response.ok:
        pacer.backoff(30)
        raise BrowserError(f"Search page unavailable: {response.status if response else 'no response'}")
    course = job.get('course', 'SSC')
    await page.locator('label[for="rdlistCourse_'+('1' if course=='HSSC' else '0')+'"]').click()
    exam_type = str(job.get('exam_type', '2'))
    exam = exam_details(exam_type, course)
    await page.locator("#ddlExamType").select_option(exam_type)
    await page.locator("#ddlExamYear").select_option(str(job["year"]))
    selection = await page.evaluate("""() => [document.querySelector('input[name="rdlistCourse"]:checked')?.value,
        document.querySelector('#ddlExamType')?.value, document.querySelector('#ddlExamYear')?.value]""")
    if selection != [course, exam_type, str(job["year"])]:
        raise ReviewRequired("Form selection not retained")
    await page.locator("#txtFormNo").fill(job["roll"])
    await page.wait_for_function("() => { const i=document.querySelector('#imgCaptcha'); return i && i.complete && i.naturalWidth>0; }")
    png = await page.locator("#imgCaptcha").screenshot(path=str(folder / "captcha.png"))
    progress('reading_captcha')
    resources=getattr(args,'_resources',None)
    if resources is None:
        ocr = await asyncio.to_thread(recognize, png)
    else:
        async with resources.ocr:
            ocr = await asyncio.to_thread(recognize, png)
    atomic_write(folder / "ocr.json", json.dumps(ocr, indent=2))
    candidate = ocr["text"]
    if args.captcha_mode == "assist":
        async with operator_lock:
            await page.bring_to_front()
            answer = await asyncio.to_thread(input, f"Roll {job['roll']} CAPTCHA suggested {candidate or '(unreadable)'}; Enter accepts, type correction, or /skip: ")
            if answer.strip() == "/skip":
                raise NeedsOperator("Operator skipped CAPTCHA")
            candidate = answer.strip() or candidate
    elif not candidate or (ocr["confidence"] < 50 and not ocr["variants_agree"]):
        raise CaptchaRejected("OCR uncertain; fresh challenge required")
    if not candidate:
        raise NeedsOperator("No CAPTCHA answer supplied")
    await page.locator("#txtCaptcha").fill(candidate)
    progress('waiting_to_submit')
    await pacer.wait()
    progress('submitting')
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=45000):
        await page.locator("#Button1").click()
    progress('saving_result')
    await capture(page, folder)
    doc = Document(await page.content())
    errors = " ".join([doc.text("lblError"), doc.text("lblError2")]).strip()
    if "captcha" in errors.lower():
        raise CaptchaRejected(errors)
    if errors:
        raise ReviewRequired("Board message: " + errors)
    if page.url.split("?")[0].rstrip("/").split("/")[-1].lower() != exam['endpoint']:
        raise ReviewRequired("Unexpected result endpoint; inspect saved page")
    parsed = parse_result(await page.content(), job["roll"], job["year"], exam_type=exam_type, course=course)
    if course == 'HSSC':
        from .demographics import apply_age_policy
        apply_age_policy(parsed, getattr(args,'missing_dob_policy','require'),
                         getattr(args,'estimated_age_at_exam',18))
    photo_name = None
    if getattr(args,'save_student_photos',False):
        from .photos import download_photo
        progress('saving_student_photo')
        try:
            payload, parsed['student_photo'] = await download_photo(page, await page.content())
        except (ValueError, BrowserError) as exc:
            if getattr(args,'photo_storage','sqlite')!='files':raise ReviewRequired(str(exc)) from exc
            parsed['student_photo_status']='missing'
            parsed['student_photo_error']=str(exc)
        if parsed.get('student_photo'):
            photo_name = 'student-photo.' + parsed['student_photo']['extension']
            atomic_write(folder / photo_name, payload)
    parsed["source_url"] = page.url
    parsed["fetched_at"] = now()
    atomic_write(folder / "parsed.json", json.dumps(parsed, indent=2, ensure_ascii=False))
    receipt = {}
    for name in ("card.html", "card.png", "parsed.json", *([photo_name] if photo_name else [])):
        path = folder / name
        digests = getattr(args, '_artifact_digests', None)
        receipt[name] = {"path": str(path.resolve()), "sha256": digests(path) if digests is not None else hashlib.sha256(path.read_bytes()).hexdigest()}
    atomic_write(folder / "receipt.json", json.dumps(receipt, indent=2))
    return parsed, receipt


async def collect(args):
    if getattr(args, 'headless', False) and args.captcha_mode != 'auto':
        raise ValueError('Headless collection requires --captcha-mode auto; assisted mode needs a visible CAPTCHA')
    if not 1 <= args.workers <= MAX_WORKERS:
        raise ValueError(f'Workers must be between 1 and {MAX_WORKERS}')
    resources=CollectionResources(getattr(args,'ocr_workers',2),getattr(args,'min_available_memory_mb',1536),
        emit=lambda **v:print(json.dumps(v),flush=True))
    configure_ocr_children()
    args._resources=resources
    with locked_store(args.database) as db:
        from .publishing import ArtifactDigests
        digests = ArtifactDigests()
        args._artifact_digests = digests
        exam_type = exam_type_for_grade(getattr(args, 'grade', 10))
        course = course_for_grade(getattr(args, 'grade', 10))
        batch = import_batch(db, args.csv, args.school, args.year, exam_type=exam_type, course=course)
        pending = prepare_jobs(db, batch, args.retry_unresolved, digests=digests)
        selected = pending[:args.limit] if args.limit else pending
        print(json.dumps({"batch": batch, "queued_now": len(selected), "pending_total": len(pending), "workers": args.workers,
                          "class": exam_details(exam_type, course)['grade'], "exam_type": exam_type,
                          "submission_spacing": "per_worker", "interval_seconds": args.interval,
                          "headless": getattr(args, 'headless', False)}), flush=True)
        print(json.dumps({'phase':'resource_policy','ocr_workers':getattr(args,'ocr_workers',2),
                          'ocr_threads_per_process':1,'ocr_timeout_seconds':20,
                          'minimum_available_memory_mb':resources.minimum}),flush=True)
        queue = asyncio.Queue()
        for job in selected:
            queue.put_nowait(dict(job))
        cooldown = SharedCooldown()
        operator_lock = asyncio.Lock()
        low_disk = asyncio.Event()
        last_evidence_check = [0, 0]

        def space_available():
            if low_disk.is_set():
                return False
            quota=getattr(args,'max_evidence_mib',0)
            if quota:
                if time.monotonic()-last_evidence_check[0]>15:
                    last_evidence_check[:]=[time.monotonic(),sum(p.stat().st_size for p in Path(args.artifacts).rglob('*') if p.is_file() and not p.is_symlink())]
                if last_evidence_check[1]>=quota*1024**2:
                    low_disk.set()
                    print(json.dumps({'phase':'stopping','reason':'evidence_quota','pending_rolls_preserved':True}),flush=True)
                    return False
            for target in (Path(args.database).resolve().parent, Path(args.artifacts).resolve()):
                while not target.exists():
                    target = target.parent
                if shutil.disk_usage(target).free < getattr(args,'min_free_disk_gib',2) * 1024 ** 3 or os.statvfs(target).f_favail<128:
                    low_disk.set()
                    print(json.dumps({'phase': 'stopping', 'reason': 'low_disk_space', 'minimum_free_gib': getattr(args,'min_free_disk_gib',2),
                                      'path': str(target), 'pending_rolls_preserved': True}), flush=True)
                    return False
            return True

        async def worker(browser, number):
            pacer = Pacer(args.interval, cooldown)
            context = await browser.new_context(viewport={"width": 1280, "height": 1000})
            page = await context.new_page()
            page.set_default_timeout(20000)
            try:
                while not queue.empty() and space_available():
                    if not await resources.wait_for_memory(needed=lambda:not queue.empty()):break
                    try:job = queue.get_nowait()
                    except asyncio.QueueEmpty:break
                    with db:
                        db.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=?", (now(), job["id"]))
                    for retry in range(3):
                        stamp = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%Y%m%dT%H%M%S%f")
                        folder = Path(args.artifacts) / f"job-{job['id']}" / stamp
                        folder.mkdir(parents=True, exist_ok=False)
                        with db:
                            attempt_id = db.execute("INSERT INTO attempts (job_id,started_at,evidence) VALUES (?,?,?)", (job["id"], now(), str(folder.resolve()))).lastrowid
                            db.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=?", (job["id"],))
                        state, error, parsed, receipt = "failed", None, None, None
                        retryable = False
                        try:
                            def progress(phase):
                                print(json.dumps({"worker": number, "job": job['id'], "phase": phase}), flush=True)
                            parsed, receipt = await lookup(page, job, folder, args, pacer, operator_lock, progress=progress)
                            # Photo + identity commit atomically before cleanup may discard captures.
                            from .photos import save_photo
                            with db:
                                save_photo(db,job['id'],parsed,folder,
                                    getattr(args,'photo_storage','sqlite'),getattr(args,'photo_root',None))
                                if getattr(args,'photo_storage','sqlite')=='files':
                                    atomic_write(folder/'parsed.json',json.dumps(parsed,indent=2,ensure_ascii=False))
                                    receipt['parsed.json']['sha256']=hashlib.sha256((folder/'parsed.json').read_bytes()).hexdigest()
                                    atomic_write(folder/'receipt.json',json.dumps(receipt,indent=2))
                                db.execute("UPDATE attempts SET ended_at=?,outcome='verified',detail=NULL WHERE id=?",(now(),attempt_id))
                                db.execute("UPDATE jobs SET state='verified',result_json=?,receipt_json=?,error=NULL,updated_at=? WHERE id=?",(json.dumps(parsed),json.dumps(receipt),now(),job['id']))
                            state = "verified"
                        except (CaptchaRejected, BrowserError) as exc:
                            if isinstance(exc, BrowserError) and not getattr(browser, 'is_connected', lambda: True)():
                                raise
                            error = str(exc)
                            retryable = retry < 2
                            state = "retry_wait" if retryable else "needs_review" if isinstance(exc, CaptchaRejected) else "failed"
                            if isinstance(exc, BrowserError):
                                pacer.backoff(5 * (retry + 1))
                        except (ReviewRequired, NeedsOperator) as exc:
                            state, error = "needs_review", str(exc)
                        except Exception as exc:
                            state, error = "failed", f"{type(exc).__name__}: {exc}"
                        if state != 'verified':
                            with db:
                                db.execute("UPDATE attempts SET ended_at=?,outcome=?,detail=? WHERE id=?", (now(), state, error, attempt_id))
                                db.execute("UPDATE jobs SET state=?,result_json=?,receipt_json=?,error=?,updated_at=? WHERE id=?", ("running" if retryable else state, json.dumps(parsed) if parsed else None, json.dumps(receipt) if receipt else None, error, now(), job["id"]))
                        if state == 'verified' and not (getattr(args,'keep_verified_artifacts',False) or getattr(args,'export_reports',False)):
                            from .retention import discard_verified_capture
                            try:
                                discard_verified_capture(db,job['id'],folder.parent)
                            except (OSError,ValueError) as exc:
                                # A cleanup problem cannot undo a committed result.
                                print(json.dumps({'job':job['id'],'phase':'capture_cleanup_deferred','reason':str(exc)}),flush=True)
                        print(json.dumps({"worker": number, "job": job["id"], "state": state, "attempt": retry + 1, "reason": error}), flush=True)
                        if not retryable:
                            break
                        await asyncio.sleep(3 if state == "retry_wait" else 5)
                    queue.task_done()
            finally:
                await close_safely(context)

        interrupted, failure = False, None
        try:
            if selected and space_available():
                async with async_playwright() as playwright:
                    options = {"headless": getattr(args, 'headless', False)}
                    if args.browser_executable:
                        options["executable_path"] = args.browser_executable
                    browser = await playwright.chromium.launch(**options)
                    try:
                        results = await asyncio.gather(*(worker(browser, n + 1) for n in range(min(args.workers, len(selected)))), return_exceptions=True)
                        failures = [result for result in results if isinstance(result, BaseException)]
                        if failures:
                            raise RuntimeError("Worker stopped; resume recovers interrupted rows: " + "; ".join(str(result) for result in failures))
                    except asyncio.CancelledError:
                        interrupted = True
                        raise
                    finally:
                        await close_safely(browser)
        except asyncio.CancelledError:
            interrupted = True
        except Exception as exc:
            failure = exc
        if interrupted or failure is not None:
            with db:
                db.execute("UPDATE jobs SET state='pending',error='Interrupted; ready to resume',updated_at=? WHERE state='running'", (now(),))
                db.execute("UPDATE attempts SET ended_at=?,outcome='interrupted' WHERE ended_at IS NULL", (now(),))
        if getattr(args, 'export_reports', False) and not low_disk.is_set():
            from .reports import export_batch, print_export_summary
            destination, summary = export_batch(db, batch, args.output, include_pending=False, progress=lambda message: print(message, flush=True), digests=digests)
            print_export_summary(destination, summary)
        else:
            scope = (args.year, course, exam_type)
            summary = dict(db.execute('SELECT state,count(*) FROM jobs WHERE year=? AND course=? AND exam_type=? GROUP BY state', scope))
            summary['total'] = sum(summary.values())
            print(json.dumps({'database': str(Path(args.database).resolve()), 'class': exam_details(exam_type, course)['grade'],
                              'states': summary, 'reports_generated': False}), flush=True)
            print(f"Saved student records in SQLite: {summary.get('verified', 0):,} / {summary['total']:,} imported rolls", flush=True)
        if interrupted:
            print('Stopped. Saved results are committed to SQLite; rerun the same command to resume.', flush=True)
            return 130
        if failure is not None:
            raise failure
        if low_disk.is_set():
            print('Stopped before starting more rolls: less than 2 GiB free. Saved results remain in SQLite; pending rolls can resume.', flush=True)
            return 2
        return 0 if summary.get("failed", 0) + summary.get("needs_review", 0) == 0 else 2
