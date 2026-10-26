# Youth Games registration: visible or headless workers

`youthgames_register` is implemented beside the BISE collector and normalizer. It uses the existing `.venv`, a separate SQLite registration database, and the operator-confirmed Lahore input/image. No further installation is needed on this machine.

The visible one-applicant live pilot succeeded on 21 October 2026. A server success flag and structured success notification with a tracking ID were captured. The portal omitted the older visible tracking element; the parser now supports its current Flasher JSON payload. The original conservative receipt was reconciled locally without another POST.

Original pilot snapshot: **1 registered, 6 ready, 15 age-ineligible, 0 uncertain**; use the status command for current counts. The shared image was accepted by the registration endpoint in this pilot; this is a registration acceptance observation, not a claim of subsequent departmental approval.

## Ready master workflow (recommended)

The current combined ready master is the input for `registration_profiles/master.json`. Scope is saved LAHORE and KASUR records from the published BISE master, with the configured Secondary/Open/Athletics and Lahore/Shalimar/UC178 settings. Original source district/address and field origins remain in matching provenance.

Refresh normalization and bind the latest exact input in one repeatable command:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register sync
```

`sync` runs `normalization_profiles/combined.json`, refreshes `normalized_output/Combined/Ready to Register.csv`, validates the matching provenance/photo and updates the master profile's input/confirmation fingerprint. It uses the configured operator settings and excludes known uploaded/uncertain/blocked identities. It performs no applicant POST and does not import the local registration queue. Unchanged input leaves the profile and report mtimes unchanged.

The first sync creates the Master profile using the existing `registration_profiles/lahore.json` operator settings, shared database, campaign and photo. Later syncs preserve edits in Master and update only its input references/hash. Override the source/settings paths with `--normalization-config PATH --settings PATH`; `--settings` is used only for initial creation. Scope/category/location changes that the current single-location Open Category adapter cannot handle fail explicitly. A changed photo fails its existing hash check.

Prepare local jobs and refresh live form options without submitting:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register sync && .venv/bin/python -m youthgames_register prepare --config registration_profiles/master.json
```

Start/resume registration from the refreshed ready master:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register sync && .venv/bin/python -m youthgames_register submit --config registration_profiles/master.json --limit 0 --workers 1 --captcha-mode auto
```

The command above uses one visible worker and automatic visible-addition CAPTCHA. Optional headless and parallel execution is described below. `--captcha-mode auto` fills the sum without a terminal prompt; `--captcha-mode assist` remains available for manual answers. Submission still refreshes live rules and uses the shared portal/campaign/BForm unique key. A registered identity cannot become a new job by appearing in another profile or a refreshed master. New/changed rows enter the local queue only after preparation; repeated preparation upserts existing keys. An empty ready file returns `nothing_to_register` and cannot fall back to the previous prepared batch.

The live read-only preparation check imported **980 ready records** with **0 input conflicts** on 21 October 2026. No additional applicant POST was sent. Open `registration_output/Master/Registration Results.xlsx` for that prepared queue. Counts change as collection and registration progress. The old Lahore pilot profile below remains available for its original snapshot; Master is the main input for future batches.

## Continue the remaining ready students

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register submit --config registration_profiles/lahore.json --limit 0 --workers 1 --captcha-mode auto
```

This opens a visible Chromium browser and processes one applicant at a time. Auto mode reads the visible addition challenge, calculates the sum and fills it without prompting. It accepts only the bounded `number + number = ?` format, never executes page text, and stops before submission if the format changes. In explicit assist mode, enter the numeric answer or `/skip` to leave the applicant ready and stop the run. `--limit 0` means all ready applicants, not the 15 ineligible students. Registered applicants stay skipped. One context and tab per worker are reused for the run. Every applicant starts with a fresh form GET and a newly solved challenge.

To process just one more ready applicant, change `--limit 0` to `--limit 1`. Repeating a command selects remaining ready applicants; it does not repeat an already registered applicant. A single specific source roll can be selected with `--roll ROLL_NUMBER`.

