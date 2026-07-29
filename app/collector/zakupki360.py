from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import settings
from app.procedure_url import procedure_url_from_sources


@dataclass(frozen=True)
class CollectionStats:
    query: str
    collected: int
    external_ids: tuple[str, ...]
    new_external_ids: tuple[str, ...]
    saved_or_updated: int
    skipped_existing: int
    full_load_attempts: int
    failed_full_loads: int

    @property
    def unique_external_ids(self) -> int:
        return len(self.external_ids)


def parse_dt(value: str | None):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_token(data: Any) -> str:
    if isinstance(data, str):
        return data.strip().strip('"')

    if isinstance(data, dict):
        for key in ("access_token", "token", "jwt", "bearerToken", "bearer_token"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value

    raise RuntimeError(f"Cannot find token in response: {data}")


def infer_law(full: dict) -> str | None:
    url = (full.get("tenderUrl") or "").lower()
    law_id = full.get("lawId")

    if "/44fz/" in url or "44fz" in url:
        return "44-ФЗ"

    if "/223/" in url or "223fz" in url or "223-фз" in url:
        return "223-ФЗ"

    if law_id is not None:
        return f"lawId={law_id}"

    return None


def organization_name(full: dict) -> str | None:
    placer = full.get("placerOrganization") or {}

    for key in ("fullName", "name"):
        value = placer.get(key)
        if value:
            return value

    customers = full.get("customers") or []
    if customers:
        first = customers[0] or {}
        return first.get("fullName") or first.get("name")

    return full.get("placerOrganizationName")


def build_description(full: dict) -> str:
    parts = []

    for key in [
        "name",
        "deliveryPlace",
        "deliveryTerm",
        "tenderTypeName",
        "tenderStageName",
        "contactPerson",
        "contactEMail",
        "contactPhone",
    ]:
        value = full.get(key)
        if value:
            parts.append(str(value))

    products = full.get("products") or []
    for product in products[:10]:
        if isinstance(product, dict):
            parts.extend(str(v) for v in product.values() if v)

    documents = full.get("documents") or []
    for document in documents[:20]:
        if isinstance(document, dict):
            title = document.get("title")
            if title:
                parts.append(str(title))

    return "\n".join(parts)


class Zakupki360Client:
    def __init__(self) -> None:
        if not settings.z360_login or not settings.z360_password:
            raise RuntimeError("Z360_LOGIN and Z360_PASSWORD must be set in .env")

        self.base_url = settings.z360_base_url.rstrip("/")
        self.rate_limit_seconds = settings.z360_rate_limit_seconds
        self.client = httpx.Client(base_url=self.base_url, timeout=60)
        self.token: str | None = None
        self.login_retry_count = 0
        self.last_request_at = 0.0

    def wait_rate_limit(self, rate_limit_seconds: float | None = None) -> None:
        effective_rate_limit_seconds = (
            self.rate_limit_seconds
            if rate_limit_seconds is None
            else rate_limit_seconds
        )
        now = time.monotonic()
        elapsed = now - self.last_request_at
        wait_for = effective_rate_limit_seconds - elapsed

        if wait_for > 0:
            time.sleep(wait_for)

    def request(
        self,
        method: str,
        url: str,
        *,
        rate_limit_seconds: float | None = None,
        **kwargs,
    ) -> httpx.Response:
        self.wait_rate_limit(rate_limit_seconds)

        if self.token:
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {self.token}"
            headers["Accept"] = "application/json"
            kwargs["headers"] = headers

        response = self.client.request(method, url, **kwargs)
        self.last_request_at = time.monotonic()

        if response.status_code == 429:
            print("Rate limit hit. Stop current API call without retrying to protect API quota.")

        response.raise_for_status()
        return response

    def login(
        self,
        *,
        max_retries: int = 3,
        retry_backoff_seconds: float = 20.0,
    ) -> None:
        for attempt in range(max_retries + 1):
            response = self.client.post(
                "/token",
                json={
                    "login": settings.z360_login,
                    "password": settings.z360_password,
                },
            )
            self.last_request_at = time.monotonic()

            if response.status_code != 429:
                response.raise_for_status()
                break

            if attempt >= max_retries:
                response.raise_for_status()

            self.login_retry_count += 1
            retry_after = response.headers.get("retry-after")
            try:
                sleep_seconds = (
                    float(retry_after)
                    if retry_after
                    else retry_backoff_seconds * (attempt + 1)
                )
            except ValueError:
                sleep_seconds = retry_backoff_seconds * (attempt + 1)

            print(
                "Z360 token rate limit hit. "
                f"Retry {attempt + 1}/{max_retries} after {sleep_seconds:g}s."
            )
            time.sleep(sleep_seconds)

        data = response.json()
        self.token = extract_token(data)

        print("Z360 auth OK")

    def close(self) -> None:
        self.client.close()

    def search_orders(
        self,
        query: str,
        days_back: int,
        initial_price_from: float | None,
        max_short_results: int,
    ) -> list[dict]:
        publish_from = (date.today() - timedelta(days=days_back)).isoformat()

        params = {
            "SearchString": query,
            "PublishDateFrom": publish_from,
            "Law44": "true",
            "Law223": "true",
            "Commerce": "true",
            "IsActive": "true",
        }

        if initial_price_from is not None:
            params["InitialPriceFrom"] = initial_price_from

        print(f"Searching Z360: query={query!r}, PublishDateFrom={publish_from}")

        response = self.request("GET", "/api/orders/search", params=params)
        data = response.json()

        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected search response: {type(data).__name__}")

        print(f"Found short orders: {len(data)}")

        return data[:max_short_results]

    def get_order(self, order_id: int) -> dict:
        response = self.request("GET", f"/api/orders/{order_id}")
        data = response.json()

        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected order response: {type(data).__name__}")

        return data


def save_order(conn, short: dict, full: dict) -> None:
    order_id = short.get("orderId")
    if not order_id:
        return

    raw = {
        "source": "zakupki360",
        "short": short,
        "full": full,
        "description": build_description(full),
    }

    title = full.get("name") or short.get("name") or f"Закупка {order_id}"
    customer = organization_name(full)
    price = full.get("maxPrice") or short.get("maxPrice")
    published_at = parse_dt(full.get("publishDate") or short.get("publishDate"))
    deadline_at = parse_dt(full.get("endingDate") or short.get("endingDate"))
    url = procedure_url_from_sources({}, full)
    law = infer_law(full)
    procedure_type = full.get("tenderTypeName")
    region = full.get("deliveryPlace")

    products = full.get("products") or []

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenders (
                source, external_id, title, customer_name, initial_price,
                law, procedure_type, region, published_at, deadline_at,
                url, okpd2, raw, status
            )
            VALUES (
                'zakupki360', %(external_id)s, %(title)s, %(customer_name)s,
                %(initial_price)s, %(law)s, %(procedure_type)s, %(region)s,
                %(published_at)s, %(deadline_at)s, %(url)s, %(okpd2)s, %(raw)s, 'new'
            )
            ON CONFLICT (source, external_id)
            DO UPDATE SET
                title = EXCLUDED.title,
                customer_name = EXCLUDED.customer_name,
                initial_price = EXCLUDED.initial_price,
                law = EXCLUDED.law,
                procedure_type = EXCLUDED.procedure_type,
                region = EXCLUDED.region,
                published_at = EXCLUDED.published_at,
                deadline_at = EXCLUDED.deadline_at,
                url = EXCLUDED.url,
                okpd2 = EXCLUDED.okpd2,
                raw = EXCLUDED.raw,
                status = 'new',
                updated_at = now();
            """,
            {
                "external_id": str(order_id),
                "title": title,
                "customer_name": customer,
                "initial_price": price,
                "law": law,
                "procedure_type": procedure_type,
                "region": region,
                "published_at": published_at,
                "deadline_at": deadline_at,
                "url": url,
                "okpd2": Jsonb(products),
                "raw": Jsonb(raw),
            },
        )


def tender_exists(conn, order_id: int | str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM tenders
            WHERE source = 'zakupki360'
              AND external_id = %s
            LIMIT 1;
            """,
            (str(order_id),),
        )
        return cur.fetchone() is not None


