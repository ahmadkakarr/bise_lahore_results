# Youth Games registration app — inspected implementation plan

Status: V1 implemented; one authorized visible live pilot registered successfully on 21 October 2026. See `YOUTHGAMES_REGISTER.md` for current commands, acceptance evidence and limits. This document retains the original design rationale and read-only inspection findings. Six ready applicants remain unsubmitted; 15 are age-ineligible for the selected category.

## 1. Confirmed operator choices and live findings

- Input: `normalized_output/Lahore/Normalized.csv` plus its provenance and manifest. It contains 22 identities.
- Pilot profile: Schools (Secondary), Open Category, Athletics; Lahore division/district, Shalimar, UC178 Manawan.
- The operator explicitly confirmed the selected shared contact/address, derived gender and estimated-size values for this batch. Record that confirmation against the input fingerprint; retain the original per-field provenance. Do not repeatedly ask for the same confirmation on an unchanged batch.
- Shared image requested by the operator: `pictures/profile_pic.jpeg`. Inspected JPEG, 162,536 bytes, 1106 × 1422 pixels. It is a Youth Games graphic, not an individual portrait. It meets the inspected file-type/size constraints; acceptance of a common graphic as an applicant profile photo has not been established by a submission. Do not describe it as each child's photograph.
- Official page: `https://youthgames.punjab.gov.pk/#registration`; the intended form is `#registration-form`, method POST, multipart action `/applicant`. Scope every operation to that form; other page forms must not be submitted.
- Current Secondary DOB bounds: **2009-01-01 through 2016-12-31**, inclusive, using the published **2026-12-31 cutoff**. Exactly **7/22** rows are inside this frontend range; **15/22** are outside. Server acceptance remains untested. Those 15 are ineligible for the selected category under the inspected rule; do not change DOB or choose a different education category merely to fit age.
- Photo input is required; PNG/JPG/JPEG, maximum **262,144 bytes** (256 KiB).
- Current UC option is **UC No. 178, Manawan Ram Pur Jageer**, observed value `2253`. The shorter normalized label needs one explicit alias, not fuzzy selection of an arbitrary UC.
- Open Category hides the institute field. UC stays required for the school category.
- CAPTCHA is a visible addition challenge (`digit + digit = ?`) with a one/two-digit answer field. First pilot uses assisted human entry. Image OCR is unnecessary for the inspected challenge.
- Final submission goes through a declaration modal. The site states that submitted data cannot subsequently be changed/deleted; preview and reliable receipts matter.

Live IDs are observations, not permanent constants: category 2; Open Category 12; division Lahore 6; district Lahore 21; Shalimar 77; UC178 2253; Athletics 1. Re-resolve labels and parent scope on each catalog inspection; fail on missing/ambiguous matches.

## 2. Separate app and data boundary

Create `youthgames_register`, a separate Python package alongside `bise_results` and `bise_normalize`, using the existing Playwright/Pillow/openpyxl environment. The first release accepts exactly one worker and always launches a visible Chromium window (`headless=False`). No dependency on the user's Brave profile or extension is required.

```text
youthgames_register/
  __main__.py             inspect / prepare / preview / submit / status / export / reconcile
  config.py               strict versioned registration profiles
  catalog.py              current form contract and dependent dropdown resolution
  importer.py             immutable input/provenance/photo snapshots
  validation.py           age, required values, aliases, photos and confirmation checks
  browser.py              one visible worker, form fill and declaration flow
  store.py                durable jobs, attempts, receipts and process lock
  reports.py              current results after each terminal attempt
registration_profiles/
  lahore.json
registration_inputs/
  Lahore/overrides.csv     optional individual corrections/photos
data/youthgames.sqlite3    separate registration state
registration_output/
  Lahore/
    Registration Results.xlsx
    Registered.csv
    Ready.csv
    Ineligible.csv
    Missing Details.xlsx
    Uncertain Submissions.xlsx
    Summary.txt
    Receipts/<roll-or-local-id>/
      receipt.json
      response.html
      result.png
    Data/manifest.json
artifacts/youthgames-inspection/2026-10-21/   current read-only evidence
```

Keep original BISE and normalization files untouched. Never overwrite collection state. Snapshot input, normalized provenance, shared image/individual photo and effective config for each prepared batch. Names, CNICs, photo files and responses remain local; console progress uses local job/roll IDs rather than full identity/contact payloads.

## 3. Form mapping

