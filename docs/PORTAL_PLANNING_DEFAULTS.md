# Schools (Secondary) → Private: portal-aware planning defaults

Status: additive feature branch; **not deployed** and **not connected to the live dashboard editor**. No applicant POST is performed by this module.

## Grounded inputs

- Official, integrity-checked portal catalog snapshot and editable versioned sports profile are required.
- Category: `Schools (Secondary)`; subcategory: `Private`.
- The portal supplies `ucs_by_district` but no UC-to-tehsil parent relationship.
- The captured Lahore snapshot contained 291 district UCs. The application computes the current count from a verified snapshot; it does not hardcode 291 as immutable truth.
- Secondary options are Athletics, Badminton, Cricket (Hard Ball), Football, Kabaddi (Boys Only), and Tug of War, subject to live catalog revalidation.
- Kabaddi (Boys Only) is excluded from female suggestions, with a positive female weight rejected.
- Existing `sports_profiles/lahore_private_9th_raiwind.json` and `sports_profiles/lahore_private_9th_model_town.json` provide prior operator-approved planning ratios. **Neither is a portal gender rule or an official participation-prevalence study.**

## Pure API for the editor

```python
import json
from bise_normalize.planning_defaults import (
    build_defaults, search_ucs, select_all_ucs, update_uc_selection,
)
from youthgames_shared.catalog import load

snapshot = load("data/youthgames_catalog.json", max_age_hours=48)
policy = json.load(open("sports_profiles/lahore_private_9th_raiwind.json"))
draft = build_defaults(snapshot, policy, tehsil="Shalimar")
# draft["selected_uc_ids"] defaults to all currently cataloged Lahore district UCs.
# This does NOT claim any UC officially belongs to Shalimar.

matches = search_ucs(draft, "UC No. 178")
draft = update_uc_selection(draft, [
    uid for uid in draft["selected_uc_ids"] if uid != matches[0]["id"]
])
draft = select_all_ucs(draft)
```

No source database writes, registration database writes, applicant preparation, or POSTs occur here.

## Editor wiring and safe state transitions

1. When a **new profile** selects category, subcategory, division and district, call `build_defaults`. Default-select every UC returned for the district. Display a descriptive notice that the district-wide UC list has **no verified tehsil membership**.
2. Show a searchable checkbox list, selected count, **Select All** and per-UC toggle. Search only changes displayed options, not selection. Call `update_uc_selection` on toggles. If none remain, `uc_required_unmet` is true and applying a profile must be blocked.
3. Selecting or switching **tehsil** must not filter the district's UC options. A named tehsil alone is insufficient location evidence for any individual UC. Do not set a misleading “Shalimar UCs” heading.
4. Keep manual deselections on ordinary editor rerender. Rebuild only on an explicit reset or a genuine new category/division/district context; do not silently restore all 291 after each click.
5. Render female and male sports with the supplied editable weights. Both sets must total 100% independently; unknown and boys-only female selections fail validation. Keep these clearly labeled as administrative planning suggestions.
6. A save/apply command must be separately authorized, create an audited immutable revision and preview affected **unposted** candidates. The draft itself is `planning_only=True`, `applicant_posts=0`; do not submit or reclassify any already prepared, dispatched, uncertain, registered or frozen-allocation identity.
7. The draft exposes `catalog_sha256`, `catalog_fetched_at` and `catalog_refresh_required`; verify a fresh official catalog and revalidate saved option IDs before applying. A changed catalog should cause explicit review, not silent remapping or inferred replacements.

## Regression checks

`python -m unittest tests.test_portal_planning_defaults -v`

Test coverage includes 291 district-wide defaults under named Shalimar, search, individual deselection, Select All, empty selection, female Kabaddi exclusion, percentage validation, invalid portal combo, tampered catalog, catalog staleness and switching district.

**Not yet validated:** running this test command in the actual project runtime, wiring the module into the authenticated profile editor, applying a persisted audited revision, and end-to-end UI confirmation. These remain separate acceptance steps. Existing Raiwind/Model Town immutable allocations and historical registration receipts must not be modified.
