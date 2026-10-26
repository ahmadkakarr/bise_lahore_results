# Normalize saved BISE records

`bise_normalize` is a separate offline Python application. Configured profiles read `data/results.sqlite3` in a read-only transaction and write `normalized_output/`, without modifying collection records or original cards. CSV/XLSX sources remain supported for explicit imports. Excel output is disabled by default; registration CSV and audit files remain available. Regular normalization is offline; the explicit catalog refresh command opens a read-only visible browser. It uses the existing `.venv`; no installation command is needed on this machine.

## Lahore profile: run or resume

The configured profile selects only saved `LAHORE` identities. Settings supplied by the operator:

| Setting | Value |
|---|---|
| Category | Schools (Secondary) |
| SubCategory | Open Category |
| Sport | Athletics |
| Division / District | Lahore / Lahore |
| Tehsil | Shalimar |
| UC | UC No. 178, Manawan Ram Pur Jageer |
| Missing Phone | 03211234567 |
| Missing Address | Original board Institution / District |

Locations and fallback contact/size values are operator supplied. The portal-aware profiles validate accepted dropdown labels and category DOB bounds against a fresh official catalog; they do not independently establish residence or garment fit. Kasur selects original KASUR records but uses the same target Lahore/Shalimar/UC178 settings as explicitly requested; original KASUR remains in Address and audit.

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize run --config normalization_profiles/lahore.json
```

Edit `normalization_profiles/lahore.json`, then rerun the same command. Unchanged input/settings and valid output produce `Already up to date`, leaving file hashes and modification times unchanged. New master rows or changed settings rebuild the selected output without appending duplicates. Run again after new records are committed to SQLite; no master export is required. The read-only snapshot includes committed WAL data and excludes pending/unresolved lookups.

## Files to open

```text
normalized_output/Lahore/
  Normalized.csv                 exact sample's 17-column order; partial rows retained
  Complete Records.csv           required fields present; may include age-ineligible students
  Age Eligible.csv               category DOB bounds satisfied, including incomplete rows
  Eligible.csv                   age eligible + complete + accepted portal options
  Ready to Register.csv          eligible, excluding known uploads/uncertain/blocked states
  Ineligible.csv                 age exclusions with roll, DOB, bounds and reason
  Age Unknown.csv                missing/invalid DOB or category rule
  Already Registered.csv         known local/external registrations
  Registration Blocked.csv       uncertain/in progress/rejected/unknown states
  Summary.txt
  Data/
    manifest.json                input/output fingerprints and counts
    effective-profile.json       effective settings used for the export
    provenance.jsonl             original fields and per-field origins
    excluded.csv                 source rows outside the selection / invalid identity
    duplicates.csv               repeated source/target keys
