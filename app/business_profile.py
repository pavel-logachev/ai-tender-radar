from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_BUSINESS_PROFILE_PATH = Path("config/business_profile.yaml")

DEFAULT_DOMESTIC_CONTEXT_KEYWORDS = [
    "российская платформа",
    "российский производитель",
    "российское оборудование",
    "отечественное по",
    "реестр российской промышленной продукции",
    "реестр минпромторга",
    "реестр минцифры",
    "пп рф 1875",
    "пп 1875",
    "пп 719",
    "национальный режим",
    "ограничение допуска",
    "запрет допуска",
    "ст-1",
    "реестровая запись",
    "российского происхождения",
]

DEFAULT_FOREIGN_VENDOR_RECOMMENDATION_KEYWORDS = [
    "dell",
    "poweredge",
    "hpe",
    "hewlett packard",
    "hewlett-packard",
    "hp ",
    "proliant",
    "lenovo",
    "thinksystem",
    "huawei",
    "cisco",
    "supermicro",
    "inspur",
    "samsung",
]

DEFAULT_DOMESTIC_SAFE_VENDOR_EXAMPLES = [
    "YADRO",
    "Aquarius",
    "DEPO",
    "Kraftway",
    "Рикор",
    "Fplus",
    "Норси-Транс",
    "Аэродиск",
]


def normalize(value: object) -> str:
    return str(value or "").lower().replace("ё", "е")


def contains_any(text: str, patterns: list[str]) -> bool:
    normalized = normalize(text)
    return any(normalize(pattern) in normalized for pattern in patterns if pattern)


def load_business_profile(path: str | Path | None = None) -> dict[str, Any]:
    profile_path = Path(path or DEFAULT_BUSINESS_PROFILE_PATH)

    if not profile_path.exists():
        raise FileNotFoundError(f"Business profile not found: {profile_path}")

    data = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}

    if not isinstance(data, dict):
        raise ValueError(f"Business profile must be a mapping: {profile_path}")

    return data


def profile_rule_keywords(
    profile: dict[str, Any] | None,
    group_name: str,
    fallback: list[str],
    *,
    field_name: str = "keywords",
) -> list[str]:
    rule_patterns = (profile or {}).get("rule_patterns") or {}

    if not isinstance(rule_patterns, dict):
        return fallback

    group = rule_patterns.get(group_name) or {}
    if not isinstance(group, dict):
        return fallback

    values = group.get(field_name) or []
    if not isinstance(values, list):
        return fallback

    keywords = [str(value) for value in values if value]
    return keywords or fallback


def domestic_context_keywords(profile: dict[str, Any] | None = None) -> list[str]:
    return profile_rule_keywords(
        profile,
        "domestic_restrictions",
        DEFAULT_DOMESTIC_CONTEXT_KEYWORDS,
    )


def foreign_vendor_recommendation_keywords(profile: dict[str, Any] | None = None) -> list[str]:
    return profile_rule_keywords(
        profile,
        "foreign_vendor_recommendations",
        DEFAULT_FOREIGN_VENDOR_RECOMMENDATION_KEYWORDS,
    )


def domestic_safe_vendor_examples(profile: dict[str, Any] | None = None) -> list[str]:
    return profile_rule_keywords(
        profile,
        "domestic_safe_vendor_examples",
        DEFAULT_DOMESTIC_SAFE_VENDOR_EXAMPLES,
        field_name="vendors",
    )


def row_text(row: dict[str, Any]) -> str:
    parts: list[str] = [
        str(row.get("title") or ""),
        str(row.get("customer_name") or ""),
        str(row.get("region") or ""),
    ]

    result = row.get("result") or {}
    if isinstance(result, dict):
        parts.append(str(result.get("summary") or ""))
        for key in ("positive_matches", "why_relevant", "manual_checks"):
            value = result.get(key)
            if isinstance(value, list):
                parts.extend(str(item) for item in value)

    return " ".join(parts)


