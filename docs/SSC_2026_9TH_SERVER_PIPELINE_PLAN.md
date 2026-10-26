# SSC 2026 Part-I server pipeline: external photos and resumable registration

Status: proposed design, 26 October 2026 (Asia/Karachi). This document does not
create containers, migrate state, start collection or submit registration forms.
The operator confirmed that original photos must be external files; databases
store verified references rather than image BLOBs for this new workflow.

## Current evidence and migration boundary

The local SSC 2026 Part-I queue contains 287,861 jobs: 127,176 verified,
160,371 pending, 313 needs_review and one failed. Its database is approximately
752 MiB. The checked database has no photo table and none of the saved Part-I
rows has student_photo.sha256 metadata. Existing results therefore cannot be
described as already having recoverable stored portraits. Inspect any surviving
local capture/photo files before scheduling recovery; do not invent a portrait
or silently use a generic picture.

The current remote controller and registration photo reader have Inter Part-II
2024 assumptions: fixed deployment state/paths, HSSC/year/part checks and a frozen
source-database hash with immutable SQLite reads. These must become explicit
versioned source/photo providers. Copying the Inter profile and changing a year
is insufficient. Existing Inter BLOB references remain supported and unchanged.

## Deployment and responsibility boundaries

Create one dedicated, unprivileged container for the new 9th workflow, with a
proposed starting budget of four vCPU, 6 GiB RAM, 64 GiB disk, eight headless
browser workers and two OCR workers. Resolve actual VMID/address/storage through
live inventory and ownership reservations; do not hardcode a guessed free ID.
Collector and normalizer receive separate resource limits. Start with a bounded
pilot and measure memory/disk before increasing worker count.

The new container owns collection, original photos, photo recovery, incremental
normalization and its registration-ready outbox. The existing registration
service remains the sole registration execution/confirmation authority. An
authenticated private ingest/photo interface connects them. This enables
collection and registration simultaneously without a second independent
writable confirmation ledger. It also avoids moving the current live Inter
registration job merely to introduce this new source.

```text
New 9th container
  headless collector / old-photo recovery
       | result commit + photo metadata + durable change event
       v
  results.sqlite3        photos/originals/<hash shards>/<sha256>.jpg
       | coherent reads             ^
       v                            | authenticated, hash-bound read
  incremental normalizer            |
       | ready outbox               |
       +---- private authenticated ingest ----> Existing registration service
                                                     |
                                    one authoritative registration ledger
                                                     |
                                        existing HTTP/browser submission engine
                                                     |
                                      portal receipt, tracking ID, confirmation
```

The central dispatcher can continue existing Inter jobs while admitting ready
9th applicants under an explicit scheduling policy. It retains profile/campaign
fairness and existing global pacing/cooldowns. New containers do not create new
portal quota. If registration execution is later moved, hand over a verified
ledger after draining its previous writer; never run two independently seeded
copies as registration authorities. SQLite is not shared over NFS/SMB.

Before enabling new registration intake, reconcile verified confirmations from
the existing local/server ledgers into that authority with original receipt and
tracking lineage, rather than replacing its database. Local/remote run commands
must honor the recorded campaign writer ownership and refuse an accidental
second independent runner. A disconnected copy cannot supply fleet-wide duplicate
protection; the operational guarantee requires every active runner to use the
same authority and canonical campaign identity.

## Persistent layout

```text
/var/lib/bise-results/workflows/ssc-2026-part1/
  config/                  versioned collector/normalization/destination policy
  input/rolls.csv          immutable audited source list
  state/results.sqlite3    jobs, parsed source records, photo references, events
  state/workflow.sqlite3   normalization revisions, readiness, outbox, cursors
  photos/originals/ab/cd/<sha256>.jpg
  photos/upload/ab/cd/<derivative-sha256>.jpg
  runtime/                 heartbeat, compact logs and phase counters
  evidence/                bounded unresolved captures, never the photo store
  snapshots/               verified database snapshots and asset manifests
```

Use a configured media-root alias plus a relative path, rather than absolute
laptop paths in application records. Moving/restoring the workflow then needs
one root mapping change instead of rewriting every applicant. A human-readable
roll/photo index is generated from metadata; duplicate files or filesystem links
are not required. Photos, student data and runtime secrets remain outside Git.

## Photo storage and commit protocol

