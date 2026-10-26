# Inter Part-II 2024: server collection plan

Status updated 24 October 2026: collection-only age-estimate/photo pilot is authorized.
Three authorized live HSSC result cards (500001–500003) were inspected. Each has
name/father name and identifiers, but no DOB element/text. No detail link was
present. Exact B-Form lookup in the existing verified local results database
found no DOB match for any of these three students. The operator has no alternate
DOB file and explicitly requires DOB. This does not establish that every possible
HSSC layout omits DOB, but the inspected source is insufficient for bulk use.

The operator has now explicitly supplied a cohort assumption: approximately 18
years old in exam year 2024. Store that assumption in `age_estimate`, with
`source=operator_cohort_assumption`, approximate-year precision and
`individual_verified=false`. In 2026 this means approximately 20, not a verified
birthday or individual age. `date_of_birth` stays empty; do not invent a day,
month or official date. Missing DOB still blocks games registration eligibility.

The default collector policy remains `require`. The explicit HSSC-only
`--missing-dob-policy estimate --estimated-age-at-exam 18` allows collection
verification (matching source identity/exam), independent of sports readiness.
The remote profile uses this collection-only policy. A bounded dedicated LXC
pilot is implemented by `python -m bise_remote.pilot` (setup/start/status/download).
The full controller is now implemented by `python -m bise_remote` with
`start`, `status`, `watch`, `logs`, `stop`, and `download`. Older proposed
preflight/deploy command names further below remain historical design notes;
use the operational README commands. Full collection uses a persistent systemd
service and `/var/lib/bise-results/jobs/inter-2024-part2-annual/` data root.
The existing pilot's four records/photos seed the full DB once via online backup.
A once-per-minute host storage guardian runs independently of the laptop.
Monitoring never claims the collection lock or stops the service.

`--save-student-photos` fetches the original `imgDisplay` image from the board's
same-session `ImageLoader.aspx`, validates image format/dimensions/size and hashes
the bytes. Photo BLOB + metadata and student identity commit in one transaction.
Only then may verified raw screenshots/HTML be discarded. The photo is not a
board logo, signature or cropped page screenshot. Missing/invalid photo leaves
the capture for review when photo collection is requested.

`student_photos.job_id` references the exam-scoped result job; original bytes,
SHA-256, MIME, dimensions and source URL are durable in the same SQLite snapshot.
`bise_results photos --database PATH --output PATH` materializes originals as
`2024/HSSC/Part-2/<roll>.jpg` (or png), plus `Photo Mapping.json` containing the
exact student B-Form, roll/exam key and checksum. Identical exports skip image
writes. This prepares future photo lookup; it does not submit registrations or
silently replace the currently configured shared games photo. The upload-size
check records whether the original fits the existing 256 KiB portal budget.

## Verified starting point

Input: `roll_number_slips/2024/12/BISE_Lahore_12th_2024_Roll_Numbers_Without_Cancelled.csv`.
Offline audit: 194,851 rows, all unique and valid, no duplicate/invalid rows.
SHA-256: `99d58eb6cd6c43b76dced0c3bb03187c81273f7281a83465683250f4e5a72d9d`.

The official search form offers Intermediate (`HSSC`), Part-II Annual (`2`),
and 2024. This establishes form selectors only: a real HSSC result's endpoint,
DOM and subject layout need a bounded pilot before bulk collection.

The CLI/import/parser now carry HSSC separately from SSC; class 12 selects HSSC
Part-II Annual. Existing SSC defaults and identity keys remain compatible. The
missing-DOB collection gate still applies to HSSC, independently of parser support.

Authenticated read-only Proxmox inspection at 2026-10-24 10:49:59 UTC found:

| Resource | Available / observed |
| --- | --- |
| Host root / template storage | about 66 GiB free |
| local-lvm container/VM pool | about 670 GiB free |
| Thin pool | 815.6 GiB total, data 17.84%, metadata 0.84% used |
| Host RAM | about 45 GiB available |
| CPU | 56 logical processors |

This is a point-in-time inventory, not a resource reservation. Raw counts are
in `docs/evidence/inter-2024-server-preflight.json`. Recheck at placement.