```

`Complete` describes configured field completeness, including supplied fallbacks and estimates. Gender fallback, shared phone, board-label address and estimated sizes are identified in Details/provenance; completeness is not a claim that they were independently verified or measured. Existing explicit values/individual corrections take priority. Missing marks do not exclude saved students.

Managed previous outputs are preserved under `normalized_output/.normalization/previous/`, with hash inventories. Do not edit generated CSV/workbooks as your source of truth: put corrections in a profile or supplemental/override CSV.

Excel files are optional only: set `output.write_excel=true` explicitly to create them. Existing generated files are archived by normal publication when a changed profile is run.

## Selection and settings

All relative paths resolve against the JSON profile file. Keep the source outside the output destination. Unknown settings and unsupported modes fail explicitly.

Select one or several full board labels:

```json
"selection": {"institutions": ["LAHORE"], "institution_match": "exact_case_insensitive"}
```

Use an empty institution list for all saved students. Additional filters compose with AND:

```json
"row_ranges": [[2, 500]],
"rolls": [],
"roll_bounds": ["100000", "100500"],
"result_statuses": [],
"limit": null
```

CSV row 1 is the header; row numbers count logical CSV records, including quoted multiline records as one row. `limit` is a positive integer or null. `result_statuses` empty means all academic outcomes, including ABSENT/FAIL/marks-not-improved, provided the collector saved student identity.

For SQLite, use `source.format="sqlite"`, `source.path="../data/results.sqlite3"` and explicit `source.exam` (BISE Lahore, SSC, year, Annual, part I or II). Rows are sorted by roll, with projected header row 1 and Saved records starting at row 2. `selection.row_ranges` applies to this ordered projection; prefer roll/institution filters for stable selections as the database grows. Worksheets/cell ranges do not apply to SQLite.

For optional Excel imports, set `source.path` to the XLSX, `source.sheet` to `All Students` or an institution student sheet, and optionally `source.cell_range` to a bounded range such as `A1:V500`. `source.header_row` is the absolute header row. The selected columns must contain the configured identity columns and Roll No/Collection Status/Institution / District. Overview/Subject Marks are not student source tables. CSV cannot have a sheet or A1 range.

Set Category/SubCategory, Sport and locations in `defaults`. Full school names can be set in `defaults.Institute` for a known school batch or mapped by exact board label:

```json
"school_name_mappings": {"Original board label": "Full actual school name"}
```

The mapping is inside `participation`. A district-only source does not become an asserted school name. Without an actual school name, SubCategory becomes Open Category, Institute stays blank and is optional. Original board text remains available as Address fallback and in the audit. Unclassified board labels are flagged for mapping rather than silently asserted to be school names.

Age is calculated on `size_estimation.as_of_date`, initially `2026-10-20`. Birthday handling uses completed years; February 29 birthdays advance on March 1 in non-leap years. Edit age/gender lookup rules to change estimated sizes; a gap/overlap fails validation. Values are generalizations, not measurements. Missing/invalid DOB or unsupported gender leaves size missing. No DOB is changed to fit age/category rules.

## Inspect, validate and preview

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize inspect --source data/results.sqlite3 --template "sample/Raiwind_1336_UC273_Urban_Automator_Clean_Without_53_Rejected.csv"
```

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize validate --config normalization_profiles/lahore.json && .venv/bin/python -m bise_normalize preview --config normalization_profiles/lahore.json --limit 10
```

Validation performs the selected transform and reports counts without publishing output. Preview additionally prints bounded roll/issue summaries, not full identities. Structural errors return nonzero; partial rows are an explicit successful transform. An empty selection preserves the previous successful output and returns an error.

## Another profile

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize init --profile Kasur --config normalization_profiles/kasur.json --source data/results.sqlite3 --template "sample/Raiwind_1336_UC273_Urban_Automator_Clean_Without_53_Rejected.csv" --output normalized_output/Kasur
```

`init` preserves an existing profile byte-for-byte. Configure the new profile's own selection/location/category values before running it. Competing runs targeting the same output folder are locked out. Portal-aware profiles also lock their shared normalization ledger; run Lahore and Kasur sequentially.

## Supplemental details and individual corrections

V1 uses CSV supplemental files with target column names and a BForm or Roll No key. Main input supports SQLite/CSV/XLSX. For example, place a details file under `normalization_inputs/`:

```json
"enrichment": {
  "sources": [{"path": "../normalization_inputs/student_details.csv", "key": "BForm"}],
  "overrides": "../normalization_inputs/student_overrides.csv"
}
```

Keep identifier columns as text. BForm joins accept the same 13 digits with/without hyphens. Repeated matches produce a conflict instead of arbitrary selection. Roll No matches are scoped to the profile's single exam context.

Override CSV uses Roll No (preferred when present) or BForm as its key. Changing an existing name, DOB or BForm requires `IdentityCorrection=yes`; unmarked conflicting identity values are reported and do not overwrite the board identity. Every correction retains the original source in provenance. Explicit nonidentity values such as measured sizes or actual phone/address override fallbacks.

## Verification and limits

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m unittest discover -s tests -v
```

The regression suite includes collector, normalizer and registration tests, with synthetic identities and intercepted browser submissions. Tests cover schema/text formatting, independent selections, identity preservation, overrides, duplicate conflicts, input changes, idempotence, profile locks and publication rollback. All test identities are synthetic.

SQLite input uses a coherent read-only transaction and fingerprints the selected Saved exam records, so unrelated queue changes do not invalidate normalization. Committed WAL records are included. Other input files are snapshotted and checked against a BISE export manifest when present. Large CSV processing, joins and deduplication use a temporary disk-backed SQLite database; workbooks are written in streaming mode. Original source files and the collector database remain untouched.

Enable `portal.enabled`, set `validation.catalog` to a catalog JSON path and `validation.eligibility` to `{"mode":"catalog"}`. The configured Lahore/Kasur profiles already do this. Catalogs expire after 48 hours by default; stale/missing/hash-mismatched catalogs or a missing declared registration DB fail before publication. Profiles with portal disabled retain completeness-only behavior. The sample is used for exact schema, not as a source of donor personal details or evidence of actual garment fit.

## Refresh both profiles and check age before publication

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize audit --config normalization_profiles/lahore.json && .venv/bin/python -m bise_normalize audit --config normalization_profiles/kasur.json
```

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize run --config normalization_profiles/lahore.json && .venv/bin/python -m bise_normalize run --config normalization_profiles/kasur.json
```

Both commands are repeatable. Audit performs selection/transforms/rule checks without changing published reports or the ledger. Regular runs use `data/normalization.sqlite3`: unchanged rows reuse cached transforms even after the master grows; changed profile/enrichment values invalidate affected transform context. Published per-profile evaluations are indexed by BForm, exam/roll and readiness. Cache and run receipts commit only after successful publication.

Fast published-status lookup:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize status --config normalization_profiles/lahore.json
```

