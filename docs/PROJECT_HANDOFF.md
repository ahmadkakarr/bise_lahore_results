# BISE Lahore results: continuation notes

Read this file, README.md, the exported database schemas and the readable local
chat before proposing changes. This handoff describes existing software and
operator decisions; it does not authorise new external submissions.

## Application map

- `bise_results`: Playwright 9th/10th result collection, parsing, durable SQLite,
  resume/deduplication and optional reports. Default collection is DB-only.
- `bise_normalize`: JSON profile-driven mapping, portal eligibility and reports.
- `bise_allocate`: frozen cohorts, UC/sport allocation, private pools, profile
  overrides, pinned-input resumption and the direct HTTP registration CLI.
- `youthgames_register`: browser/HTTP submission, receipt evidence, shared
  registration ledger, bound independent network routes, paced retries and UI.
- `youthgames_shared`: common name rules. `run_alarm`: supervised run-end alarm.
- `scripts/run_workload.py`: independent systemd scopes for collection/submission.
- `tests/fixtures`: synthetic portal/results fixtures; regressions do not need
  real registrations. Requirements and requirements.lock.txt define dependencies.

## Authoritative data

`data/results.sqlite3` contains imported rolls, exam-scoped results and collection
history. `data/normalization.sqlite3` contains normalization identities,
evaluations and runs. `data/allocations.sqlite3` contains frozen allocation
cohorts/targets/sports and audited overrides. `data/youthgames.sqlite3` contains
registration jobs, attempts, immutable registered identities, tracking IDs,
retry affinity and pacing. Database summaries in the bundle are from its own
snapshots, not the historical counts in conversation messages.

Confirmed registrations are terminal: never submit them again. SQLite locks,
receipt hashes and the shared portal/campaign/BForm identity guard are essential.
Unknown POST outcomes are not confirmed registrations. Explicit bounded retries
preserve the original attempt and connection affinity. Never fabricate tracking
IDs, silently clear the ledger, or delete uncertain evidence to unblock work.

## Operator decisions and current configuration

- 2026 Part-I Annual is 9th; Part-II Annual is 10th. Same roll number in different
  exams must remain independent. Preserve original Institution/District labels.
- Default output is SQLite; do not reintroduce automatic Excel/card exports.
- Location allocations are operator-authorised administrative allocations;
  they are not inferred residential addresses. Preserve provenance.
- District-only Lahore Raiwind and named private-school cohorts are separate.
  Private pool split originally fixed 10,000 Raiwind and 14,737 Model Town rows.
- Model Town's remaining cohort was changed from Private to Open Category through
  the audited `participation_changes` policy. Its filename still says private
  because it identifies the original source pool. Completed historical Private
  registrations retain their original submitted payload/receipts. Consult the
  current override and profiles, not the filename alone.
- Sport assignments are frozen, deterministic and gender-specific according to
  the approved profiles; already submitted applicants must not be reassigned.
- Missing phone/address and estimated sizes were operator-confirmed earlier;
  preserve their derivation/provenance instead of treating them as board fields.

## Network / reliability

Wi-Fi and USB are independent explicit routes; ordinary laptop routing/DNS
remain on Wi-Fi. The exact observed phone has a stable udev interface alias and
an isolated NetworkManager policy-routing profile. These OS settings are local
machine configuration, not portable state inferred from source. Inspect
`docs/USB_ROUTING_FIX.md` before changing anything. Never change global routes
or rotate egress to get around an existing route's cooldown. Honor server waits.

Portal transport errors, local link availability and IP-echo failure are separate
health signals. The route-health evidence tests cover false offline transitions,
genuine unplug/replug, source changes, echo outages and peer independence.
Ctrl+C and successful completion are quiet alarm exits. Error exits can alarm.

## Latest open issue: automation-platform messages pending

The related automation project is outside this checkout:
`/home/ahmad/code/deomeeautomations/automation-platform`, with sibling
`/home/ahmad/code/deomeeautomations/whatsappbot`. On 23 October, its available
runtime records showed WhatsApp SERVER_ACK pending messages, a later session
conflict at 11:18:40 Asia/Karachi, and a terminal Ctrl+C stop at 11:43:14.
A connection-only check subsequently got server 401. That does not establish
BISE as the cause of the earlier pending acknowledgements.

The user later clarified that the observed symptom during BISE collection is
**messages remain pending**, rather than the whole dashboard crashing. That
specific timing/ACK interaction remains unproven and needs follow-up after valid
WhatsApp linking; do not present the later 401 as proof explaining every earlier
pending message. Do not replay uncertain message deliveries as a diagnostic.

Coexistence protections added: bounded OCR, available-RAM admission, separate
systemd workload scopes; favoured automation service resource settings; a kernel
auth-directory lease in the WhatsApp worker and a no-business-message handshake
diagnostic. The related changed source files can be captured in the handoff's
`related/` section, but it is not a full export of those external repositories.
The source machine's current service/login state must be checked again.

## Working / validation expectations

Use Roman Urdu with the operator. Trace current evidence before claiming root
cause. Keep commands copy-pasteable and repeatable; preserve indexed skipping,
registration identity/receipt history and unrelated work. Run focused offline
regressions for changes. Do not run live submissions or send test messages as
part of reading this bundle. Database paths in stored receipts may be absolute;
the export records their original-to-bundle map. Do not rewrite or resume those
identities on another computer without reviewing path integrity and ownership.

The local exporter never uploads. Code/context and full private database/proof
parts are separate so the operator can choose what to provide to another tool.
