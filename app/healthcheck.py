import psycopg

from app.config import settings


def main() -> None:
    print("AI Tender Radar backend healthcheck")
    print(f"APP_ENV={settings.app_env}")

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, now();")
            row = cur.fetchone()
            print(f"Database OK: db={row[0]}, user={row[1]}, time={row[2]}")

            cur.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name;
            """)
            tables = [r[0] for r in cur.fetchall()]
            print("Tables:", ", ".join(tables))


if __name__ == "__main__":
    main()
