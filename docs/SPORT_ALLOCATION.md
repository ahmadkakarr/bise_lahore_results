# Remaining Raiwind sports distribution

The operator approved administrative sport ratios for the remaining, eligible,
never-dispatched applicants in the existing Lahore 9th Raiwind cohort. These
are administrative assignments, not inferred individual preferences.

The editable policy is `sports_profiles/lahore_9th_raiwind.json`:

| Sport | Female | Male |
|---|---:|---:|
| Badminton | 50% | 10% |
| Athletics | 20% | 15% |
| Cricket (Hard Ball) | 10% | 40% |
| Football | 5% | 25% |
| Tug of War | 15% | 5% |
| Kabaddi (Boys Only) | 0% | 5% |

Stop the registration runner gracefully before applying a sports policy. The
operation acquires the same exclusive registration database lock and sends no
applicant POSTs:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate sports --config allocation_profiles/lahore_9th_raiwind.json --ratios sports_profiles/lahore_9th_raiwind.json --json
```

`sports` persists mappings and synchronizes normalization and pinned registration
files. The next ordinary `http-submit` prepares its actual request payloads from
that input. A form-only HTTP `prepare` can validate/import them without submitting.

`data/allocations.sqlite3` stores `sport_batches` (approved policy/hash/timestamp)
and `sport_assignments` (scheme, BForm identity, source roll, gender, UC, sport,
batch, assignment timestamp). Assignments and policies cannot be updated,
deleted or replaced. Reruns retain existing assignments; editing the ratio
applies only to additional unassigned, eligible, never-dispatched identities.
It does not reshuffle already assigned students. The frozen source cohort and
original result-card fields are unchanged.

Integer largest remainders produce exact gender-wide quotas. Those quotas are
distributed proportionally across UCs, then deterministically assigned with a
fixed seed. This preserves the small male cohort's minority-sport quotas instead
of rounding them away independently in each UC. The live category catalog
validates every sport, percentage totals and the boys-only restriction. Unknown
gender, invalid/duplicate identities and normalization-blocked students are held.

Registered identities, terminal duplicates, uncertain/held jobs, and any job
with a previous durable intent or dispatched POST are excluded from sport
reassignment. Their previous sport remains. Existing registration receipts,
tracking IDs and attempt histories are preserved. An in-flight registration
process prevents the operation through the existing DB lock.

The allocated source view exposes the assigned sport and its audit metadata;
normalization carries it into provenance and validates it against the target
fields. Registration's `selection.sport` supports either its previous single
label or an explicit allow-list, with all fresh category/gender option checks
still enforced. The handoff, browser adapter and direct HTTP adapter use the
same selection contract. There is no wildcard bypass.

Normal reruns use the existing indexed terminal ledger and pinned input cache.
Use the usual command to resume with eight workers on each configured route:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate http-submit --config allocation_profiles/lahore_9th_raiwind.json --connections connection_profiles/wifi-usb.json --workers-per-connection 8 --prepare-ahead 2 --in-flight 6 --dispatch-interval 5 --limit 0 --auto-recover --retry-uncertain --uncertain-retries 2 --alarm
```

Offline regression coverage is in `tests/test_sport_allocation.py`. It includes
integer quotas, UC conservation, deterministic reruns, mixed gender inputs,
terminal/previous-POST exclusions, policy validation, immutable mappings,
normalization-to-CSV-to-real-request-option IDs, and fast pinned resume.
