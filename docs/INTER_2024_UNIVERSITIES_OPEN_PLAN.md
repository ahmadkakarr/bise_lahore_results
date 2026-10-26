# Inter 2024 → Universities / Open Category: server workflow plan

Historical plan. The operator subsequently confirmed a provisional-DOB admin
exception. See [implemented workflow](INTER_2024_UNIVERSITIES_IMPLEMENTED.md)
for the current policy, frozen counts, source preservation and commands.

Date: 25 October 2026 (Asia/Karachi). Status: **plan only** for normalization,
allocation and registration. Collection has been stopped/disabled and a verified
records-only snapshot created on the same server. No normalization or applicant
POST was performed for this new workflow.

## Confirmed scope and actual data

- Existing dedicated Proxmox LXC 950; direct HTTPS registration, no Chromium.
- Category **Universities**, subcategory **Open Category**, for both groups.
- Administrative targets: Lahore division/district; half Raiwind, half Model
  Town, using the previously supplied UC sets and fresh portal labels/IDs.
- Each applicant uses their **own fetched original portrait**, never the old
  shared logo/photo. Original board institution/district and exam identity remain
  in provenance; category assignment does not rewrite source education/history.
- Saved result rows: **29,128**. Review: **264**. Pending/unfetched: **165,459**.
  Only saved HSSC Part-II Annual 2024 rows enter this normalization cohort.
- Exact DOB present: **0**; separately labelled cohort-age assumptions: **29,128**.
- Valid distinct B-Forms: **29,083**. Invalid/missing B-Form rows: **1**.
  **25 duplicate identity groups** contain **44 extra rows**; do not silently
  create multiple applications or pick conflicting identity/photo data.
- Original portraits: **29,128**, all currently within 256 KiB.
- A raw-row split would be **14,564 / 14,564**. That is not the final application
  count: resolve duplicate groups, exclude prior registrations and apply actual
  DOB/field validation before pinning the ready cohort. A 29,083-key candidate
  set would split 14,541 / 14,542 before those remaining checks.

## Completed stop and preservation

`bise-inter-2024.service` is inactive and disabled; committed rows were preserved
and interrupted running claims recovered as pending. Source remains:

`/var/lib/bise-results/jobs/inter-2024-part2-annual/results.sqlite3`

It is **541,642,752 bytes** (516.6 MiB), not an assumed 4–12 MiB. Student photos
are BLOBs in this same DB; they are not separate image files to copy blindly.

Records-only snapshot:

`/var/lib/bise-results/workflows/inter-2024-universities-open-20261025/snapshots/Records Only.sqlite3`

- Size: **232,615,936 bytes** (221.8 MiB).
- Source SHA-256: `531e86e4a306116f80fff59f6b93d29101ed38dcd6e080e91ddb730f9623af92`.
- Snapshot SHA-256: `831b37cbe131c273575d4d7b219293c1775d091fb14acb81983766421a42edc9`.
- Original photo payload referenced: 284,762,509 bytes; photo payload copied: **0**.
- Copy contains record/queue/history tables plus `photo_references`, retaining
  original job ID, image hash, MIME, dimensions, size and original DB location.
- Source opened read-only under its shared advisory lock; source hash unchanged;
  destination SQLite integrity/foreign keys verified. Three snapshot regressions
  cover identity preservation, zero image copying, reruns, running-claim refusal
  and changed-source protection.

This is a frozen **normalization source**, not a complete, standalone collector
restore backup: it deliberately depends on original photo BLOBs. Keep the
original DB. The attached `.manifest.json` states that dependency explicitly.
Do not run the collector against the records-only DB or mutate the snapshot.

## Live portal findings and the DOB blocker

A verified HTTPS GET of the official form (zero applicant POSTs) confirmed:

- Category label `Universities` (ID 4), `Open Category` allowed (ID 12).
- Current DOB bounds: **2000-01-01 through 2009-12-31**, inclusive, with cutoff
  2026-12-31. Revalidate bounds and IDs before actual submission.
