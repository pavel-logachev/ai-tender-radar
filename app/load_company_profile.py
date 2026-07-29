import json
from pathlib import Path

import psycopg
import yaml

from app.config import settings


PROFILE_PATH = Path("/app/config/company_profile.yaml")


def main() -> None:
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(f"Profile file not found: {PROFILE_PATH}")

    profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    name = profile.get("name", "Company")

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO company_profiles (name, profile, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (name)
                DO UPDATE SET
                    profile = EXCLUDED.profile,
                    updated_at = now()
                RETURNING id, name, updated_at;
                """,
                (name, json.dumps(profile, ensure_ascii=False)),
            )
            row = cur.fetchone()
            conn.commit()

    print(f"Company profile saved: id={row[0]}, name={row[1]}, updated_at={row[2]}")


if __name__ == "__main__":
    main()