def row_evidence_text(row: dict[str, Any]) -> str:
    parts: list[str] = [
        str(row.get("title") or ""),
    ]

    result = row.get("result") or {}
    if isinstance(result, dict):
        parts.append(str(result.get("summary") or ""))

    raw = row.get("raw") or {}
    if isinstance(raw, dict):
        parts.append(str(raw.get("description") or ""))
        full = raw.get("full") or {}
        if isinstance(full, dict):
            parts.extend(_nested_text_values(full.get("products")))
            parts.extend(_nested_text_values(full.get("okpd2")))

    parts.extend(_nested_text_values(row.get("okpd2")))

    llm_result = row.get("llm_report_result") or {}
    if isinstance(llm_result, dict):
        report = llm_result.get("report") or {}
        if isinstance(report, dict):
            parts.append(str(report.get("summary") or ""))
            parts.append(str(report.get("detected_category") or ""))
            facts = report.get("extracted_spec_facts") or {}
            if isinstance(facts, dict):
                parts.append(str(facts.get("category") or ""))
                for key in ("supply_items", "technical_characteristics"):
                    values = facts.get(key)
                    if isinstance(values, list):
                        parts.extend(str(value) for value in values)

    return " ".join(part for part in parts if part)


def row_explicit_hardware_evidence_text(row: dict[str, Any]) -> str:
    parts: list[str] = [
        str(row.get("title") or ""),
    ]

    result = row.get("result") or {}
    if isinstance(result, dict):
        parts.append(str(result.get("summary") or ""))

    raw = row.get("raw") or {}
    if isinstance(raw, dict):
        full = raw.get("full") or {}
        if isinstance(full, dict):
            for key in ("products", "okpd2"):
                parts.extend(_nested_text_values(full.get(key)))

            documents = full.get("documents") or []
            if isinstance(documents, list):
                for document in documents:
                    if isinstance(document, dict):
                        for key in ("title", "name", "fileName", "filename"):
                            parts.append(str(document.get(key) or ""))

    parts.extend(_nested_text_values(row.get("okpd2")))

    document_result = row.get("document_risk_result") or {}
    if isinstance(document_result, dict):
        parts.extend(_nested_text_values(document_result.get("key_findings")))
        parts.extend(_nested_text_values(document_result.get("evidence")))

    llm_result = row.get("llm_report_result") or {}
    if isinstance(llm_result, dict):
        report = llm_result.get("report") or {}
        if isinstance(report, dict):
            parts.append(str(report.get("summary") or ""))
            facts = report.get("extracted_spec_facts") or {}
            if isinstance(facts, dict):
                for key in ("supply_items", "technical_characteristics"):
                    values = facts.get(key)
                    if isinstance(values, list):
                        parts.extend(str(value) for value in values)

    return " ".join(part for part in parts if part)


def _nested_text_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(_nested_text_values(item))
        return values

    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_nested_text_values(item))
        return values

    if value is None:
        return []

    return [str(value)]


def get_price(row: dict[str, Any]) -> int:
    try:
        return int(float(row.get("initial_price") or 0))
    except (TypeError, ValueError):
        return 0


def get_deal_action_keywords(profile: dict[str, Any]) -> list[str]:
    values = profile.get("deal_action_keywords") or []
    return [str(value) for value in values]


