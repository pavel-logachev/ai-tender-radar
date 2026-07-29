from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.config import settings
from app.llm.base import LLMResponse
from app.llm.factory import create_llm_client


ANALYSIS_TYPE = "llm_quick_score"
DECISIONS = {"go", "maybe", "no_go"}
CONFIDENCES = {"low", "medium", "high"}
YES_NO = {"yes", "no"}

SYSTEM_PROMPT = """
You are a fast tender triage layer for an IT integrator.

Use only the supplied tender card JSON and short rule/business signals.
Do not use or ask for a full tender report. Do not invent facts, contacts,
phones, emails, prices, dates, models, or document contents.

Your task is only to decide:
- whether the tender is worth the next check;
- whether it is worth spending resources on docs/full-analysis stage;
- what should be clarified first.

Return strict compact JSON only.
""".strip()


@dataclass(frozen=True)
class QuickScoreRunResult:
    input_payload: dict[str, Any]
    report: dict[str, Any]
    response: LLMResponse
    raw_response: str


class QuickScoreParseError(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        input_payload: dict[str, Any],
        response: LLMResponse,
        raw_response: str,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.input_payload = input_payload
        self.response = response
        self.raw_response = raw_response


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]

    return value


def compact_text(value: Any, limit: int = 1200) -> str:
    if value is None:
        return ""

    if isinstance(value, (dict, list)):
        text = json.dumps(json_safe(value), ensure_ascii=False)
    else:
        text = str(value)

    text = " ".join(text.split())
    if len(text) <= limit:
        return text

    return text[: limit - 3].rstrip() + "..."


def text_value(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, (int, float, bool, Decimal)):
        return str(value)

    return json.dumps(json_safe(value), ensure_ascii=False)


def string_list(value: Any, *, limit: int = 6, item_limit: int = 260) -> list[str]:
    if value is None:
        return []

    items = value if isinstance(value, list) else [value]
    result: list[str] = []

    for item in items:
        text = compact_text(item, item_limit)
        if text:
            result.append(text)

        if len(result) >= limit:
            break

    return result


def safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value

    return None


def raw_full(tender: dict[str, Any]) -> dict[str, Any]:
    raw = tender.get("raw") or {}
    full = raw.get("full") or {}
    return full if isinstance(full, dict) else {}


def raw_short(tender: dict[str, Any]) -> dict[str, Any]:
    raw = tender.get("raw") or {}
    short_data = raw.get("short") or {}
    return short_data if isinstance(short_data, dict) else {}


def customer_name(tender: dict[str, Any], full: dict[str, Any]) -> str | None:
    if tender.get("customer_name"):
        return text_value(tender.get("customer_name"))

    placer = full.get("placerOrganization") or {}
    if isinstance(placer, dict):
        name = placer.get("fullName") or placer.get("name")
        if name:
            return text_value(name)

    customers = full.get("customers") or []
    if isinstance(customers, list) and customers:
        first = customers[0] or {}
        if isinstance(first, dict):
            name = first.get("fullName") or first.get("name")
            if name:
                return text_value(name)

    return None


def rule_result(tender: dict[str, Any]) -> dict[str, Any]:
    result = tender.get("result") or tender.get("rule_result") or {}
    return result if isinstance(result, dict) else {}


def build_short_card_text(tender: dict[str, Any], full: dict[str, Any]) -> str:
    raw = tender.get("raw") or {}
    result = rule_result(tender)
    parts = [
        tender.get("title"),
        result.get("summary"),
        raw.get("description") if isinstance(raw, dict) else None,
        full.get("name"),
        full.get("deliveryPlace"),
        full.get("deliveryTerm"),
    ]

    return compact_text("\n".join(text_value(part) for part in parts if part), 1800)


def build_rule_based_result(tender: dict[str, Any]) -> dict[str, Any]:
    return {
        "score": json_safe(first_present(tender.get("score"), tender.get("rule_score"))),
        "recommendation": first_present(tender.get("recommendation"), tender.get("rule_recommendation")),
        "confidence": first_present(tender.get("confidence"), tender.get("rule_confidence")),
        "result": rule_result(tender),
    }


def build_business_relevance_signals(
    tender: dict[str, Any],
    business_signals: dict[str, Any] | None,
) -> dict[str, Any]:
    result = rule_result(tender)
    signals: dict[str, Any] = {
        "why_relevant": string_list(result.get("why_relevant")),
        "positive_factors": string_list(result.get("positive_factors")),
        "risks": string_list(result.get("risks")),
        "manual_checks": string_list(result.get("manual_checks")),
        "positive_matches": string_list(result.get("positive_matches")),
        "negative_matches": string_list(result.get("negative_matches")),
    }

    if business_signals:
        signals.update(json_safe(business_signals))

    return signals


