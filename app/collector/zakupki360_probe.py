from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx

from app.config import settings


def mask(value: str | None, left: int = 6, right: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= left + right:
        return "***"
    return f"{value[:left]}...{value[-right:]}"


def extract_token(data: Any) -> str:
    if isinstance(data, str):
        return data.strip().strip('"')

    if isinstance(data, dict):
        for key in ("access_token", "token", "jwt", "bearerToken", "bearer_token"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value

    raise RuntimeError(f"Cannot find token in response: {data}")


def main() -> None:
    if not settings.z360_login or not settings.z360_password:
        raise RuntimeError("Z360_LOGIN and Z360_PASSWORD must be set in .env")

    base_url = settings.z360_base_url.rstrip("/")

    with httpx.Client(base_url=base_url, timeout=30) as client:
        print("=== Z360 token request ===")
        token_response = client.post(
            "/token",
            json={
                "login": settings.z360_login,
                "password": settings.z360_password,
            },
        )

        print("status:", token_response.status_code)
        print("content-type:", token_response.headers.get("content-type"))

        if token_response.status_code != 200:
            print(token_response.text[:1000])
            token_response.raise_for_status()

        try:
            token_payload = token_response.json()
        except Exception:
            token_payload = token_response.text

        if isinstance(token_payload, dict):
            print("token response keys:", list(token_payload.keys()))
        else:
            print("token response type:", type(token_payload).__name__)

        token = extract_token(token_payload)
        print("token:", mask(token))
        print("token_length:", len(token))

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        publish_from = (date.today() - timedelta(days=7)).isoformat()

        params = {
            "SearchString": "сервер",
            "PublishDateFrom": publish_from,
            "Law44": "true",
            "Law223": "true",
            "Commerce": "true",
            "IsActive": "true",
            "InitialPriceFrom": 1000000,
        }

        print()
        print("=== Z360 /api/orders/search ===")
        print("params:", params)

        search_response = client.get(
            "/api/orders/search",
            headers=headers,
            params=params,
        )

        print("status:", search_response.status_code)
        print("content-type:", search_response.headers.get("content-type"))

        if search_response.status_code != 200:
            print(search_response.text[:2000])
            search_response.raise_for_status()

        orders = search_response.json()
        print("orders_type:", type(orders).__name__)
        print("orders_count:", len(orders) if isinstance(orders, list) else "not-list")

        if isinstance(orders, list) and orders:
            first = orders[0]
            print()
            print("=== First order short keys ===")
            print(list(first.keys()))
            print("first order short:", first)

            order_id = first.get("orderId")
            if order_id:
                print()
                print(f"=== Z360 /api/orders/{order_id} ===")
                full_response = client.get(
                    f"/api/orders/{order_id}",
                    headers=headers,
                )

                print("status:", full_response.status_code)
                print("content-type:", full_response.headers.get("content-type"))

                if full_response.status_code == 200:
                    full = full_response.json()
                    print("full order keys:", list(full.keys()))
                    print("documents_count:", len(full.get("documents") or []))
                    print("products_count:", len(full.get("products") or []))
                    print("customers_count:", len(full.get("customers") or []))

                    print()
                    print("selected full order fields:")
                    for key in [
                        "orderNumber",
                        "name",
                        "tenderUrl",
                        "tenderTypeName",
                        "tenderStageName",
                        "maxPrice",
                        "publishDate",
                        "endingDate",
                        "deliveryTerm",
                        "deliveryPlace",
                        "contactPerson",
                        "contactEMail",
                        "contactPhone",
                        "guaranteeOrder",
                        "guaranteeExecuteContract",
                    ]:
                        print(f"{key}: {full.get(key)}")

                    documents = full.get("documents") or []
                    if documents:
                        print()
                        print("first document:", documents[0])
                else:
                    print(full_response.text[:2000])


if __name__ == "__main__":
    main()