Headless and 1–36 workers are now supported. Start with three; the software limit is not a measured portal quota or a guarantee of higher throughput. Use `--limit 9` for a small operator-run pilot, then `--limit 0` for all remaining ready applicants:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register sync && .venv/bin/python -m youthgames_register submit --config registration_profiles/master.json --limit 0 --workers 3 --captcha-mode auto --headless
```

`--workers 9` selects nine workers. `--headed` shows their tabs/windows. Omitted worker/visibility flags use the JSON profile; current profiles still default to one visible worker. Headless or parallel submission requires automatic CAPTCHA mode; invalid combinations fail before state/browser changes. Preview/prepare can run headless without answering a CAPTCHA.

One Chromium process hosts an isolated browser context and tab per worker. Each worker reuses its own tab; cookies, CSRF fields, CAPTCHA and attached files stay separate. A shared queue and the database's atomic ready-state claim prevent workers from selecting the same applicant. `--limit` applies to the whole run, including all workers; explicit CAPTCHA retries remain attempts of the same applicant.

The configured five-second submission interval is **per worker**, so healthy workers can fill and submit concurrently. A known uncertain response or blocked input stops new claims and new submission intents across all workers. Already dispatched requests finish capturing receipts. An attempt stopped before intent stays ready; one stopped after intent is retained conservatively. HTTP throttling/ambiguous POST responses are not automatically retried. Reconciliation remains necessary for uncertain outcomes. Ctrl-C waits for worker cleanup before releasing the state database lock.

Concurrency is verified with intercepted synthetic responses, including nine simultaneous requests; live multi-worker registration throughput has not been measured. No live applicant POST was needed to implement this feature.

## Files and configuration

Profile: `registration_profiles/lahore.json`.

```text
registration_output/Lahore/
  Registration Results.xlsx      current states, tracking IDs and row details
  Registered.csv
  Ready.csv
  Ineligible.csv
  Missing Details.xlsx
  Uncertain Submissions.xlsx
  Summary.txt
  Receipts/job-NNNNNN/attempt-NNNNNN/
    filled-comparison.json
    filled.png
    response.html
    result.png
    receipt.json
    reconciled-....json           only when captured evidence was reconciled
  Previews/
  Data/manifest.json
```

Registration state is separate: `data/youthgames.sqlite3`. Sealed normalized input/provenance/photo snapshots are under `data/youthgames_snapshots/`. Original BISE/normalization files are not modified. Keep this database and receipts together: starting over with an empty database discards local duplicate protection.

The profile pins CSV, provenance and image hashes. Do not casually change `campaign` or the state database path: duplicate identity is portal + campaign + student BForm. Source/profile renaming inside the same campaign/database does not create a new applicant. Updating the normalized CSV requires an explicit matching confirmation/fingerprint in the profile; already-registered identities are still preserved, with changes reported as conflicts.

The configured shared graphic is `pictures/profile_pic.jpeg`. Operator-confirmed defaults remain documented through normalization provenance; registration does not infer new gender, contact, residence or size values.

## Prepare and preview without submitting

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register prepare --config registration_profiles/lahore.json
```

This refreshes current form/dropdown/age constraints, verifies the source/image snapshot and prepares/export states without an applicant POST. Current Secondary DOB bounds are `2009-01-01..2016-12-31`, inclusive. The app neither changes DOB nor switches education category to fit age. The observed rule and UC alias are in the profile; drift causes a pre-submit error.

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register preview --config registration_profiles/lahore.json --limit 1 --workers 1
```

Preview fills one ready student's form and attaches the image, verifies fields and saves a screenshot/comparison under Previews. No declaration/POST is allowed. It closes after saving; the local job stays ready.

`inspect` refreshes the same read-only site contract and local prepared report. `prepare`/`inspect` can update local queue/report files; they cannot create a website registration.

## Receipts, errors and stopping

- The app verifies every selected dropdown/text field and the browser File SHA-256 before submission intent. The request gate allows one matching official `/applicant` POST per attempt and checks all non-CSRF payload fields. Chromium can omit file bytes from intercepted postData; in that case the actual browser File hash and multipart filename are verified. When bytes are present, their hash is checked too.
- Only a server success marker plus one unambiguous tracking ID marks a job registered. Current server Flasher success envelopes are parsed as JSON, never evaluated as JavaScript. A generic 200 or arbitrary tracking text is insufficient.
- A captured, explicit CAPTCHA-only rejection may retry with a fresh form/challenge, at most twice. Other validation errors are retained as rejected. Duplicate CNIC evidence is not counted as a new registration. Phone validation errors do not establish that a student was already registered.
- After a possibly dispatched POST, timeout/browser loss/crash becomes uncertain. The app never auto-retries that applicant and blocks further profile submissions until reconciliation. The configured interval starts at five seconds; HTTP throttling/ambiguous server errors stop rather than blindly repeating requests.
- Ctrl-C before submission intent leaves a resumable local draft. Ctrl-C after intent/dispatch retains an uncertain state. Reports update after each terminal attempt and on graceful shutdown; parallel in-flight jobs may temporarily appear as filling/submitting. Hard crashes retain committed database state; export can rebuild reports.
- Success receipts are hash checked. Missing local receipt files do not make a registered applicant ready again. Original and derived reconciliation receipts are both retained.

Status and export never submit:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register status --config registration_profiles/lahore.json && .venv/bin/python -m youthgames_register export --config registration_profiles/lahore.json
```

For a captured success response initially classified uncertain, reconciliation is local:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register reconcile --config registration_profiles/lahore.json --job JOB_ID
```

V1 reconciliation verifies the stored dispatched request binding, artifact hashes and captured server success/tracking ID. If that evidence is absent, it leaves the job uncertain for official operator reconciliation. It does not invent a lookup API, retry an unknown registration or accept arbitrary tracking text.

## Validation

All **165 regression tests** pass, including registration domain, state, browser, CLI and concurrent-worker tests. Browser tests use synthetic identities and intercepted local responses, with no live applicant requests. Coverage includes current Flasher receipts, one-shot POST/image/field gates, preview blocking, inclusive DOB bounds, UC parent scope, duplicate keys across profiles, crash windows, receipt reconciliation and source/image fingerprints.

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m unittest discover -s tests -v
```