| Normalized value | Target element | Handling |
|---|---|---|
| Category | `#participation-category` | Resolve exact current label. |
| SubCategory | `#participation-subcategory` | Wait after category; choose Open Category for this profile. |
| Institute | Institution search/manual controls | For Open Category, leave empty; do not fill a hidden stale value. |
| FullName | `#first-name` | This field's visible label is Full name; submit the complete name here. |
| FatherName | `#last-name` | Visible label is Father Name, despite the misleading HTML name. Do not split the student's name into first/last name. |
| BForm | `#cnic` | Convert canonical 13 digits to `12345-1234567-1`; preserve leading zeros. |
| Gender | `#gender` | Choose explicit resolved label/value; retain operator confirmation/provenance. |
| DOB | `#dob` | ISO date; validate current selected category bounds before form submission. |
| Division | `#division` | Select Lahore first. |
| District | `#district` | Wait for and select the correct child of division. |
| Tehsil | `#tehsil` | Wait for and select the correct child of district. |
| UC | `#union-council` | Resolve the configured explicit UC alias within Lahore district. The inspected page populates UC by district, not proof of a tehsil-level membership relationship. |
| Phone | `#mobile-phone` | Format `03211234567` as `0321-1234567`, per the current pattern. |
| Address | `#address` | Use the operator-confirmed supplied value, with original provenance. |
| Sport | `#sport-id` | Athletics must exist after category/subcategory/gender filters. |
| KitSize | `#kit-size-id` | Resolve label; do not confuse text size with option ID. |
| JoggersSize | `#jogger-size-id` | Resolve EU size label to the current option ID. |
| Shared/individual image | `#profile-photo-input` | Attach the snapshotted actual configured file; verify preview and file hash. |
| Assisted answer | `#captcha` | Fresh challenge in the same context; never reuse an earlier student's answer. |

Native select changes must trigger the page's own dependency handlers before any optional Select2 refresh. Prefer option/locator waits and actual DOM state checks; this site's CSP rejects `wait_for_function` string evaluation. The successful read-only inspection used locator waits without disabling CSP.

## 4. Prepare and validate before opening the pilot

1. Import the exact 17-column schema and link it to normalized source provenance. Preserve a canonical immutable applicant snapshot.
2. Verify input/output manifest hashes; refuse a half-published or externally changed normalization snapshot. Register imports idempotently.
3. Refresh and fingerprint the official catalog, required fields, age rules, image constraints and aliases. Identify form drift before any POST.
4. Produce the 7 age-compatible candidates and 15 age-ineligible rows for the current snapshot. Refresh these counts if source/catalog changes.
5. Validate all remaining values and image constraints. Shared image use is an explicit operator selection, not inferred. No placeholder portraits, DOB alterations or arbitrary category changes.
6. Apply existing operator confirmation to this exact batch's supplied defaults. Changes to identity/contact/location/sizes/photo after preparation create a changed draft; do not mutate an already-submitted applicant or silently submit that identity again.
7. Build the preview and queue. `prepare` and `preview` cannot POST `/applicant` or accept the final declaration.

The profile example is `docs/youthgames-profile.example.json`. Actual images and source/provenance are already available; no additional data question is needed to finish this plan. Technical acceptance of the common graphic and the site's response contract must be determined by the first actual pilot.

## 5. First visible pilot: one eligible applicant

1. Select one deterministic, locally valid eligible candidate (`--limit 1`, or explicit roll selection).
2. Open one Chromium browser and a fresh BrowserContext for that applicant. A fresh context prevents profile photo/localStorage, CAPTCHA and previous form values from leaking between students.
3. Load the real form, retain session/CSRF state and resolve dropdowns in dependency order. Fill identity/contact/location/preferences and attach the configured image.
4. Compare all filled values, selected option IDs and attached file against the prepared snapshot. Run the browser's visible validation. Save a pre-submit screenshot and redacted field comparison.
5. In preview mode stop at the filled form. In explicit submit mode, obtain the assisted challenge answer and show the final declaration. The unchanged batch's existing operator confirmation covers its supplied facts; do not request consent again.
6. Persist a submission intent and payload/image hashes **before** dispatch. Install navigation/response capture **before** the final click.
7. Click through the site's own submit/declaration flow once. Do not force `disclaimer_accepted` or bypass frontend eligibility.
8. Capture response body, registration-success indicators and any tracking ID before the transient toast disappears (site code includes a roughly 4.2-second auto-close). Take receipt screenshot and atomically write receipt + database terminal state.
9. Mark registered only on an unambiguous server success/receipt linked to this submitted snapshot. HTTP 200, a completed click or no visible error is insufficient. Save an explicit validation rejection separately.
10. Export results immediately. The first pilot completes only when the receipt/tracking ID and rerun skipping are verified. Do not claim server acceptance, duplicate behavior or shared-image approval from this planning inspection.

## 6. Durable idempotency and unknown outcomes

Registration creates external state; its recovery rules must differ from read-only result collection.

- Unique local applicant key: official portal + explicit campaign/event + canonical student BForm. Do not use profile name, filename or row number as the deduplication key. Changing schools, source filenames or normalization versions must not create an automatic duplicate registration.
- Sport/category are part of the submitted snapshot. Do not assume separate registrations for the same applicant/sport are allowed without verified site rules.
- States: `imported`, `blocked_input`, `ineligible`, `ready`, `filling`, `filled`, `submitting`, `registered`, `rejected`, `already_registered_external`, `uncertain`.
- Confirmed registered keys stay skipped on rerun. Changed data for a registered identity becomes a reported conflict; never silently resubmit it.
- If interrupted before submission intent, return the local draft to ready. If interrupted/timeout/browser loss occurs after possible POST dispatch, mark **uncertain**, never automatically pending/retry.
- The website has not exposed a verified server idempotency key or reconciliation endpoint in this inspection. Therefore exactly-once submission cannot be guaranteed across network ambiguity. The app can prevent automatic repeat POSTs and retain evidence.
- Reconcile uncertain entries using an actual receipt/tracking result or official operator confirmation. Never invent a public lookup API or assume a timeout means failure.
- A website duplicate/already-registered message is distinct from a new successful registration. Retain its exact evidence; do not invent a tracking number.
- Safe bounded retries: form loading and explicit CAPTCHA/validation rejection. Ambiguous responses after a POST are not blind-retryable.
- First pilot stops on uncertainty, portal outage, unexpected declaration, new field requirement or catalog drift. Existing receipts remain durable.
- One collector process holds an exclusive registration DB lock. Per-job transitions and terminal receipts commit before processing the next row.

