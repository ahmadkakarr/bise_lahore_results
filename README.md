cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results collect roll_number_slips/2026/10th/roll_numbers_only.csv --class 10 --limit 0 --workers 18 --captcha-mode auto --headless --retry-unresolved

cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results collect roll_number_slips/2026/9th/roll_numbers_only.csv --year 2026 --class 9 --limit 0 --workers 18 --captcha-mode auto --headless --retry-unresolved

cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize run --config normalization_profiles/lahore.json && .venv/bin/python -m bise_normalize run --config normalization_profiles/kasur.json

cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize run --config normalization_profiles/combined.json

cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register sync

cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register sync && .venv/bin/python -m youthgames_register submit --config registration_profiles/master.json --limit 0 --workers 1 --captcha-mode auto

Headless, three parallel registration workers (1–36 supported):

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register sync && .venv/bin/python -m youthgames_register submit --config registration_profiles/master.json --limit 0 --workers 3 --captcha-mode auto --headless
```

# BISE Lahore Results

Python + visible or optional headless Playwright browser collection for SSC / Matric, 2026 Part-I (9th) and Part-II (10th) Annual. Reports are grouped by the exact **Institution/District** value returned on the board card.

## Database-first workflow

Collection saves student records directly in `data/results.sqlite3`. Normal completion and Ctrl-C do **not** generate Excel, CSV masters, copied card folders or ZIP archives. Verified capture files are removed only after their complete parsed data and hash-bound retention record commit; unresolved captures remain under `artifacts/`. Use `--keep-verified-artifacts` to retain originals explicitly. [Audited cleanup and database-only retention](docs/DATABASE_RETENTION.md). A free-space check stops new lookups below 2 GiB.

Normalization profiles read a coherent, read-only snapshot of committed database records for their configured exam. They no longer depend on an exported master. Normalization keeps the required CSV files for registration, with Excel disabled (`output.write_excel=false`). See [database workflow and recovery verification](docs/DATABASE_WORKFLOW.md).

Inter Part-II 2024 has a separate [server pilot and collection policy](docs/INTER_2024_REMOTE_PLAN.md).
The four-roll pilot runs in dedicated Proxmox LXC 950, independently of this
laptop. From this checkout, `.venv/bin/python -m bise_remote.pilot status`
shows the guest job log; `download` obtains a hash-verified SQLite snapshot under
`artifacts/inter-2024-server-pilot/results.sqlite3`. `start` resumes only those
four pilot rolls, skipping completed records; it does not launch the full list.

HSSC DOB is absent on the inspected cards. Explicit
`--missing-dob-policy estimate --estimated-age-at-exam 18` preserves a labelled,
unverified cohort estimate (about 20 in 2026), keeping official DOB empty and
games registration blocked by default pending a verified DOB. A separate,
operator-confirmed provisional-DOB exception for the frozen university cohort is
documented in [the implemented server workflow](docs/INTER_2024_UNIVERSITIES_IMPLEMENTED.md).
This preserves original blank DOBs and marks provisional dates for correction;
it does not verify individual age. Default collection still
requires DOB. `--save-student-photos` saves the original board student portrait
in SQLite before raw captures are deleted. To materialize the pilot photos:

```sh
.venv/bin/python -m bise_results photos --database artifacts/inter-2024-server-pilot/results.sqlite3 --output 'artifacts/inter-2024-server-pilot/Student Photos'
```

The photo mapping links original photo hashes/paths to exact roll/exam and
student B-Form; no existing registration profile is changed automatically.

Full Inter 2024 server collection uses a separate durable job directory and
persistent systemd service. Start/resume and monitor from this checkout:

```sh
.venv/bin/python -m bise_remote start
.venv/bin/python -m bise_remote watch
```

The full input has 194,851 unique rolls, with 8 headless browser workers and 2
OCR workers. Pilot identities/photos seed the full DB once; repeated starts
refuse a second active collector and saved rows are skipped. `watch` reconnects
after laptop/network interruptions; Ctrl+C closes only the monitor. `status`
gives one status view, `logs` follows detailed events, and `stop` explicitly
stops/disables the collector while preserving its DB. `download` obtains a
hash-verified resumable online snapshot under
`remote_downloads/Inter 2024 - Part-II Annual/`.

The job data lives at `/var/lib/bise-results/jobs/inter-2024-part2-annual/` in
LXC 950. Guest storage admission pauses below 4 GiB and resumes at 6 GiB; an
independent host timer checks pool capacity/metadata every minute. Stale/unsafe
host health pauses new work. Unresolved evidence has a 1 GiB quota. Collection
runs independently of the laptop and resumes after guest reboot via systemd.

## Historical pilot status

- Input is `roll_numbers_only.csv`; it can grow beyond the original 51-roll sample. Use Overview/status for current imported and saved counts.
- Installed environment: Python 3.14, Playwright 1.64, Tesseract 5.5.3; pinned packages in `requirements.lock.txt`.
- Original 51-roll sample completed and 17 old review cases recovered locally. The expanded import has 285,793 rolls; the latest verified export contains 111 saved student records. Pending queue entries remain in SQLite rather than becoming blank workbook rows.
- Parsed roll, year/SSC heading, subject totals, board result statement, improvement note and Institution/District; independent raw-HTML checks passed.
- Master and institution workbooks contain every processed student record. The latest 111 exported identities agree with SQLite; report file hashes verify. Imported/pending totals appear separately in Overview.
- Forty-eight regression tests pass, including graceful-stop export, closed-driver cleanup, large pending queues, covering identity-first acceptance, PASS-prefixed totals, ABSENT/NOT IMPROVED summaries, missing/changed subject layouts, preserved identity data, offline review recovery, idempotent cache/resume, exports, artifact hashes, grouping and formula escaping.

[Implementation plan](docs/IMPLEMENTATION_PLAN.md) · [Pilot evidence](docs/evidence/pilot-verification.json) · [Identity-field repair evidence](docs/evidence/identity-fields-verification.json) · [Full-batch parser evidence](docs/evidence/partial-results-verification.json) · [Friendly-output verification](docs/evidence/friendly-output-verification.json) · [Parallel browser verification](docs/evidence/parallel-workers-verification.json) · [Interrupted-export verification](docs/evidence/interrupted-export-verification.json) · [Headless verification](docs/evidence/headless-workers-verification.json)

## Collect or resume a batch

From a desktop terminal, run:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results collect roll_number_slips/2026/10th/roll_numbers_only.csv --class 10 --limit 0 --workers 2 --captcha-mode auto
```

