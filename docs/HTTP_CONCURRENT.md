# Concurrent HTTP submission and terminal progress

The old `http-pilot` command verifies the same 20 pinned UC applicants. A completed pilot sends no new forms. It now prints a short summary by default; `--json` restores its complete structured output.

For an unattended run, direct HTTP is the normal path: it avoids Chromium processes while retaining the existing field validation, server receipt checks, registration ledger and dashboard. Browser mode remains useful for inspecting changed JavaScript/UI behavior; it does not remove the portal limiter.

For explicitly approved parallel Wi-Fi/USB processing, opt in with `--connections`. [Multi-connection setup](MULTIPLE_CONNECTIONS.md) documents independent route pacing and one shared identity ledger. The shared spacing behavior below describes the default single-connection mode.

Recommended conservative starting command for all remaining eligible students:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && GAMES_WORKERS=6 && .venv/bin/python -m bise_allocate http-submit --config allocation_profiles/lahore_9th_raiwind.json --workers "$GAMES_WORKERS" --prepare-ahead 2 --in-flight 2 --dispatch-interval 6 --limit 0 --auto-recover
```

The same command now uses a bounded preparation pipeline by default: four forms may prepare in parallel, and up to four POSTs may independently await responses. To make these limits explicit:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate http-submit --config allocation_profiles/lahore_9th_raiwind.json --workers 18 --prepare-ahead 4 --in-flight 4 --limit 0
```

`--prepare-ahead` accepts 1–8 (default 4); `--in-flight` accepts 1–36 (default 4). Both effective limits are capped at the worker count. A worker owns its cookie session until that applicant's response has been saved. Workers awaiting confirmation do not block preparation of subsequent applicants. These concurrency limits do not authorize a higher portal rate or an 18-request burst.

`--limit 0` means all remaining Ready identities. For a bounded run, use `--limit 18`. `--workers` accepts 1–36 total, default 18. With `--connections`, `--workers-per-connection 6` instead requests six on EACH interface (12 total for two connections, cap 36 combined). The software cap is not a measured portal quota. Repeating the command skips confirmed and already externally registered applicants by the shared portal/campaign/BForm key. Uncertain submissions require reconciliation unless the operator explicitly selects the bounded retry/continuation policy below.

The first run after this update refreshes normalization once. Subsequent runs reuse the confirmed pinned input when its files, settings and normalization policy are unchanged. The source BISE database is not rescanned during this fast resume. Add `--refresh-input` to rebuild eligible input after collecting more source data; this still never resubmits completed identities. Recovered rows and changes to configuration/catalog/policy automatically invalidate the resume cache. CSV/provenance/photo hashes are still verified on every registration run.

The `prepared_inputs` SQLite table records the resume context and input hash. The immutable `registered_applicants` ledger has a portal/campaign/BForm primary key. Queue selection uses a covering index and returns only pending job IDs and roll numbers; full payloads are fetched only when a worker claims a row. Historical receipts are not opened to skip completed students. `portal_dispatch_clock` preserves the last dispatch without rescanning historical attempts at every restart.

## Automatic temporary-error recovery

For the operator-confirmed portal BForm uniqueness workflow, add `--retry-uncertain --uncertain-retries 2 --auto-recover`. Old uncertain attempts stay preserved. During the run, a new lost/unclear response gets at most two extra attempts after the assigned connection's durable backoff. Duplicate refusal becomes terminal `already_registered_external`, success saves tracking, and unresolved applicants stay uncertain while the next students continue. `--uncertain-retries 0` quarantines immediately without blocking other applicants. Ctrl+C, local receipt/disk failures and worker errors retain their stopping behavior. Details and the captured duplicate-response evidence: [uncertain-response policy](MULTIPLE_CONNECTIONS.md#explicit-retry-of-uncertain-submissions).

The saved mobile timeout exposed a 60-second limit across POST and its redirect GET. POST/redirect requests now have a 180-second total budget and 90-second idle reads. Form GET requests retain a 60-second total budget. On failure, receipts include received redirect hops and the interrupted request method/path/phase rather than losing all intermediate diagnostics. A longer response budget does not alter dispatch spacing.

`--auto-recover` is **on by default** for `http-submit`. It keeps an unfinished applicant queued after an explicit, receipt-verified throttle refusal, even after more than two refusals. Retry count is not capped for this narrow temporary outcome. Every retry gets a fresh cookie/CSRF/form session and a separate audited attempt. Confirmed registrations are never replayed. Unknown POST outcomes are retried only with the explicit policy above.