## Architecture and ownership

Keep the collector implementation, parser, tests and job profiles in this repo.
Add a service descriptor, profile, instance and deploy adapter in
`digitalstudio-platform/ops/proxmox-platform` using its existing service/instance
and managed-LXC conventions. Do not copy generated outputs, old databases,
venv, context archives, unrelated services or registration credentials.

Use a dedicated unprivileged LXC, initially 4 vCPU, 6 GiB RAM and a 32 GiB disk
on `local-lvm`. The current profile uses 8 headless browser workers and 2 OCR processes; then
measure before selecting 9/18 workers. Apply CPU/memory/IO limits to the service.
Validate Chromium's sandbox in the guest; do not solve compatibility by making
the container privileged. Provision a bounded /dev/shm. Use the existing pinned
dependency lock and a tested Python/Chromium/Tesseract runtime.

Select the VMID and private IP from the platform's catalog/reservations and
live resources immediately before deployment, with a collision lock and durable
reservation. Do not assume an apparently unused VMID is unreserved. No shared
host networking, default routes or existing service settings need changing.

Store persistent state separately from versioned app releases:

```text
/opt/bise-results/releases/<source-sha>/
/var/lib/bise-results/jobs/inter-2024-part2-annual/
    input/rolls.csv
    input/manifest.json
    results.sqlite3
    runtime/heartbeat.json
    evidence/                 # bounded unresolved captures only
    checkpoints/latest.json
    snapshots/                # bounded, coherent downloadable snapshots
```

Start the collector under systemd inside the guest, independent of the SSH
session/laptop. Enable boot recovery and retry transient browser/network errors
with backoff. Config/parser failures become explicit held states rather than
restart storms. Completion stays complete. A single service lock prevents
duplicate writers. The laptop can disconnect or sleep without stopping the job;
no remote monitor can prevent an underlying server/power outage.

No public web endpoint is required. Use authenticated SSH/private management
transport with host-key verification and a limited service operator. The
Proxmox credentials remain on the control plane, outside the collector guest.
The server uses its own internet connection; laptop Wi-Fi/USB routes are not
available to it. Validate portal reachability from the guest during preflight.

## HSSC correctness and compatibility

1. Introduce one explicit exam descriptor: board, year, course, part, session.
   `--class 12 --year 2024` maps to HSSC Part-II Annual. Keep legacy class 9/10
   defaults, batch IDs and saved SSC rows unchanged.
2. Carry the course into import, identity hashes, form selection, endpoint checks,
   parser validation, reports and retention. Retain the indexed unique key
   `(roll, year, course, exam_type)`; do not let equal SSC/HSSC roll numbers collide.
3. Inspect a small authorized HSSC pilot, retain temporary evidence and construct
   synthetic regression fixtures for its actual layouts. Check roll, HSSC,
   Annual session, year and part before accepting a result. Never guess the
   result endpoint or reuse SSC parsing just because labels look similar.
4. Preserve board-provided names, DOB, identifiers, institution/district and
   marks. Fields absent on HSSC cards stay explicitly absent; do not invent them.
5. Audit and pin the full CSV hash. Repeated start/deploy commands reuse the
   same exam/input job. A changed input is an explicit update, not an accidental
   new submission queue. Confirmed saved rows skip via indexes before browser work.

## Durable commits, cleanup and capacity

The existing collector already commits each successful result and records a
retention hash before deleting that verified job's captures. Preserve this
ordering and adapt it to HSSC. Do not defer DB commits until 500 results.

Every 500 additional verified students, publish a small atomic checkpoint with
counts, last progress time, source/roll-list hashes and retention totals. This
is a progress/recovery milestone, not a separate set of loose result files or
a full DB copy per batch. Sweep deferred verified captures after confirming the
stored parsed record and retention metadata. A crash between commit and deletion
must be recoverable without refetching or deleting uncommitted evidence.

Retain unresolved/error evidence with a proposed 1 GiB quota. At quota, pause
new lookups and surface an actionable state; do not silently erase unparsed
failures. Rotate bounded service logs and keep only the latest two successful
periodic downloadable snapshots. Never delete the authoritative DB as cleanup.

