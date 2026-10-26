# Lahore 9th: Raiwind administrative allocation

The operator confirmed that equal UC distribution is an administrative assignment allowed by the portal. It is recorded as an allocation, not inferred residence. There are no three intermediate batches: the exact frozen cohort is distributed across 20 UCs.

## Scope and persistence

- Source: Saved SSC 2026 Part-I records whose original board institution field is exactly LAHORE and whose DOB satisfies the Secondary category bounds.
- Initial expected cohort: 18,925 rows. The original collector database is read-only and unchanged.
- Allocation database: `data/allocations.sqlite3`. Tables store the frozen source record, scheme definition/hash, source fingerprint, assignment timestamp and official division/district/tehsil/UC IDs.
- Scheme: `Lahore 9th Raiwind`. Tehsil: Raiwind (76), district Lahore (21), division Lahore (6).
- Stable ascending-roll round-robin allocation gives 947 records to UCs 255–259 and 946 to UCs 260–274. Rerunning never reshuffles, adds or overwrites students. Use a separate scheme for a changed cohort or policy.
- The same BForm can appear on conflicting source rolls. Allocation preserves those source records; normalization checks duplicates across all UCs together and withholds all conflicting records from Ready.

## Exact portal values

| UC | Portal label | Portal ID |
|---|---|---:|
| 255 | UC No. 255, Satto Katia | 2330 |
| 256 | UC No. 256, Ali Raza Abad | 2331 |
| 257 | UC No. 257, Wapda Town | 2332 |
| 258 | UC No. 258, Chung Panj Garan | 2333 |
| 259 | UC No. 259, Azmeer Town | 2334 |
| 260 | UC No. 260, Maraka | 2335 |
| 261 | UC No. 261, Mohnalwal | 2336 |
| 262 | UC No. 262, Shamekv Bhattian | 2337 |
| 263 | UC No. 263, Manga | 2338 |
| 264 | UC No. 264, Sultanke | 2339 |
| 265 | UC No. 265, Manga Ottar | 2340 |
| 266 | UC No. 266, Talab Saray | 2341 |
| 267 | UC No. 267, Manak | 2342 |
| 268 | UC No. 268, Jodhu Dheer | 2343 |
| 269 | UC No. 269, Bhobatian | 2344 |
| 270 | UC No. 270, Ariyan | 2345 |
| 271 | UC No. 271, Jia Bagga | 2346 |
| 272 | UC No. 272, Raiwind (Rural) | 2347 |
| 273 | UC No. 273, Raiwind (Urban) | 2348 |
| 274 | UC No. 274, Babliana Ottar | 2349 |

Labels are resolved by unique UC number inside the official Lahore catalog, not fuzzy name matching. The catalog is refreshed using a browser with applicant POSTs blocked. Before a real submission, the registration adapter reloads the live parent controls and validates selected option IDs and outgoing fields.

## Commands

Create or verify the stable cohort:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate apply --config allocation_profiles/lahore_9th_raiwind.json
```

Normalize this allocation and pin the registration handoff; no form submissions:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate sync --config allocation_profiles/lahore_9th_raiwind.json
```

One real registration per UC, with up to 18 visible worker tabs:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate pilot --config allocation_profiles/lahore_9th_raiwind.json --workers 18
```

The pilot pins one Ready applicant per UC in `allocation_pilots` before dispatch. Repeating the pilot retains the same identities and skips confirmed registrations. It does not silently choose a replacement after an ambiguous or rejected outcome. Existing conservative reconciliation rules remain in force. The broader cohort is prepared locally, but only the pinned 20 rolls are selected for live pilot submission.

Live per-UC assignment and registration totals:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate status --config allocation_profiles/lahore_9th_raiwind.json
```

Append `--roll ROLL_NUMBER` for that student's assigned UC, actual submitted location, state, tracking ID and receipt path. Registration links use the global portal/campaign/BForm key, not roll alone, so prior uploads stay protected across profiles and exams. A prior registration in another UC is reported as registered elsewhere and is not resubmitted or relabeled.

