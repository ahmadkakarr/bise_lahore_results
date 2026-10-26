# Private ChatGPT / developer handoff

Run from any directory:

```sh
/home/ahmad/code/personal/bise_lahore_results/.venv/bin/python /home/ahmad/code/personal/bise_lahore_results/scripts/export_context.py
```

Each run creates `context_exports/YYYY-MM-DD_HH-MM-SS_UTC/` with owner-only
permissions. It never uploads, starts forms, submits registrations, sends messages,
deletes source files or modifies database rows. Repeating the command captures a
new dated state; previous exports are excluded from subsequent exports.

- `Code-and-Context.zip`: all project source, tests/fixtures, profiles, dependency
  files, useful local input files, docs, source map, database schemas/counts and
  readable locally available user/assistant chat including embedded images.
- `Data-results.zip`, `Data-normalization.zip`, `Data-allocations.zip`,
  `Data-youthgames.zip`: complete current SQLite snapshots including every table,
  row, index, trigger, ledger and tracking ID. SQLite's online backup API includes
  committed WAL changes without copying a potentially inconsistent raw DB/WAL.
  Each snapshot is consistent independently; it is not a single transaction across
  all four live databases.
- `Evidence-001.zip` etc.: original DB-linked registration receipts and their
  SHA-referenced proof files, historical registration input snapshots, current
  configured normalized inputs, and available unresolved result captures.
- `START_HERE.md`, `CAPTURE_REPORT.json`, `BUNDLE.json`, `SHA256SUMS.txt` explain
  the scope, original paths, exact gaps and integrity. `MANIFEST.json` inside the
  code ZIP indexes every logical file, SHA-256 and containing archive.

Start with the **code/context ZIP** in the web conversation. Add the relevant DB
parts when actual row/state analysis is needed. Supply evidence volumes for
receipt/recovery investigation or complete local restoration. The small `related/`
appendix contains only the known cross-project files changed for coexistence,
not entire copies of the automation-platform or WhatsApp repositories.

The private data parts retain the actual student records the operator requested.
There is no implicit anonymization of full SQLite data. Environment-file secret
values, credential/key files, raw tool messages, system/developer prompts and
private model reasoning are excluded. Visible chat text redacts common reusable
secret forms. Local project chat is all matching Codex session segments available
on disk at capture time; browser-only history is not fetched.

## Why a normal ZIP was large

Most space is repeated portal responses, old report/output copies, backups and
normalization caches, rather than source code. ZIP normally compresses each HTML
file independently and cannot share the repeated portal layout across receipts.
Evidence uses **solid XZ compression** in a uniquely named `.tar.xz` container per volume, wrapped in an ordinary
ZIP, to preserve all referenced original bytes while sharing repeated content.
The volume target is 180 MiB of compressed evidence; individual files are not
split, so this is a soft target. Database ZIP entries use standard DEFLATE for
faster snapshots and broad compatibility. No third-party exporter dependencies are needed.

To inspect a nested evidence volume with Python:

```python
import io, tarfile, zipfile
with zipfile.ZipFile("Evidence-001.zip") as archive:
    container = next(name for name in archive.namelist() if name.endswith(".tar.xz"))
    with tarfile.open(fileobj=io.BytesIO(archive.read(container)), mode="r:xz") as evidence:
        print(evidence.getnames()[:10])
        # For trusted local restoration only:
        # evidence.extractall("restored", filter="data")
```

The export omits venv, bytecode, generated normalization cache, old duplicated
backups, prior exports and unreferenced generated output. It keeps current source,
full authoritative databases and the reference closure needed to investigate
registered/uncertain state. DB-only retained BISE results do not require old
images already deleted by the application's retention policy. Existing missing
references or evidence-hash mismatches are **reported**, never silently repaired.

The database records retain their original absolute paths and operational state.
The manifest maps them to bundle paths. Do not resume submissions on a new host
by clearing its ledger or overwriting frozen identity/receipt history.

Verify without creating a new export:

```sh
python3 scripts/export_context.py --verify context_exports/THE_DATED_EXPORT
```

Verification streams every logical file, checks SHA-256/length and archive
integrity, and rejects unsafe/duplicate/unmanifested paths. Export status is
`COMPLETE` only for a successful capture of the selected existing local scope;
known gaps result in `INCOMPLETE` and exit code 2. A failed/interrupted export
returns nonzero and writes `EXPORT_FAILED.json`. No incomplete capture is called
complete merely because its ZIPs are small.

For a code/schema-only handoff without private row/proof data, explicitly use
`--data schema`. `--chat none` explicitly omits chat. These are scope options,
not the full-data default. The root project currently has no Git history;
the exporter records that fact and never creates a Git repository implicitly.