def build_document_flags(tender: dict[str, Any], full: dict[str, Any]) -> dict[str, Any]:
    card_documents = full.get("documents") or []
    if not isinstance(card_documents, list):
        card_documents = []

    document_risk = tender.get("document_risk_result") or {}
    if not isinstance(document_risk, dict):
        document_risk = {}

    return {
        "card_documents_count": len(card_documents),
        "downloaded_documents_count": safe_int(tender.get("docs_count")),
        "downloaded_documents_with_text_count": safe_int(tender.get("docs_with_text")),
        "document_risk_analysis_available": bool(document_risk),
        "document_risk": {
            "tailoring_risk": document_risk.get("tailoring_risk"),
            "risk_score": document_risk.get("risk_score"),
            "documents_count": document_risk.get("documents_count"),
            "documents_with_text": document_risk.get("documents_with_text"),
        } if document_risk else {},
    }


def build_quick_score_input(
    tender: dict[str, Any],
    *,
    business_signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    full = raw_full(tender)
    short_data = raw_short(tender)

    return json_safe(
        {
            "tender": {
                "id": str(tender.get("tender_id") or tender.get("id") or ""),
                "external_id": tender.get("external_id"),
                "title": tender.get("title"),
                "customer": customer_name(tender, full),
                "price_nmc": tender.get("initial_price"),
                "currency": tender.get("currency") or "RUB",
                "deadline": tender.get("deadline_at"),
                "published_at": tender.get("published_at"),
                "region_or_delivery_place": tender.get("region") or full.get("deliveryPlace"),
                "delivery_term": full.get("deliveryTerm"),
                "law": tender.get("law"),
                "procurement_method": tender.get("procedure_type") or full.get("tenderTypeName"),
                "stage": full.get("tenderStageName"),
                "platform": short_data.get("etpName"),
                "url": tender.get("url") or full.get("tenderUrl"),
            },
            "contact_from_card": {
                "person": full.get("contactPerson"),
                "phone": full.get("contactPhone"),
                "email": full.get("contactEMail"),
            },
            "short_card_text": build_short_card_text(tender, full),
            "rule_based_result": build_rule_based_result(tender),
            "business_relevance_signals": build_business_relevance_signals(
                tender,
                business_signals,
            ),
            "document_availability_flags": build_document_flags(tender, full),
        }
    )


def build_user_prompt(input_payload: dict[str, Any]) -> str:
    input_json = json.dumps(input_payload, ensure_ascii=False, indent=2)

    return f"""
Analyze this tender card quickly and return only the compact JSON object.

Required output schema:
{{
  "decision": "go | maybe | no_go",
  "confidence": "low | medium | high",
  "why_interesting": ["short fact-based reason"],
  "main_risks": ["short risk"],
  "missing_data": ["specific missing item to check first"],
  "should_run_full_analysis": "yes | no",
  "next_action_short": "one short concrete action"
}}

Rules:
- Do not output markdown.
- Keep arrays to 0-4 items.
- Keep every string short.
- Use no facts outside input JSON.
- Contacts/phone/email may only come from contact_from_card; do not invent them.
- This is not a full presales report.
- should_run_full_analysis means whether to continue to docs/full-analysis stage.
- should_run_full_analysis=yes means the tender is worth spending resources on the next pipeline stage.
- should_run_full_analysis=no means do not spend more pipeline resources.
- decision=go normally implies should_run_full_analysis=yes.
- decision=maybe can imply yes when missing data should be checked in docs/full-analysis.
- decision=no_go normally implies should_run_full_analysis=no.
- Do not interpret should_run_full_analysis as "are documents already downloaded".

Tender card JSON:
{input_json}
""".strip()


def extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()

    try:
        parsed = json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError(f"Cannot parse JSON from LLM quick-score response: {text[:1500]}")

        parsed = json.loads(raw[start : end + 1])

    if not isinstance(parsed, dict):
        raise RuntimeError("LLM quick-score response is not a JSON object")

    return parsed


def parse_error_detail(text: str, exc: Exception) -> str:
    preview = compact_text(text, 700) or "<empty response>"
    return f"Cannot parse LLM quick-score JSON: {exc}. response_preview={preview}"


def normalize_choice(value: Any, allowed: set[str], fallback: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "nogo":
        normalized = "no_go"

    if normalized in allowed:
        return normalized

    return fallback


def normalize_yes_no(value: Any) -> str | None:
    if isinstance(value, bool):
        return "yes" if value else "no"

    normalized = str(value or "").strip().lower()
    if normalized in YES_NO:
        return normalized

    if normalized in {"true", "1", "run", "continue", "full_analysis", "download", "download_docs"}:
        return "yes"

    if normalized in {"false", "0", "stop", "skip"}:
        return "no"

    return None


def normalize_should_run_full_analysis(
    value: Any,
    *,
    decision: str,
    missing_data: list[str],
) -> str:
    normalized = normalize_yes_no(value)

    if decision == "no_go":
        return "no"

    if normalized:
        return normalized

    if decision == "go":
        return "yes"

    if decision == "maybe" and missing_data:
        return "yes"

    return "no"


def normalize_quick_score(parsed: dict[str, Any]) -> dict[str, Any]:
    report = parsed.get("report")
    if isinstance(report, dict):
        parsed = report

    decision = normalize_choice(parsed.get("decision"), DECISIONS, "maybe")
    missing_data = string_list(parsed.get("missing_data"), limit=4)
    should_run_value = parsed.get("should_run_full_analysis")

    normalized = {
        "decision": decision,
        "confidence": normalize_choice(parsed.get("confidence"), CONFIDENCES, "low"),
        "why_interesting": string_list(parsed.get("why_interesting"), limit=4),
        "main_risks": string_list(parsed.get("main_risks"), limit=4),
        "missing_data": missing_data,
        "should_run_full_analysis": normalize_should_run_full_analysis(
            should_run_value,
            decision=decision,
            missing_data=missing_data,
        ),
        "next_action_short": compact_text(parsed.get("next_action_short"), 420),
    }

    if not normalized["next_action_short"]:
        normalized["next_action_short"] = "Review the card, then decide whether to run full analysis."

    return normalized


def response_meta(response: LLMResponse, *, json_mode: bool, input_chars: int) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "provider": response.provider,
        "model": response.model,
        "json_mode": json_mode,
        "input_chars": input_chars,
    }

    if response.response_id:
        meta["response_id"] = response.response_id

    if response.latency_seconds is not None:
        meta["latency_seconds"] = round(response.latency_seconds, 3)

    if response.usage:
        meta["usage"] = response.usage

    return meta


def run_llm_quick_score(
    tender: dict[str, Any],
    *,
    business_signals: dict[str, Any] | None = None,
    provider: str | None = None,
    model: str | None = None,
    max_output_tokens: int = 700,
    json_mode: bool = True,
) -> QuickScoreRunResult:
    input_payload = build_quick_score_input(tender, business_signals=business_signals)
    user_prompt = build_user_prompt(input_payload)
    client = create_llm_client(provider=provider, model=model)

    response = client.generate_chat_completion(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.0,
        max_tokens=max_output_tokens,
        json_mode=json_mode,
    )
    raw_response = response.text or ""
    try:
        report = normalize_quick_score(extract_json(raw_response))
    except Exception as exc:
        raise QuickScoreParseError(
            parse_error_detail(raw_response, exc),
            input_payload=input_payload,
            response=response,
            raw_response=raw_response,
        ) from exc

    return QuickScoreRunResult(
        input_payload=input_payload,
        report=report,
        response=response,
        raw_response=raw_response,
    )


def save_llm_quick_score(
    *,
    tender_id: str,
    result: QuickScoreRunResult,
    metadata: dict[str, Any] | None = None,
) -> None:
    meta = response_meta(
        result.response,
        json_mode=bool((metadata or {}).get("json_mode", True)),
        input_chars=len(json.dumps(result.input_payload, ensure_ascii=False)),
    )
    if metadata:
        meta.update(json_safe(metadata))

    stored_result = {
        "provider": result.response.provider,
        "model": result.response.model,
        "created_at": datetime.now().isoformat(),
        "meta": meta,
        "quick_score_input": result.input_payload,
        "report": result.report,
        "raw_response": result.raw_response,
    }

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM analysis_results
                WHERE tender_id = %s
                  AND analysis_type = %s;
                """,
                (tender_id, ANALYSIS_TYPE),
            )

            cur.execute(
                """
                INSERT INTO analysis_results (
                    tender_id,
                    analysis_type,
                    model,
                    result,
                    score,
                    recommendation,
                    confidence
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    tender_id,
                    ANALYSIS_TYPE,
                    result.response.model,
                    Jsonb(json_safe(stored_result)),
                    None,
                    result.report["decision"],
                    result.report["confidence"],
                ),
            )

        conn.commit()