- `showUcByCategoryName.Universities = false`. Keep UC allocations in our
  administrative ledger; do not force the schools' mandatory UC input into
  Universities requests. Send an empty/omitted UC only as required by the
  freshly verified native form contract. Division/district/tehsil still use the
  official options.
- The six previously used sports are available under Universities. Reuse the
  approved ratios as editable planning defaults, with boys-only restrictions.

The approximate assumption “18 in 2024” implies approximately 20 in 2026; it
does not establish an individual birthday. No January 1 or other invented DOB
will be substituted. These rows belong in **Age Unknown / Needs DOB**, not
Age Ineligible. With the existing data, the exact-DOB gate yields **0 ready
applications**. A verified individual DOB keyed by B-Form is needed before a
live pilot or batch; actual eligibility cannot be counted from the estimate.

## Architecture: same container, distinct ownership of state

Reusing LXC 950 is reasonable while fetching is stopped: four HTTP workers need
far fewer resources than eight browsers. Use a separate registration systemd
unit, immutable source release and writable workflow directory; do not reuse
the collector service/queue DB for registration writes. Initially cap the new
unit around 1 vCPU / 1 GiB and measure rather than granting the whole host.

```text
workflows/inter-2024-universities-open-20261025/
  snapshots/Records Only.sqlite3       # read-only source
  config/batch.json                   # global settings + hashes
  config/raiwind.json
  config/model-town.json
  state/normalization.sqlite3         # transform cache/provenance
  state/allocations.sqlite3           # pinned identity → tehsil/UC/sport
  state/registration.sqlite3          # one campaign-wide queue/receipts ledger
  reports/Summary.json
  reports/Needs DOB.csv
  reports/Identity Conflicts.csv
  reports/Already Registered.csv
  reports/Raiwind/Ready to Register.csv
  reports/Model Town/Ready to Register.csv
  reports/Combined Ready to Register.csv
  receipts/                           # compact response evidence, private
  runtime/status.json                 # atomic counters/heartbeat
```

SQLite is authoritative. CSVs are optional interchange/review outputs, no Excel
master or copied portrait folders. Snapshot/normalization/registration each
have their own hashes, manifests and lifecycle. Original source is read-only;
hold its existing shared lock while resolving original BLOBs so a collector
writer cannot race it. Never place the SQLite files on a shared network drive.

## Preparation and allocation

1. Freeze the source/cohort hash and read only `(2024,HSSC,Part-II,Annual)` saved
   records. Audit required identifiers, exact DOB, photo ownership and prior
   registration state before building a ready queue.
2. Normalize B-Forms to 13 digits; group duplicate identities. Collapse a group
   only when source identity agrees, recording all roll aliases; conflicting
   names/father IDs/DOBs/photos remain reviewable. Invalid identity rows remain
   in reports, never silently disappear.
3. Import the existing campaign's **confirmed and reserved/uncertain** ledger
   from the laptop through a coherent export, with portable receipt hashes and
   tracking provenance. Do not start a clean empty ledger and re-register old
   applicants. Stop local registration writers for the handoff; initially make
   the server the single campaign writer. Parallel independent laptop/server
   SQLite ledgers would not provide a shared duplicate guard.
4. Assign non-conflicting, unregistered unique candidates 50:50 by a stable
   ordering and pinned seed, stratified by approved gender/sport if needed.
   Age-unknown candidates can receive a draft administrative allocation; they
   still cannot enter the live ready queue. Group counts
   differ by at most one. Store each assignment once; reruns and new reports
   cannot reshuffle a registered person. Known allocated/registered historical
   entries retain their existing assignment/payload. Later DOB/eligibility
   checks filter the ready subset without reshuffling frozen candidates;
   ready/success totals can therefore differ between the targets.
