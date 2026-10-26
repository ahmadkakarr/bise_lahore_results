"""Audit, collect and export BISE SSC 9th/10th results with visible or headless browsers."""

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from .importer import audit
from . import MAX_WORKERS


async def inspect_form(args) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright missing. Run the README installation command first.") from exc

    from .exams import exam_type_for_grade, course_for_grade
    exam_type = exam_type_for_grade(args.grade)
    course = course_for_grade(args.grade)
    stamp = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%Y%m%dT%H%M%S%f")
    destination = Path(args.output) / stamp
    destination.mkdir(parents=True, exist_ok=False)
    async with async_playwright() as playwright:
        options = {"headless": False}
        if args.browser_executable:
            options["executable_path"] = str(Path(args.browser_executable).resolve())
        browser = await playwright.chromium.launch(**options)
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            page.set_default_timeout(20000)
            response = await page.goto("http://result.biselahore.com/", wait_until="domcontentloaded", timeout=60000)
            if response is None or not response.ok:
                raise RuntimeError(f"Form request failed: {response.status if response else 'no response'}")
            # Labels remain clickable even when the custom CSS hides radio inputs.
            await page.locator('label[for="rdlistCourse_'+('1' if course=='HSSC' else '0')+'"]').click()
            await page.locator("#ddlExamType").select_option(exam_type)
            await page.locator("#ddlExamYear").select_option(str(args.year))
            await page.wait_for_function("""() => {
                const img = document.querySelector('#imgCaptcha');
                return img && img.complete && img.naturalWidth > 0;
            }""")
            selection = await page.evaluate("""() => ({
                course: document.querySelector('input[name="rdlistCourse"]:checked')?.value,
                exam_type: document.querySelector('#ddlExamType')?.value,
                year: document.querySelector('#ddlExamYear')?.value,
                maxlength: document.querySelector('#txtFormNo')?.maxLength,
                captcha_image_present: !!document.querySelector('#imgCaptcha'),
                fields: [...document.querySelectorAll('input,select')]
                  .filter(el => el.type !== 'hidden')
                  .map(el => ({id:el.id, type:el.type, name:el.name}))
            })""")
            if (selection["course"], selection["exam_type"], selection["year"]) != (course, exam_type, str(args.year)):
                raise RuntimeError("The live form did not retain the requested course/year/class selection.")
            selection.update({"url": page.url, "captured_at": stamp, "result_submitted_by_script": False})
            (destination / "form.json").write_text(json.dumps(selection, indent=2) + "\n")
            await page.screenshot(path=str(destination / "form.png"), full_page=True)
            await page.locator("#imgCaptcha").screenshot(path=str(destination / "captcha.png"))
            print(f"Visible form ready. Evidence: {destination}")
            print("Script has not submitted a result. Inspect the browser; press Enter here to save the current page and close.")
            await asyncio.to_thread(input)
            # Useful for operator-driven inspection of an actual result card.
            await page.screenshot(path=str(destination / "operator-page.png"), full_page=True)
            (destination / "operator-page.html").write_text(await page.content(), encoding="utf-8")
            (destination / "operator-page.txt").write_text(await page.locator("body").inner_text(), encoding="utf-8")
            print("Current page saved. If the result opened in another tab, this captures the original tab only.")
        finally:
            await browser.close()


