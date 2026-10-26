# Friendly results output

The normal entry point is one workbook at a fixed, readable location:

```text
output/
  Matric 2026 - Part-II Annual/
    Matric 2026 - Master Results.xlsx
    Matric 2026 - Master Results.csv
    Read Me.txt
    All Result Cards.zip
    Result Cards/
      100001.png
    Institutions/
      KASUR/
        KASUR Results.xlsx
        KASUR Results.csv
        KASUR Result Cards.zip
        Result Cards/
      LAHORE/
        ...
    Data/
      results.json
      institutions.json
      manifest.json
```

The master workbook opens on Overview: saved/pending counts, board outcomes and institution navigation. All Students contains every student's details, identity fields, marks/status. Format 5 removes the Result Card column and card hyperlinks from every Excel student sheet; card images and CSV paths remain available. Institution tabs provide the same data filtered by the board's institution value. Subject Marks contains the complete recognized subject breakdown. Missing Results contains only unresolved lookups; absent/not-improved/partial student records remain in All Students.

Headers use readable labels. Filters, frozen headers, alternating row colors, text-formatted identity fields, sensible widths, status colors and print settings make the workbook usable in Excel/LibreOffice. Missing marks remain blank.

Each exam has one master across imported lists in the same database. Roll/exam uniqueness prevents duplicates. Institution folders have readable safe names; only actual name collisions receive a numeric suffix. Each institution's workbook, CSV and card archive can be shared on their own.

Reruns update the same current folder. An unchanged, hash-verified export is reused without creating another version. Changed output is built separately, then published with rollback on failure; the previous generated folder is retained outside output under `artifacts/report-archive/`. Old batch-id/timestamp output is moved to that archive with a verified file-hash inventory. Unknown/unmanaged folders are preserved.

Thirty-eight passing regression tests cover workbook identity/marks retention, navigation/card links, safe unique names, multi-list deduplication, unchanged reruns, archive preservation, publishing rollback and the existing parser/cache behavior. Final acceptance independently verified all 51 saved cards, workbook/CSV identity fields, image/report links, archive inventories and an unchanged no-op rerun without website requests. Evidence: `docs/evidence/friendly-output-verification.json`.

## Long-running input

CLI exports show processed records by default and keep the entire pending/imported queue counts in Overview. This avoids hundreds of thousands of blank student rows. The explicit --include-pending option restores full queue rows when requested. Export occurs at normal completion or after one graceful Ctrl-C, with a separate DB-only export command available after a hard stop. Existing files open in spreadsheet applications must be reopened to load changes.

Format-5 verification: 65,531 students exported without Excel URL-limit warnings; all student rows, identity fields, overview navigation, card archives and CSV card paths are retained. Existing format-4 reports rebuild on the next export; previous reports remain archived. Evidence: `docs/evidence/excel-card-urls-removal-verification.json`.
