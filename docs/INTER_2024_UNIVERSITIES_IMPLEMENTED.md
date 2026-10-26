# Inter 2024 university registration workflow

Implemented on 25 October 2026. This supersedes the DOB gate and proposed
execution steps in `INTER_2024_UNIVERSITIES_OPEN_PLAN.md` for this frozen cohort.
The operator explicitly confirmed a department/admin correction process allowing
provisional DOB registration. This exception is operator-reported; the website
does not independently verify it, and its disclaimer still states submissions
cannot be changed or deleted through the portal.

## Preserved source and provisional dates

Collection remains stopped and disabled in LXC 950. Neither the original
`jobs/inter-2024-part2-annual/results.sqlite3` nor the records-only snapshot is
modified by normalization or registration. Original DOBs remain blank.

Each valid B-Form gets a deterministic, randomly distributed date within 2006,
based on the explicitly requested assumption of age 18 in 2024. Reruns produce
the same date. These are **provisional registration dates, not verified dates of
birth or independently verified age eligibility**. Provenance and request proofs
retain the original blank, generated value, policy seed, correction-required flag,
operator exception and `department_policy_verified_by_site=false`.

Original portraits are read from the frozen source SQLite by job ID and checked
against roll, B-Form, exam and SHA-256 before multipart submission. No original
photo BLOB table is copied into the registration database or records snapshot.
Keep the original source database: the photo references depend on it.

## Frozen allocation and validation

Of 29,128 saved rows, 69 rows in duplicate identity groups, one invalid identity
and two prior registrations are excluded. The 29,056 remaining rows split evenly:
14,528 Raiwind and 14,528 Model Town. Field validation holds another eight rows:

| Target | Ready at preparation | Field issues |
| --- | ---: | ---: |
| Raiwind | 14,526 | 2 |
| Model Town | 14,522 | 6 |
| Total | 29,048 | 8 |

Both use **Universities / Open Category**, Lahore division/district. The operator's
administrative UC assignments remain in provenance; the Universities form hides
UC and receives an empty `uc_id`. Residence is not inferred from that assignment.

All 21 offered games have positive weights. Weights sum to 100: Athletics 28,
Badminton 20, Hard Ball Cricket 12, Tug of War 8, Football 5, Basketball 4,
E-Sports/Table Tennis/Volleyball 3 each, Chess/Hockey 2 each, and Cycling,
Fencing, Kabaddi, Karate, Padel Tennis, Taekwondo, Wrestling, Handball, Squash,
Laser Run 1 each. Largest-remainder allocation preserves totals within each
target/gender group. Boys-only games are excluded for female rows. This is an
administrative allocation policy, not an inference about individual preferences.

## Existing engines and durable ownership

`bise_normalize.university` pins the cohort, then calls the existing normalizer.
`bise_remote.registration_worker` calls the existing HTTPX registration runner.
There is no new browser engine or second registration implementation.

Workflow root in LXC 950:

```text
/var/lib/bise-results/workflows/inter-2024-universities-open-20261025/
  snapshots/Records Only.sqlite3
  config/batch.json, catalog.json, normalize-*.json
  config/register.json, register-pilot.json
  input/raiwind.csv, model-town.csv
  state/allocations.sqlite3, normalization.sqlite3, registration.sqlite3
  reports/Raiwind/, Model Town/, Combined/
  registration_output/Full/Runs/...
  registration_output/Pilot/Runs/...
  runtime/status.json, last-result.json, events.jsonl
```

Confirmed terminal identities from the old laptop ledger are imported as compact,
immutable records. No fake attempts or receipts are generated for them. The old
database is retained, with a server-owner sidecar preventing accidental local
registration writes. Confirmed identities skip before form preparation.

Four workers share a six-second dispatch interval and two in-flight slots.
POST order alternates Raiwind, Model Town; the next target and dispatch sequence
commit with dispatch intent in SQLite and survive restart. Preparation errors
release their turn. Receipts and tracking IDs use the existing durable ledger.

## Operator commands

The usual laptop monitor follows the active registration task:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_remote watch
```

Ctrl+C closes this monitor only. A single view and detailed events:

```sh
.venv/bin/python -m bise_remote status --job registration
.venv/bin/python -m bise_remote logs --job registration
```

The bounded pilot selects exactly the two pinned applicants. Repeating it skips
their confirmed registrations. It does not start the full queue:

```sh
.venv/bin/python -m bise_remote pilot --job registration
```

The full run is a separate operator action, **not started during this pilot**:

```sh
.venv/bin/python -m bise_remote start --job registration
```

Stop registration with `bise_remote stop --job registration`. Collection controls
remain available using explicit `--job collection`; do not resume the collector
while this workflow depends on its frozen source hash.

Preparation refuses changed frozen cohort policy. Later policy/source changes
need a versioned cohort, preserving prior confirmed registrations. Private live
evidence is under `artifacts/inter-2024-university-registration/`; aggregate,
non-identity verification is under `docs/evidence/`.

## Verified outcome

The live pilot registered one applicant per tehsil and saved two tracking IDs.
Both receipt hashes and original-photo hashes verified, and dispatch order was
Raiwind then Model Town, six seconds apart. Source `quick_check` returned `ok`
and its SHA-256 remained unchanged. Repeating the pilot skipped both applicants:
zero new POSTs, and the attempt/dispatch counts remained two. The existing
`bise_remote watch` displayed registration status and both tracking IDs; Ctrl+C
closed only the monitor. The relevant normalization, ledger, HTTP, legacy browser,
snapshot and remote compatibility suite passed **122 tests**. Detailed aggregate
evidence: [verification JSON](evidence/inter-2024-universities-server-verification.json).

After the two successful pilot registrations, **29,046** prepared records remained.
The operator subsequently requested continued processing; full registration was
started separately. Pilot and full profiles now have distinct owned receipt
folders. The legacy pilot receipts remain at their original paths, and an
idempotent migration changes only the configured output paths. Regression checks
cover the legacy owner conflict, preserved receipt bytes and stable reruns.
