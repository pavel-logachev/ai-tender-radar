from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import settings


IMPLEMENTATION_WORDS = [
    "внедрение",
    "настройка",
    "сопровождение",
    "пусконалад",
    "монтаж",
    "интеграция",
]


def normalize(text: str | None) -> str:
    return (text or "").lower().replace("ё", "е")


def contains_any(text: str, keywords: list[str]) -> list[str]:
    found = []
    normalized = normalize(text)

    for keyword in keywords:
        kw = normalize(keyword).strip()
        if kw and kw in normalized:
            found.append(keyword)

    return found


def load_company_profile(conn) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT profile
            FROM company_profiles
            ORDER BY updated_at DESC
            LIMIT 1;
            """
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError("Company profile not found")

    return row["profile"]


def score_tender(tender: dict, profile: dict) -> dict:
    scoring = profile.get("scoring", {})

    positive_keywords = profile.get("positive_keywords", [])
    negative_keywords = profile.get("negative_keywords", [])
    preferred_regions = profile.get("preferred_regions", [])

    title = tender.get("title") or ""
    raw_description = (tender.get("raw") or {}).get("description", "")
    text = f"{title}\n{raw_description}"

    score = 0
    why_relevant = []
    context_factors = []
    risks = []
    rejection_reasons = []
    manual_checks = [
        "Проверить требования к участнику и опыту",
        "Проверить обеспечение заявки/контракта",
        "Проверить сроки поставки/выполнения",
        "Проверить наличие конкретного производителя и возможность аналога",
        "Проверить историю заказчика и победителей",
    ]

    positive_matches = contains_any(text, positive_keywords)
    negative_matches = contains_any(text, negative_keywords)
    implementation_matches = contains_any(text, IMPLEMENTATION_WORDS)

    has_profile_signal = bool(positive_matches)

    if positive_matches:
        score += int(scoring.get("keyword_match", 30))
        why_relevant.append(
            f"Найдены профильные IT-признаки: {', '.join(positive_matches[:8])}"
        )
    else:
        rejection_reasons.append("Не найдено профильных IT-признаков")

    if implementation_matches and has_profile_signal:
        score += int(scoring.get("implementation_or_support", 15))
        why_relevant.append(
            f"Есть признаки работ/услуг: {', '.join(implementation_matches)}"
        )

    if negative_matches:
        score += int(scoring.get("negative_keyword_penalty", -50))
        rejection_reasons.append(
            f"Найдены стоп-слова: {', '.join(negative_matches[:8])}"
        )

    price = tender.get("initial_price")
    min_price = profile.get("min_price_rub")
    max_price = profile.get("max_price_rub")

    if price is not None and min_price is not None and max_price is not None:
        price = float(price)
        price_text = f"{price:,.0f} ₽".replace(",", " ")

        if min_price <= price <= max_price:
            score += int(scoring.get("price_in_range", 20))
            context_factors.append(f"Сумма в целевом диапазоне: {price_text}")
        elif price < min_price:
            score += int(scoring.get("too_small_penalty", -30))
            rejection_reasons.append(f"Сумма ниже минимального порога: {price_text}")
        else:
            risks.append(f"Сумма выше целевого диапазона: {price_text}")

    region = tender.get("region") or ""
    if region:
        region_keywords = [
            item for item in preferred_regions
            if normalize(item) not in ("вся россия", "вся рф", "россия", "рф")
        ]
        region_matches = contains_any(region, region_keywords)

        if region_matches:
            score += int(scoring.get("region_match", 10))
            context_factors.append(f"Регион подходит: {region}")
        elif has_profile_signal:
            risks.append(f"Регион требует ручной проверки: {region[:160]}")

    deadline_at = tender.get("deadline_at")
    days_left = None
    too_soon = False

    if deadline_at:
        now = datetime.now(timezone.utc)
        days_left = (deadline_at - now).days

        if days_left >= 5:
            score += int(scoring.get("enough_time_before_deadline", 15))
            context_factors.append(f"До окончания подачи достаточно времени: {days_left} дн.")
        elif days_left < 3:
            too_soon = True
            score += int(scoring.get("too_soon_deadline_penalty", -30))
            risks.append(f"Мало времени до окончания подачи: {days_left} дн.")

    score = max(0, min(100, score))

    if not has_profile_signal:
        score = min(score, 25)

    if negative_matches and not has_profile_signal:
        score = 0

    if too_soon:
        score = min(score, 60)

    if not has_profile_signal or (negative_matches and score < 40):
        recommendation = "no_go"
        confidence = "high"
    elif score >= 70:
        recommendation = "go"
        confidence = "medium"
    elif score >= 40:
        recommendation = "maybe"
        confidence = "medium"
    else:
        recommendation = "no_go"
        confidence = "high"

    return {
        "summary": title,
        "score": score,
        "recommendation": recommendation,
        "confidence": confidence,
        "positive_matches": positive_matches,
        "negative_matches": negative_matches,
        "implementation_matches": implementation_matches,
        "days_left": days_left,
        "why_relevant": why_relevant,
        "positive_factors": context_factors if recommendation != "no_go" else [],
        "context_factors": context_factors,
        "risks": risks,
        "rejection_reasons": rejection_reasons,
        "manual_checks": manual_checks,
    }


def main() -> None:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        profile = load_company_profile(conn)

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT *
                FROM tenders
                WHERE status IN ('new', 'scored')
                ORDER BY created_at DESC
                LIMIT 100;
                """
            )
            tenders = cur.fetchall()

        print(f"Found {len(tenders)} tenders to score")

        for tender in tenders:
            result = score_tender(tender, profile)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM analysis_results
                    WHERE tender_id = %s
                      AND analysis_type = 'rule_based_score';
                    """,
                    (tender["id"],),
                )

                cur.execute(
                    """
                    INSERT INTO analysis_results (
                        tender_id, analysis_type, model, result,
                        score, recommendation, confidence
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        tender["id"],
                        "rule_based_score",
                        "rules-v1",
                        Jsonb(result),
                        result["score"],
                        result["recommendation"],
                        result["confidence"],
                    ),
                )

                cur.execute(
                    """
                    UPDATE tenders
                    SET status = 'scored', updated_at = now()
                    WHERE id = %s;
                    """,
                    (tender["id"],),
                )

        conn.commit()

    print("Scoring finished")


if __name__ == "__main__":
    main()