All workers share a cooldown. Each new retry episode uses at least 15, 30, 60, 120, 240, then 300 seconds; a longer server-requested wait plus two seconds always wins. Refusals received together during the same cooldown are coalesced into one episode. HTTP-429 form GETs participate in the same policy. On a throttle episode, shared dispatch spacing increases conservatively toward a maximum local target of 60 seconds; a larger operator interval remains in force. It does not automatically accelerate again after a few successful requests. No fixed local setting guarantees the portal will never throttle.

The `portal_pacing` SQLite table stores portal/campaign, dispatch spacing, retry/throttle rounds, reason and `not_before`. Restart restores the cooldown and slower pacing before form fetching/submission. `submission_recoveries` independently preserves original refusal receipts and server deadlines. Worker-count changes do not reset registration protection or pacing. An explicit `--dispatch-interval` sets a new operator baseline of at least five seconds, replacing older configured/non-throttle baselines. Previously recorded server-throttle spacing and every cooldown deadline still win. Without an explicit interval, the old baseline is retained.

Temporary form GET transport errors and HTTP 429/500/502/503/504 use three safe attempts per cycle. With auto recovery on, exhaustion starts another backoff cycle instead of ending the batch. This also works during initial preflight, before any applicant is claimed. If a worker's GET fails or its prepared form repeatedly ages out before intent, its completed undispatched attempt stays audited and the applicant is requeued after cooldown. No progress/completion count is added for these temporary deferrals.

`--no-auto-recover` retains the older bounded mode: explicit throttle retries stop at the profile retry count, exhausted safe GET cycles stop the run, and row validation rejection stops the batch. It still respects already saved server cooldowns. Shared contract failures, receipt or disk errors and uncertain POSTs still stop for review in either mode. Auto recovery deliberately does not claim that every possible failure can be retried safely. Ctrl-C interrupts cooldown waits immediately.

## Name validation and row quarantine

The saved 2026-10-22 rejection was caused by a father name formatted as `AHSAN -UL-HAQ`. The captured portal rule is `^[\p{L}]+(?:[ .'-][\p{L}]+)*$`: a space followed by a hyphen is two separators. Normalization and the wire adapter now produce `AHSAN-UL-HAQ`, preserving every letter and the original board/provenance value. Separator changes are recorded in normalization provenance and private HTTP request proofs. Digits, slashes and ambiguous mixed punctuation require individual correction.

Only a matching explicit name rejection, its latest dispatched attempt, valid response/receipt hashes and a safe available correction authorize local recovery of an old rejected job. The old payload and attempt remain audited. A correction already sent and rejected does not receive another automatic retry. Unknown, damaged or mixed success/error evidence never authorizes replay.

With `--auto-recover` (default), explicit applicant validation failures are saved as rejected and quarantined while unrelated students continue. Invalid local names are blocked before an applicant POST. Each run writes `Needs Attention.csv` with job/roll, state, reason and receipt path. A drained batch containing quarantined rows finishes as `complete_with_issues`, with exit code zero; its rows are not counted as registered. Their database states and evidence remain available for correction. Unknown POST outcomes still stop for reconciliation; shared contract, disk and audit failures also stop. `--no-auto-recover` retains stop-on-rejection behavior.

## Terminal display

Allocation runs keep a pinned batch overview above the worker table:
`Batch total 18,925 · Completed 11,065 · REMAINING 7,854 · Held 6`.
Completed includes successful registrations and explicit portal duplicates.
Remaining includes eligible Ready and active applicants; Held is separate for
normalization conflicts, validation failures and unresolved uncertain outcomes.
These three buckets sum to the fixed batch total. Confirmed outcomes immediately
decrease Remaining, while uncertain-to-Ready recovery moves rows back from Held.
Repeated notifications cannot double-count a batch registration.

`Run selected at start` is explicitly the initial queue, including when `--limit`
selects only part of the batch. The run progress bar measures processed rows;
it is not the current whole-batch remaining count. The header separately shows
Completed before this run and Completed this run. Allocation counters are seeded
from read-only SQLite snapshots at startup/after preparation, updated in constant
time from committed events, and reconciled against SQLite at shutdown. There is
no full-cohort query after each student.

`registration_output/<profile>/Current Progress.json` is atomically refreshed
as counts change. Each run also retains `Progress.json`. `Summary.json` now
includes final `batch_progress` and `run_selected_at_start`, as well as the
allocation totals added by the CLI; these totals are no longer stdout-only.
The derived progress files contain aggregate counts and no student identities.
An older draining run cannot overwrite a newer run's current snapshot. Cache
write/read failures are shown without stopping registration or changing receipt
state. SQLite and receipts remain authoritative; a hard crash can leave the last
progress snapshot stale until the next DB reconciliation.