def iter_target_categories(profile: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    categories = profile.get("target_categories") or {}
    items: list[tuple[str, dict[str, Any]]] = []

    if not isinstance(categories, dict):
        return items

    for name, cfg in categories.items():
        if isinstance(cfg, dict):
            items.append((str(name), cfg))

    return sorted(items, key=lambda item: int(item[1].get("llm_priority", 999)))


def match_target_category(row: dict[str, Any], profile: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    text = row_evidence_text(row)

    for name, cfg in iter_target_categories(profile):
        keywords = [str(value) for value in (cfg.get("keywords") or [])]
        if contains_any(text, keywords):
            return name, cfg

    return None, None


def is_excluded_vertical(row: dict[str, Any], profile: dict[str, Any]) -> bool:
    text = row_text(row)
    groups = profile.get("excluded_verticals") or {}

    if not isinstance(groups, dict):
        return False

    for cfg in groups.values():
        if not isinstance(cfg, dict):
            continue
        keywords = [str(value) for value in (cfg.get("keywords") or [])]
        if contains_any(text, keywords):
            return True

    return False


def is_low_priority_deal(row: dict[str, Any], profile: dict[str, Any]) -> bool:
    text = row_text(row)
    groups = profile.get("low_priority_deal_types") or {}

    if not isinstance(groups, dict):
        return False

    for cfg in groups.values():
        if not isinstance(cfg, dict):
            continue
        keywords = [str(value) for value in (cfg.get("keywords") or [])]
        if contains_any(text, keywords):
            return True

    return False


def is_generic_equipment_title(row: dict[str, Any], profile: dict[str, Any]) -> bool:
    title = str(row.get("title") or "")
    rule_patterns = profile.get("rule_patterns") or {}
    generic_cfg = rule_patterns.get("generic_equipment_titles") if isinstance(rule_patterns, dict) else {}
    fallback = [
        "поставка оборудования",
        "закупка оборудования",
        "приобретение оборудования",
    ]
    keywords = fallback
    if isinstance(generic_cfg, dict) and isinstance(generic_cfg.get("keywords"), list):
        keywords = [str(value) for value in generic_cfg.get("keywords") or [] if value] or fallback

    return contains_any(title, keywords)


def has_generic_equipment_target_hardware_evidence(row: dict[str, Any], profile: dict[str, Any]) -> bool:
    rule_patterns = profile.get("rule_patterns") or {}
    generic_cfg = rule_patterns.get("generic_equipment_titles") if isinstance(rule_patterns, dict) else {}
    fallback = [
        "поставка серверного оборудования",
        "поставка сервера",
        "поставка серверов",
        "поставка системы хранения",
        "поставка схд",
        "сервер",
        "серверы",
        "серверное оборудование",
        "система хранения данных",
        "система хранения",
        "схд",
        "коммутатор",
        "коммутаторы",
        "поставка коммутатора",
        "поставка коммутаторов",
        "маршрутизатор",
        "маршрутизаторы",
        "поставка маршрутизатора",
        "поставка маршрутизаторов",
        "межсетевой экран",
        "поставка межсетевого экрана",
        "поставка сетевого оборудования",
        "сетевое оборудование",
        "вычислительный комплекс",
        "программно-аппаратный комплекс",
        "ибп",
        "источник бесперебойного питания",
        "телекоммуникационный шкаф",
        "шкаф телекоммуникационный",
    ]
    keywords = fallback
    if isinstance(generic_cfg, dict) and isinstance(
        generic_cfg.get("explicit_hardware_evidence_keywords"),
        list,
    ):
        keywords = [
            str(value)
            for value in generic_cfg.get("explicit_hardware_evidence_keywords") or []
            if value
        ] or fallback

    return contains_any(row_explicit_hardware_evidence_text(row), keywords)


def is_full_deal_for_category(row: dict[str, Any], profile: dict[str, Any], category_cfg: dict[str, Any]) -> bool:
    text = row_evidence_text(row)
    generic_title = is_generic_equipment_title(row, profile)

    if generic_title and not has_generic_equipment_target_hardware_evidence(row, profile):
        return False

    full_deal_keywords = [str(value) for value in (category_cfg.get("full_deal_keywords") or [])]
    if contains_any(text, full_deal_keywords):
        return True

    category_keywords = [str(value) for value in (category_cfg.get("keywords") or [])]
    action_keywords = get_deal_action_keywords(profile)

    return contains_any(text, category_keywords) and contains_any(text, action_keywords)


def category_priority(category_cfg: dict[str, Any] | None) -> int:
    if not category_cfg:
        return 999

    try:
        return int(category_cfg.get("llm_priority", 999))
    except (TypeError, ValueError):
        return 999