Headless pilot (no browser windows), using nine isolated worker sessions:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results collect roll_number_slips/2026/10th/roll_numbers_only.csv --class 10 --limit 30 --workers 18 --captcha-mode auto --headless --retry-unresolved
```

After checking the 30-result pilot, use `--limit 0` to resume all pending rolls. Saved results stay skipped. Headless requires automatic CAPTCHA mode; inspection and assisted/manual CAPTCHA stay visible. The BISE collector accepts 1–18 workers. The optimal count needs measured CPU/RAM, OCR, server latency and error-rate data. The machine reports 16 logical CPUs and approximately 16 GB RAM; this is not a guarantee of a particular worker count or throughput.

Eighteen headless contexts passed the local 9th/Part-I intercepted-response check with 18 overlapping submissions and a 0.081-second start gap. All 13 worker/pacing/CLI regression tests pass, including 18-worker unique processing for both classes and rerun skipping. This check used fixed fake OCR and sent no board requests; live 18-worker throughput and OCR load remain unmeasured. Evidence: `docs/evidence/headless-18-workers-verification.json`. Games registration supports a separate 1–36 browser-worker limit.

Nine headless Chromium contexts passed the local intercepted-response concurrency check: nine overlapping submissions, a 0.108-second start gap and no board requests. This test used fixed fake OCR; live nine-worker throughput and OCR load remain unmeasured. All 51 regression tests pass, including nine-worker unique job processing and rejection of invalid counts before state changes. Evidence: `docs/evidence/headless-9-workers-verification.json`.

Three real headless Chromium contexts passed the local intercepted-response test without DISPLAY/WAYLAND_DISPLAY, with three overlapping requests and no board requests. The operator subsequently ran a live five-result headless pilot with three workers. All five latest attempts verified successfully; their raw cards/identity fields and all exported hashes were independently checked. The master contains 116 saved records after that pilot. Evidence: `docs/evidence/headless-live-pilot-verification.json`.

`--limit 0` processes all pending rows. It reuses the existing batch `0fe944074f8135cc` and skips verified cards whose required artifact hashes still match. Every worker has its own context/session. Default mode stays visible; `--headless` hides the browser windows. OCR runs locally. Each worker spaces its own submissions at least three seconds apart. Workers have independent submission clocks, so two workers can load forms, run OCR and submit concurrently. Server-error cooldown is shared and rechecked before submission. Assist mode still serializes human CAPTCHA prompts; use `--captcha-mode auto` for independent operation.

Progress logs show each worker's `opening_form`, `reading_captcha`, `waiting_to_submit`, `submitting` and `saving_result` phases. Exact start/finish times depend on page/OCR/network latency; one worker does not wait for the other's healthy lookup. A separate visible-browser test with locally intercepted responses confirmed two overlapping submissions, starting 0.042 seconds apart, with no board refetch.

The runner tries at most three challenges/network attempts per row. A matching roll/exam and student name are sufficient to accept the record. Missing marks, institution, result statement or subject layout do not send student data to review. Available identity/marks/status fields are exported; missing or conflicting numeric totals stay blank, with raw values and descriptive data notes retained. CAPTCHA pages and mismatched/missing student identity remain unresolved. Captcha rejects cannot become not-found results. Exit code 2 means failed/review rows remain; the summary still prints and committed records remain in SQLite.

To explicitly retry unresolved rows with manual CAPTCHA correction:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results collect roll_number_slips/2026/10th/roll_numbers_only.csv --class 10 --limit 0 --workers 1 --captcha-mode assist --retry-unresolved
```

