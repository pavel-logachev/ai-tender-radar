from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.pipeline import run_search_profile as runner


def profile_with_queries(queries: list[str]) -> dict:
    return {
        "defaults": {
            "days_back": 14,
            "min_price": 500000,
            "limit_full_per_query": 10,
            "max_short_results_per_query": 50,
        },
        "groups": {"core": {"packs": ["pack_a"]}},
        "packs": {"pack_a": {"description": "Pack A", "queries": queries}},
    }


def collection_stats(query: str, external_id: str = "1") -> SimpleNamespace:
    return SimpleNamespace(
        query=query,
        collected=1,
        external_ids=(external_id,),
        new_external_ids=(external_id,),
        saved_or_updated=1,
        skipped_existing=0,
        full_load_attempts=1,
        failed_full_loads=0,
    )


class RunSearchProfileTest(unittest.TestCase):
    def run_main(self, argv: list[str], profile: dict) -> tuple[int, str]:
        output = io.StringIO()
        with (
            patch.object(runner, "load_search_profile", return_value=profile),
            redirect_stdout(output),
        ):
            code = runner.main(argv)
        return code, output.getvalue()

    def test_dry_run_does_not_sleep_or_collect(self) -> None:
        profile = profile_with_queries(["q1", "q2"])

        with (
            patch.object(runner, "time") as time_mock,
            patch.object(runner, "collect_with_client") as collect_mock,
        ):
            code, output = self.run_main(
                ["--group", "core", "--dry-run", "--query-delay-seconds", "20"],
                profile,
            )

        self.assertEqual(code, 0)
        time_mock.sleep.assert_not_called()
        collect_mock.assert_not_called()
        self.assertIn("Query delay seconds: 20 (skipped in dry-run)", output)

    def test_query_delay_is_used_between_queries(self) -> None:
        profile = profile_with_queries(["q1", "q2"])
        fake_client = SimpleNamespace(login_retry_count=2, close=Mock())

        with (
            patch.object(runner, "create_z360_client", return_value=fake_client),
            patch.object(
                runner,
                "collect_with_client",
                side_effect=[collection_stats("q1", "1"), collection_stats("q2", "2")],
            ) as collect_mock,
            patch.object(runner, "enrich_with_rule_based_metrics"),
            patch.object(runner, "time") as time_mock,
        ):
            code, output = self.run_main(
                ["--group", "core", "--skip-score", "--query-delay-seconds", "0.25"],
                profile,
            )

        self.assertEqual(code, 0)
        self.assertEqual(collect_mock.call_count, 2)
        time_mock.sleep.assert_called_once_with(0.25)
        self.assertIn("Token/login retries: 2", output)
        fake_client.close.assert_called_once()

    def test_recoverable_query_failure_goes_to_failed_summary(self) -> None:
        profile = profile_with_queries(["q1", "q2"])
        fake_client = SimpleNamespace(login_retry_count=0, close=Mock())

        with (
            patch.object(runner, "create_z360_client", return_value=fake_client),
            patch.object(
                runner,
                "collect_with_client",
                side_effect=[collection_stats("q1", "1"), RuntimeError("temporary 429")],
            ),
            patch.object(runner, "enrich_with_rule_based_metrics"),
            patch.object(runner, "time"),
        ):
            code, output = self.run_main(
                ["--group", "core", "--skip-score", "--query-delay-seconds", "0"],
                profile,
            )

        self.assertEqual(code, 0)
        self.assertIn("Run status: partial", output)
        self.assertIn("Queries: succeeded=1, failed=1, total=2", output)
        self.assertIn("- [pack_a] q2: RuntimeError: temporary 429", output)

    def test_no_successful_queries_returns_nonzero(self) -> None:
        profile = profile_with_queries(["q1", "q2"])
        fake_client = SimpleNamespace(login_retry_count=3, close=Mock())

        with (
            patch.object(runner, "create_z360_client", return_value=fake_client),
            patch.object(runner, "collect_with_client", side_effect=RuntimeError("token limit")),
            patch.object(runner, "enrich_with_rule_based_metrics") as enrich_mock,
            patch.object(runner, "time") as time_mock,
        ):
            code, output = self.run_main(
                ["--group", "core", "--skip-score", "--query-delay-seconds", "0.1"],
                profile,
            )

        self.assertEqual(code, 1)
        self.assertIn("Run status: failed", output)
        self.assertIn("Queries: succeeded=0, failed=2, total=2", output)
        self.assertIn("Token/login retries: 3", output)
        time_mock.sleep.assert_called_once_with(0.1)
        enrich_mock.assert_not_called()

    def test_partial_run_summary_is_success_exit_code(self) -> None:
        profile = profile_with_queries(["q1", "q2", "q3"])
        fake_client = SimpleNamespace(login_retry_count=1, close=Mock())

        with (
            patch.object(runner, "create_z360_client", return_value=fake_client),
            patch.object(
                runner,
                "collect_with_client",
                side_effect=[
                    collection_stats("q1", "1"),
                    RuntimeError("query failed"),
                    collection_stats("q3", "3"),
                ],
            ),
            patch.object(runner, "enrich_with_rule_based_metrics"),
            patch.object(runner, "time"),
        ):
            code, output = self.run_main(
                ["--group", "core", "--skip-score", "--query-delay-seconds", "0"],
                profile,
            )

        self.assertEqual(code, 0)
        self.assertIn("Run status: partial", output)
        self.assertIn("Queries: succeeded=2, failed=1, total=3", output)

    def test_list_packs_does_not_require_selection(self) -> None:
        profile = profile_with_queries(["q1"])

        with patch.object(runner, "collect_with_client") as collect_mock:
            code, output = self.run_main(["--list-packs"], profile)

        self.assertEqual(code, 0)
        self.assertIn("pack_a", output)
        collect_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