Rich supplies live progress/percentage, collection elapsed time, estimated remaining time, successful registrations/minute over the last 60 seconds (excluding startup), per-worker roll/UC/activity, per-UC saved counts, recent outcomes/tracking IDs, and a portal-wide cooldown countdown. Deferred rows are shown separately from validation failures. Stopped summaries include their reason and use a warning border. The header separates Preparing, Prepared, Awaiting response, Saving, and Waiting/idle workers, with the actual configured limits. Startup duration is displayed separately; ETA waits for at least three recent successes. The cooldown countdown refreshes while no network events are arriving. Redirected output uses periodic plain summaries. `--json` prints the structured final result. The raw student data, photos and session tokens are not printed in progress events.

[Open the synthetic dashboard preview](evidence/http-dashboard-preview.svg). Regenerate it without network or database access:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && PYTHONPATH=. .venv/bin/python scripts/preview_http_dashboard.py
```

Detailed events and a compact final summary are saved under `registration_output/Lahore 9th Raiwind/Runs/<UTC run ID>/`. SQLite remains authoritative for status, tracking IDs and receipts; no Excel files are generated.

## Reliability

HTTPX supplies asynchronous networking, bounded connect/read/write/pool timeouts and pooled connections. Each worker owns one client/connection. Cookies reset for each applicant while the TCP pool is reused. Every applicant gets a new form GET; the live fields, option IDs, DOB rules and photo bytes are checked again before dispatch. Off-origin redirects and POST-preserving redirects are rejected. A successful POST redirect is followed with a GET.

One process holds the registration database lock; workers use short synchronous transactions on the event loop's SQLite connection. The input files are snapshotted and checked once per run, then sealed and reused. Common form data is parsed once during preparation, rather than repeatedly parsing the same HTML for every queued student. Each job still validates its fresh live form before submitting.

The configured submission interval now applies **across all HTTP workers**, with a minimum of five seconds between dispatches (at most 12/minute before network latency). Each worker still owns its connection, and POST responses can overlap. Form GET/validation now runs **outside** the dispatch lock. The preparation semaphore bounds the complete window of fetching/prepared sessions; it is released at dispatch. A separate semaphore bounds outstanding POSTs through receipt persistence. Only pacing waits, freshness checks, and durable intent/dispatch markers hold the shared dispatch lock; neither a form GET nor a POST response wait holds it.

Prepared forms use a conservative local maximum age of 30 seconds, measured from the beginning of GET. This is a local freshness bound, not a verified portal expiry guarantee. The form is checked again inside the dispatch gate; aged forms or forms prepared before a shared server cooldown are discarded and fetched afresh, keeping the same undispatched attempt. Repeated expiry returns the job to Ready without a POST. Queueing more forms does not improve permitted throughput and can cause unnecessary refreshes. The last dispatch is loaded from SQLite on restart. Increasing worker count does not increase the portal submission quota. Five seconds is a conservative local interval, not a published server requirement. Actual overlapping POSTs occur when responses outlast permitted dispatch spacing. No portal burst allowance has been confirmed, so simultaneous burst starts remain disabled. Whitelisted public rate-limit/Retry-After headers, when supplied by the portal, are retained across redirects in private run logs and request proofs; cookie and CSRF headers are excluded. The implementation does not infer a higher permitted rate from successful responses.

On 2026-10-21, the actual portal returned HTTP 200 with `data-has-server-errors="1"` and a `#registration-errors` banner: “Too many registration attempts. Please wait 48 seconds and try again.” The first bulk run had 20 successes and 12 explicit throttle refusals. The previous classifier accidentally extracted navigation/declaration lists and concealed that reason. The classifier now reads the named server summary and explicit visible validation feedback, never unrelated lists.

All workers honor the full server wait plus two seconds. Only this exact, explicit throttle refusal, without a success marker or tracking ID or other validation error, is eligible for a throttle retry. The original receipt, response hashes and dispatched attempt are preserved; a derived receipt and `submission_recoveries` audit authorize a return to Ready. Old rejected jobs are recovered only after checking the latest dispatched attempt, job/payload binding and all receipt hashes. The cooldown persists in SQLite across restarts. Automatic mode keeps retrying verified temporary refusals with increasing backoff; bounded mode stops at the configured count. Other validation failures are quarantined in automatic mode; unknown POSTs are never replayed. The separate narrow name-format recovery policy is described above.

Safe GET cycles have at most three attempts and share server-error/Retry-After cooldowns; automatic mode schedules another cycle on temporary exhaustion. Timeouts, cancellation or ambiguous responses after durable dispatch become uncertain; workers stop new claims while already dispatched requests preserve their outcomes. Low disk stops before input copying and before claiming further rows.

