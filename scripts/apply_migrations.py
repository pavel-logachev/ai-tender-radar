"""Apply checksum-bound SQL migrations without printing database credentials."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_[a-z0-9_]+\.sql$")
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"
LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    filename: str
    path: Path
    checksum_sha256: str
    sql: str


def migration_checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def discover_migrations(directory: Path) -> list[Migration]:
    if not directory.exists() or not directory.is_dir():
        raise MigrationError(f"migration directory does not exist: {directory}")
    migrations: list[Migration] = []
    seen_versions: set[str] = set()
    for path in sorted(directory.glob("*.sql"), key=lambda item: item.name):
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        version = match.group("version")
        if version in seen_versions:
            raise MigrationError(f"duplicate migration version: {version}")
        seen_versions.add(version)
        try:
            sql = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"migration must be UTF-8: {path.name}") from exc
        if not sql.strip():
            raise MigrationError(f"migration is empty: {path.name}")
        migrations.append(
            Migration(
                version=version,
                filename=path.name,
                path=path,
                checksum_sha256=migration_checksum(sql),
                sql=sql,
            )
        )
    return migrations


def read_applied_migrations(connection: Any) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.schema_migrations');")
        row = cursor.fetchone()
        if not row or row[0] is None:
            return {}
        cursor.execute("SELECT version, checksum_sha256 FROM schema_migrations;")
        return {str(version): str(checksum) for version, checksum in cursor.fetchall()}


def migration_plan(
    migrations: list[Migration],
    applied: dict[str, str],
) -> list[Migration]:
    pending: list[Migration] = []
    for migration in migrations:
        existing_checksum = applied.get(migration.version)
        if existing_checksum is None:
            pending.append(migration)
            continue
        if existing_checksum != migration.checksum_sha256:
            raise MigrationError(
                f"checksum mismatch for applied migration {migration.filename}"
            )
    unknown = sorted(set(applied) - {migration.version for migration in migrations})
    if unknown:
        raise MigrationError(f"database has unknown migration versions: {', '.join(unknown)}")
    return pending


def apply_pending_migrations(connection: Any, pending: list[Migration]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(LEDGER_SQL)
        for migration in pending:
            cursor.execute(migration.sql)
            cursor.execute(
                """
                INSERT INTO schema_migrations (version, filename, checksum_sha256)
                VALUES (%s, %s, %s);
                """,
                (migration.version, migration.filename, migration.checksum_sha256),
            )
    connection.commit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=DEFAULT_MIGRATIONS_DIR,
        help="Directory containing ordered SQL migrations.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL URL. Defaults to DATABASE_URL; never printed.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate local migration files without connecting to PostgreSQL.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Check database migration state without applying changes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        migrations = discover_migrations(args.migrations_dir.resolve())
        if args.dry_run:
            for migration in migrations:
                print(f"valid {migration.filename} sha256={migration.checksum_sha256}")
            print(f"migration_files={len(migrations)} mode=dry_run")
            return 0

        database_url = args.database_url or os.getenv("DATABASE_URL")
        if not database_url:
            raise MigrationError("DATABASE_URL or --database-url is required")

        import psycopg

        with psycopg.connect(database_url) as connection:
            applied = read_applied_migrations(connection)
            pending = migration_plan(migrations, applied)
            if args.check:
                for migration in pending:
                    print(f"pending {migration.filename}")
                print(
                    f"migration_files={len(migrations)} applied={len(applied)} "
                    f"pending={len(pending)} mode=check"
                )
                return 1 if pending else 0
            apply_pending_migrations(connection, pending)
            for migration in pending:
                print(f"applied {migration.filename}")
            print(
                f"migration_files={len(migrations)} applied_now={len(pending)} "
                "mode=apply"
            )
            return 0
    except (MigrationError, OSError) as exc:
        print(f"migration_error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