Each photo reference records full source identity (board/year/course/exam/roll),
canonical student identity, asset ID, SHA-256, relative path, MIME type, byte size,
dimensions, source URL/time and storage schema version. An original is immutable
and stored once by hash. A changed portrait creates a new asset/version; it does
not overwrite evidence used by an earlier registration attempt.

Fetch only the real candidate-image element in its board session, validate the
source endpoint and identity, and reject board logos, HTML/error responses,
oversized/invalid images and source/student mismatches. First prove that the
9th layout exposes a real candidate portrait in a live pilot; HSSC support alone
does not establish SSC availability.

Write bytes to a temporary file on the destination filesystem, verify content,
fsync, atomically rename to the final hash path and fsync the directory. Then
commit its database reference/readiness/change event. Only committed verified
references may become upload-ready. If a crash leaves an unreferenced file, an
auditor can safely relink or later collect it after a grace period; a pending
file is never presented as ready. Photo failures have their own retry state and
do not invalidate an otherwise correctly saved result.

Keep original bytes intact. If portal requirements need a smaller image, create
a separately hashed upload derivative and link it to the original. Validate the
current portal's accepted format/size before marking that asset upload-ready.
Registration reads the referenced bytes automatically; HTTP uses multipart bytes
and the browser adapter uses its supported file buffer or temporary upload path.
The operator never manually copies portraits into registration folders.

## Preserve existing work and recover missing portraits

1. Inventory the source database, saved records, input list, usable surviving
   photos/captures and existing normalization/allocation/confirmation state.
2. Take a consistent SQLite backup (including committed WAL data), record hashes
   and integrity checks, and retain the original local databases unchanged.
3. Seed a dedicated exam-scoped destination while preserving logical identities,
   results, queue/attempt history and intentional-retention evidence. Keep a job
   ID migration map where local numeric IDs differ. Translate external resource
   references explicitly rather than trusting old absolute paths.
4. Verify source/destination counts and record fingerprints, resume eligibility,
   source exam isolation and confirmation-ledger reconciliation before starting.
5. Existing verified results stay verified. Discover and verify any surviving
   portraits first; add only genuinely missing/unusable photos to a separate
   recovery queue. Board sessions may require reopening the corresponding result
   page to obtain the image, but that is a portrait recovery attempt, not a second
   imported result or a new sports registration.
6. New pending roll lookups collect result and original portrait together when
   available. Use a bounded scheduler so new-result progress and old-photo
   recovery both advance under one board-request budget. The pilot will determine
   a configurable starting split; do not promise an arbitrary backfill speed.

Photographs missing from the actual source receive an explicit photo_missing
state. A record can be saved and normalized while remaining blocked from
registration until its required photo is verified. Missing portraits do not
authorize a generic substitute.

## Live collection-to-registration flow

Maintain a transactionally appended change sequence for result/photo changes;
do not watch only increasing job IDs, a whole-database hash or filesystem mtime.
An old row gaining a recovered photo must be revisited even if its job ID is far
behind the collection cursor. Each stage commits its output and consumed cursor
together. Cross-service delivery is at least once, with idempotent acceptance;
advance a delivered outbox entry only after the receiver's durable acknowledgment.

Normalizer uses coherent short-lived read-only WAL snapshots and writes its own
workflow state. It must not use immutable=1 against the live collector database.
Use source lineage plus per-record content/photo hashes instead of requiring
the entire live database to keep one frozen hash. Process new/changed rows in
bounded microbatches, initially around 250 rows or a configurable short interval.
Do not rebuild all CSV/Excel reports whenever another record arrives.

Readiness requires verified source identity, valid actual DOB/age eligibility,
required configuration/fields, verified applicant/photo binding and absence from
the central confirmation index. Keep independent reasons such as waiting_photo,
age_ineligible, blocked_input, ready and already_registered. Reuse existing
validated class-9 settings, allocation and sport assignments where applicable;
missing district/UC/institute/sport policy is not guessed. Preserve the original
institution text, actual DOB and administrative-allocation provenance.

Normalize-only and continuous pipeline modes must both exist. Registration is
started explicitly through the operator workflow, independently of collection.
Once enabled, it waits when no rows are ready and picks up newly ready rows
without a process restart or manual import/export. Disable one stage without
losing another stage's durable progress. Existing CSV exports remain optional
compatibility/report artifacts, not the authoritative live queue.

