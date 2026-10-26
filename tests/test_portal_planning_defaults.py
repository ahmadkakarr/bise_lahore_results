"""Planning defaults: synthetic portal fixtures, never real applicant submissions."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from bise_normalize.planning_defaults import (
    build_defaults, search_ucs, select_all_ucs, update_uc_selection,
)
from youthgames_shared.catalog import PORTAL, signature


SPORTS = [
    "Athletics", "Badminton", "Cricket (Hard Ball)", "Football",
    "Kabaddi (Boys Only)", "Tug of War",
]
WEIGHTS = {
    "Female": {
        "Athletics": 20, "Badminton": 50, "Cricket (Hard Ball)": 10,
        "Football": 5, "Kabaddi (Boys Only)": 0, "Tug of War": 15,
    },
    "Male": {
        "Athletics": 15, "Badminton": 10, "Cricket (Hard Ball)": 40,
        "Football": 25, "Kabaddi (Boys Only)": 5, "Tug of War": 5,
    },
}


def fixture():
    data = {
        "categories": [{"id": "3", "name": "Schools (Secondary)"}],
        "divisions": [{"id": "1", "name": "Lahore"}],
        "districts_by_division": {"1": [{"id": "21", "name": "Lahore"}]},
        "tehsils_by_district": {"21": [
            {"id": "76", "name": "Raiwind"}, {"id": "77", "name": "Shalimar"},
        ]},
        "ucs_by_district": {"21": [
            {"id": str(i), "name": f"UC No. {i}, Lahore District"}
            for i in range(1, 292)
        ]},
        "participation": {
            "subCategoriesByCategoryName": {"Schools (Secondary)": ["Private"]},
            "subCategories": [{"id": "9", "name": "Private"}],
            "showUcByCategoryName": {"Schools (Secondary)": True},
            "sportsByCategoryName": {"Schools (Secondary)": SPORTS},
            "sports": [
                {"id": str(i + 1), "name": name} for i, name in enumerate(SPORTS)
            ],
        },
    }
    return {
        "schema_version": 1, "source_url": PORTAL + "/#registration",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data, "content_sha256": signature(data),
    }


def policy():
    return {
        "schema_version": 1, "version": "remaining-v1",
        "category": "Schools (Secondary)", "weights": deepcopy(WEIGHTS),
    }


class PortalPlanningDefaultsTests(unittest.TestCase):
    def test_private_291_district_ucs_selected_without_tehsil_claim(self):
        draft = build_defaults(fixture(), policy(), tehsil="Shalimar")
        self.assertEqual(draft["subcategory"]["name"], "Private")
        self.assertEqual(draft["tehsil"]["name"], "Shalimar")
        self.assertEqual(len(draft["uc_options"]), 291)
        self.assertEqual(len(draft["selected_uc_ids"]), 291)
        self.assertEqual(draft["uc_selection_scope"], "district")
        self.assertFalse(draft["tehsil_to_uc_mapping_verified"])
        self.assertTrue(draft["planning_only"])
        self.assertEqual(draft["applicant_posts"], 0)

    def test_search_and_deselect_preserve_manual_override(self):
        original = build_defaults(fixture(), policy())
        matched = search_ucs(original, "No. 291")
        self.assertEqual([item["id"] for item in matched], ["291"])
        changed = update_uc_selection(original, ["3", "1"])
        self.assertEqual(changed["selected_uc_ids"], ["1", "3"])
        self.assertEqual(len(original["selected_uc_ids"]), 291)
        self.assertEqual(search_ucs(changed, "No. 291"), matched)
        self.assertEqual(len(select_all_ucs(changed)["selected_uc_ids"]), 291)
        with self.assertRaisesRegex(ValueError, "non-catalog"):
            update_uc_selection(changed, ["999"])
        with self.assertRaisesRegex(ValueError, "duplicates"):
            update_uc_selection(changed, ["1", "1"])

    def test_zero_selected_never_becomes_registration_ready(self):
        draft = update_uc_selection(build_defaults(fixture(), policy()), [])
        self.assertEqual(draft["selected_uc_ids"], [])
        self.assertTrue(draft["uc_required_unmet"])
        self.assertTrue(draft["planning_only"])

    def test_boys_only_is_excluded_for_females(self):
        draft = build_defaults(fixture(), policy())
        self.assertNotIn(
            "Kabaddi (Boys Only)",
            [s["name"] for s in draft["gender_sports"]["Female"]],
        )
        self.assertIn(
            "Kabaddi (Boys Only)",
            [s["name"] for s in draft["gender_sports"]["Male"]],
        )
        self.assertNotIn("Kabaddi (Boys Only)", draft["planning_weights"]["Female"])
        self.assertEqual(sum(draft["planning_weights"]["Female"].values()), 100)
        self.assertEqual(sum(draft["planning_weights"]["Male"].values()), 100)

    def test_invalid_gender_weights_and_portal_labels_fail_closed(self):
        invalid = policy()
        invalid["weights"]["Female"]["Kabaddi (Boys Only)"] = 1
        invalid["weights"]["Female"]["Badminton"] = 49
        with self.assertRaisesRegex(ValueError, "Boys-only"):
            build_defaults(fixture(), invalid)
        invalid = policy()
        invalid["weights"]["Male"]["Football"] = 24
        with self.assertRaisesRegex(ValueError, "total 100"):
            build_defaults(fixture(), invalid)
        with self.assertRaises(ValueError):
            build_defaults(fixture(), policy(), subcategory="Open Category")
        with self.assertRaises(ValueError):
            build_defaults(fixture(), policy(), tehsil="Invented")

    def test_catalog_integrity_and_staleness(self):
        broken = fixture()
        broken["data"]["ucs_by_district"]["21"].pop()
        with self.assertRaisesRegex(ValueError, "unverified"):
            build_defaults(broken, policy())
        stale = fixture()
        stale["fetched_at"] = (
            datetime.now(timezone.utc) - timedelta(days=3)
        ).isoformat()
        self.assertTrue(build_defaults(stale, policy())["catalog_refresh_required"])

    def test_new_district_uses_its_own_ucs_without_old_selection(self):
        catalog = fixture()
        catalog["data"]["districts_by_division"]["1"].append(
            {"id": "22", "name": "Kasur"}
        )
        catalog["data"]["tehsils_by_district"]["22"] = []
        catalog["data"]["ucs_by_district"]["22"] = [
            {"id": "K1", "name": "Kasur UC One"}
        ]
        catalog["content_sha256"] = signature(catalog["data"])
        changed = build_defaults(catalog, policy(), district="Kasur")
        self.assertEqual(changed["selected_uc_ids"], ["K1"])
        self.assertEqual(changed["district"]["name"], "Kasur")


if __name__ == "__main__":
    unittest.main()
