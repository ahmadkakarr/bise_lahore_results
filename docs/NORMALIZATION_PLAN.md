# BISE master to sample-format normalization — proposed design

Status: V1 implemented and run for the operator's Lahore profile. See `NORMALIZATION.md` for actual supported commands and settings. This document retains the design rationale. Live catalog/eligibility checks remain future work and are rejected explicitly; V1 supplemental/override inputs are CSV, while main input supports CSV/XLSX.

## 1. Inspected inputs

- Template: `sample/Raiwind_1336_UC273_Urban_Automator_Clean_Without_53_Rejected.csv`.
- Template contains 1,336 rows and these 17 columns, in this exact order:
  `Category, SubCategory, Institute, FullName, FatherName, BForm, Gender, DOB, Division, District, Tehsil, UC, Phone, Address, Sport, KitSize, JoggersSize`.
- Template BForm values contain 13 digits, DOB uses `YYYY-MM-DD`, and Phone uses 11 digits. All template fields are populated. Template values are examples, not defaults for new students.
- Template Category/SubCategory are `Schools (Middle)` / `Private`. The supplied screenshot shows `Schools (Secondary)` / `Open Category`. The operator will set Category/SubCategory per profile; neither pair is silently copied from the sample.
- Inspected published master snapshot: 1,026 rows, of which 1,015 contain saved student identities; 11 contain lookup issues. The collector may publish more rows later.
- The master has identity, DOB, BForm, father CNIC, marks and the board's Institution/District. It has no Gender, Phone, Address, Sport, KitSize or JoggersSize columns.
- Actual board Institution/District values in this snapshot include district-only text. This is not sufficient to reconstruct a school name, home address, tehsil or UC.
- CSV has no sheets. The corresponding Excel workbook has `All Students`, institution tabs, `Overview`, `Subject Marks` and `Missing Results`. Only student tables are valid normalization sources.
- A read-only audit of all 1,015 saved card HTML files found no Gender/Sex field IDs or Gender/Sex words in visible text. Missing gender in these cards is not a demonstrated parser omission.
- Comparing the requested odd-last-digit = Male / even-last-digit = Female rule to the sample's explicit Gender values yielded 1,165 matches and 171 mismatches out of 1,336. This does not establish whether the sample gender or identifier is wrong; it establishes that parity cannot be treated as independently confirmed gender for this workflow. No student-level identities were printed or changed during the audit.
- An independent second calculation using membership in `13579` rather than modulo reproduced the same result: 918 rows marked Male end in 1; 247 rows marked Female end in 2; 171 rows marked Female end in 1. The user-requested mapping is therefore unambiguous, while the sample's explicit labels conflict in 171 rows. Names are not independent confirmation of those students' gender.

## 2. Application boundary

Create a separate Python package `bise_normalize`, next to `bise_results`, using the existing virtual environment. It has its own CLI, profiles, tests, state and output. It does not invoke Playwright or run collection. Normalization reads published CSV/XLSX; it never changes the BISE master, collection database, original card or collector status.

CSV master is the recommended input. XLSX is supported when sheet or cell-range selection is useful. Renamed `master.csv` files also work: headers, not the filename, define the input contract.

```text
bise_results/                 existing collector
bise_normalize/               proposed separate offline application
  __main__.py                 inspect / init / validate / preview / run
  config.py                   strict, versioned JSON configuration
  sources.py                  stable CSV/XLSX snapshots and selection
  transform.py                field mapping and deterministic conversions
  enrich.py                   optional keyed supplemental data
  validate.py                 completeness, duplicates and conflicts
  publish.py                  deterministic artifacts and atomic replacement
normalization_profiles/
  raiwind.json                operator-editable settings
  kasur.json                  a separate selection and settings profile
normalization_inputs/
  student_details.csv         optional operator-supplied missing information
  student_overrides.csv       optional individual corrections
normalized_output/            separate from output/
  Raiwind UC273/
    Normalized.csv            exact 17-column contract, including partial rows
    Normalized.xlsx           easy human inspection
    Complete Records.csv      complete against this profile's declared rules
    Missing Details.xlsx      one student per row, with missing/conflicting fields
    Summary.txt
    Data/
      manifest.json           fingerprints, counts and artifact hashes
      provenance.jsonl        per-row source and field-level origins
      excluded.csv            skipped source rows and reasons
      duplicates.csv          duplicate keys and conflicting identity evidence
.normalization/               internal locks and previous managed outputs
tests/normalization/           independent fixture-based regressions
```