Assist mode shows one terminal prompt at a time: Enter accepts the suggestion, typing replaces it, `/skip` marks that row for review. Press Ctrl-C once to stop: workers close and unfinished rows remain resumable. Completed records are already committed to SQLite; shutdown prints database counts and does not compile reports. Rerun the same collect command to resume; saved rows stay skipped. Multiple collector processes are blocked by a database file lock; workers within one process run concurrently.

To process only five new rows, use `--limit 5`. Default collection is five rows, one worker and assisted CAPTCHA. Keep the original CSV and batch label unchanged to resume the same input batch. Optional `--school` labels input batches only; output grouping always follows the board field.

## Optional legacy reports

These files are produced only by explicit `bise_results export` or `collect --export-reports`. They are **not** refreshed by normal collection; the database is authoritative. The following describes the historical workbook layout.

Open **[Matric 2026 - Master Results.xlsx](<output/Matric 2026 - Part-II Annual/Matric 2026 - Master Results.xlsx>)**. This is the main file for processed students (111 saved rows in the latest export). If it was already open in Excel/LibreOffice, close and reopen it to load the updated file.

- **Overview** opens first: student counts, board outcomes and clickable institution navigation.
- **All Students**: names, parents, DOB, student/father CNIC, registration, available marks/status. Excel omits result-card URLs.
- **KASUR / LAHORE**: filtered student tabs, all inside the master workbook.
- **Subject Marks**: theory/practical breakdown.
- **Missing Results**: only unresolved lookups. It is empty for the current batch; absent/not-improved/partial records remain in All Students.

The output folder now has readable, fixed names:

```text
output/
  Matric 2026 - Part-II Annual/
    Matric 2026 - Master Results.xlsx
    Matric 2026 - Master Results.csv
    All Result Cards.zip
    Read Me.txt
    Result Cards/
    Institutions/
      KASUR/
        KASUR Results.xlsx
        KASUR Results.csv
        KASUR Result Cards.zip
        Result Cards/
      LAHORE/
        ...
    Data/
      results.json
      institutions.json
      manifest.json
```

Institution folders can be shared with their recipients. Cards are available by roll number in Result Cards folders and ZIP archives. Excel omits the Result Card column and per-card hyperlinks; CSV retains its Result Card relative-path column. Identity values stay text with leading zeros/hyphens intact; marks stay numeric where provided, and missing totals stay blank.

The master combines processed results from imported lists for the same SSC year/exam; overlapping rolls are included once. Never-processed pending/running rolls are excluded by default, while Overview shows the complete imported and unsaved counts. Use `export --include-pending` only when you explicitly want unprocessed rows too. Institution/District grouping always follows the board field. A missing institution stays blank and has a separate “Institution not provided” folder.

**Rerunning updates this same current folder.** An unchanged, hash-verified export reuses the same files without new timestamps or archives. Changed reports are built separately and published with rollback on failure. Previous versions and the four original timestamp snapshots are preserved with file-hash inventories outside output under `artifacts/report-archive/`.

Check progress:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results status 0fe944074f8135cc
```

Refresh the same master from saved data, without opening a browser:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results export 37c58828d045c270
```

