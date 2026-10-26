# Fast Ctrl-C export

The collector commits each result to SQLite before report generation. Export still publishes the same CSV/XLSX master, institution reports, original-card images and ZIPs, with archive/rollback and hash verification. Collection/normalization paths and resume keys remain the same.

## Root cause and implementation

1. `Worksheet.max_row` scanned every cell each time a student cell or subject row was formatted. That made workbook construction grow quadratically. A profiled synthetic 500-student workbook spent about 16 of its 24 seconds just finding the last row. Explicit row counters remove those repeated scans, including missing-result sheets. Column widths now read headers only.
2. The collector now serializes XLSX directly with XlsxWriter, avoiding a large openpyxl cell/XML object graph. Tables, overview, text identifiers, institution tabs, subject breakdown remain available. Format 5 omits per-card URLs from Excel; card images and CSV paths remain available. The installed/pinned lxml backend also accelerates the other apps that still use openpyxl. New-machine installation still uses the requirements command. Overview navigation still uses standard internal/institution-workbook links. There is no per-student Excel card hyperlink.
3. PNG images are already compressed: card ZIPs now store original image bytes without running DEFLATE on them again. ZIP CRCs and receipt/report SHA-256 checks remain. An append-only ZIP stream calculates its SHA-256 during writing, so those large new ZIPs do not need to be read a second time just to build their manifest. The master gets independent copies of original images; institution cards can share those copies through hard links only within this new generation, with copy fallback when unsupported. Original evidence and previous report generations never share these links. This reduces card duplication while keeping institution folders/ZIPs usable on their own.
4. Validate each captured job once across overlapping batches. A collector-run/export digest cache reuses a hash only while device, inode, size, nanosecond mtime and ctime match. File changes invalidate the cache; it is held only in this Python operation/run and is not persisted as a substitute for verification. Collection, startup cache validation and Ctrl-C export share hashes already calculated from receipt bytes, while rechecking file stamps to detect changes. A standalone export starts fresh. The archive's before/after inventory can reuse unchanged digests across a directory rename. A changed report signature avoids needlessly hashing old outputs before rebuilding. Unchanged reruns still verify published files and preserve their mtimes.
5. Add an exam/state SQL index and visible export stages/timings. The terminal reports verification, master writing, institution progress and publication instead of remaining silent. `Data/manifest.json` records stage timings. Changed outputs still build in staging and publish atomically; an error preserves the previous published folder and committed DB records.

Regular `collect` commands automatically use this implementation on completion or Ctrl-C. An already running Python process must finish/restart to load updated code. One Ctrl-C stops collection and begins export; allow the printed export stages to finish.

## Export saved data only

No browser or result refetch is needed:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_results export --year 2026 --class 9
```

Use `--class 10` for the existing 10th master. A batch ID remains supported. Default CLI export contains processed rows; `--include-pending` explicitly exports the full queue and can produce much larger workbooks. The collector/database lock still prevents export while another collector owns the same DB.

## Verification

The tests cover bounded full-sheet scans, hash-cache mutation detection (including same-size edits with restored mtime), overlapping-batch verification, PNG ZIP contents, hard-link copy fallback, source/archived-card isolation, idempotence, archive rollback and Ctrl-C recovery. Large benchmarks use synthetic identities/images in a temporary folder on the project disk, without touching production DB/output or making network requests.

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m scripts.benchmark_export --rows 10000
```

Default benchmark: 10,000 students, 8 subjects each, 10 institutions and 320 KiB per card. It measures full publication for collector-stop (receipt hashes already calculated during collection) and a subsequent cold unchanged export, including CSV, Excel, cards, ZIPs and integrity checks. Add `--cold` to measure independent first export without the in-memory collector cache. Evidence: `docs/evidence/export-performance.json`. Its figures describe that synthetic workload on this machine; actual file sizes, institution distribution and disk load affect runtime.

Writer references: [XlsxWriter worksheet tables](https://xlsxwriter.readthedocs.io/working_with_tables.html) and [memory-mode constraints](https://xlsxwriter.readthedocs.io/working_with_memory.html). Normal (non-constant-memory) mode retains Excel tables; IDs remain text and board strings are never automatically interpreted as formulas or URLs.

## Measured result on this machine

The completed synthetic collector-stop benchmark produced all reports for 10,000 students in **130.655 seconds**. It included 80,000 subject rows, 10 institution packs, 320 KiB/card and about 9.95 GB of physical report files. Stage times: load/verify 4.669s, master 53.457s, institutions 51.894s and final report hashing 19.740s. A fresh-process-style unchanged export took 71.276s while checking all original/published bytes and preserved file mtimes. This cold verification still performs substantial disk IO; it does not pretend that unchanged multi-GB files were independently checked instantly.

154 regressions passed, including Ctrl-C recovery and old/new report integrity. Evidence and limitations are recorded in [verification](evidence/export-performance-verification.json).
