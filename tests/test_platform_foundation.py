from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from app.platform.analysis_runs import ANALYSIS_RUN_INSERT_SQL, insert_analysis_run
from app.platform.contracts import AnalysisRunDraft, CanonicalSourceRecord
from app.platform.jobs import (
    JobState,
    ReplayDecision,
    new_job,
    redrive_dead_letter,
    replay_decision,
    retry_delay_seconds,
    transition_job,
)
from app.platform.source_adapters import (
    FileSourceAdapter,
    SourceAdapterError,
)
from app.platform.versioning import (
    build_ai_version_binding,
    canonical_json,
    sha256_json,
)
from scripts.apply_migrations import (
    MigrationError,
    discover_migrations,
    migration_plan,
)


WORKSPACE_ID = UUID("11111111-1111-1111-1111-111111111111")
JOB_ID = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


class VersioningContractTest(unittest.TestCase):
    def test_canonical_hash_is_independent_of_mapping_order(self) -> None:
        first = {"b": [2, 1], "a": {"value": 3}}
        second = {"a": {"value": 3}, "b": [2, 1]}

        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(sha256_json(first), sha256_json(second))

    def test_version_binding_covers_prompt_model_schema_context_and_input(self) -> None:
        binding = build_ai_version_binding(
            prompt_version="prompt-v1",
            prompt_template="Classify {{input}}",
            model_provider="routerai",
            model_id="model/test",
            model_config={"temperature": 0.1},
            schema_version="schema-v1",
            schema={"type": "object"},
            context="source context",
            input_payload={"external_id": "42"},
            code_revision="abc123",
        )

        self.assertEqual(binding.prompt_version, "prompt-v1")
        self.assertEqual(binding.code_revision, "abc123")
        for value in (
            binding.prompt_sha256,
            binding.model_config_sha256,
            binding.schema_sha256,
            binding.context_sha256,
            binding.input_sha256,
        ):
            self.assertRegex(value, r"^[0-9a-f]{64}$")

    def test_analysis_run_writer_is_insert_only(self) -> None:
        binding = build_ai_version_binding(
            prompt_version="prompt-v1",
            prompt_template="Prompt",
            model_provider="routerai",
            model_id="model/test",
            model_config={},
            schema_version="schema-v1",
            schema={"type": "object"},
            context="context",
            input_payload={"id": "1"},
            code_revision="abc123",
        )
        output = {"decision": "go"}
        draft = AnalysisRunDraft(
            workspace_id=WORKSPACE_ID,
            task_type="lead.triage",
            status="succeeded",
            binding=binding,
            output=output,
            output_sha256=sha256_json(output),
        )
        executed: list[tuple[str, tuple[object, ...]]] = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def execute(self, sql: str, params: tuple[object, ...]) -> None:
                executed.append((sql, params))

        class Connection:
            def cursor(self) -> Cursor:
                return Cursor()

        identity = insert_analysis_run(
            Connection(),
            draft,
            run_id=JOB_ID,
            started_at=NOW,
            finished_at=NOW,
        )

        self.assertEqual(identity, JOB_ID)
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0][0], ANALYSIS_RUN_INSERT_SQL)
        self.assertIn("INSERT INTO analysis_runs", ANALYSIS_RUN_INSERT_SQL)
        self.assertNotIn("UPDATE ", ANALYSIS_RUN_INSERT_SQL.upper())
        self.assertNotIn("DELETE ", ANALYSIS_RUN_INSERT_SQL.upper())

    def test_analysis_run_writer_rejects_mismatched_output_hash(self) -> None:
        binding = build_ai_version_binding(
            prompt_version="prompt-v1",
            prompt_template="Prompt",
            model_provider="routerai",
            model_id="model/test",
            model_config={},
            schema_version="schema-v1",
            schema={"type": "object"},
            context="context",
            input_payload={"id": "1"},
            code_revision="abc123",
        )
        draft = AnalysisRunDraft(
            workspace_id=WORKSPACE_ID,
            task_type="lead.triage",
            status="succeeded",
            binding=binding,
            output={"decision": "go"},
            output_sha256="0" * 64,
        )

        with self.assertRaisesRegex(ValueError, "does not match"):
            insert_analysis_run(object(), draft, started_at=NOW, finished_at=NOW)