The same export command works with either known batch id for the same exam and includes saved records from all lists. Export requires that explicit command or the `--export-reports` opt-in. Normal collection prints the database path and counts instead. Older cached parser versions and usable reviewed responses are repaired locally before export/resume. Original captures and source input remain unchanged.

`verified` means a matched student record was saved. It does not promise every marks field exists: 20 current cards contain numeric overall totals, 28 contain FAIL outcomes without numeric overall totals, one is ABSENT and two are MARKS NOT IMPROVED. Their identity and available subject data remain available in the master. The Available Details and Additional Notes columns describe missing data.

Data/manifest.json contains completion/counts and exported file hashes. Structured JSON uses the stable machine field names. Payments, invoices and print-layout PDFs are outside this implementation.

## Input and form inspection

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results audit roll_number_slips/2026/10th/roll_numbers_only.csv
```

`inspect` opens the selected form visibly and captures the current original tab when Enter is pressed in the terminal. This is useful if the board changes its layout:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results inspect
```

The adapter was verified with bundled Chromium. An optional fresh Brave instance can be used with `--browser-executable /usr/bin/brave`, but that executable has not been smoke-tested. Existing Brave tabs/extensions are not attached; document-control access returned 403 during initial inspection.

## Normalize results into the sample format

The separate offline `bise_normalize` app reads saved records directly from `data/results.sqlite3` and writes `normalized_output/`. The configured Lahore profile selects saved LAHORE students, uses Secondary/Open Category/Athletics and the supplied Shalimar/UC178 settings. Edit `normalization_profiles/lahore.json`, then rerun:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize run --config normalization_profiles/lahore.json
```

Open `normalized_output/Lahore/Normalized.csv`. Excel output is disabled in the configured profiles. Portal-aware Lahore/Kasur profiles also export `Ready to Register.csv`, age-ineligible reasons and registered/blocked reports. Use the ready file for a new upload batch, rather than the unfiltered master. Unchanged input/settings return `Already up to date`; new source rows rebuild without appending duplicates. DOB and identifiers are preserved; user-requested derived gender, shared-phone/address fallbacks and size estimates have per-field origins in audit/provenance. [Normalization guide](docs/NORMALIZATION.md) covers filters, CSV/XLSX sheets/ranges, profiles, corrections and output files.

## Refresh the ready master and games input

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register sync
```

This refreshes normalization from the configured exam in the BISE database and connects `normalized_output/Combined/Ready to Register.csv` to `registration_profiles/master.json`, including matching provenance and pinned hashes. Run it after saved database records grow or uploads finish. It sends no registration POST. To start/resume one visible registration worker:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register sync && .venv/bin/python -m youthgames_register submit --config registration_profiles/master.json --limit 0 --workers 1 --captcha-mode auto
```

Prepared jobs/results are under `registration_output/Master/`; uploaded identities stay protected by the shared database and campaign. [Master workflow](docs/YOUTHGAMES_REGISTER.md#ready-master-workflow-recommended).

## Youth Games registration

The separate `youthgames_register` app supports 1–36 isolated workers, optional `--headless`, reused tabs and automatic visible-addition CAPTCHA. The default remains one visible worker. The Lahore live pilot registered one applicant and saved a tracking receipt; six are ready and 15 are age-ineligible. Open `registration_output/Lahore/Registration Results.xlsx`. [Registration guide](docs/YOUTHGAMES_REGISTER.md) covers submission, preview, reports, durable duplicate protection and uncertain-outcome recovery.

To continue only the remaining ready applicants:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register submit --config registration_profiles/lahore.json --limit 0 --workers 1 --captcha-mode auto
```

`--captcha-mode auto` answers supported addition challenges without prompting. Use `--captcha-mode assist` only with one visible worker for manual entry. Confirmed registered rows stay skipped; uncertain outcomes block further profile submission until reconciled.

## Fresh-machine installation

The current machine is already set up. For a new environment:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && python3 -m venv .venv && .venv/bin/python -m pip install --upgrade pip && .venv/bin/python -m pip install -r requirements.lock.txt && .venv/bin/python -m playwright install chromium
```

Tesseract is an OS dependency, already present at `/usr/bin/tesseract`. Ubuntu/Debian installation if missing: `sudo apt-get install tesseract-ocr`. Missing `venv`: install the matching Python venv package. Missing browser libraries: `.venv/bin/python -m playwright install-deps chromium` may request sudo.

Tests:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m unittest discover -s tests -v
```