Network transport errors now use a separate durable 5/10/20/30-second retry streak, reset by a successful form GET. Server deadlines and throttle spacing remain independent and cannot be cleared by transport recovery. Fixed-interface mode additionally pauses inner GET retries while offline, refreshes socket pools after connection errors/reconnect, and wakes transport waiters after a verified link/address transition. See [mobile reconnect recovery](MULTIPLE_CONNECTIONS.md#recovery-of-a-stalled-mobile-connection). With explicit `--retry-uncertain --auto-recover`, ambiguous POSTs follow the bounded, audited continuation policy documented there; no socket reset itself replays a POST.

Press Ctrl-C once to stop new work and finish in-flight requests. Pressing again cancels remaining requests, conservatively preserving dispatched attempts as uncertain. SQLite stays open until cancellation receipts have committed. No executor threads wait for terminal input.

## Installation

The current environment already has HTTPX and Rich installed. On another checkout, use the pinned dependency file:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m pip install -r requirements.lock.txt
```

## Verification scope

For the 2026-10-22 name-validation/quarantine and fast-resume update, 28 focused offline regressions passed in 2.697 seconds, including the exact separator case, receipt-bound recovery, refusal to replay a previously corrected rejection, quarantine/continuation, immutable registration ledger, 20,000 completed identities, pinned-resume invalidation and CLI options. A read-only check confirmed the original roll 447726 receipt is intact and its father-name formatting has a safe correction. Production DB rows were not changed and no live portal requests/submissions were made during this update. The broader suites below remain operator-run:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && PYTHONPATH=tests .venv/bin/python -m unittest tests.test_name_resilience tests.test_pinned_resume tests.test_http_resilience tests.test_http_concurrent tests.test_browser_recovery tests.test_registration_ledger tests.test_normalization tests.test_portal_normalization tests.test_allocations -v
```

New cases cover more than two explicit refusals followed by success, fresh session tokens on every retry, temporary preflight/worker GET outages, persisted backoff and spacing after restart, coalesced refusals, longer server waits, Ctrl-C while cooling down, and stopping without replay after an ambiguous POST. The evidence and test counts below describe earlier versions, not acceptance of this latest update.

Concurrency is tested with real HTTPX clients and intercepted synthetic responses. Tests cover parallel GET preparation, overlapping POSTs with nonzero shared pacing and independent in-flight bounds, preparation continuing during a pending POST, 18 and 32 overlapping POSTs with the pacing clock disabled in synthetic tests, independent cookie sessions, unique job dispatch, shared pacing across workers and restarts, explicit throttle recovery with fresh sessions, bounded retry, receipt tamper refusal, prepared-form expiry, cooldown invalidation, cancellation during preparation, recent-rate/startup accounting, safe rate-header capture, rerun skipping, safe GET throttling/retries, no ambiguous POST replay, in-flight receipt preservation, before/after-intent cancellation, redirects, low disk and compact terminal/JSON output. A live HTTPX GET also validates the official form/session/UC mapping without a new applicant POST. This implementation task does not register the remaining production cohort.

The corrected pacing was then verified with two real authorized registrations (no throttle refusals, both tracking receipts verified). Previously saved 99 registrations were unchanged; allocation total became 62. The full regression suite passed 226 tests, followed by 37 final focused tests including the extra cooldown and receipt-preservation cases. See [recovery and live verification evidence](evidence/http-throttle-recovery.json).

## Preparation pipeline verification

The shared 242,620-byte catalog fingerprint is now computed once during common input preparation; every fresh submitted form is still checked separately. A read-only benchmark prepared all 18,717 input records in 4.005 seconds, with one catalog fingerprint and no network requests or applicant POSTs. This measures input preparation, not total command startup (normalization, file sealing and database import are additional steps).

A four-record authorized live pilot registered all four with valid tracking receipts, four parallel form GETs and zero throttle refusals. Dispatch gaps were 5.004, 5.007 and 5.011 seconds, compared with a 6.901-second mean in the preceding full run. Live POST responses completed before the next permitted slot, so live peak in-flight was one; overlapping POST responses were verified in delayed-response tests with nonzero shared pacing. The public `X-RateLimit-Limit: 5` / `Remaining: 4` headers were captured, but do not establish the quota's scope, time window or a burst allowance. All 240 previously registered rows and their receipt hashes remained unchanged.

[Pipeline verification, backup and performance evidence](evidence/http-pipeline-verification.json).
# Automatic portal catalog freshness

Allocation apply/normalize/sync (including a submission's input refresh), sports
allocation and private-pool normalization share the same catalog refresh. A
valid catalog older than 48 hours is refreshed with a verified HTTPS GET of the
official registration page before normalization. It sends no applicant POSTs
and needs no browser. Fresh catalogs stay local without a network request.
Corrupt hashes, future timestamps and changed form contracts still fail
validation; network/parse failures preserve the previous catalog file. Frozen
input/receipt checks and completed-applicant skipping remain in place.
