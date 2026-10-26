# BISE Lahore result collection: implementation and remaining work

## Confirmed scope

SSC / Matric, 2026, Part-II Annual; 51 supplied six-digit roll numbers. User confirmed reports must be saved by the **Institution/District** text on each board card. A guessed school or academy name is not used for output grouping.

Python remains the selected stack: Playwright async for visible browser control, Pillow/Tesseract for local CAPTCHA OCR, SQLite for durable state, openpyxl for Excel. Default browser launch uses `headless=False`; the optional `--headless` collection flag uses headless mode with automatic CAPTCHA.

## Current evidence, 20 October 2026

| Check | Outcome |
|---|---|
| Original CSV | 51 unique, no invalid/duplicate rolls, original SHA256 unchanged |
| Installed setup | Python 3.14, Playwright 1.64, Tesseract 5.5.3 |
| Operator inspection | SSC / exam type 2 / 2026 selection retained |
| Genuine result discovery | OCR accepted; same tab navigates to `ResultPart2.aspx` |
| Full batch | 51 accepted identities; 17 old review cases recovered locally; zero pending/review |
| Parser reconciliation | Returned roll, SSC/year heading, subject max/obtained totals and overall total verified |
| Institution grouping | 37 Kasur and 14 Lahore records, including summary-only outcomes |
| Exports | Master has 51 unique roll outcomes; institution reports/ZIPs reconcile |
| Receipts | Raw-card identity/marks/institution comparisons, PNG decode, ZIP CRC, manifest hashes pass |
| Offline checks | 43 regression tests cover independent worker pacing, overlap and shared-cooldown extension, readable workbooks, links, reversible output migration, stable publication, identity-first acceptance, summary layouts, offline review recovery, identity fields, cached-card upgrades, preserved source evidence, identity/layout rejection, null marks, cache/resume recovery, grouping, deduplication, process lock and Excel formula escaping |

Pilot evidence is in `docs/evidence/pilot-verification.json`. Initial source evidence is in `docs/evidence/inspection-summary.json`. The earlier Brave document-control request returned `403: arc control transport is not enabled`; subsequent live automation used the installed Playwright Chromium in visible mode.

## Implemented operator flow

1. Audit/import the CSV without changing its bytes; rolls remain strings.
2. Deterministic batch identity includes input hash, input label, year, SSC and exam type. Reusing the same input resumes the same batch.
3. Recover interrupted claims while holding an exclusive OS process lock. Recheck required artifact hashes before trusting cached cards.
4. Queue a bounded number of pending unique rolls; one to nine isolated worker contexts, default one. Three workers passed a live headless pilot; nine-worker concurrency is verified locally.
5. Explicitly select Matric, exam type 2 and requested year; read back the form values.
6. Screenshot the currently rendered CAPTCHA in that same session, apply local OCR, then assist or auto-submit.
7. Save the response and accept matched student identity even when marks or subject layout are unavailable. Parse recognized optional fields and retain raw unsupported table data.
8. Commit each row separately; update the stable human-readable exam folder. Unchanged exports reuse existing files. The master merges imported lists for that exam and deduplicates overlapping rolls.