The profile owns one predictable output folder. CSV is UTF-8 with BOM for Excel compatibility; identity values remain strings. Excel explicitly formats CNIC/BForm, phone and identifiers as text. The 17-column target export does not gain Roll No, Father CNIC, status or audit columns. Those are retained in provenance and the inspection workbook.

## 3. Mapping contract

| Target | Source / operator setting | Rule |
|---|---|---|
| Category | JSON setting | Required explicit setting; do not copy sample category automatically. |
| SubCategory | JSON setting with operator-authorized fallback | Use profile choice when an actual institute is present; when it is absent, set `Open Category`. Category itself remains the profile's choice. |
| Institute | Explicit mapping/operator setting or a board value identified as an actual school | Preserve the full original board Institution/District in provenance. District-only or unresolved labels do not become school names. Missing Institute is blank and not required for Open Category. |
| FullName | Student Name | Preserve spelling and case; normalize surrounding/repeated whitespace only. |
| FatherName | Father Name | Same handling as FullName. |
| BForm | Student CNIC / B-Form | Remove permitted formatting separators; require exactly 13 digits for a valid value. Keep original in provenance. Never substitute Father CNIC. |
| Gender | Explicit board/confirmed value first; operator-requested student-ID parity fallback | When explicit gender is absent, the proposed draft export uses odd last digit = Male, even = Female on a valid 13-digit student BForm. Record the origin as a user-requested derived mapping, not board/independently verified gender. Never use Father CNIC or names. |
| DOB | Date of Birth | Interpret known BISE `DD/MM/YYYY` as day-first; export ISO `YYYY-MM-DD`. Excel date cells supported. Preserve actual birth date; invalid values remain an issue with their raw text retained. |
| Division | Profile or explicit source mapping | Board district is not proof of a student's residence. No implicit geographical assignment. |
| District | Profile or explicit source mapping | Keep configured residence separately from board Institution/District. |
| Tehsil | Profile or supplemental data | No district-to-tehsil guessing. |
| UC | Profile or supplemental data | Preserve selected display label; optional catalog validates hierarchy. |
| Phone | Existing explicit contact, otherwise operator-provided shared fallback | Fill a missing phone with `03211234567`; retain real supplied contacts. Mark fallback as a shared operator contact, not an independently verified personal number. Preserve leading zero. |
| Address | Existing explicit address, otherwise original board Institution/District | The operator requested this source label as the fallback address. Copy its full text even when target Institute is blank under Open Category. Mark it as a board-label address, not an independently verified home/postal address. |
| Sport | Profile, explicitly defined rule or supplemental data | No random assignment or sample-derived gender rule. |
| KitSize | Explicit value first; configurable age/gender lookup fallback | Apply the proposed profile table only when missing and age/gender are usable. Mark as an estimated size, not a measurement. |
| JoggersSize | Explicit value first; configurable age/gender lookup fallback | Same estimate policy; output a string and specify EU sizing in the profile. Do not present estimates as actual fit. |

Template is used for headers/order/format only. It does not serve as a donor dataset for phone numbers, locations, schools or preferences. Father CNIC and registration/roll identifiers remain available in audit/enrichment matching even though the target sample lacks their columns.

## 4. Selection

Default selection is the CSV master, all data rows, `Collection Status = Saved` only. Missing marks or a failed academic result do not invalidate an otherwise saved identity. Filtering PASS/FAIL/ABSENT is optional and explicit.

Support these composable selections; when combined, filters use AND:

- CSV: logical row intervals such as `[2, 500]`, with header row numbered 1. These refer to CSV records, not physical lines in quoted multi-line CSV.
- XLSX: a specific sheet, header row and optional A1 range; omitted columns required by the mapping are a configuration error. No workbook-wide scan across duplicate student tabs.
- One or several full board Institution/District values. Matching can ignore case/outer whitespace, but stored originals remain unchanged. Exact match is the default; substring/regex needs explicit opt-in.
- Explicit roll lists or inclusive roll bounds; roll identifiers stay strings.
- Optional saved-state/result-status filters and deterministic row limit, applied after selection and before enrichment.