See `docs/evidence/youthgames-live-pilot-verification.json` for aggregate live-pilot evidence. Student-level receipt/tracking data stays in registration_output, not documentation or logs.

## CAPTCHA mode and interruption

The original V1 used assisted entry. Automatic visible-addition solving is now implemented, and the Master/Lahore profiles default to `auto`. The CLI mode overrides the profile for that invocation, so an old command explicitly containing `--captcha-mode assist` still asks for input. Use `--captcha-mode auto` for unattended addition solving; per-worker pacing, declaration, request verification and receipt rules still apply. Headless/parallel submission requires auto mode; manual assist remains for one visible worker. Every fresh form/CAPTCHA retry reads its own visible challenge. Changed challenges are re-read before accepting the answer; unsupported formats stop instead of guessing.

Assisted entry now uses cancellable Linux terminal I/O instead of `asyncio.to_thread(input)`. One Ctrl-C removes the input reader and can exit without waiting for an executor input thread. Cancellation before submission intent leaves the job ready; outcomes after dispatch remain uncertain until reconciled. The interrupted job 23 was verified ready with no dispatched POST on its interrupted attempt.

## Worker browser lifecycle

The Master/Lahore profiles now set `browser.fresh_context_per_applicant=false`. Each worker creates its own browser context and tab for its batch (the first also handles catalog inspection); it closes them when the command finishes or is stopped. It no longer closes/reopens the window after each result.

Each applicant and explicit CAPTCHA retry still navigates to the registration URL with a fresh GET, obtains the current CSRF/CAPTCHA and overwrites/verifies all student fields and the attached file. One new POST gate is used per attempt and stays unarmed between jobs. Receipts/screenshots are saved before navigating to the next applicant; a completed job's receipt remains immutable. An uncertain outcome still stops processing.

`fresh_context_per_applicant=true` remains supported for isolated contexts when explicitly needed. Reuse is the default in the template and current operator profiles; no command-line change is required.

Headless/concurrency evidence: [verification](evidence/youthgames-headless-workers-verification.json). Three- and nine-worker tests use real Chromium with intercepted synthetic traffic; the separate live headless form check performed zero mutation requests.


## Browser-free HTTP pilot

A bounded HTTP pilot is implemented in `youthgames_register.http_pilot`. One live applicant from the existing Master ready queue registered successfully through Python urllib HTTPS: job 56, attempt 40, one applicant POST, HTTP 200. The server success marker and structured tracking notification agree with the committed database entry. Response/request-proof hashes and the Registered.csv row were independently verified. No Chromium process is launched by this transport.

The pilot checks the pinned input/consent/photo against the existing prepared job, obtains a fresh cookie session and CSRF/CAPTCHA, reads live option and DOB data without executing JavaScript, validates the exact multipart fields/image bytes, and uses the shared registration DB lock and intent/dispatch ledger. Unknown POST outcomes become uncertain and are never automatically replayed. A failed form GET creates no submission attempt.

For a deliberately requested additional one-student registration:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register.http_pilot --config registration_profiles/master.json
```

Each invocation selects the next ready applicant and can create one new real registration. It uses the currently prepared, pinned profile input; it does not refresh/import another batch. The normal submit command still uses the browser transport, with its existing worker settings. The HTTP pilot does not add bulk or concurrent HTTP execution.

Nine synthetic HTTP regressions cover success receipts, saved-row skipping, live field/photo mismatch, strict data-only catalog parsing, failed GET recovery, post-dispatch timeouts, explicit rejection without retry, 419 classification and redirect replay prevention. Evidence: [HTTP live pilot](evidence/youthgames-http-live-pilot-verification.json).

## Database-only reporting and Raiwind assignments

Registration now defaults to `state.write_reports=false`: submission state, tracking IDs and receipt paths remain in SQLite. Existing workbooks are historical snapshots; explicit `write_reports=true` restores optional legacy reports. Live assignment/registration queries and the one-per-UC pilot are in [the Raiwind allocation guide](RAIWIND_ALLOCATION.md).

The separate Raiwind one-per-UC HTTP command also prepares its pinned jobs without a browser. See [direct HTTPS UC verification](RAIWIND_ALLOCATION.md#direct-https-one-additional-pilot-per-uc). It seals 20 specific confirmed rows, so repeated runs do not select another student. The low-level one-applicant command also accepts `--roll` to constrain selection and prevent fallback to a different ready applicant.

## Concurrent HTTPX runner

The separate `bise_allocate http-submit` command now handles 1–32 asynchronous HTTP workers with a Rich progress dashboard and the shared registration ledger. [Usage, preview and verification scope](HTTP_CONCURRENT.md).

Use `--diagnostics` on `submit` or `preview` for a Chromium dashboard, per-run JSONL/Summary, and per-attempt network status/rate headers bound to the receipt. See [the bounded headless comparison command](BROWSER_DIAGNOSTICS.md).