Full collection command, pilot batch `0fe944074f8135cc`:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results collect roll_numbers_only.csv --limit 0 --workers 2 --captcha-mode auto
```

Other exact commands are in README. Installation and longer full-batch execution are operator-run as requested.

## Website adapter

| Control | Verified selector/value |
|---|---|
| Form | ASP.NET Web Forms `form1`, POST `./` |
| Matric | label for `rdlistCourse_0`, value `SSC` |
| Roll | `txtFormNo`, six digits |
| Exam | `ddlExamType`, Part-II=`2` |
| Year | `ddlExamYear`, `2026` |
| CAPTCHA | `imgCaptcha`, `txtCaptcha` |
| Submit | `Button1` |
| Error labels | `lblError`, `lblError2` |
| Result endpoint | `ResultPart2.aspx` |
| Card identity | `lblRollNoval`, `lblSession`, `Name`, `lblFatherName` |
| Date of birth | `lblDOB` -> `date_of_birth` |
| Student B-form/CNIC | `lblBFARM` -> `student_cnic` |
| Father CNIC | `lblFatherNIC` -> `father_cnic` |
| Registration number | `lblRegNum` -> `registration_number` |
| Institution/District | `lblExamCenter` (caption `lblcen`) |
| Subject table | `GridStudentData` |
| Board outcome | `lblResultinSentences`, total table row |
| Declaration date | `lblResultdate` |

The initial live form defaulted to Intermediate, unlike the supplied Matric screenshot. The script therefore sets SSC explicitly every time. Browser-managed Web Forms state remains fresh; stale VIEWSTATE and pasted CAPTCHA URLs are never replayed.

## Session isolation, pacing and OCR

Each worker owns a BrowserContext and active page. Cookies/session storage are isolated even though one visible browser process is reused. Contexts may appear as separate windows rather than tabs in a single window. Shared-session concurrent CAPTCHA requests are avoided because server-side session binding can invalidate another tab's challenge.

CAPTCHA is captured from the currently rendered image after its load completes. The code crops the screenshot border, upscales and runs two Tesseract variants with a single-line/alphanumeric configuration. Six-character format is based on the observed live samples. There is no blind O/0 or I/1 substitution. OCR candidates/confidence are local attempt evidence; only the server response and verified card demonstrate success.

Assisted mode serializes terminal prompts across workers. Auto mode requires a candidate and either sufficient confidence or agreement; uncertain OCR requests a fresh challenge. Each row has at most three attempts per run; unresolved rows require explicit `--retry-unresolved` to revisit. Three seconds is the minimum configurable spacing per worker, a conservative starting value rather than a published board quota. Every worker owns an independent Pacer; healthy submissions can overlap. A shared cooldown deadline applies network/server-error backoff to all workers, and waiting workers recheck extensions before proceeding. Worker phase logs make concurrent activity visible. Exact server rate limits and CAPTCHA expiry/reuse behavior remain unmeasured.

## Parsing and data contract

The live table has grouped headers: SUBJECT; MAXIMUM MARKS (TH, PR, TOTAL); MARKS OBTAINED (TH-I, TH-II, PR-II); TOTAL; PERCENTILE SCORE; REL. GRADE; STATUS. The parser checks this header layout and each row's cell count before extracting data.

- Returned roll must equal the requested roll.
- Heading must identify SSC Annual and the requested year; endpoint must be Part-II.
- Candidate name is required alongside matching roll/exam. Institution/District, marks, subject tables and result statement are optional; available student fields are accepted without review.
- Numeric totals are reconciled with available subject data. Inconsistent totals are left null with original raw values and data notes preserved; identity remains accepted.
- Missing/absent/withheld values remain raw text/null; they are never changed to zero.
- Maximum marks come from the card; 1100/1200 are not hardcoded.
- Board statements, per-subject grades/statuses and notes such as MARKS IMPROVED are preserved verbatim. Percentile is not relabeled as percentage.
- Derived percentage is only obtained / card maximum when unambiguous.
- Missing/changed subject layouts and optional fields retain the accepted identity. `data_completeness` and `data_notes` describe available data. CAPTCHA pages, missing student identity or mismatched roll/exam remain unresolved.

Actual saved tables provide de-identified regression fixtures for PASS-prefixed numeric totals, ABSENT and MARKS NOT IMPROVED summary-only cards. Unknown optional layouts keep raw table cells in `subjects_raw` without discarding identity. No guessed message is classified as not-found.

Parser version `ssc-part2-v3` includes DOB, student B-form/CNIC, father CNIC and registration number in structured data and all report formats. Values are taken from explicit board labels and remain display strings, including leading zeros, hyphens or masking. Missing source values remain blank; no date/identity inference is performed. Excel stores these fields with text formatting. Photo paths are not structured report columns.

## Durable storage and interruption recovery

SQLite tables: `batches`, `jobs`, `members`, `attempts`.

- Job uniqueness: `(roll, year, course, exam_type)`; school is excluded so a fetch can be reused across input batches.
- Membership uniqueness: batch plus job; duplicate source rows do not duplicate report rows. Source order and original valid-row count remain available.
- One process holds a nonblocking OS file lock for the database. Multiple browser workers run inside that process, using short SQLite transactions.
- Since the exclusive process lock proves no other collector is alive, stale `running` claims can safely return to pending on restart; unfinished attempt receipts are marked interrupted. Per-worker distributed leases are unnecessary for this local single-process design.
- States are pending, running, verified, needs_review and failed. Retry outcomes are logged in attempts; interrupted backoff restarts conservatively from pending rather than preserving an exact next-at timestamp.
- Per-attempt artifacts use unique directories. Required card HTML, PNG and parsed JSON are written before the verified DB commit; SHA256 receipts accompany them.
- Missing or changed required artifacts invalidate verified cache entries and requeue them on collection.
- Before export or collection, old parser versions or cached records missing identity-field keys are reparsed locally from hash-verified original HTML. Upgraded parsed JSON/receipts are written as separate revisions before updating the DB; original evidence and fetched timestamps remain intact. Valid current cards stay cached. Identity-validation failures remain unresolved; missing optional data is accepted. Previously reviewed/failed responses with a saved HTML/PNG capture are also reparsed locally, creating derived receipts before accepting recovered identity. Write failures leave the previous DB row intact.
- Ctrl-C leaves completed rows committed and interrupted rows recoverable. Re-running the identical command resumes rather than restarting verified rows.

A fixture-based interruption/recovery test passes. An actual browser process kill midway through production collection has not been exercised. Automatic refreshed re-fetch/version history for already verified results is future work; retain state and artifacts rather than deleting them to force refresh.

## Output contract

[Output design](OUTPUT_DESIGN.md) records the human-facing layout. One fixed exam folder contains one main workbook:

```text
output/Matric 2026 - Part-II Annual/
  Matric 2026 - Master Results.xlsx
  Matric 2026 - Master Results.csv
  Read Me.txt
  All Result Cards.zip
  Result Cards/
  Institutions/KASUR/KASUR Results.xlsx
  Institutions/LAHORE/LAHORE Results.xlsx
  Data/results.json
  Data/institutions.json
  Data/manifest.json