def existing_external_ids(conn, external_ids: list[str] | tuple[str, ...]) -> set[str]:
    ids = [str(value) for value in external_ids if value]
    if not ids:
        return set()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT external_id
            FROM tenders
            WHERE source = 'zakupki360'
              AND external_id = ANY(%s);
            """,
            (ids,),
        )
        rows = cur.fetchall()

    return {
        str(row["external_id"] if isinstance(row, dict) else row[0])
        for row in rows
    }


def collect(
    query: str,
    days_back: int,
    limit_full: int,
    min_price: float | None,
    reload_existing: bool = False,
    max_short_results: int | None = None,
    client: Zakupki360Client | None = None,
) -> CollectionStats:
    if client is None:
        client = Zakupki360Client()

    if not client.token:
        client.login()

    short_results_limit = max_short_results or max(limit_full * 5, limit_full)

    short_orders = client.search_orders(
        query=query,
        days_back=days_back,
        initial_price_from=min_price,
        max_short_results=short_results_limit,
    )

    external_ids = tuple(
        dict.fromkeys(
            str(short.get("orderId"))
            for short in short_orders
            if short.get("orderId")
        )
    )

    saved = 0
    skipped_existing = 0
    failed = 0
    full_load_attempts = 0
    new_external_ids: tuple[str, ...] = ()

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        existing_ids_before = existing_external_ids(conn, external_ids)
        new_external_ids = tuple(
            external_id
            for external_id in external_ids
            if external_id not in existing_ids_before
        )

        for idx, short in enumerate(short_orders, start=1):
            order_id = short.get("orderId")
            if not order_id:
                continue

            if full_load_attempts >= limit_full:
                print(f"Full load request limit reached: {limit_full}")
                break

            if not reload_existing and tender_exists(conn, order_id):
                skipped_existing += 1
                print(f"[{idx}/{len(short_orders)}] Skip existing order {order_id}, full load not needed.")
                continue

            full_load_attempts += 1
            print(f"[{idx}/{len(short_orders)}] Loading full order {order_id}...")

            try:
                full = client.get_order(int(order_id))
                save_order(conn, short, full)
                saved += 1
            except httpx.HTTPStatusError as exc:
                failed += 1
                status_code = exc.response.status_code
                print(f"Failed to load/save order {order_id}: HTTP {status_code}")
                if status_code == 429:
                    print("Rate limit hit while loading full orders. Stop collection to protect API quota.")
                    break
            except Exception as exc:
                failed += 1
                print(f"Failed to load/save order {order_id}: {exc}")

        conn.commit()

    print(f"Saved/updated tenders: {saved}")
    print(f"Skipped existing tenders without full load: {skipped_existing}")
    print(f"Full load requests attempted: {full_load_attempts}")
    print(f"Failed full loads: {failed}")

    return CollectionStats(
        query=query,
        collected=len(short_orders),
        external_ids=external_ids,
        new_external_ids=new_external_ids,
        saved_or_updated=saved,
        skipped_existing=skipped_existing,
        full_load_attempts=full_load_attempts,
        failed_full_loads=failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect tenders from Zakupki360 API")
    parser.add_argument("--query", default="сервер", help="Search string")
    parser.add_argument("--days-back", type=int, default=7, help="Publish date from today-days")
    parser.add_argument("--limit-full", type=int, default=20, help="Maximum number of full orders to load")
    parser.add_argument("--max-short-results", type=int, default=None, help="How many short search results to inspect before full loading")
    parser.add_argument("--reload-existing", action="store_true", help="Reload full orders even if tender already exists in DB")
    parser.add_argument("--min-price", type=float, default=1000000, help="InitialPriceFrom")

    args = parser.parse_args()

    collect(
        query=args.query,
        days_back=args.days_back,
        limit_full=args.limit_full,
        min_price=args.min_price,
        reload_existing=args.reload_existing,
        max_short_results=args.max_short_results,
    )


if __name__ == "__main__":
    main()
