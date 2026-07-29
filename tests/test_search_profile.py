from __future__ import annotations

import unittest
from pathlib import Path

from app.pipeline.search_profile import (
    QueryRunSummary,
    aggregate_by_pack,
    aggregate_total,
    defaults,
    load_search_profile,
    query_specs_for_packs,
    selected_pack_names,
)


class SearchProfileTest(unittest.TestCase):
    def test_load_search_profile_has_required_packs(self) -> None:
        profile = load_search_profile(Path("config/search_profile.yaml"))

        self.assertGreaterEqual(
            set(profile["packs"]),
            {
                "storage_basic",
                "storage_extended",
                "servers_basic",
                "servers_extended",
                "optional_network",
            },
        )

        for pack_name, pack in profile["packs"].items():
            self.assertIsInstance(pack["queries"], list)
            self.assertGreaterEqual(len(pack["queries"]), 3, pack_name)
            self.assertTrue(all(isinstance(query, str) and query.strip() for query in pack["queries"]))

    def test_select_pack_and_group_preserves_order_and_deduplicates(self) -> None:
        profile = load_search_profile(Path("config/search_profile.yaml"))

        selected = selected_pack_names(
            profile,
            groups=["core"],
            packs=["storage_basic", "optional_network"],
        )

        self.assertEqual(
            selected,
            ["storage_basic", "servers_basic", "optional_network"],
        )

    def test_query_specs_for_selected_packs(self) -> None:
        profile = load_search_profile(Path("config/search_profile.yaml"))
        specs = query_specs_for_packs(profile, ["storage_basic"])

        self.assertTrue(specs)
        self.assertEqual({spec.pack for spec in specs}, {"storage_basic"})
        self.assertIn("СХД", [spec.query for spec in specs])

    def test_unknown_pack_is_rejected(self) -> None:
        profile = load_search_profile(Path("config/search_profile.yaml"))

        with self.assertRaisesRegex(ValueError, "Unknown pack"):
            selected_pack_names(profile, packs=["missing_pack"])

    def test_defaults_read_new_block_and_legacy_fallback(self) -> None:
        profile = {
            "days_back": 7,
            "min_price": 1000,
            "limit_full_per_query": 3,
            "packs": {"p": {"queries": ["q"]}},
        }

        self.assertEqual(
            defaults(profile),
            {
                "days_back": 7,
                "min_price": 1000,
                "limit_full_per_query": 3,
            },
        )

    def test_aggregate_deduplicates_external_ids(self) -> None:
        first = QueryRunSummary(
            pack="storage_basic",
            query="q1",
            collected=3,
            external_ids={"1", "2"},
            new_external_ids={"1", "2"},
            saved_or_updated=2,
            skipped_existing=0,
            full_load_attempts=2,
            failed_full_loads=0,
            scored_external_ids={"1"},
            rule_based_passed_external_ids={"1"},
        )
        second = QueryRunSummary(
            pack="storage_basic",
            query="q2",
            collected=4,
            external_ids={"2", "3"},
            new_external_ids={"3"},
            saved_or_updated=1,
            skipped_existing=1,
            full_load_attempts=1,
            failed_full_loads=1,
            scored_external_ids={"1", "2", "3"},
            rule_based_passed_external_ids={"1", "3"},
        )
        third = QueryRunSummary(
            pack="servers_basic",
            query="q3",
            collected=1,
            external_ids={"3"},
            new_external_ids=set(),
            saved_or_updated=0,
            skipped_existing=1,
            full_load_attempts=0,
            failed_full_loads=0,
            scored_external_ids={"3"},
            rule_based_passed_external_ids={"3"},
        )

        total = aggregate_total([first, second, third])

        self.assertEqual(total.collected, 8)
        self.assertEqual(total.unique, 3)
        self.assertEqual(total.new, 3)
        self.assertEqual(total.saved_or_updated, 3)
        self.assertEqual(total.skipped_existing, 2)
        self.assertEqual(total.full_load_attempts, 3)
        self.assertEqual(total.failed_full_loads, 1)
        self.assertEqual(total.scored, 3)
        self.assertEqual(total.rule_based_passed, 2)

        by_pack = aggregate_by_pack([first, second, third])
        self.assertEqual([(item.label, item.unique) for item in by_pack], [("storage_basic", 3), ("servers_basic", 1)])


if __name__ == "__main__":
    unittest.main()
