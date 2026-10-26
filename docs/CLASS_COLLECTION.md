# SSC 2026: separate 9th and 10th collection

The collector supports the supplied folders directly. Both `Roll Number` and `Roll Numbers` headers are accepted; original CSVs are never rewritten. The year and explicit `--class` choose the actual board exam, independently of the input filename.

| Class | Input | Board selection | Verified endpoint | Output folder |
|---|---|---|---|---|
| 9th | `roll_number_slips/2026/9th/roll_numbers_only.csv` | SSC, Part-I Annual, 2026 | `RollSlip.aspx` | `output/Matric 2026 - Part-I Annual/` |
| 10th | `roll_number_slips/2026/10th/roll_numbers_only.csv` | SSC, Part-II Annual, 2026 | `ResultPart2.aspx` | `output/Matric 2026 - Part-II Annual/` |

## Plan implemented

1. Keep the existing shared `data/results.sqlite3`. Jobs are already unique by year/course/exam type/roll; import and batch/report scope now use that full key. Add exam metadata to old batch rows with Part-II defaults, preserving all old job IDs, results and receipts. Input paths do not enter the batch identity: moving a CSV does not make saved rolls new work.
2. Keep the legacy no-flag default at 10th. `--class 9` explicitly selects Part-I, validates the selection, checks its observed endpoint and requires a 9TH CLASS heading. A Part-I response cannot be accepted as Part-II or vice versa.
3. Parse the actual nine-class subject table and totals, retain source identity/DOB/student and father identity numbers, and preserve partial records when marks are unavailable. Where Part-I does not print a numeric maximum, sum valid subject maxima and mark that origin. Use a Part-I parser revision independently of the existing Part-II revision, so old 10th caches do not need a parser upgrade.
4. Keep the existing report layout and schema, institution/district grouping, original cards, subject sheets, archive/rollback and resume logic. Publish separate Part-I and Part-II master folders; exam-scoped exports never combine their students, queue totals or batch memberships.
5. Audit the two actual lists and verify a bounded live 9th lookup. Regress class selection, singular/plural headers, same-roll separation, old schema migration, cache reuse, parser mismatch/partial results and separate/idempotent masters. Test real browser submission against intercepted synthetic responses.

## Commands

9th:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results collect roll_number_slips/2026/9th/roll_numbers_only.csv --year 2026 --class 9 --limit 0 --workers 18 --captcha-mode auto --headless --retry-unresolved
```

10th:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results collect roll_number_slips/2026/10th/roll_numbers_only.csv --year 2026 --class 10 --limit 0 --workers 18 --captcha-mode auto --headless --retry-unresolved
```

Run one collector command at a time: their workers share one database and the collector holds its process lock. Up to 18 workers within that command run concurrently. Headless means no visible windows; pacing and shared error cooldown are retained. `--retry-unresolved` revisits failed/review records while valid saved receipts remain cached.

Both commands save to `data/results.sqlite3`; `(year, course, exam_type, roll)` separates 9th and 10th records. Normal completion and Ctrl-C print database counts without generating Excel/CSV masters, copied card folders or ZIPs. Original response/card evidence stays in `artifacts/`.

`normalization_profiles/9th_combined.json` reads Saved Part-I identities directly from the database. Existing Lahore/Kasur/Combined profiles read Part-II from the same database. All have Excel output disabled; normalized CSV is still available for registration. Prior master files remain historical snapshots and are no longer the normalization source. See [database workflow](DATABASE_WORKFLOW.md).

Before a large collection, `--limit 5` bounds a pilot. Offline audits can be repeated with `python -m bise_results audit PATH`; `inspect --year 2026 --class 9` opens the correct form visibly without scripted submission. Never change a student's date of birth to satisfy eligibility rules.

The BISE collector supports 1–18 workers. If a collector is already running with nine workers, press Ctrl-C once, wait for its database/stop summary, then restart the same command with `--workers 18`. Saved results stay cached. Do not run a second collector while the first holds the database lock. Local 18-context verification used intercepted 9th responses and fixed fake OCR; live throughput remains unmeasured. See `docs/evidence/headless-18-workers-verification.json`.