```

Overview opens first, with record counts and links to student tabs/institution reports. All Students contains every matched identity, optional marks and card-image links. Separate KASUR/LAHORE tabs are inside the master. Subject Marks and Missing Results retain the detailed breakdown and unresolved list. Human-friendly headers, Excel tables, filters, frozen headers, text identity columns, status colors and print settings are applied. Institution folders include their own CSV, card images and card ZIPs.

Each exam master combines lists in the same database, deduplicated by the existing roll/year/course/exam key. Missing institution values stay blank with a separate readable folder. Safe colliding folder/sheet names receive numeric suffixes; routine labels have no hash suffix.

An export signature plus complete file-hash check makes unchanged reruns no-ops. Changed reports are built in a hidden staging directory, prior managed output is archived, and publication rolls back if the rename fails. The four old batch-id/timestamp snapshots are now under artifacts/report-archive/Legacy output, with independently verified inventories. Unknown folders are preserved. No website request or change to saved job data was needed for this migration.

Final verification: all 51 master records, identity fields, optional totals/statuses and all result-card links match stored data; institution counts are 37 Kasur and 14 Lahore. Re-export preserves exact bytes and file modification times. Existing parser/cache regressions and eight additional presentation/publication tests pass (38 total). Evidence: docs/evidence/friendly-output-verification.json.

## Source layout

```text
bise_results/
  __main__.py    # audit, inspect, collect, export, status
  importer.py    # CSV audit
  captcha.py     # local OCR
  parser.py      # recognized SSC Part-II card contract
  store.py       # SQLite, process lock, membership and recovery
  runner.py      # visible workers, pacing, retries, receipts
  reports.py     # stable exam master + institution exports
  presentation.py # readable workbook, navigation and image links
  publishing.py   # safe names, reversible archives and publication
  probe.py       # bounded original schema-discovery utility