`inspect` lists headers, valid student sheets, counts and available institution labels. `preview` reports row numbers and field issues before publication. Selecting an empty/nonexistent sheet, bad range or unknown field produces an actionable error; an empty selection does not silently replace the previous successful output.

## 5. Configuration and enrichment

Each profile is strict JSON with a schema version, source path, selection, mappings, explicit settings, optional enrichment paths, validation and output settings. Relative paths resolve against the config file, never the shell's working directory. Unknown/misspelled keys fail validation.

Use explicit settings for facts that really apply to the entire selected group. For different schools/locations, use narrow conditional rules or separate profiles. Rules run on a documented scope such as exact board label + roll list; matching two contradictory rules produces an issue rather than silent last-writer-wins behavior.

Field precedence:

1. Individually keyed operator override.
2. Supplemental student data keyed by a verified BForm, or explicit Roll No + exam context when BForm is unavailable.
3. Unambiguous scoped profile rule.
4. Explicit profile default for that field.

BISE identity fields come from the master by default. Conflicting supplemental name/BForm/DOB does not overwrite authoritative identity unless an individual correction is explicitly marked; every correction records original/new value and origin. Student BForm is never matched against Father CNIC implicitly. Multiple enrichment matches are reported, not resolved arbitrarily. Leading-zero values, missing keys and duplicate external rows get regression coverage.

The operator clarified automatic fallback values: Address = original student's board Institution/District; missing Phone = `03211234567`; missing sizes = deterministic generalization by age/gender. Keep individual overrides available. Sport remains an explicit editable profile value because no requested sport has yet been supplied. No random assignment is needed.

Missing-institute policy is an explicit operator decision, not heuristic category classification: first resolve actual school names from mappings/settings or a board label explicitly classified as a school. District-only values are retained as source facts. When actual school is absent, emit blank Institute + `Open Category`; conditional validation must not count Institute as missing in that case. If the selected Category is incompatible with Open Category in an optional current catalog, report the incompatibility rather than altering Category silently.

Gender handling has separate provenance values such as `board_explicit`, `operator_confirmed` and `student_bform_parity_unverified`. Known explicit/confirmed values take precedence over the user-requested parity fallback. Validate a complete 13-digit student identifier before computing the mapping; masked/invalid/blank IDs produce no value. Preserve explicit gender values outside Male/Female rather than reducing them through parity. If a supplied explicit value conflicts with parity, preserve the explicit value and expose the conflict. The target CSV remains the exact 17-column draft; origins/quality flags are stored in the workbook and provenance, not inserted into the CSV schema.

### Proposed deterministic size generalization

This is an editable operator heuristic for draft preparation, not a fitted or medically/anthropometrically validated size model. The sample contains sizes but does not establish a dependable sizing formula. Prefer measured/operator-supplied sizes whenever present. Age means completed years on profile `as_of_date`; use a fixed date so an unchanged rerun tomorrow does not change output.

| Age | Male kit / EU joggers | Female kit / EU joggers |
|---|---|---|
| 8–11 | Small / 36 | Small / 35 |
| 12–14 | Medium / 39 | Medium / 37 |
| 15–17 | Large / 41 | Medium / 38 |
| 18–100 | Large / 42 | Medium / 39 |

All values and bounds live in JSON and are proposed starting estimates. Missing/invalid DOB or gender, unhandled explicit gender and ages outside the configured range leave size blank with a specific issue. A gap/overlap in configured size rules fails validation. Profile `as_of_date` initially uses `2026-10-20`; it is separate from any later sport eligibility cutoff. Estimated sizes do not imply that a student's identity needs BISE review.

Optional location/value catalog JSON can validate Category/SubCategory, Division/District/Tehsil/UC and sports. A catalog must include its source/date. Sample values and a screenshot alone do not prove current live dropdown options or eligibility. Offline normalization can run without a catalog and say `live_rules_not_checked`.

## 6. Missing information and duplicates