5. Within each target, distribute its ready group as evenly as possible across
   that target's approved UC list. Raiwind uses 255–274. Model Town uses the
   exact previously supplied 52-number set, including 84–88, 198–241, 255–257.
   Resolve fresh official labels/IDs without fuzzy matching. Some UC numbers
   occur in both operator-supplied sets; persist `(allocation batch, tehsil, UC)`
   so they are not accidentally merged, and retain administrative provenance.
6. Keep source institution/district unchanged. Both destination profiles use
   Universities/Open Category; an institution name is not manufactured. Existing
   phone/address/gender/size/sport defaults remain explicit editable policies
   with origins; individual verified fields override defaults. Estimated sizes
   may use a labelled age estimate, but it must never become an official DOB.

Approved sport-weight planning defaults:

| Sport | Female | Male |
| --- | ---: | ---: |
| Badminton | 50% | 10% |
| Athletics | 20% | 15% |
| Cricket (Hard Ball) | 10% | 40% |
| Football | 5% | 25% |
| Tug of War | 15% | 5% |
| Kabaddi (Boys Only) | 0% | 5% |

Keep the ratios editable in JSON, apply largest-remainder rounding, and never
infer an individual's preference from gender. Gender inferred from B-Form
parity remains labelled as derived/unverified; it is not an official board field.

## Original-photo delivery

Use `photo_references.job_id` to fetch the exact BLOB from the original DB through
a read-only per-worker connection. Verify the saved SHA, MIME, dimensions and
256 KiB limit immediately before building multipart data. Read bytes directly
into the HTTP upload; do not export 29,128 files or copy the original photo table
into working/registration DBs. Persist only the reference/hash in queue payloads.
Missing, mismatched or oversized images block that applicant; there is no shared
logo fallback. Keep original bytes unchanged, with any future resizing policy
explicitly versioned and traceable rather than overwriting originals.

## Four workers with durable alternating dispatch

Use **four total HTTP workers** for both groups and one durable dispatcher:

```text
POST start order: R1 → M1 → R2 → M2 → R3 → M3 → …
                 Raiwind  Model Town  Raiwind  Model Town
```

Workers prepare independent sessions/forms concurrently. They do not each run
an independent alternator. The dispatcher atomically claims the next eligible
identity and next turn, persists a sequence/lease, then commits before network
work; no SQLite transaction or global response lock spans an HTTP request.
Worker failures recover expired pre-dispatch leases. Restart preserves the next
group/sequence. Network responses may finish in a different order; fair dispatch
does not imply exactly equal success totals when one group has rejected rows.
If one group has no ready records, continue the other without a deadlock or
repeatedly polling blocked rows. Config can support “4 Raiwind then 4 Model Town”
later; default is the operator's per-next-form alternation.

Server networking uses its own connection. Laptop Wi-Fi/USB/interface/proxy
profiles are not copied. Start with four workers, two prepared forms, two
in-flight requests and **one shared 6-second dispatch interval** for the portal
connection, with accepted CSRF/session isolation. Four workers do not imply
fourfold successful submissions; measure actual portal latency/throttle data.

## Idempotency, recovery and evidence

- Global uniqueness: canonical portal + actual campaign + normalized B-Form.
  Profiles/tehsils cannot create a second registration for the same applicant.
- Durable stages: ready → leased/preparing → intent → dispatched → response
  sealed → confirmed. Store tracking ID, allocation, sport, photo hash, payload
  hash, response classification and receipt hash together.
- Confirmed rows are terminal and indexed; skip them in SQL before any GET,
  photo decode or POST. Cached normalization keys include frozen source/profile/
  catalog/correction hashes; unchanged inputs do no repeated transform work.
- A definitive portal duplicate becomes `already_registered_external`; keep its
  response, skip it and continue. Do not invent an unavailable tracking ID.
- Before-dispatch failures can requeue with fresh form/CSRF. After-dispatch
  timeouts are uncertain, never automatically “failed” or “registered”. Park
  that row; continue other rows. Only a verified duplicate-safe, bounded retry
  policy can retry an uncertain submission, retaining all earlier attempts.