```

## Current acceptance and future input

All 51 input identities are independently matched with saved raw HTML, including DOB and student/father CNIC values. All available numeric overall totals match raw card text. Original accepted records, source captures, previous report snapshots and input CSV are unchanged. No additional lookup attempts were needed to recover the 17 review cases. All 51 are skipped on idempotent resume, including summary-only results.

The final batch contains 20 complete records, 28 partial records with FAIL outcomes and no numeric overall total, and three summary-only records (one ABSENT, two MARKS NOT IMPROVED). `complete_collection` concerns accepted student identities, while `data_completeness` describes optional marks coverage. Final report artifacts and per-institution ZIPs pass count, image, CRC and hash checks. Evidence: `docs/evidence/partial-results-verification.json`.

For future input, run the normal collect command. Export/resume first recovers usable saved responses locally. Review is reserved for missing or mismatched student identity or a request that did not produce a usable card. No guaranteed throughput or completion time is established.

## References

- [Official result site](http://result.biselahore.com/)
- [Playwright Python library and visible mode](https://playwright.dev/python/docs/library)
- [Browser-context isolation](https://playwright.dev/python/docs/browser-contexts)
- [Browser compatibility](https://playwright.dev/python/docs/browsers)
- [Tesseract installation](https://tesseract-ocr.github.io/tessdoc/Installation.html)

## Parallel processing verification

The original global submission lock serialized clicks even though form/OCR tasks were async. Worker-local pacers now permit simultaneous requests while retaining shared error backoff. Five new async/unit/integration regressions cover independent first submits, each worker's interval, shared/extended cooldown, simultaneous worker activity, distinct contexts and one commit per job (43 default tests total). An explicit real headed Chromium check routes every request locally, uses fake OCR and confirms two overlapping POST requests with a measured 0.042-second start gap. Existing board results are not fetched again. Evidence: docs/evidence/parallel-workers-verification.json. Board throughput on a new batch has not been benchmarked by this test.

## Graceful stop and large imports

The expanded input contains 285,793 rolls. Collection previously exported only on normal completion; Ctrl-C followed by Browser.close losing the driver skipped that export even though each accepted row had already committed. The CLI now catches cancellation, tolerates already-closed contexts/browser, marks interrupted claims resumable and exports committed processed rows before returning status 130. Worker startup failure also exports prior committed data before reporting its error. Hard termination still needs a separate export.

Collection and the `export` CLI omit never-processed pending/running rows by default. Overview/manifest preserve complete imported and unsaved queue counts; `complete_collection` checks the full queue. An explicit `--include-pending` export supports full queue rows. Cached reparsing streams only eligible captured states rather than loading all pending records. The Python export helper retains its optional full-row mode for existing callers.

The actual master now contains 111 unique saved records, matching all DB identities; its artifact hashes pass. Three new stop/export/large-queue regressions bring the default suite to 46 tests. Evidence: docs/evidence/interrupted-export-verification.json.

## Optional headless collection

`collect --headless --captcha-mode auto` supports the same parsing, queue, idempotency and reporting contract without visible windows. Default mode, manual inspection and assisted CAPTCHA remain visible. The software worker range is 1–9; the optimal machine/server throughput has not been established. The inspected machine exposes 16 logical CPUs and approximately 16 GB RAM.

Two new regressions verify a three-worker headless launch/commit and rejection of headless-assisted mode before state changes (48 default tests total). A separate actual headless Chromium test with DISPLAY/WAYLAND_DISPLAY removed confirms three overlapping locally intercepted requests (0.019-second start gap), no external board requests and working DOM/image capture. OCR was fixed for that concurrency test. A five-new-result operator-run live pilot is documented before an unlimited batch. Evidence: docs/evidence/headless-workers-verification.json.
