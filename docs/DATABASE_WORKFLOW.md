# Database-only collection

Collection commits each finished lookup to `data/results.sqlite3`. The default collect command creates no Excel/CSV master, duplicated report card folders or ZIP archives. Original HTML/cards/receipts remain under `artifacts/` for verification and cache reuse. Existing output and archives are preserved.

## Collect or resume

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results collect roll_number_slips/2026/9th/roll_numbers_only.csv --year 2026 --class 9 --limit 0 --workers 18 --captcha-mode auto --headless --retry-unresolved
```

Press Ctrl-C once and wait for the database/stop summary. Finished records are already committed; unfinished records remain resumable. Rerunning uses the same database, exam and receipt checks, and skips valid saved records. Below 2 GiB free on the database or artifacts filesystem, new lookups stop while pending work is preserved.

Part-II/10th uses the corresponding input and `--class 10`. Run one collector process at a time. Old report generation remains an explicit `export` command or `collect --export-reports` opt-in; it is not part of this recommended workflow.

## Normalize directly from the database

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize run --config normalization_profiles/9th_combined.json
```

All four configured normalization profiles use SQLite and `output.write_excel=false`. Their previous exam, institution, location and participation selections remain in effect. The 9th Combined profile selects all Saved Part-I students; Lahore/Kasur/Combined select their configured Part-II students.

A read-only SQLite transaction streams only Saved records in the requested exam, including committed WAL records. The source fingerprint represents those projected student fields, so unrelated pending jobs or other exams do not force a rebuild. Nothing writes to the collection database. Normalization still produces the CSV files required by registration, audit/provenance and its existing cache/ledger. It does not depend on an exported master workbook or CSV. Rerunning unchanged input/settings verifies the existing publication without appending duplicate rows.

For a read-only database count inspection:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize inspect --source data/results.sqlite3
```

## Recovery check on 2026-10-21

The warning-heavy exporter was observed with approximately 102 MB free and substantial swapping/I/O waiting. The process subsequently exited on its own. No process was killed and no original data, reports or archives were deleted during recovery. No full report export was attempted.

The source database passed `PRAGMA quick_check`. Its 104,849 Saved Part-I records and 3,776 Saved Part-II records match the verified coherent backup in `artifacts/backups/stalled-export-20261021T132259Z/results.sqlite3`. All 104,849 Part-I original card paths existed (existence check, not a fresh full-content hash audit). A read-only normalization projection independently read all 104,849 Saved Part-I records. Previous published workbooks are historical snapshots and were not rebuilt.

Regression verification: 187 tests passed, including normal completion, cancellation, low disk, WAL reads, exam isolation, stable fingerprints, CSV-only normalization, rerun idempotence and explicitly opted-in legacy reports. See [verification evidence](evidence/database-only-recovery-verification.json).
