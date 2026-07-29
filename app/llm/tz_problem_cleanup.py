from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any


DEFAULT_TZ_PROBLEMS_ITEM = (
    "Явных противоречий в ТЗ не найдено; проверить срок поставки, "
    "эквивалентность и реестровые требования."
)

TZ_PROBLEM_SIGNAL_PATTERNS = (
    r"противореч",
    r"конфликт",
    r"неясн",
    r"непонятн",
    r"не понят",
    r"уточн",
    r"риск",
    r"невозмож",
    r"сомнител",
    r"отсутств",
    r"не указан",
    r"не указано",
    r"нет данных",
    r"нет срока",
    r"запрет",
    r"запрещ",
    r"не допуска",
    r"без эквивалент",
    r"эквивалент.*(?:нет|не|запрет|не допуска|отсутств)",
    r"отклонени",
    r"барьер",
    r"огранич",
    r"нацрежим",
    r"реестр",
    r"реестров",
    r"письм[оа] производител",
    r"авторизацион",
    r"локальн\w* инженер",
    r"местн\w* инженер",
    r"опыт участник",
    r"лиценз.*(?:барьер|риск|огранич|участник|опыт)",
    r"сертификат.*(?:барьер|риск|огранич|участник|опыт)",
    r"\bcontradict",
    r"\bconflict",
    r"\bunclear",
    r"\bambiguous",
    r"\bmissing",
    r"\babsent",
    r"\bimpossible",
    r"\binfeasible",
    r"\brisk",
    r"\brejection",
    r"\bbarrier",
    r"\bno equivalent",
    r"\bequivalent.*not allowed",
    r"\bmanufacturer authorization",
    r"\blocal engineer",
)

TZ_TECHNICAL_REQUIREMENT_PATTERNS = (
    r"\bcpu\b",
    r"\bram\b",
    r"\braid\b",
    r"\bpcie?\b",
    r"\bocp\b",
    r"\bddr\d?\b",
    r"\bsas\b",
    r"\bnvme\b",
    r"\bsata\b",
    r"\bsff\b",
    r"\blff\b",
    r"\b[1248]u\b",
    r"\bssd\b",
    r"\bhdd\b",
    r"\bports?\b",
    r"\bstorage\b",
    r"\bnetwork\b",
    r"\bsoftware\b",
    r"процессор",
    r"ядр",
    r"поток",
    r"частот",
    r"\bкэш\b",
    r"кэша",
    r"озу",
    r"оперативн",
    r"памят",
    r"слот",
    r"накопител",
    r"диск",
    r"контроллер",
    r"порт",
    r"интерфейс",
    r"видеоинтерфейс",
    r"сетев",
    r"ethernet",
    r"sfp",
    r"qsfp",
    r"rj-?45",
    r"форм-?фактор",
    r"бп",
    r"блок\w* питани",
    r"питани[ея]",
    r"охлаждени",
    r"мощност",
    r"высот\w* в стойке",
    r"порт\w* управлен",
    r"сертификат",
    r"лицензи",
    r"гаранти",
    r"комплект",
    r"программн",
)

TZ_REQUIREMENT_ONLY_PATTERNS = (
    r"количество",
    r"конфигураци",
    r"тип",
    r"объем",
    r"скорост",
    r"уровн",
    r"верси",
    r"резервирован",
    r"требует",
    r"требуется",
    r"должен",
    r"должна",
    r"должно",
    r"не менее",
    r"не ниже",
    r"обязател",
    r"наличие",
    r"поддержка",
    r"поставка",
    r"предоставить",
    r"соответств",
    r"характеристик",
    r"\brequired\b",
    r"\bmust\b",
)


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [text for item in value if (text := text_value(item))]
    text = text_value(value)
    return [text] if text else []


def _normalize_tz_problem_text(text: str) -> str:
    normalized = str(text or "").lower().replace("ё", "е")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _matches_any_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _matches_explicit_tz_problem(text: str) -> bool:
    has_sff_lff_2u_conflict = (
        re.search(r"\bsff\b", text)
        and re.search(r"\blff\b", text)
        and re.search(r"\b2u\b", text)
    )
    if has_sff_lff_2u_conflict:
        return True

    has_hot_swap_m2_issue = re.search(r"(?:hot[- ]?swap|горяч\w* замен)", text) and re.search(
        r"\bm\.?2\b",
        text,
    )
    if has_hot_swap_m2_issue:
        return True

    explicit_barriers = (
        r"письм[оа] производител",
        r"авторизацион",
        r"локальн\w* инженер",
        r"местн\w* инженер",
        r"эквивалент.*(?:не допуска|запрещ|нет|no)",
        r"без эквивалент",
        r"\bmanufacturer authorization",
        r"\blocal engineer",
        r"\bno equivalent",
    )
    return _matches_any_pattern(text, explicit_barriers)


def _is_similar_to_requirement(text: str, requirements: list[str]) -> bool:
    if not text:
        return False

    for requirement in requirements:
        normalized = _normalize_tz_problem_text(requirement)
        if not normalized:
            continue
        if text == normalized:
            return True
        if min(len(text), len(normalized)) >= 24 and (
            text in normalized or normalized in text
        ):
            return True
        if SequenceMatcher(None, text, normalized).ratio() >= 0.86:
            return True

    return False


def clean_problems_in_tz(
    problems: Any,
    critical_requirements: Any,
) -> list[str]:
    requirements = string_list(critical_requirements)
    cleaned: list[str] = []
    seen: set[str] = set()

    for item in string_list(problems):
        normalized = _normalize_tz_problem_text(item)
        if not normalized or normalized in seen:
            continue

        has_problem_signal = _matches_any_pattern(normalized, TZ_PROBLEM_SIGNAL_PATTERNS)
        is_explicit_problem = _matches_explicit_tz_problem(normalized)
        is_plain_requirement = _matches_any_pattern(
            normalized,
            TZ_TECHNICAL_REQUIREMENT_PATTERNS,
        ) or _matches_any_pattern(normalized, TZ_REQUIREMENT_ONLY_PATTERNS)
        repeats_requirement = _is_similar_to_requirement(normalized, requirements)

        if not (has_problem_signal or is_explicit_problem) and (
            is_plain_requirement or repeats_requirement
        ):
            continue

        cleaned.append(item)
        seen.add(normalized)

    return cleaned or [DEFAULT_TZ_PROBLEMS_ITEM]


def tz_requirement_context_from_report(report: dict[str, Any]) -> list[str]:
    tech = report.get("technical_spec_analysis")
    tech = tech if isinstance(tech, dict) else {}

    values: list[str] = []
    for value in (
        report.get("critical_requirements"),
        report.get("critical_tz_requirements"),
        report.get("what_is_required"),
        report.get("selection_parameters"),
        report.get("supply_composition"),
        tech.get("what_is_required"),
        tech.get("key_characteristics"),
        tech.get("implementation_or_services_required"),
    ):
        values.extend(string_list(value))

    return values


def clean_report_problems_in_tz(report: dict[str, Any]) -> list[str]:
    return clean_problems_in_tz(
        report.get("problems_in_tz"),
        tz_requirement_context_from_report(report),
    )


def apply_tz_problems_guardrail(report: dict[str, Any]) -> None:
    report["problems_in_tz"] = clean_report_problems_in_tz(report)