def main(alarm_default=False) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    audit_parser = commands.add_parser("audit", help="Validate the supplied roll-number CSV offline")
    audit_parser.add_argument("csv", type=Path)
    inspect_parser = commands.add_parser("inspect", help="Open the selected Matric form visibly; no scripted submission")
    inspect_parser.add_argument("--class", dest="grade", type=int, choices=(9, 10, 12), default=10)
    inspect_parser.add_argument("--year", type=int, default=2026)
    inspect_parser.add_argument("--output", default="artifacts/inspection")
    inspect_parser.add_argument("--browser-executable", help="Optional separate Brave instance, e.g. /usr/bin/brave")
    collect_parser = commands.add_parser("collect", help="Collect supplied rolls with OCR, resume and Institution/District reports")
    from run_alarm import add_arguments
    add_arguments(collect_parser,default=alarm_default)
    collect_parser.add_argument("csv", type=Path)
    collect_parser.add_argument("--year", type=int, default=2026)
    collect_parser.add_argument("--class", dest="grade", type=int, choices=(9, 10, 12), default=10, help="9/10 select SSC; 12 selects HSSC Part-II Annual")
    collect_parser.add_argument("--school", default="Institution-wise", help="Optional input batch label; exports always use board Institution/District")
    collect_parser.add_argument("--limit", type=int, default=5, help="Maximum new lookups this run; 0 processes all pending")
    collect_parser.add_argument("--workers", type=int, choices=range(1, MAX_WORKERS + 1), default=1)
    collect_parser.add_argument('--ocr-workers',type=int,choices=range(1,9),default=2,
        help='Maximum simultaneous OCR processes; browser workers stay independent (default 2)')
    collect_parser.add_argument('--min-available-memory-mb',type=int,default=1536,
        help='Pause new lookups below this available RAM in MiB; automatically resume (default 1536, 0 disables)')
    collect_parser.add_argument("--captcha-mode", choices=("assist", "auto"), default="assist")
    collect_parser.add_argument("--interval", type=float, default=3, help="Minimum seconds between each worker's submissions; workers run independently, minimum 3")
    collect_parser.add_argument("--database", type=Path, default=Path("data/results.sqlite3"))
    collect_parser.add_argument("--artifacts", type=Path, default=Path("artifacts/results"))
    collect_parser.add_argument("--output", type=Path, default=Path("output"))
    collect_parser.add_argument("--browser-executable")
    collect_parser.add_argument("--headless", action="store_true", help="Run without browser windows; requires --captcha-mode auto")
    collect_parser.add_argument("--retry-unresolved", action="store_true", help="Explicitly retry failed/review rows; verified results stay cached")
    collect_parser.add_argument("--export-reports", action="store_true", help="Opt in to Excel/CSV/card reports; default saves SQLite and unresolved captures only")
    collect_parser.add_argument('--keep-verified-artifacts', action='store_true', help='Keep verified captures; default DB-only collection removes them after DB commit')
    collect_parser.add_argument('--min-free-disk-gib',type=float,default=2,help='Stop claiming rolls below this disk reserve (minimum 2 GiB)')
    collect_parser.add_argument('--max-evidence-mib',type=int,default=0,help='Pause admission at this evidence quota; 0 disables the quota')
    collect_parser.add_argument('--missing-dob-policy', choices=('require','estimate'), default='require',
        help='HSSC only: collect with a separately labelled cohort age estimate; never creates a DOB or sports eligibility')
    collect_parser.add_argument('--estimated-age-at-exam', type=int, default=18)
    collect_parser.add_argument('--save-student-photos', action='store_true', help='Save original imgDisplay photos in SQLite before deleting captures')
    photos_parser = commands.add_parser('photos', help='Export original SQLite student photos with an exact applicant mapping')
    photos_parser.add_argument('--database',type=Path,required=True)
    photos_parser.add_argument('--output',type=Path,required=True)
    export_parser = commands.add_parser("export", help="Export an existing batch without opening a browser")
    export_parser.add_argument("batch", nargs='?', help='Optional batch ID; otherwise select the requested year/class')
    export_parser.add_argument('--year', type=int)
    export_parser.add_argument('--class', dest='grade', type=int, choices=(9, 10, 12))
    export_parser.add_argument("--database", type=Path, default=Path("data/results.sqlite3"))
    export_parser.add_argument("--output", type=Path, default=Path("output"))
    export_parser.add_argument("--include-pending", action="store_true", help="Also write never-processed rows; default exports processed results only")
    status_parser = commands.add_parser("status", help="Show stored batch progress without opening a browser")
    status_parser.add_argument("batch")
    status_parser.add_argument("--database", type=Path, default=Path("data/results.sqlite3"))
    cleanup_parser = commands.add_parser('cleanup', help='Audit DB and clean verified captures/redundant reports; preserve registration receipts')
    cleanup_parser.add_argument('--database',type=Path,default=Path('data/results.sqlite3'))
    cleanup_parser.add_argument('--root',type=Path,default=Path('.'))
    cleanup_parser.add_argument('--apply',action='store_true',help='Delete eligible files after a coherent DB backup and audit manifest; default only plans')
    args = parser.parse_args()
    from run_alarm import guarded
    return guarded(args,_execute,'BISE '+args.command+' / '+str(getattr(args,'grade','')))


def _execute(args):
    try:
        if args.command == "audit":
            report = audit(args.csv)
            print(json.dumps(report, indent=2))
            return 1 if report["invalid_rows"] or report["unique_rolls"] == 0 else 0
        if args.command == 'cleanup':
            from .retention import cleanup
            result=cleanup(args.root,args.database,apply=args.apply,progress=lambda message:print(message,flush=True))
            print(json.dumps(result,indent=2))
            return 0
        if args.command == "collect":
            if not 1 <= args.estimated_age_at_exam <= 100 or (args.missing_dob_policy == 'estimate' and args.grade != 12):
                raise ValueError('Estimated missing-DOB collection requires class 12 and a valid cohort age')
            if args.limit < 0 or args.interval < 3:
                raise ValueError("--limit must be nonnegative and --interval must be at least 3 seconds")
            import math
            if not math.isfinite(args.min_free_disk_gib) or args.min_free_disk_gib<2 or args.max_evidence_mib<0:
                raise ValueError('Invalid disk reserve/evidence quota')
            from .runner import collect
            return asyncio.run(collect(args))
        if args.command == 'photos':
            from .store import locked_store
            from .photos import export_photos
            if not args.database.is_file():
                raise ValueError('Existing results database required')
            with locked_store(args.database) as db:
                print(json.dumps(export_photos(db,args.output),indent=2))
            return 0
        if args.command == "export":
            from .store import locked_store
            from .reports import export_batch, print_export_summary
            with locked_store(args.database) as db:
                batch = args.batch
                if batch is None:
                    from .exams import exam_type_for_grade, course_for_grade
                    year, grade = args.year or 2026, args.grade or 10
                    row = db.execute("SELECT id FROM batches WHERE year=? AND course=? AND exam_type=? ORDER BY created_at DESC,id DESC LIMIT 1", (year, course_for_grade(grade), exam_type_for_grade(grade))).fetchone()
                    if row is None:
                        raise ValueError(f'No imported batch for class {grade}, year {year}; collect first')
                    batch = row['id']
                elif args.year is not None or args.grade is not None:
                    raise ValueError('Use either a batch ID or --year/--class for export')
                destination, summary = export_batch(db, batch, args.output, include_pending=args.include_pending, progress=lambda message: print(message, flush=True))
            print_export_summary(destination, summary)
            return 0
        if args.command == "status":
            from collections import Counter
            from .store import locked_store, jobs_for
            with locked_store(args.database) as db:
                if db.execute("SELECT 1 FROM batches WHERE id=?", (args.batch,)).fetchone() is None:
                    raise ValueError("Unknown batch id")
                rows = jobs_for(db, args.batch)
                print(json.dumps({"batch": args.batch, "total": len(rows), "states": dict(Counter(row["state"] for row in rows))}))
            return 0
        asyncio.run(inspect_form(args))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(alarm_default=True))