class FileSourceAdapterTest(unittest.TestCase):
    def test_json_snapshot_is_validated_and_paginated(self) -> None:
        payload = {
            "schema_version": "procurement-source-v1",
            "records": [
                {
                    "source": "client_export",
                    "external_id": "A-1",
                    "title": "Server purchase",
                    "initial_price": 1250000,
                    "published_at": "2026-07-22T10:00:00+03:00",
                    "url": "https://example.test/tenders/A-1",
                },
                {
                    "source": "client_export",
                    "external_id": "A-2",
                    "title": "Storage purchase",
                    "currency": "RUB",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            adapter = FileSourceAdapter(path, allowed_root=directory)

            first = adapter.fetch(limit=1)
            second = adapter.fetch(cursor=first.next_cursor, limit=1)

        self.assertEqual(first.records[0].external_id, "A-1")
        self.assertIsNotNone(first.next_cursor)
        self.assertEqual(second.records[0].external_id, "A-2")
        self.assertIsNone(second.next_cursor)
        self.assertEqual(first.source_snapshot_sha256, second.source_snapshot_sha256)

    def test_cursor_fails_closed_when_snapshot_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.jsonl"
            path.write_text(
                '{"external_id":"A-1","title":"One"}\n'
                '{"external_id":"A-2","title":"Two"}\n',
                encoding="utf-8",
            )
            adapter = FileSourceAdapter(path, allowed_root=directory)
            first = adapter.fetch(limit=1)
            path.write_text('{"external_id":"A-3","title":"Changed"}\n', encoding="utf-8")

            with self.assertRaisesRegex(SourceAdapterError, "snapshot changed"):
                adapter.fetch(cursor=first.next_cursor, limit=1)

    def test_invalid_url_and_ambiguous_datetime_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CanonicalSourceRecord(
                source="file_import",
                external_id="A-1",
                title="Unsafe",
                url="https://user:" + "password@example.test/tender",
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.csv"
            path.write_text(
                "external_id,title,published_at\nA-1,Unsafe,2026-07-22T10:00:00\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SourceAdapterError, "timezone"):
                FileSourceAdapter(path, allowed_root=directory).fetch()

    def test_file_outside_allowed_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / "signals.jsonl"
            path.write_text('{"external_id":"A-1","title":"One"}\n', encoding="utf-8")
            with self.assertRaisesRegex(SourceAdapterError, "outside allowed_root"):
                FileSourceAdapter(path, allowed_root=allowed)

    def test_nested_secret_like_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "secret-like key"):
            CanonicalSourceRecord(
                source="file_import",
                external_id="A-1",
                title="Unsafe payload",
                raw={"connector": {"access_token": "must-not-be-imported"}},
            )


class DurableJobContractTest(unittest.TestCase):
    def test_replay_reuses_same_payload_and_rejects_changed_payload(self) -> None:
        job = new_job(
            workspace_id=WORKSPACE_ID,
            kind="signal.import",
            operation_key="file:sha256:1",
            payload={"path": "signals.json"},
            now=NOW,
            job_id=JOB_ID,
        )

        self.assertEqual(
            replay_decision(job, incoming_payload={"path": "signals.json"}),
            ReplayDecision.REUSE,
        )
        self.assertEqual(
            replay_decision(job, incoming_payload={"path": "other.json"}),
            ReplayDecision.CONFLICT,
        )

    def test_state_machine_requires_lease_and_bounds_retries(self) -> None:
        job = new_job(
            workspace_id=WORKSPACE_ID,
            kind="signal.import",
            operation_key="file:sha256:1",
            payload={},
            max_attempts=3,
            now=NOW,
            job_id=JOB_ID,
        )
        validated = transition_job(job, JobState.VALIDATED, now=NOW)
        accepted = transition_job(validated, JobState.ACCEPTED, now=NOW)
        with self.assertRaisesRegex(ValueError, "lease_owner"):
            transition_job(accepted, JobState.PROCESSING, now=NOW)

        processing = transition_job(
            accepted,
            JobState.PROCESSING,
            now=NOW,
            lease_owner="worker-1",
        )
        retry = transition_job(
            processing,
            JobState.RETRY_SCHEDULED,
            now=NOW,
            error_class="dependency_unavailable",
        )

        self.assertEqual(processing.attempts, 1)
        self.assertEqual(retry.state, JobState.RETRY_SCHEDULED)
        self.assertIsNone(retry.lease_owner)
        self.assertGreater(retry.available_at, NOW)

    def test_retry_jitter_is_deterministic_and_redrive_needs_authority(self) -> None:
        self.assertEqual(
            retry_delay_seconds(job_id=JOB_ID, attempt=2),
            retry_delay_seconds(job_id=JOB_ID, attempt=2),
        )
        job = new_job(
            workspace_id=WORKSPACE_ID,
            kind="delivery.telegram",
            operation_key="message:1",
            payload={},
            now=NOW,
            job_id=JOB_ID,
        )
        dead = transition_job(
            job,
            JobState.DEAD_LETTERED,
            now=NOW,
            error_class="invalid_payload",
        )
        with self.assertRaises(PermissionError):
            redrive_dead_letter(dead, authorized=False, now=NOW)
        redriven = redrive_dead_letter(dead, authorized=True, now=NOW)
        self.assertEqual(redriven.state, JobState.ACCEPTED)
        self.assertEqual(redriven.attempts, 0)


class MigrationContractTest(unittest.TestCase):
    def test_commercial_foundation_migration_is_additive_and_discoverable(self) -> None:
        directory = Path(__file__).resolve().parents[1] / "database" / "migrations"
        migrations = discover_migrations(directory)

        self.assertEqual([item.version for item in migrations], ["0001"])
        sql = migrations[0].sql.upper()
        self.assertIn("CREATE TABLE IF NOT EXISTS ANALYSIS_RUNS", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS JOBS", sql)
        self.assertIn("ATR_JSONB_CONTAINS_SECRET_KEY", sql)
        self.assertIn("FOREIGN KEY (WORKSPACE_ID, SIGNAL_ID)", sql)
        self.assertNotIn("DROP TABLE", sql)
        self.assertEqual(migration_plan(migrations, {}), migrations)

    def test_applied_migration_checksum_mismatch_fails(self) -> None:
        directory = Path(__file__).resolve().parents[1] / "database" / "migrations"
        migration = discover_migrations(directory)[0]
        with self.assertRaisesRegex(MigrationError, "checksum mismatch"):
            migration_plan([migration], {migration.version: "0" * 64})


if __name__ == "__main__":
    unittest.main()
