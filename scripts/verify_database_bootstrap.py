"""Verify that a PostgreSQL database was cleanly bootstrapped by all migrations.

The database URL is accepted through ``DATABASE_URL`` or ``--database-url`` and
is never printed. This command is read-only: migration application remains the
responsibility of ``scripts/apply_migrations.py``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.apply_migrations import DEFAULT_MIGRATIONS_DIR, discover_migrations


EXPECTED_EXTENSIONS = {"pgcrypto", "pg_trgm", "vector"}
EXPECTED_TABLES = {
    "analysis_results",
    "analysis_runs",
    "company_profile_versions",
    "company_profiles",
    "delivery_receipts",
    "documents",
    "feedback",
    "jobs",
    "lead_notes",
    "outcome_events",
    "processing_events",
    "procurement_signals",
    "schema_migrations",
    "source_connections",
    "tenders",
    "workspace_audit_events",
    "workspace_memberships",
    "workspace_tenders",
    "workspaces",
}
APPEND_ONLY_TRIGGER = "analysis_runs_append_only"


class DatabaseBootstrapError(RuntimeError):
    pass


def _single_column(connection: Any, query: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(query)
        return {str(row[0]) for row in cursor.fetchall()}


def exercise_append_only_enforcement(connection: Any) -> None:
    workspace_id = uuid4()
    run_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspaces (id, slug, name)
            VALUES (%s, %s, %s);
            """,
            (
                workspace_id,
                f"bootstrap-verifier-{workspace_id.hex[:12]}",
                "Disposable bootstrap verifier",
            ),
        )
        cursor.execute(
            """
            INSERT INTO analysis_runs (
                id, workspace_id, task_type, status, prompt_version,
                prompt_sha256, model_provider, model_id, model_config_sha256,
                schema_version, schema_sha256, context_sha256, input_sha256,
                code_revision, error_class
            )
            VALUES (
                %s, %s, 'bootstrap.verify', 'failed', 'verification-v1',
                %s, 'verification', 'none', %s, 'verification-v1', %s, %s,
                %s, 'verification', 'expected_failure'
            );
            """,
            (
                run_id,
                workspace_id,
                "0" * 64,
                "0" * 64,
                "0" * 64,
                "0" * 64,
                "0" * 64,
            ),
        )
        try:
            cursor.execute(
                "UPDATE analysis_runs SET error_class = 'mutated' WHERE id = %s;",
                (run_id,),
            )
        except Exception as exc:
            connection.rollback()
            if "analysis_runs is append-only" not in str(exc):
                raise DatabaseBootstrapError(
                    "append-only mutation failed for an unexpected reason"
                ) from exc
            return
    connection.rollback()
    raise DatabaseBootstrapError("analysis_runs accepted a forbidden update")


def verify_database(
    connection: Any,
    migrations_dir: Path,
    *,
    exercise_append_only: bool = False,
) -> dict[str, int]:
    expected_versions = {
        migration.version for migration in discover_migrations(migrations_dir)
    }
    tables = _single_column(
        connection,
        """
        SELECT tablename
        FROM pg_catalog.pg_tables
        WHERE schemaname = 'public';
        """,
    )
    extensions = _single_column(
        connection,
        """
        SELECT extname
        FROM pg_catalog.pg_extension
        WHERE extname IN ('pgcrypto', 'pg_trgm', 'vector');
        """,
    )
    applied_versions = _single_column(
        connection,
        "SELECT version FROM schema_migrations;",
    )
    triggers = _single_column(
        connection,
        """
        SELECT trigger_name
        FROM information_schema.triggers
        WHERE event_object_schema = 'public'
          AND event_object_table = 'analysis_runs';
        """,
    )

    missing_tables = EXPECTED_TABLES - tables
    if missing_tables:
        raise DatabaseBootstrapError(
            f"missing expected tables: {', '.join(sorted(missing_tables))}"
        )
    missing_extensions = EXPECTED_EXTENSIONS - extensions
    if missing_extensions:
        raise DatabaseBootstrapError(
            f"missing expected extensions: {', '.join(sorted(missing_extensions))}"
        )
    if applied_versions != expected_versions:
        raise DatabaseBootstrapError(
            "migration ledger does not exactly match local migration versions"
        )
    if APPEND_ONLY_TRIGGER not in triggers:
        raise DatabaseBootstrapError("analysis_runs append-only trigger is missing")
    if exercise_append_only:
        exercise_append_only_enforcement(connection)

    return {
        "tables": len(tables),
        "extensions": len(extensions),
        "migrations": len(applied_versions),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL URL. Defaults to DATABASE_URL; never printed.",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=DEFAULT_MIGRATIONS_DIR,
        help="Directory containing the migration set to verify.",
    )
    parser.add_argument(
        "--exercise-append-only",
        action="store_true",
        help="Attempt one synthetic forbidden update in a transaction that is rolled back.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        print(
            "database_bootstrap_error: DATABASE_URL or --database-url is required",
            file=sys.stderr,
        )
        return 2

    try:
        import psycopg

        with psycopg.connect(database_url) as connection:
            summary = verify_database(
                connection,
                args.migrations_dir.resolve(),
                exercise_append_only=args.exercise_append_only,
            )
        print(
            "database_bootstrap_ok "
            f"tables={summary['tables']} extensions={summary['extensions']} "
            f"migrations={summary['migrations']} "
            "append_only_trigger="
            f"{'enforced' if args.exercise_append_only else 'present'}"
        )
        return 0
    except (DatabaseBootstrapError, OSError, psycopg.Error) as exc:
        print(f"database_bootstrap_error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