Check guest free disk, free inodes and cgroup memory before claiming work;
`/proc/meminfo` alone may report host capacity inside an LXC. Initially pause
new work below 4 GiB disk free and resume above 6 GiB. Snapshot creation also
requires space for the SQLite backup plus compressed output and a reserve.
Finish/commit in-flight work when possible, without exhausting the reserve.

Check host thin-pool data and metadata usage through the platform management
layer as well: a guest's free disk does not prove the host pool is safe. Pause
new jobs when the host reserve/threshold policy is breached; existing services
must not lose their resource reserves. Recheck capacity during sustained runs.

Size 32 GiB is an initial budget, not a prediction of final HSSC storage. Measure
DB/evidence bytes per saved row after the first 500/1,000 and project the total
with headroom. Expand deliberately if needed; do not depend on thin provisioning
as an unlimited capacity guarantee.

## Laptop CLI and database download

Proposed command contract (to be implemented; these are not runnable commands yet):

```text
bise_remote preflight --profile inter-2024-part2
bise_remote deploy --profile inter-2024-part2
bise_remote start --profile inter-2024-part2
bise_remote status --profile inter-2024-part2
bise_remote watch --profile inter-2024-part2
bise_remote logs --profile inter-2024-part2
bise_remote stop --profile inter-2024-part2
bise_remote download --profile inter-2024-part2
```

`watch` is read-only and reconnectable. Show saved/total, remaining, failed/held,
workers, recent rate, last successful save, heartbeat age, free storage and cleanup
totals. Distinguish disconnected monitoring from a stopped server job. Counts use
indexed/read-only SQLite queries or atomic cached status; monitoring must not
take the collector's exclusive application lock. Ctrl+C closes the monitor only;
stopping collection requires the explicit `stop` command.

Download uses SQLite's online backup API while collection continues, never raw
copying a live DB without its WAL. Validate the snapshot using `quick_check`,
record exam/counts/schema/snapshot time and SHA-256, compress it, and transfer
with resumable rsync over SSH. Verify hash, decompress and validate locally
before atomically publishing it under `remote_downloads/Inter 2024 - Part-II Annual/`.
Unchanged snapshot hashes skip repeat transfers. Resume an interrupted transfer
only against the same pinned remote snapshot; retain it until acknowledged or
explicitly expired. Bound snapshot TTL/count and prevent cleanup racing downloads.

Primary download is `.sqlite3` (optionally compressed), not a plain SQL text dump.
It contains the actual tables/indexes/history. A `.sql.gz` dump can be an optional
export. Keep the server snapshot separate from the laptop's current results DB;
an eventual merge must validate exam keys and preserve existing SSC/registration
state. Never overwrite the laptop database implicitly.

## Implementation / acceptance sequence

1. HSSC model/import/parser additions and SSC regression tests locally.
2. Resource/cleanup/checkpoint and remote-controller tests, service packaging.
3. Read-only placement/capacity preflight; build and deploy the isolated guest.
4. A 3-result pilot verifies actual HSSC identity/layout and DB-only retention.
5. A bounded 500-result run measures storage, throughput and sustained behavior.
6. Test monitor disconnect, collector interruption/restart, guest restart,
   unplugged laptop, network outage, low disk/inodes, cleanup-after-commit crash,
   repeated start/input import and concurrent start exclusion.
7. Download a snapshot during writes; verify checksum, integrity, WAL-committed
   rows and local count/identity preservation. Test interrupted/repeated downloads
   and a snapshot cleanup/download race.
8. Start the full 194,851 queue after pilot acceptance. Retry only unresolved
   work with bounded policy, server cooldowns and durable attempt history.

Rollback stops this service and reselects the previous immutable app release;
retain job data and a pre-migration SQLite snapshot. Do not auto-downgrade a
changed DB schema or destroy the container/state as rollback. Existing billing,
Android Builder, registration and laptop workflows remain separately owned.

Default assumption: 2024 Inter Part-II **Annual** only, taken from the supplied
CSV. No games normalization/upload and no migration of existing 2026 jobs are
included in this first server-collection scope.