After reviewing a successful pilot, a separate explicit command can submit the remaining prepared cohort:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register submit --config registration_profiles/lahore_9th_raiwind.json --limit 0 --workers 18 --captcha-mode auto
```

## Data ownership and output

The remaining never-dispatched cohort can also receive stable, approved gender-wise
sport ratios without changing its UC assignment. See [sport allocation and resume](SPORT_ALLOCATION.md).

`data/allocations.sqlite3` is authoritative for assignment. Its `jobs` view exposes the frozen, allocated cohort to the existing normalizer. Explicit `source.allocation_scheme` scopes the view so schemes cannot mix.

`data/normalization.sqlite3` retains normalized identities and readiness evaluations. `data/youthgames.sqlite3` is authoritative for submission state, `jobs.tracking`, `jobs.receipt` and durable intent/dispatch attempts. The status command joins these identities to the allocation without duplicating tracking state into a stale second table.

Existing normalization/registration profiles remain separate. The allocation uses `normalization_profiles/lahore_9th_raiwind.json`, `registration_profiles/lahore_9th_raiwind.json` and their own output folders. Normalization and registration keep Excel disabled. CSV/provenance files remain the existing pinned handoff contract; registration no longer rebuilds large reports after every applicant by default. Original receipt evidence is retained.

The pilot report is `registration_output/Lahore 9th Raiwind/UC Pilot Verification.json`. It verifies one record per UC, confirmed state, saved tracking, exact location match and receipt hashes. It lives outside the normalization publication so it does not invalidate normalization fingerprints.

## Verified live outcome: 21 October 2026

All 18,925 records were allocated, with 18,919 initially ready and six duplicate-BForm records withheld across three conflicting pairs. The 20 pinned pilot identities each received a confirmed server registration and a saved tracking ID. Their receipt hashes, payload bindings, selected UC/tehsil IDs and unique dispatch counts were independently checked. All 996 pre-existing registration jobs, including 39 confirmed registrations, were unchanged.

The initial 18-worker round completed 12 registrations; six page GETs timed out waiting for unrelated page resources and two other jobs stopped before dispatch. The form loader now waits for DOM content and validates the live form and options. A regression uses a hanging image to verify this behavior. Only the same pinned identities with a bound valid receipt and no intent/dispatch were eligible for GET-timeout recovery; the remaining eight then registered. No ambiguous POST was retried.

After refreshing the handoff, 18,899 students remain Ready, 20 are registered and six are blocked for duplicate identities. Repeating the pilot produced zero attempts and zero new POSTs. The full regression suite passed 199 tests; the final focused suite passed 18. Live status for all 18,925 rows uses the indexed exam/roll normalization key and was measured at approximately 0.7–1.3 seconds. Full aggregate evidence: `docs/evidence/raiwind-allocation-verification.json`.

## Direct HTTPS: one additional pilot per UC

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate http-pilot --config allocation_profiles/lahore_9th_raiwind.json
```

This command uses Python urllib HTTPS and never launches a browser, including job preparation. It pins a second, separate set of 20 Ready identities in `allocation_http_pilots`, excluding the browser-pilot identities. Exactly one applicant per UC is selected; the HTTP pilot profile and its small immutable input snapshot keep this selection stable as the main ready file changes.

Each applicant receives a fresh cookie session and form GET. Live UC/parent IDs, category/DOB rules, confirmation, exact fields and image bytes are checked before one multipart POST. Durable intent/dispatch, tracking and response/request-proof receipts use the existing registration database. Requests run sequentially with the configured spacing. A rejection or ambiguous outcome stops the pilot; ambiguous POSTs are never replayed. A repeat after success sends zero further applicant requests.

The report is `registration_output/Lahore 9th Raiwind HTTP Pilot/HTTP UC Pilot Verification.json`. Its 20 registrations are additional to the earlier browser pilot. Assignment counts stay fixed; the shared status command shows registrations across both transports.

Verified HTTP outcome on 21 October 2026: all 20 UCs received one additional successful HTTP registration, each with a distinct tracking ID, valid response/proof hashes, correct UC/tehsil IDs and exactly one dispatch. The 59 registrations that existed before this test were unchanged. The allocation now has 40 registered, 18,879 ready and six duplicate-conflict records blocked. The repeated HTTP command made no additional submission attempts or POSTs. Full regressions: 202 passed; final focused HTTP/allocation checks: 22 passed. Aggregate evidence: `docs/evidence/raiwind-http-uc-pilot-verification.json`.

For concurrent direct HTTP processing of the remaining Ready students, use [the HTTPX/Rich bulk command](HTTP_CONCURRENT.md). The one-per-UC HTTP pilot retains its separate pinned identities.