def save_llm_quick_score_error(
    *,
    tender_id: str,
    error: QuickScoreParseError,
    metadata: dict[str, Any] | None = None,
) -> None:
    meta = response_meta(
        error.response,
        json_mode=bool((metadata or {}).get("json_mode", True)),
        input_chars=len(json.dumps(error.input_payload, ensure_ascii=False)),
    )
    if metadata:
        meta.update(json_safe(metadata))

    stored_result = {
        "provider": error.response.provider,
        "model": error.response.model,
        "created_at": datetime.now().isoformat(),
        "status": "failed",
        "meta": meta,
        "quick_score_input": error.input_payload,
        "error": {
            "stage": "parse",
            "detail": error.detail,
            "raw_response_preview": compact_text(error.raw_response, 1200),
        },
        "raw_response": error.raw_response,
    }

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM analysis_results
                WHERE tender_id = %s
                  AND analysis_type = %s;
                """,
                (tender_id, ANALYSIS_TYPE),
            )

            cur.execute(
                """
                INSERT INTO analysis_results (
                    tender_id,
                    analysis_type,
                    model,
                    result,
                    score,
                    recommendation,
                    confidence
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    tender_id,
                    ANALYSIS_TYPE,
                    error.response.model,
                    Jsonb(json_safe(stored_result)),
                    None,
                    None,
                    None,
                ),
            )

        conn.commit()