- A saved identity remains in `Normalized.csv` when optional target fields are missing. This does not send it back to BISE collection review and does not fetch the result again.
- `Missing Details.xlsx` includes only normalization issues, with actionable reasons such as `missing_phone`, `missing_school_name`, `invalid_bform` or `enrichment_conflict`.
- `Complete Records.csv` means complete against the declared profile rules. It is not a claim of successful registration or current website eligibility.
- Configured fallback gender/contact/address and estimated sizes can satisfy mechanical completeness while retaining their provenance flags. The summary and workbook separately count estimated/shared/derived fields; `Complete Records` never means independently verified personal facts or measured sizes.
- Institute is conditionally required only outside `Open Category`; a blank Institute under the authorized Open Category fallback is not a missing-details issue.
- Default minimum input identity is a saved source row with its Roll No and student name. Invalid source rows are logged in `excluded.csv`, with source lineage.
- An invalid/masked BForm is not repaired or fabricated. A partial normalized row retains other fields, leaves the invalid target value blank and points to its raw source in the inspection/audit artifacts.
- Repeat appearances of the same exam + roll + course are one source identity. Identical repeated rows collapse deterministically, with all source locations retained.
- Different rolls sharing the same valid BForm are a target collision: all associated rows remain visible in the inspection/audit output, and none enters `Complete Records.csv` until explicitly resolved. Do not arbitrarily choose one or discard source facts.
- Output ordering is stable by exam and roll. The requested gender/size fallback rules are deterministic and versioned; DOB is preserved and Sport is configured. No random assignments.
- DOB is never shifted to fit a category. Optional age-eligibility filtering needs an explicit dated rule/cutoff; excluded rows receive their actual DOB and reason in audit. No live age rules are assumed in this plan.

## 7. Idempotency and collector coexistence

Take one coherent read-only input snapshot. For published BISE output, use its manifest hashes to check the selected master snapshot; if the collector publishes a replacement mid-read, retry a bounded number of times or report that input changed. A copied standalone master uses stable read/hash checks. Never hold the collector's database lock or read a half-written spreadsheet.

Compute a fingerprint from source bytes, selected sheet/ranges, effective configuration, enrichment/catalog bytes, exact target schema and normalizer version. Key order and whitespace in equivalent JSON must not produce different results. Runtime timestamps and absolute output paths must not invalidate identical content.

Same fingerprint + valid output hashes: print `Already up to date` and change no files/mtimes. New source rows or edited settings: recompute from the selected snapshot and replace the managed profile output atomically; never append duplicate rows. A changed source also requires reevaluating existing rows, because updates may affect prior values. Rebuilding local files is separate from fetching BISE results.

Use streaming CSV/read-only XLSX and disk-backed joins/deduplication where necessary. Source and per-row transform hashes can reuse unchanged transformations without compromising complete revalidation of selection/conflicts. A per-profile lock prevents competing normalizers; different profiles can run independently.

Build into a staging directory. Preserve the previous managed output outside the friendly output tree, verify file inventories, then publish. Failures restore the previous report. If a config points at an unmanaged existing folder, do not overwrite it. Missing/edited managed artifacts force a verified rebuild. Human edits belong in an override/enrichment file, not generated Normalized.csv.

## 8. Commands — implemented in V1

Use the existing `.venv`; no new browser installation is needed. Initial implementation should rely on stdlib + existing openpyxl.

Inspect the master and sample contract:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize inspect --source "output/Matric 2026 - Part-II Annual/Matric 2026 - Master Results.csv" --template "sample/Raiwind_1336_UC273_Urban_Automator_Clean_Without_53_Rejected.csv"
```

Create a profile once; rerunning init must not overwrite an edited profile:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize init --profile raiwind --source "output/Matric 2026 - Part-II Annual/Matric 2026 - Master Results.csv" --template "sample/Raiwind_1336_UC273_Urban_Automator_Clean_Without_53_Rejected.csv"
```

Validate and inspect a bounded preview:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize validate --config normalization_profiles/raiwind.json && .venv/bin/python -m bise_normalize preview --config normalization_profiles/raiwind.json --limit 20
```

The repeatable operator command after profile setup:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_normalize run --config normalization_profiles/raiwind.json
```