- Explicit throttle messages/429 pause the shared connection, honor Retry-After
  and persist backoff (e.g. 30/60/120 seconds, capped). Resume gradually; never
  stop the entire batch because one record is invalid or a short throttle occurs.
- On restart, reconcile sealed receipts locally before any new POST. A crash
  after portal acceptance must not erase an acknowledgement or repeat a confirmed
  application. Leases cannot silently reset dispatched/uncertain rows to ready.
- Storage guard, bounded logs/evidence, heartbeat and systemd restart remain
  independent of the laptop. On low disk, finish safe commits and pause admission.
- Monitor shows both group totals, submitted/saved/duplicate/uncertain/blocked,
  next target, per-worker activity, rate, cooldown and last success. Ctrl+C closes
  the monitor; explicit remote stop pauses the job and preserves all state.

## Required code changes; old commands are not yet suitable

Inspection found real compatibility gaps, so this cannot be done by changing
two values in the old 9th profiles:

1. `bise_normalize/sources.py` only accepts SSC scopes: add explicit HSSC Part-II
   support without loosening existing SSC validation.
2. `bise_allocate/app.py` currently restricts allocation to SSC Part-I and explicit
   Private subcategory: generalize the exam/category contract with new profiles.
3. `youthgames_register/config.py` only accepts shared-graphic photos: add original
   SQLite-reference mode and per-applicant byte/hash verification, keeping legacy
   mode functional.
4. HTTP fields currently assume school-style UC/options and profile checks named
   `observed_secondary_dob_*`: make field requirements/category bounds derive
   from the selected catalog category while preserving old profile compatibility.
5. Add the campaign-wide alternating dispatcher, lease/recovery persistence and
   server registration service/control commands; reuse the existing HTTP client,
   classifiers, pacing, ledger protections and dashboard components.

Create fresh Inter 2024 profiles/releases. Do not alter old SSC 9th Raiwind/Model
Town profiles, completed applications, accepted receipts or collection source DB.

## Implementation and acceptance sequence

1. Finish/read-only audit and decide verified DOB enrichment. Current output can
   be a normalized **draft** with Age Unknown reports, not a ready submission list.
2. Implement HSSC/category/individual-photo support and schema migrations locally.
3. Add regression cases for legacy SSC, Universities bounds/missing DOB, hidden
   UC handling, incorrect photo ownership, hash/size errors, duplicates, terminal
   ledger skip, equal stable allocation and 4-worker fair dispatch under races.
4. Test restart at each intent/receipt stage, old ledger handoff, a transient
   outage/throttle, one empty target queue, low disk and monitor disconnect.
5. Generate reproducible drafts/ready subsets on the working state DB, verify
   the original source hash still matches and zero photo bytes were duplicated.
6. Deploy only the required source/pinned dependencies/new profiles to a separate
   immutable release in LXC 950. No laptop venv, old outputs, Wi-Fi routes or
   unrelated credentials are copied. Install a separate constrained systemd unit.
7. Once actual DOBs make genuine ready rows available and live execution is
   authorized, test one/then a few registrations from each target with the exact
   original photo, confirmation/tracking evidence and alternating start order.
8. Enable unattended four-worker execution only after that pilot, retaining
   checkpointed next turn and one authoritative campaign ledger.

Proposed controller commands after implementation are prepare/preview/start/
watch/status/stop under a registration-specific remote entrypoint. They are not
implemented or runnable yet. Existing `bise_remote watch` still monitors the now
stopped **fetcher**, not a registration job. Rollback stops the new registration
unit and reselects the prior code release, preserving DBs, receipts and original
photo source; never restore an old ledger over newly confirmed applications.

Local audit artifacts: `artifacts/inter-2024-registration-plan/` contains the
stopped status, live catalog GET, snapshot manifest and aggregate cohort audit.