Add `--roll 100001` or `--bform 00000-0123456-1` to filter. These are published snapshot counts; rerun normalization after new uploads to refresh them. Upload states are read through a read-only SQLite transaction, including committed WAL data, never by copying only the DB main file. Registered/external-duplicate, uncertain, active, rejected and unknown states are excluded from Ready. Existing ready rows may remain Ready: the uploader's shared portal/campaign/BForm unique key prevents duplicate jobs/submission.

If the catalog expires, refresh visibly (all applicant POSTs remain blocked), then run again:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize catalog --refresh --output data/youthgames_catalog.json && .venv/bin/python -m bise_normalize run --config normalization_profiles/lahore.json && .venv/bin/python -m bise_normalize run --config normalization_profiles/kasur.json
```

The Secondary DOB interval observed on 2026-10-21 is 2009-01-01 through 2016-12-31 inclusive, cutoff 2026-12-31. The actual captured rule is recorded per row and in `Data/portal-catalog.json`; age for size estimates is a separate calculation. Never change a DOB to fit a category.

## Filtered registration handoff

Use `Ready to Register.csv`, `Data/ready-to-register-provenance.jsonl` and `Data/manifest.json` together. The registration app now supports matching provenance subsets and checks all three against manifest hashes. Its profile must pin the filtered CSV hash and operator confirmation for that exact batch. Refreshing reports alone never imports or submits new applicants. Recheck live rules at upload time; Ready is a preparation snapshot, not a receipt or photo/server-acceptance check.

The existing `registration_profiles/lahore.json` remains pinned to its original 22-row pilot input, preserved with identical hashes under `data/registration_inputs/`. Its six pending applicants and one registered receipt remain available. Newly normalized identities are not silently added to that confirmed pilot batch. The registration ledger/campaign remains shared across future profiles.

## One combined ready file for Lahore and Kasur

`normalization_profiles/combined.json` selects saved LAHORE and KASUR records from the same master using their shared operator settings. Run it to refresh the combined file directly:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize run --config normalization_profiles/combined.json
```

Open `normalized_output/Combined/Ready to Register.csv` (exact 17-column format). Combined provenance is `Data/ready-to-register-provenance.jsonl` beside its manifest. Source district remains in Address/audit. The normalizer reuses cached transforms, applies BForm conflict detection across both selections and checks the shared registration DB. No separate per-profile refresh is necessary to update this combined output.

## Refresh normalization and games input together

Use `.venv/bin/python -m youthgames_register sync` from the project root for the end-to-end local refresh. It runs the combined normalization profile and pins the resulting ready CSV/provenance/manifest into `registration_profiles/master.json`; no manual hash editing is needed. The settings/confirmation come from the configured operator profile; later syncs preserve its edits. Then use the Master profile with the games `prepare` or `submit` command. See [master workflow](YOUTHGAMES_REGISTER.md#ready-master-workflow-recommended).


## 9th: normalize the whole saved master

`normalization_profiles/9th_combined.json` reads committed Part-I / 9th records directly from SQLite and selects every Saved student. Its institution list is empty (no institution filter); it does not inherit the 10th Combined profile's exact LAHORE/KASUR selection. Participation/location/contact/size defaults remain editable in this 9th profile, and board institution labels remain in Address and audit/provenance.

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize run --config normalization_profiles/9th_combined.json
```

Open `normalized_output/9th Combined/Ready to Register.csv` for the exact 17-column registration input. `Normalized.csv` contains all normalized students; `Data/provenance.jsonl` records field issues and `Data/duplicates.csv` identifies records withheld for conflicting BForms. No Excel files are generated. Run the same command after saved 9th database records change; unchanged inputs are verified and return Already up to date without appending duplicates.

Age Eligible checks only the configured category DOB bounds. Ready to Register also requires complete valid fields, no duplicate BForm within the selected source, and no protected registration state. In the checked 30,234-student snapshot, 28,363 are age-eligible; three of those have duplicate BForms, leaving 28,360 ready. The 765 unresolved lookup rows do not contain saved student identities and are excluded.

This command publishes normalization files; the existing default registration sync still targets its own configured normalization profile. No portal submission happens during normalization.
