# SSC Part-I 2026 server workflow

This workflow has its own unprivileged guest, source DB, systemd services,
profile, operator state, input and storage guard. It does not stop or redeploy
the Inter collector/registration services. The server runs independently of SSH
and of the laptop monitor.

## Daily commands

Monitor (Ctrl+C closes only the monitor):

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_remote watch --profile remote_profiles/matric-2026-part1.json
```

Start/resume all collection, portrait recovery, normalization and central inbox
delivery services. Repeating this command does not create another process:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_remote start --profile remote_profiles/matric-2026-part1.json
```

Intentional stop (preserves results, assets, queues, receipts and cursors):

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_remote stop --profile remote_profiles/matric-2026-part1.json
```

Coherent database + original-photo backup/download, with resumable transfer,
hash checks and explicit restored media-root mapping:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_remote download --profile remote_profiles/matric-2026-part1.json
```

The verified bundle/restored directory goes under
`remote_downloads/Matric 2026 - Part-I Annual/`. These files contain private
student information and remain excluded from Git. The initial scoped migration
snapshot also remains under `artifacts/ssc-2026-part1/` on the laptop. A database
alone is insufficient to restore external pictures.

## Setup and code updates

With this SSC workflow stopped, `prepare` validates/reserves guest ownership,
deploys an immutable release, transfers a coherent scoped seed only when the
remote database is absent, and installs the isolated services and central inbox
relay. Existing remote records are never replaced by a later prepare.

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_remote prepare --profile remote_profiles/matric-2026-part1.json
```

Run the bounded live acceptance before the first full start:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_remote pilot --profile remote_profiles/matric-2026-part1.json
```

It fetches up to three pending results and three prior-record portraits, verifies
all prior identities, external-file hashes, DB integrity, retention and zero photo
BLOBs. Its report is `runtime/acceptance.json` in the server workflow root.
It never sends a Youth Games application. Do not run a pilot alongside full
collection; both intentionally use the same source queue lock.

## Storage and processing contract

- Sixteen headless result workers, two bounded OCR slots; one separate headless
  portrait-backfill worker. Existing verified results remain verified. Backfill
  compares source exam, roll, names, DOB and both identity numbers before linking
  a photo; it never replaces a saved board identity with the fetched revision.
- Originals are immutable files in `photos/originals/<hash-prefix>/<sha256>.jpg`
  or `.png`. `photo_assets` records source job, BForm, dimensions, root alias,
  relative path, media type and SHA256. The legacy compatibility BLOB table stays
  empty in this workflow. Captures can be discarded only after durable retention;
  permanent photos live outside capture folders.
- Missing photos retry with capped backoff. Identity mismatches are quarantined.
  New results without a downloadable photo remain saved and enter recovery;
  they do not enter the registration outbox until the original passes checks.
- `result_events` records committed result/photo changes. `pipeline.sqlite3`
  has a transactional cursor, normalized rows, indexed states and revisioned
  `ready_outbox`. Late photos on old job IDs are processed. Repeated events do
  not create repeated ready revisions. Duplicate BForms are held.
- Actual board DOB remains unchanged. Official catalog rules determine age
  eligibility. A stale catalog refreshes automatically; unavailable rules block
  new delivery instead of silently assuming eligibility. Oversized originals
  remain preserved and blocked for upload until a derivative is supplied.
- The initial normalization settings inherit `9th_combined.json`: Secondary /
  Open Category, existing operator defaults and original DOB. These are candidate
  records, not an instruction to submit them with those locations/sport values.
  Any revised allocation/profile needs explicit review before registration.
- Free-space pause/resume reserves are 6/8 GiB, with host thin-pool and inode
  checks. Supervisor logs rotate. The dedicated guest has a 64-GiB volume;
  its free space is distinct from total Proxmox pool space.

## Central registration handoff

A root-owned host service uses the existing authenticated Proxmox path to read
bounded ready pages, verify/copy only their content-addressed portraits to the
existing central registration guest, and insert `source_inbox` rows **inside its
existing registration database**. There is no public intake endpoint, second
registration ledger, or SQLite network filesystem.

The source marks a revision delivered only after a durable central acknowledgement.
Lost acknowledgements replay safely. The central authority checks its canonical
campaign/BForm confirmation ledger and existing jobs before staging anything.
Confirmed identities are marked `already_registered`; new rows are held as
`awaiting_registration_policy`. Existing central jobs retain ownership. Delivery
receipts remain in the source `pipeline.sqlite3`.

**Live SSC registration is disabled.** Enabling the portal dispatcher for this
new source requires the approved location/sport/category settings and an explicit
registration start. Meanwhile collection, photo recovery, normalization and
durable central staging can run together without manual file copying. The new
`original_file_reference` provider is available to the existing registration
adapters; every upload checks applicant/exam binding and original SHA256.

## Regressions

```sh
cd /home/ahmad/code/personal/bise_lahore_results && PYTHONPATH=tests .venv/bin/python -m unittest tests.test_remote_queue_contention tests.test_ssc_file_pipeline tests.test_remote_full tests.test_remote_foundation tests.test_remote_pilot tests.test_hssc_photos_age tests.test_university_workflow tests.test_private_registration tests.test_retention tests.test_workload_scope -q
```

Coverage includes source migration compatibility, old Inter paths, external-file
integrity, wrong-identity refusal, file-before-DB rollback, late photo events,
idempotent normalization/delivery, confirmed-identity skipping, backup/restore,
storage admission and monitor independence. Live acceptance is recorded privately
under `artifacts/ssc-2026-part1/` and in the remote runtime directory.

## Concurrent queue recovery

The SSC profile uses 16 result workers on 8 vCPUs / 12 GiB RAM. Resume
reads cached records in 500-row pages and commits claim repairs before checking
retention. A page's read cursor closes before repair writes; file checks do not
hold a write transaction. Photo recovery selects one job from its compact partial
queue index before loading that student's full result. This prevents a large
photo backlog or collector startup from monopolizing the SQLite writer.

Automatic recovery publishes a fresh heartbeat and remaining delay every five
seconds. The regression recreates the old `database is locked` failure with an
independent writer during resume, and verifies both concurrent writes and bounded
photo claims against a 5,000-job queue.
