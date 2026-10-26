# Parallel headless browser comparison

For unattended production runs with automatic throttle/temporary-GET recovery, use [direct HTTP automatic recovery](HTTP_CONCURRENT.md). Browser mode below remains the diagnostic comparison path and still stops on an explicit server throttle.

This uses real Playwright Chromium, independent worker contexts/tabs, and the shared registration SQLite ledger. It is separate from `bise_allocate http-submit`. Workers can fill and submit concurrently; there is no global five-second HTTP dispatch gate. The existing profile interval applies independently between successive submissions by each worker. First submissions do not wait for other workers. Browser contexts/tabs are reused within each worker.

Run one bounded wave of 18 total applicants:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate sync --config allocation_profiles/lahore_9th_raiwind.json && .venv/bin/python -m youthgames_register submit --config registration_profiles/lahore_9th_raiwind.json --headless --workers 18 --limit 18 --captcha-mode auto --diagnostics
```

For a 20- or 36-worker bounded wave, change both `--workers 18` and `--limit 18` to the chosen count. Browser worker support is 1–36. `--limit` is a single total shared across workers. `--limit 0` processes all remaining Ready rows; inspect the bounded-wave diagnostics before running a long batch. Retrying a command continues with remaining Ready identities; previously registered or uncertain identities are not submitted again. Any uncertain job in the same portal/campaign blocks submission until reconciled.

Diagnostics are enabled by default for `submit`. `--diagnostics` / `--debug` enable the dashboard and extra network logs; `--no-diagnostics` / `--no-debug` disable them. Essential SQLite state, tracking IDs and response receipts are always retained.

To recover proven temporary refusals locally, refresh input, and process every remaining Ready applicant with an editable worker count:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && GAMES_WORKERS=3 && .venv/bin/python -m youthgames_register recover --config registration_profiles/lahore_9th_raiwind.json && .venv/bin/python -m bise_allocate sync --config allocation_profiles/lahore_9th_raiwind.json && .venv/bin/python -m youthgames_register submit --config registration_profiles/lahore_9th_raiwind.json --headless --workers "$GAMES_WORKERS" --limit 0 --captcha-mode auto --diagnostics
```

Set `GAMES_WORKERS` to any integer from 1 through 36. Rerun the same command to resume Ready records. Confirmed identities are excluded by the append-only database ledger, regardless of source/profile changes. See [registration identity protection](REGISTRATION_LEDGER.md).

No assumption is made that browsers avoid the portal's rate limits. A captured explicit server throttle stops new claims while already dispatched requests finish and save evidence. An ambiguous response/timeout is retained as uncertain, never automatically replayed. Existing explicit CAPTCHA-only retries remain bounded. Actual first-submission times vary with browser form readiness; workers are not forced into a synchronized burst.

The sustained 18-worker run on 2026-10-21 saved 38 new registrations, recognized one existing registration, and then received two explicit HTTP-200 throttle refusals asking for 1/2 seconds. The form submission limiter applies to browser traffic too; its scope is not established by this run. Three workers is a conservative starting configuration, not a measured guarantee against throttling. Increasing workers can increase refusals rather than saved throughput.

`recover` makes zero network requests. It restores only hash-verified explicit server refusals and the exact shared-stop guard case proven to have no dispatched POST. Original receipts and attempt outcomes are retained with derived recovery receipts and `submission_recoveries` audit rows. A legacy race could mark a guard-blocked request as `blocked_input`; it is recoverable only when the receipt, guard error and undispatched attempt all agree. Generic errors, unknown requests and confirmed registrations are never reset. Submit also performs local recovery for its prepared input and waits for any saved cooldown before submitting. Run recovery **before sync** so normalization includes those recovered rows in its next Ready input. No automatic background restart or adaptive concurrency is enabled; an explicit throttle still stops the browser run.

Existing external registrations are displayed as completed skips, not failures. Locally confirmed registrations retain their immutable tracking receipts. The 38 new receipts and one external receipt from the sustained run were verified during local recovery; see [recovery audit](evidence/browser-throttle-recovery-20261021T185634Z.json).

## Evidence

The Rich dashboard identifies the transport as Headless Chromium, shows worker/roll/UC activity, recent confirmed registration speed, per-UC results and peak in-flight counts. At the end it reports actual dispatched POSTs, document HTTP 429 responses, form-level throttle refusals and the stop reason.

Each run writes:

```text
registration_output/Lahore 9th Raiwind/Runs/Browser-<UTC timestamp>/
  events.jsonl
  Summary.json
```

Each dispatched attempt retains its existing `Receipts/job-.../attempt-.../` folder with response HTML, filled/result screenshots and `receipt.json`, plus `browser-network.json`. The network evidence is included in the receipt's hash list. It records main-document GET/POST paths, statuses, time to response and whitelisted rate-limit/Retry-After headers. Cookies, CSRF tokens, query strings and POST bodies are excluded from the network logs; private screenshots/HTML remain local evidence. Preflight catalog responses are also recorded in the run log, even if preparing the browser fails before the first applicant starts.

HTTP 429 and a server validation banner saying “Too many registration attempts” are counted separately: the latter can arrive as HTTP 200. Server counter scope/time window is not inferred from the header's numeric limit.

Ctrl-C once stops new work and finishes in-flight receipts. A second Ctrl-C cancels remaining work; dispatched unknown outcomes are preserved for reconciliation. No Excel reports are enabled by this mode.

## Operator-run regressions

The code and tests are provided without launching live browsers, making applicant POSTs, or running the long suite during this change.

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m unittest tests.test_browser_recovery tests.test_registration_ledger tests.test_browser_diagnostics tests.test_youthgames_workers tests.test_youthgames tests.test_http_concurrent -v
```

These include intercepted 20-worker Chromium submissions, saved network/receipt hashes, server throttle queue stopping, worker override bounds, cookie/token exclusion from diagnostics, CLI selection and cancellation/idempotency coverage. The browser tests use synthetic applicants and intercepted responses.