`run` prints selected/exported/complete/missing/excluded/duplicate counts, source fingerprint and exact output file path. A partial-data export is a successful transform with an explicit partial summary. Invalid config, source corruption and failed publication are errors. CLI selection overrides, if supported, must be recorded in the effective config and fingerprint.

## 9. Implementation sequence and acceptance

1. Lock schema and operator decisions; implement configuration validation and `inspect`/`init`.
2. Implement snapshot readers, CSV/XLSX selection and exact identity/DOB/BForm mapping.
3. Add explicit settings, keyed enrichment, corrections and conflict tracking.
4. Produce target CSV/workbook, completeness workbook, provenance and stable publication.
5. Add repeatable CLI, concise documentation and fixture-based regression coverage.
6. Run a bounded local normalization pilot, reconcile selected source rows against output and demonstrate two identical runs leave hashes/mtimes unchanged.

Required regressions:

- Exact 17-column order; header-only sample schema support; no accidental father-CNIC substitution.
- Day-first DOB to ISO, leap days, invalid dates, Excel dates and leading zeros.
- Full school names preserved despite short/sanitized folder/tab names; district text is not inferred to be a school or residence.
- Missing actual Institute forces SubCategory `Open Category`, preserves profile Category, and does not trigger missing-Institute validation. An actual school retains its complete name and the chosen SubCategory.
- Explicit board/confirmed gender beats any parity suggestion; odd/even suggestions use only a valid student's ID, never Father's CNIC, and remain marked unverified. Conflicts, masked IDs and nonbinary explicit values are preserved correctly. Add synthetic fixtures rather than copying student PII into tests.
- Missing phone uses the exact shared string `03211234567`; existing contact survives. Missing Address copies the full original board label even when Institute is blank. Neither fallback changes the source.
- Fixed-date completed-age calculation handles birthdays and leap-day policy explicitly. Size rule boundaries, missing DOB/gender, unsupported values, explicit size overrides, gaps/overlaps and unchanged-rerun estimates have regressions.
- Saved records with missing marks remain exportable; source lookup failures are excluded explicitly.
- CSV row bounds and XLSX sheet/header/range selection; no duplicate import across workbook tabs.
- Profile defaults, scoped-rule conflicts, explicit identity correction and keyed enrichment collisions.
- Duplicate source identities and duplicate target BForms; partial rows retained and complete rows withheld only for actual configured issues.
- Idempotent init/run, input/config/enrichment changes, broken output repair, interrupted publish and unmanaged-folder protection.
- Read while collector publishes; preserve original source hashes and existing collector behavior.
- Regression suite for bise_results continues to pass; normalization tests run without board/browser traffic.

## 10. Decisions requested before implementation

Confirmed decisions:

- Category/SubCategory are set by the operator in each profile.
- Actual school name missing: use `Open Category` and blank/non-required Institute.
- Missing Address uses the student's original board Institution/District text.
- Missing Phone uses the operator-supplied shared number `03211234567`.
- Missing sizes use configurable age/gender generalization, labeled estimated; individual explicit values take priority.
- The operator requested the odd/even student-ID mapping. It is included as a draft fallback with derived provenance; sample conflicts are retained as evidence rather than called verified gender.

Further profile setup needs:

- Initial source selection: all saved students, named institution/district, row bounds, or XLSX sheet/range.
- The actual Division/District/Tehsil/UC and school name for that selected group, if applicable. Do not inherit Raiwind UC273 from the sample automatically.
- The intended Sport value; size heuristic values and fixed age-reference date remain editable settings.
- Whether the output only needs sample-format normalization or also dated eligibility validation. Recommended first version: offline normalization + explicitly configured completeness checks.

Design defaults: CSV master, saved identities selected by profile, profile-selected Category, Open Category when actual Institute is absent, original board label as missing Address, shared phone when missing, requested student-ID parity as a derived fallback, deterministic estimated sizes, exact sample schema, separate partial/complete views, no live eligibility claim. Missing profile/location/Sport settings are visible in `validate` and `preview`. The operator subsequently authorized implementation and running the Lahore profile; the actual settings and commands are documented in `NORMALIZATION.md`.
