# Permanent exclusion of registered applicants

The shared `data/youthgames.sqlite3` database is authoritative for both browser and direct-HTTP registration. The identity key is `(portal, campaign, canonical BForm)`; changing workers, profile names, normalized files, class/roll references or source defaults does not create another registration for the same identity in that campaign.

`registered_applicants` is the append-only confirmation index. It stores the original job ID, terminal status, confirmation time, tracking ID, receipt path and payload fingerprint. Existing `registered` and `already_registered_external` jobs are backfilled without changing their original job data or evidence. `registered_students` is an easy-to-inspect SQL view with roll, student name, applicant key, status, registered time, tracking ID and receipt path.

Protection layers:

1. Normalization excludes protected portal/campaign identities from Ready exports.
2. Browser input preparation excludes ledger identities before live form validation, including an accidentally stale or reimported input file. Entirely completed inputs bind to their existing jobs and return without loading a portal form.
3. Ready queue selection and the atomic `begin()` claim independently exclude the ledger.
4. SQLite triggers prohibit resetting/deleting/replacing a confirmed job, changing its protected metadata, or starting a new attempt for it.
5. The confirmation ledger rejects update/deletion; replacement of an existing identity is ignored. Terminal confirmations cannot be silently changed by another app path.
6. WAL, FULL synchronous commits, transactional intent/dispatch markers, a process lock and conservative uncertain-outcome recovery remain in place. Unknown dispatched outcomes are never treated as unsubmitted.

The database can be inspected with:

```sql
SELECT status, COUNT(*) FROM registered_students GROUP BY status;
SELECT roll, student_name, status, registered_at, tracking_id, receipt_path
FROM registered_students ORDER BY registered_at DESC LIMIT 50;
```

`already_registered_external` remains distinct from a locally confirmed registration: it may not have a local tracking ID. Missing legacy evidence does not authorize a new POST. Repairs require explicit evidence reconciliation, not resetting the applicant to Ready.

Keep this database and its backups: the guarantees apply to this retained database and campaign. A deleted/replaced database or deliberately changed campaign is a different state source. Diagnostic on/off switches never disable duplicate protection or essential receipts.

The migration audit and backup path are recorded in [the local deduplication audit](evidence/registration-deduplication-audit.json). It verified unchanged terminal rows, matching ledger metadata, no Ready collisions, and SQLite integrity. No applicant requests were made for that audit.

Operator-run regressions:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m unittest tests.test_registration_ledger tests.test_browser_diagnostics tests.test_youthgames_workers tests.test_youthgames -v
```

New regression cases cover SQL resets/deletion/replacement, append-only ledger behavior, restart/backfill idempotency, changed input/profile protection, stale input skipping before catalog access, worker limits through 36 and diagnostics on/off. Long regression/browser runs were not executed during this change; syntax and CLI checks were performed.