An optional visible-browser concurrency check, using only locally intercepted requests and a fixed fake OCR candidate:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m unittest discover -s tests -p verify_parallel_browser.py -v
```

Input data, SQLite state, original card HTML and screenshots remain local and are excluded from Git. Share the relevant institution's report/card ZIP with its recipient.

## Separate 9th and 10th lists

The two inputs under `roll_number_slips/2026/9th` and `10th` are supported. Use `--class 9` or `--class 10`; repeatable commands and their separate Part-I/Part-II master paths are in [class collection guide](docs/CLASS_COLLECTION.md). Both save to the same database with separate exam keys; optional legacy reports retain their class folders and institution grouping. Existing 10th results stay cached after moving the CSV. Run the collector commands one at a time because they share the database lock.

## Fast stop and resume

For the separate server-side **9th 2026 collector with original photos stored as
files**, use the [SSC server operations guide](docs/SSC_SERVER_OPERATIONS.md).
Its explicit remote profile keeps monitoring and state separate from Inter 2024.

Ctrl-C does not trigger report generation. Each finished lookup is committed to SQLite during collection. Restart the same command to resume pending/unresolved work while saved records stay cached. DB-only retention preserves complete parsed data and unresolved evidence; a matching retention ledger prevents deliberately removed captures from triggering a refetch.

For 9th normalization, read the database directly:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize run --config normalization_profiles/9th_combined.json
```

This produces registration CSV files without Excel. Historical optional-export benchmarks remain in [the earlier performance notes](docs/EXPORT_PERFORMANCE.md).

## Stable Raiwind UC allocation

The confirmed Lahore 9th district-only cohort can be allocated evenly and normalized directly from SQLite, with live per-UC registration counts and tracking links. See [Raiwind allocation commands and verification](docs/RAIWIND_ALLOCATION.md). The pilot selects one durable applicant per UC and supports up to 18 workers.

## Concurrent direct HTTP registration

Use `bise_allocate http-submit --config allocation_profiles/lahore_9th_raiwind.json --workers 6 --prepare-ahead 2 --in-flight 2 --dispatch-interval 6 --limit 0 --auto-recover` for an unattended run of remaining Ready students. HTTPX supports up to 36 workers, with separate preparation and in-flight limits. Verified temporary refusals and safe form GET outages automatically retry after shared, durable backoff; throttle episodes slow dispatch pacing. Confirmed identities are skipped before form validation, and unknown POSTs require reconciliation. Rich shows progress, cooldown and saved logs. The completed `http-pilot` command verifies its original fixed 20 students. [Commands, recovery behavior, and operator-run regressions](docs/HTTP_CONCURRENT.md).

Reruns reuse pinned input and skip completed applicants through indexed SQLite lookups. Use `--refresh-input` when newly collected BISE data needs normalization. Safe name separator formatting is shared by normalization and submission, with originals retained. In automatic mode, row validation failures are quarantined in each run's `Needs Attention.csv`. Unknown POST outcomes require reconciliation by default; `--retry-uncertain --uncertain-retries 2 --auto-recover` explicitly permits bounded retries using the observed portal BForm duplicate check, then keeps unresolved applicants uncertain while other students continue.

Collection and registration terminal commands enable a repeating local failure alarm by default for errors, crashes and closed terminals. Successful completion and Ctrl+C stay silent. Use `--no-alarm` to disable it, `.venv/bin/python -m run_alarm stop` to silence a ringing alert, or `.venv/bin/python -m run_alarm test --seconds 3` for a short audible test. [Alarm behavior, controls and silent regressions](docs/RUN_ALARM.md).

Approved Wi-Fi + mobile USB tethering can use `http-submit --connections connection_profiles/wifi-usb.json`: fixed interface-bound HTTP clients, separate durable pacing/cooldowns and one shared registration ledger. Wi-Fi can start/work while the configured phone is unplugged; mobile workers resume when it reconnects. New work is shared among online workers, while previously dispatched identities/retries stay with their recorded route. Naturally changed public IPs are accepted without manual re-pinning; stable interface keys preserve pacing and DHCP sources refresh on reconnect. [Connection discovery, network recovery and run command](docs/MULTIPLE_CONNECTIONS.md). Ordinary commands keep their existing connection behavior.

For an operator-run parallel headless browser comparison with saved network diagnostics, use [Browser diagnostics](docs/BROWSER_DIAGNOSTICS.md). This path has per-worker pacing and does not use the HTTP runner's shared dispatch gate.
