# Database-only collection and project cleanup

`data/results.sqlite3` stores complete parsed BISE results. Verified rows keep their roll/exam key, identity, subject marks, status, source URL, timestamps and capture receipt metadata even after redundant files are intentionally deleted. `database_retention` records the result/receipt SHA-256 values and cleanup manifest. Resume accepts a matching explicit retention record; an unexplained missing or modified capture still follows the existing recovery path. A changed result or receipt invalidates that exemption.

Default DB-only collection now removes the verified job's capture directory only after the parsed row commits and retention metadata commits. Failed/review/pending captures remain on disk. `--keep-verified-artifacts` preserves captures when explicitly desired. `--export-reports` also keeps new captures for its card exports. Optional future exports can read pruned DB-only rows as verified and omit their unavailable card files.

Inspect a cleanup plan without deleting files:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results cleanup
```

Apply a fresh plan after creating and verifying a coherent BISE DB backup:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results cleanup --apply
```

Cleanup acquires the collector lock, checks SQLite integrity, verifies saved identity keys, and compares all job rows before/after. It deletes only verified `artifacts/results/job-*` trees bound to their saved receipts and known generated report trees under `output` / `artifacts/report-archive` whose previously exported results remain verified in this DB. It preserves unresolved/unknown captures, unmanaged folders, source lists, profiles, DBs/backups, normalization output, registration input snapshots and all Youth Games tracking/receipt artifacts. Registration can continue because its database and output trees are not modified.

Audit plans, per-file deletion journals and final summaries are under `artifacts/cleanup/<UTC run>/`; backups are under `data/backups/results-before-cleanup-*.sqlite3`. Deleted originals cannot be reparsed or recreated from SQLite; the complete already-parsed data remains. Removing the original HTML is an explicit storage-retention choice, not an attempt to keep a historical archive.

Operator-run offline regressions (not run automatically during cleanup):

```sh
cd /home/ahmad/code/personal/bise_lahore_results && PYTHONPATH=tests .venv/bin/python -m unittest discover -s tests -p 'test_retention.py' -v
```
