"""Read-only, portal-grounded defaults for a normalization profile editor.

This module creates *planning drafts*, not registration allocations or POSTs.
No UC-to-tehsil relationship is inferred from a district-wide portal list.
"""

from copy import deepcopy
from datetime import datetime, timezone

from youthgames_shared.catalog import PORTAL, exact, signature


def _verified_snapshot(snapshot):
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != 1
        or snapshot.get("source_url") != PORTAL + "/#registration"
        or not isinstance(snapshot.get("data"), dict)
        or signature(snapshot["data"]) != snapshot.get("content_sha256")
    ):
        raise ValueError("Missing or unverified official portal catalog snapshot")
    return snapshot["data"]


def _unique_options(options, field):
    if not isinstance(options, list):
        raise ValueError("Invalid portal options: " + field)
    ids = [str(option["id"]) for option in options]
    if len(ids) != len(set(ids)) or not all(ids):
        raise ValueError("Duplicate or blank portal option ID: " + field)
    return [{"id": str(option["id"]), "name": option["name"]} for option in options]


def _policy_weights(policy, sports):
    """Use a named, editable operator policy, never claim portal sport ratios."""
    if policy.get("schema_version") != 1 or not policy.get("version"):
        raise ValueError("A versioned sports planning policy is required")
    accepted = {item["name"] for item in sports}
    result = {}
    for gender in ("Female", "Male"):
        supplied = policy.get("weights", {}).get(gender)
        if not isinstance(supplied, dict) or set(supplied) != accepted:
            raise ValueError("Sports policy must cover every available sport for " + gender)
        if any(type(weight) is not int or not 0 <= weight <= 100 for weight in supplied.values()):
            raise ValueError("Invalid integer sport planning percentage")
        if sum(supplied.values()) != 100:
            raise ValueError("Sport planning percentages must total 100 for " + gender)
        if gender == "Female" and any(
            weight > 0 and "boys only" in sport.casefold()
            for sport, weight in supplied.items()
        ):
            raise ValueError("Boys-only sport cannot be planned for female participants")
        result[gender] = {
            item["name"]: supplied[item["name"]]
            for item in sports
            if not (gender == "Female" and "boys only" in item["name"].casefold())
        }
    return result


def build_defaults(
    snapshot,
    sports_policy,
    *,
    category="Schools (Secondary)",
    subcategory="Private",
    division="Lahore",
    district="Lahore",
    tehsil=None,
):
    """Initial defaults after choosing a new profile, not an overwrite of edits.

    The portal exposes UCs under district, not under tehsil. Selecting Shalimar
    must therefore NOT label all Lahore district UCs as Shalimar UCs.
    """
    root = _verified_snapshot(snapshot)
    part = root["participation"]
    category_option = exact(root["categories"], category)
    permitted_subcategories = part["subCategoriesByCategoryName"][category_option["name"]]
    subcategories = [
        option for option in part["subCategories"]
        if option["name"] in permitted_subcategories
    ]
    subcategory_option = exact(subcategories, subcategory)
    division_option = exact(root["divisions"], division)
    district_option = exact(
        root["districts_by_division"][str(division_option["id"])], district
    )
    tehsil_options = _unique_options(
        root["tehsils_by_district"].get(str(district_option["id"]), []), "Tehsil"
    )
    tehsil_option = exact(tehsil_options, tehsil) if tehsil else None
    district_ucs = _unique_options(
        root["ucs_by_district"].get(str(district_option["id"]), []), "UC"
    )
    if not district_ucs:
        raise ValueError("No UCs available for the selected district")
    allowed_names = part["sportsByCategoryName"][category_option["name"]]
    sports = _unique_options(
        [sport for sport in part["sports"] if sport["name"] in allowed_names],
        "Sport",
    )
    if len(sports) != len(set(allowed_names)):
        raise ValueError("Portal sports options do not match the category")
    if sports_policy.get("category") != category_option["name"]:
        raise ValueError("Sports policy belongs to another category")
    weights = _policy_weights(sports_policy, sports)
    age_hours = (
        datetime.now(timezone.utc) - datetime.fromisoformat(snapshot["fetched_at"])
    ).total_seconds() / 3600
    if age_hours < -1:
        raise ValueError("Portal catalog timestamp is in the future")
    if not part.get("showUcByCategoryName", {}).get(category_option["name"], True):
        raise ValueError("Selected category does not require UC selection")
    return {
        "kind": "normalization_profile_planning_draft",
        "version": 1,
        "planning_only": True,
        "applicant_posts": 0,
        "catalog_sha256": snapshot["content_sha256"],
        "catalog_fetched_at": snapshot["fetched_at"],
        "catalog_refresh_required": age_hours > 48,
        "category": category_option,
        "subcategory": subcategory_option,
        "division": division_option,
        "district": district_option,
        "tehsil": tehsil_option,
        "uc_selection_scope": "district",
        "tehsil_to_uc_mapping_verified": False,
        "uc_options": district_ucs,
        "selected_uc_ids": [item["id"] for item in district_ucs],
        "uc_required_unmet": False,
        "uc_selection_origin": "district_default_all",
        "sports": sports,
        "gender_sports": {
            gender: [
                item for item in sports
                if not (gender == "Female" and "boys only" in item["name"].casefold())
            ]
            for gender in ("Female", "Male")
        },
        "planning_weights": weights,
        "weights_origin": "editable_operator_policy_not_portal_rule",
        "weights_version": sports_policy["version"],
    }


def update_uc_selection(draft, selected_ids):
    """Preserve manual deselections; reject values absent from the snapshot."""
    available = [str(item["id"]) for item in draft["uc_options"]]
    selection = [str(value) for value in selected_ids]
    if len(selection) != len(set(selection)) or not set(selection) <= set(available):
        raise ValueError("UC selection contains duplicates or non-catalog IDs")
    result = deepcopy(draft)
    result["selected_uc_ids"] = [uid for uid in available if uid in set(selection)]
    result["uc_required_unmet"] = not bool(result["selected_uc_ids"])
    result["uc_selection_origin"] = "operator_override"
    return result


def select_all_ucs(draft):
    return update_uc_selection(draft, [item["id"] for item in draft["uc_options"]])


def search_ucs(draft, query):
    """Search does not implicitly select or deselect any UC."""
    needle = str(query).strip().casefold()
    return [deepcopy(item) for item in draft["uc_options"] if needle in item["name"].casefold()]
