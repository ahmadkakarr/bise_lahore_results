# Named-school Private normalization: Raiwind and Model Town

These commands only allocate/normalize. They never submit forms, acquire the registration writer lock, alter OS routes, or update existing registration jobs. The registration adapter also supports the portal's explicit Private manual institute entry. It sends the complete original Institute name and leaves institution_id blank, matching the offered manual-entry UI.

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate private-normalize --config allocation_profiles/lahore_private_9th_split.json --group raiwind
```

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate private-normalize --config allocation_profiles/lahore_private_9th_split.json --group model_town
```

Both commands may be rerun in either order. The first invocation reserves one common immutable cohort with a cap of 25,000 unique eligible, unused identities. A fixed seeded order selects up to 10,000 for Raiwind, then up to 15,000 for Model Town. The split is frozen before either group is normalized, so running the second command after the first cannot select the same students. If fewer than 25,000 qualify, Model Town receives the remainder and the shortage is reported. No public/review/duplicate candidates are substituted. Fewer than 10,000 candidates fails before cohort creation.

Source scope is BISE Lahore SSC Part-I Annual 2026, saved/verified records only. A named institute must end with Lahore or Lahore Cantt; a mention of Lahore in an address such as Lahore Road, Kasur is insufficient. Government names and public/special-ownership indicators (for example Divisional Public, Workers Welfare, Army Public, Garrison) are excluded or held for ownership review. This is a conservative candidate selector, not independent proof of every school's private ownership. The exported `Private` label is the operator-requested classification. Raw board institution names are preserved exactly in `Institute`; names or institute identities are never invented.

Candidates need valid DOB, current Secondary age eligibility, canonicalizable student/father names and a 13-digit BForm. Duplicate candidate identity groups are excluded in full. Already allocated, normalized or imported identities are skipped, regardless of their prior registration state. Source and registration databases are read-only. Pool membership, source JSON, source fingerprint and assignments are retained in SQLite. New source results do not reshuffle or append to this batch; a different cohort requires a new plan/batch and distinct scheme/profile names.

Raiwind uses the existing 20 UC numbers 255–274. Model Town uses the operator-supplied 52 numbers: 84–88, 198–241 and 255–257. The portal has district-wide UC choices, so this records an explicit administrative allocation, not inferred residence or an asserted geographic boundary. Exact portal names/IDs are resolved by number, even where supplied names differ. Both groups distribute records evenly across their own UC list, with counts differing by at most one. The already-approved female/male sports ratios are applied using stable immutable sports assignments; boys-only sports are excluded for girls.

Outputs:

- `normalized_output/Lahore Private 9th/Raiwind/Ready to Register.csv`
- `normalized_output/Lahore Private 9th/Model Town/Ready to Register.csv`
- Each group also has `Normalized.csv`, eligibility/blocked reports, provenance and an artifact-hash manifest.
- `normalized_output/Lahore Private 9th/Allocation Summary.json` records the common pool, group sizes and exact target UC IDs/names.
- Generated allocation/normalization profiles remain separate from the existing Open Category cohort. No Excel is produced.

On rerun, pool membership and sports stay unchanged. The normalizer verifies published hashes and reuses its cache. Unrelated registrations in the existing Open Category run no longer invalidate a frozen allocation's exports; updates for its own applicants still refresh ready/already-registered subsets. An expired portal catalog is refreshed with one verified HTTPS GET. A changed catalog/invalid UC fails clearly rather than inventing a replacement. Generated profiles are published atomically without overwriting operator edits.

The shared plan is `allocation_profiles/lahore_private_9th_split.json`. Existing frozen definitions reject semantic edits; preserve them and create a new plan for a different cohort.

An explicit live pilot is limited to three immutable students per group, from distinct UCs/institutes. These commands send actual portal registrations using the shared registration ledger, verified HTTPS, and six-second spacing per configured connection:

```sh
.venv/bin/python -m bise_allocate private-pilot --config allocation_profiles/lahore_private_9th_raiwind.json --connections connection_profiles/wifi-usb.json --json
.venv/bin/python -m bise_allocate private-pilot --config allocation_profiles/lahore_private_9th_model_town.json --connections connection_profiles/wifi-usb.json --json
```

Pilot inputs and profile hashes are pinned in allocations.sqlite3/private_registration_pilots. A rerun reuses the same three students, verifies saved tracking IDs and hashed receipts, and skips confirmed registrations. An unknown/rejected pilot is held for inspection; it never substitutes another student or automatically replays the unknown POST. Full-batch ready files remain separate. Run private-normalize again to refresh their ready/already-registered subsets after a successful pilot.

### Model Town: switch the remaining registrations to Open Category

The named-school source pool remains frozen. An explicit participation override can change the remaining unposted Model Town records to `Schools (Secondary)` / `Open Category` without selecting a new cohort or changing UC/sport allocations:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m bise_allocate participation --config allocation_profiles/lahore_private_9th_model_town.json --subcategory 'Open Category' --json
```

This idempotent command requires the registration run to be stopped. It acquires the normal registration writer lock, backs up the allocation/registration DBs and profiles, and appends an immutable `participation_changes` audit record in allocations.sqlite3. It regenerates the ready master, synchronizes the existing registration profile, and prepares its pending SQLite jobs with a verified form GET and **zero applicant POSTs**. All other normalized student fields must match their prior prepared values. Confirmed receipts, tracking IDs and submitted payloads are preserved; historical roll-specific rules also retain their original subcategory in reports. Unresolved nonterminal records with prior submission intent cannot be silently reclassified.

The existing `lahore_private_9th_model_town.json` filename and allocation scheme identify the original named-school source cohort. They remain valid for the ordinary `http-submit` command: its effective registration subcategory is now driven by the audited override. Repeating `private-normalize --group model_town` honors that override instead of restoring Private. The Raiwind group's participation is unchanged. A new participation override invalidates the pinned resume cache, so stale category input cannot be reused.