## 7. Remaining eligible batch and pacing

Once the one-person pilot has a confirmed receipt and a rerun skips it, process the remaining ready candidates using the same one visible worker. Default starting interval is five seconds between submissions plus actual interaction latency; this is an operational setting, not a published website quota. Honor explicit 429/Retry-After and shared backoff. No stealth/proxy rotation or rate-limit evasion.

The initial one-worker visible release is complete. Headless and 1–9 isolated workers are now implemented with per-worker pacing, shared stopping on uncertain outcomes, and synthetic intercepted concurrency regressions. Defaults remain workers=1 and headless=False. See YOUTHGAMES_REGISTER.md for current commands and validation limits; live parallel throughput remains unmeasured.

Publish the current registration workbook after each terminal applicant and on graceful Ctrl-C. A hard crash still leaves committed receipts and recoverable local state; `export` rebuilds user-facing reports without resubmitting anything.

## 8. Commands — implemented in V1

The existing `.venv` already includes the required browser/image/workbook packages. No new package installation is currently necessary.

Inspect the live contract read-only:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register inspect --config registration_profiles/lahore.json
```

Prepare the queue and inspect one filled form without submission:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register prepare --config registration_profiles/lahore.json && .venv/bin/python -m youthgames_register preview --config registration_profiles/lahore.json --limit 1 --workers 1
```

Explicit first live submission, one visible worker:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register submit --config registration_profiles/lahore.json --limit 1 --workers 1 --captcha-mode assist
```

After receipt validation, resume the remaining ready records (0 means all ready, not all imported/ineligible):

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register submit --config registration_profiles/lahore.json --limit 0 --workers 1 --captcha-mode assist
```

Status/export never submit:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register status --config registration_profiles/lahore.json && .venv/bin/python -m youthgames_register export --config registration_profiles/lahore.json
```

If manual challenges dominate later, a strictly parsed arithmetic mode can be assessed separately using the currently visible challenge. It must fail on a changed format and must never bypass or disable the challenge. Assisted mode is the first-pilot default.

## 9. Implementation order and acceptance checks

1. Build the strict profile/import/catalog validator, registration DB and read-only prepare/preview commands.
2. Build the one-worker visible browser flow, field comparisons, shared image handling and assisted CAPTCHA.
3. Implement response/receipt capture, uncertainty-safe state transitions and automatic reports.
4. Test with intercepted local responses and synthetic identities before any real POST.
5. Preview one eligible real row; then run one explicitly invoked live submit and verify its receipt.
6. Demonstrate a repeat command skips the confirmed applicant. Only then process the remaining ready rows.

Required regressions:

- FullName/FatherName mapping despite misleading first_name/last_name HTML names.
- BForm/phone formatting, leading zeros, preserved DOB and inclusive Secondary age boundaries.
- UC alias resolves the correct district-scoped option; ambiguous/missing labels fail without selecting a neighbor.
- Open Category clears/omits stale institute; hidden/required fields follow current contract.
- Photo file/MIME/size/hash checks; explicit common-graphic provenance; context/localStorage isolation between applicants.
- No mutating request in inspect/prepare/preview; declarations only in submit mode.
- Required field/age/photo failures never dispatch a POST.
- Confirmed success persists receipt/tracking and skips every equivalent reimport.
- Already-registered/validation rejection are not new success; response 200 without receipt is not enough.
- Timeout/500/navigation loss after possible dispatch becomes uncertain and cannot auto-retry, including crash windows around receipt writing.
- Ctrl-C before/after intent, report refresh and export-only recovery.
- Database process lock; source/config/photo changes do not cause repeat registration.
- Existing 75 collector/normalization regressions continue to pass. Registration tests use synthetic records and local responses; no test suite silently performs live registrations.

## 10. What is complete now

Complete: live visible inspection, exact form mapping/constraints, location alias discovery, age audit, shared image metadata/hash audit, user values/consent confirmation, durable workflow design, output structure and command specification.

Implementation follow-up completed: V1 app, local regressions, one visible live pilot, current Flasher response parsing, receipt/tracking verification, local reconciliation and registered-row reimport skipping. The pilot sent one applicant POST; no replay was performed. Remaining batch submission, broader non-Open-Category/image-override flows, and manual reconciliation without a captured server receipt are outside the completed pilot. See the current guide for supported behavior.