## One registration identity and durable attempt evidence

Canonical confirmation identity remains (portal, actual competition campaign,
canonical B-Form). Exam, roll number, source container, profile name or a changed
normalized/photo payload must not create another registration for that person
in the same campaign. Campaign identifiers describe the actual campaign, not a
new label used to bypass confirmations.

The central ledger rejects confirmed identities before form preparation and
again when atomically claiming an attempt. Snapshot the exact normalized data,
photo hash, assignment/version and source lineage before dispatch; later source
changes cannot silently change an in-flight or confirmed submission. Terminal
confirmations retain tracking ID, receipt, timestamps and payload fingerprint.
Corrections discovered after confirmation become an auditable correction report,
not automatic resubmission or a reset to Ready.

Bound retries for explicit transient failures, persist server cooldowns and retain
the existing verified duplicate-B-Form handling. After a dispatched request loses
its response, preserve uncertain state/evidence. Apply the existing explicitly
configured uncertain-retry/reconciliation policy; never infer non-submission
from a restart or claim exactly-once portal effects without portal support.

## Supervision, monitoring and storage

Separate restartable systemd services/processes for collection, normalization and
central registration, with one authority per queue, bounded worker claims,
timeouts and recovery after stale leases. Ctrl-C on the laptop monitor affects
only monitoring. Reboot restores enabled services and durable cursors/queues;
photo/result commits survive interruption. Intentional operator stop remains a
stop, not an excuse for an endless restart loop.

Extend the existing remote CLI/profile model instead of adding a second console
tool. Planned operators select the class-9 profile and choose collection,
normalization, registration or the combined status view. Existing Inter commands
remain compatible. Add actual implemented command examples only after the CLI
supports them; this plan does not claim they exist now.

Monitor separate counters: verified results/new results, photos ready/missing/
recovery, normalized, age-ineligible, registration-ready, registered/tracking,
external duplicates, uncertain, cooldown/offline, queue backlog and free space.
Avoid a single misleading processed total across different stages. Incremental
events and bounded logs supply the dashboard without rescanning whole photo trees
or serializing every historical applicant on each refresh.

Size photos from measured pilot averages, including derivative storage and future
growth; monitor available space, inodes and the host storage pool. Proposed
pause/resume reserves start at 6/8 GiB for this photo-retaining workflow and may
increase after measurement. Pause new downloads before storage exhaustion while
allowing safe processing of existing ready data where possible. Originals and
referenced upload assets are never removed by result-capture cleanup. Cleanup
requires committed result/photo integrity and retains unresolved evidence within
a separate quota. Media garbage collection is reference-aware and separately
audited; registration receipts/backups can retain old media references.

Database-only downloads stay small. A full restore/export consists of coherent
database snapshots plus an asset manifest and only missing hash-addressed photos.
Verify the combination before marking a backup complete; the backup must include
every asset referenced by its frozen database snapshot. Keep an off-host backup
and document restore/root remapping. A new database alone is not a photo backup.

## Implementation sequence and acceptance gates

1. Generalize remote profiles/state/monitoring while preserving Inter behavior;
   implement the external-photo provider and versioned references.
2. Implement audited snapshot/import and existing-photo discovery; preserve
   saved-result identity and the canonical confirmation authority.
3. Create the reserved owned class-9 container, immutable release, persistent
   directories, storage guardian and bounded services.
4. Run a bounded live portrait pilot: verify actual 9th photo availability,
   source/student binding, disk commit, hash, no BLOB use and idempotent resume.
5. Implement the incremental change/outbox pipeline and private source/photo
   provider for the existing registration service. Test using local mock portal
   responses before any separately authorized live registration pilot.
6. Test interruption at file-write/rename/DB-commit/ready-ack/POST-response
   boundaries; missing/corrupt photos, duplicate/changed inputs, backfill events,
   source/worker restarts, concurrent readers/writers, stale claims, backup/restore,
   low disk/inodes and unchanged existing Inter operation.
7. Resume full result collection and portrait recovery after migration/pilot
   checks. Enable live registration only with its selected verified category,
   locations/sport policy and separately explicit operator start.

Success means previously saved results remain saved, portraits are original
external files with correct identity/hash, every stage resumes durably, new ready
rows become available without manual copying, and confirmed applicants cannot
re-enter the same campaign through a different source/profile/container.
