from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Any

from app.business_profile import load_business_profile


def _as_int(value) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _tailoring_level(value) -> str:
    if isinstance(value, dict):
        return str(value.get("level") or value.get("risk") or "").lower()
    return str(value or "").lower()


def _llm_report(tender: dict) -> dict[str, Any]:
    result = tender.get("llm_report_result") or {}
    if not isinstance(result, dict):
        return {}

    report = result.get("report") or {}
    return report if isinstance(report, dict) else {}


def _llm_decision(report: dict[str, Any]) -> str:
    recommendation = report.get("recommendation") or {}
    if isinstance(recommendation, dict):
        decision = recommendation.get("decision")
        if decision:
            return str(decision).lower()

    verdict = report.get("verdict") or {}
    if isinstance(verdict, dict):
        decision = verdict.get("decision")
        if decision:
            return str(decision).lower()

    value = report.get("recommendation")
    return str(value or "").lower()


def _norm_text(value) -> str:
    return str(value or "").lower().replace("ё", "е")


def _contains_any(text: str, patterns: list[str]) -> bool:
    norm = _norm_text(text)
    return any(_norm_text(pattern) in norm for pattern in patterns)


def lead_hard_noise_reason_for_row(
    tender: dict[str, Any],
    assessment: dict[str, Any] | None = None,
) -> str | None:
    # Keep digest/backlog filtering aligned with the lead runner's hard-noise gate.
    from app.pipeline.run_llm_for_shortlist import (
        lead_hard_noise_reason_for_row as _lead_hard_noise_reason_for_row,
    )

    return _lead_hard_noise_reason_for_row(tender, assessment)


@lru_cache(maxsize=1)
def _business_profile() -> dict[str, Any]:
    try:
        return load_business_profile()
    except Exception:
        return {}


def _profile_rule_keywords(
    group_name: str,
    fallback: list[str],
    *,
    field_name: str = "keywords",
) -> list[str]:
    profile = _business_profile()
    rule_patterns = profile.get("rule_patterns") or {}

    if not isinstance(rule_patterns, dict):
        return fallback

    group = rule_patterns.get(group_name) or {}
    if not isinstance(group, dict):
        return fallback

    keywords = group.get(field_name) or []
    if not isinstance(keywords, list):
        return fallback

    values = [str(value) for value in keywords if value]
    return values or fallback


def _profile_excluded_vertical_patterns(fallback: list[str]) -> list[str]:
    profile = _business_profile()
    groups = profile.get("excluded_verticals") or {}

    if not isinstance(groups, dict):
        return fallback

    values: list[str] = []
    for cfg in groups.values():
        if not isinstance(cfg, dict):
            continue
        keywords = cfg.get("keywords") or []
        if isinstance(keywords, list):
            values.extend(str(value) for value in keywords if value)

    merged = values + fallback
    return list(dict.fromkeys(merged))


def _profile_target_category_patterns() -> list[str]:
    profile = _business_profile()
    groups = profile.get("target_categories") or {}

    if not isinstance(groups, dict):
        return []

    values: list[str] = []
    for cfg in groups.values():
        if not isinstance(cfg, dict):
            continue
        for key in ("keywords", "full_deal_keywords"):
            keywords = cfg.get(key) or []
            if isinstance(keywords, list):
                values.extend(str(value) for value in keywords if value)

    return list(dict.fromkeys(values))


def _profile_low_priority_deal_type_patterns() -> list[str]:
    profile = _business_profile()
    groups = profile.get("low_priority_deal_types") or {}

    if not isinstance(groups, dict):
        return []

    values: list[str] = []
    for cfg in groups.values():
        if not isinstance(cfg, dict):
            continue
        keywords = cfg.get("keywords") or []
        if isinstance(keywords, list):
            values.extend(str(value) for value in keywords if value)

    return list(dict.fromkeys(values))


def _deal_title_summary_text(tender: dict) -> str:
    result = tender.get("result") or {}
    chunks = [
        tender.get("title") or "",
        result.get("summary") or "",
    ]
    return "\n".join(str(chunk) for chunk in chunks if chunk)


def is_profile_low_priority_deal(tender: dict) -> bool:
    patterns = _profile_low_priority_deal_type_patterns()
    return bool(patterns) and _contains_any(_deal_title_summary_text(tender), patterns)


def _report_category(report: dict[str, Any]) -> str:
    category = report.get("detected_category")
    if category:
        return str(category).lower()

    facts = report.get("extracted_spec_facts") or {}
    if isinstance(facts, dict) and facts.get("category"):
        return str(facts.get("category")).lower()

    return ""


def raw_full(tender: dict) -> dict[str, Any]:
    raw = tender.get("raw") or {}
    full = raw.get("full") or {}

    if isinstance(full, dict):
        return full

    return {}


def _corpus(tender: dict) -> str:
    full = raw_full(tender)
    result = tender.get("result") or {}
    document_result = tender.get("document_risk_result") or {}
    report = _llm_report(tender)

    chunks: list[str] = [
        tender.get("title") or "",
        tender.get("customer_name") or "",
        tender.get("region") or "",
        full.get("deliveryPlace") or "",
        full.get("deliveryTerm") or "",
        full.get("orderNumber") or "",
        result.get("summary") or "",
        report.get("summary") or "",
        _report_category(report),
    ]

    for key in ("positive_matches", "why_relevant", "risks", "manual_checks"):
        value = result.get(key)
        if isinstance(value, list):
            chunks.extend(str(item) for item in value)
        elif value:
            chunks.append(str(value))

    if isinstance(document_result, dict):
        for item in document_result.get("key_findings") or []:
            chunks.append(str(item))

        for item in document_result.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            chunks.append(str(item.get("pattern") or ""))
            chunks.append(str(item.get("snippet") or ""))
            values = item.get("values") or []
            if isinstance(values, list):
                chunks.extend(str(value) for value in values)

    tech = report.get("technical_spec_analysis") or {}
    if isinstance(tech, dict):
        for key in ("mentioned_models_or_vendors", "what_is_required", "key_characteristics"):
            value = tech.get(key)
            if isinstance(value, list):
                chunks.extend(str(item) for item in value)
            elif value:
                chunks.append(str(value))

    return "\n".join(chunks)



SERVICE_NOISE_PATTERNS = [
    "разработка",
    "развити",
    "сопровожд",
    "аттестаци",
    "мониторинг жилищного фонда",
    "мониторинг и реагирование",
    "реагировани на инцидент",
    "госсопка",
    "услуг по мониторингу",
    "система мониторинга событий",
    "системы мониторинга событий",
    "приобретение и монтаж систем видеонаблюдения",
    "монтаж систем видеонаблюдения",
    "оборудования для видеонаблюдения",
    "оргтехник",
    "запасных частей для вычислительной техники",
    "периферийн",
]

TRANSPORT_WEIGHING_SERVICE_SUBJECT_PATTERNS = [
    "весогабаритн",
    "весогабаритного контроля",
    "автоматического весогабаритного контроля",
    "комплексов автоматического весогабаритного контроля",
    "аппаратно-программных автоматических весогабаритного контроля",
    "архимед",
]

TRANSPORT_WEIGHING_SERVICE_WORD_PATTERNS = [
    "техническое обслуживание",
    "технического обслуживания",
    "техническому обслуживанию",
    "техническому обслуживанию и ремонту",
    "техническому обслуживанию и ремонту комплексов",
    "ремонт",
    "ремонту",
    "содержание",
    "эксплуатац",
    "сопровождени",
]

NON_CORE_TRANSPORT_SECURITY_REASON = "non_core_transport_security_infrastructure"

TRANSPORT_SECURITY_INFRA_SUBJECT_PATTERNS = [
    "транспортная безопасность",
    "обеспечения транспортной безопасности",
    "обеспечение транспортной безопасности",
    "инженерно-техническими средствами",
    "инженерно-технические средства",
    "тс отб",
    "отб оти",
    "объект транспортной инфраструктуры",
    "объекта транспортной инфраструктуры",
    "объекты транспортной инфраструктуры",
    "ссои",
    "пп рф №969",
    "пп рф 969",
    "пп №969",
    "пп 969",
    "пп рф №2107",
    "пп рф 2107",
    "пп №2107",
    "пп 2107",
]

TRANSPORT_SECURITY_INFRA_OBJECT_PATTERNS = [
    "объекты дорожного хозяйства",
    "объектов дорожного хозяйства",
    "дорожного хозяйства",
    "автомобильная дорога",
    "автомобильной дороги",
    "мост через",
    "мостов",
    "мост",
    "путепровод",
    "км дороги",
]

TRANSPORT_SECURITY_INFRA_SYSTEM_PATTERNS = [
    "охранное телевидение",
    "охранная сигнализация",
    "охранно-пожарная сигнализация",
    "скуд",
    "система контроля и управления доступом",
    "инженерные заграждения",
    "инженерных заграждений",
    "связь и оповещение",
    "системы оповещения",
    "система оповещения",
    "ссои",
]

TRANSPORT_SECURITY_INFRA_WORK_PATTERNS = [
    "оснащение",
    "проектирование",
    "пир",
    "смр",
    "строительно-монтаж",
    "пусконалад",
    "монтаж",
    "ввод в эксплуатацию",
    "электроснабжение",
    "дгу",
    "модульные здания",
    "модульное здание",
    "заграждения",
    "сертификация тс отб",
]

TARGET_HARDWARE_MAIN_SUBJECT_PATTERNS = [
    "поставка сервер",
    "поставка сервера",
    "поставка серверов",
    "поставка серверного оборудования",
    "поставка схд",
    "поставка системы хранения",
    "поставка систем хранения",
    "поставка системы хранения данных",
    "поставка коммутатор",
    "поставка коммутаторов",
    "поставка маршрутизатор",
    "поставка маршрутизаторов",
    "поставка сетевого оборудования",
    "поставка межсетевого экрана",
    "поставка firewall",
]

EXCLUDED_VERTICAL_PATTERNS = [
    "видеонаблюдение",
    "видео наблюдение",
    "камеры видеонаблюдения",
    "камера видеонаблюдения",
    "систем видеонаблюдения",
    "система видеонаблюдения",
    "сервер видеонаблюдения",
    "видеосервер",
    "фотовидеофиксация",
    "фото-видеофиксация",
    "автоматической фотовидеофиксации",
    "комплексов автоматической фотовидеофиксации",
    "безопасный город",
    "паутина",
    "скуд",
    "система контроля и управления доступом",
    "охранная сигнализация",
    "охранно-пожарная сигнализация",
    "технические средства охраны",
    "антитеррористической защищенности",
]


TARGET_TITLE_HARDWARE_PATTERNS = [
    "поставка сервер",
    "поставка сервера",
    "серверное оборудование",
    "вычислительный комплекс",
    "схд",
    "система хранения",
    "коммутатор",
    "маршрутизатор",
    "межсетевой экран",
    "firewall",
    "программно-аппаратный комплекс",
]


NON_CORE_CONSTRUCTION_OBJECT_PATTERNS = [
    "фап",
    "фельдшерско-акушер",
    "модульное здание",
    "модульного здания",
    "модульный фап",
    "объект капитального строительства",
    "строительный объект",
]

NON_CORE_CONSTRUCTION_WORK_PATTERNS = [
    "строительств",
    "строительно-монтаж",
    "капитальн",
    "реконструкц",
]

NON_CORE_BUILDING_OBJECT_PATTERNS = [
    "здани",
    "строени",
    "сооружени",
    "объект капитального строительства",
]


def is_non_core_construction_object(tender: dict) -> bool:
    text = _deal_title_summary_text(tender)
    direct_patterns = _profile_rule_keywords(
        "non_core_construction_objects",
        NON_CORE_CONSTRUCTION_OBJECT_PATTERNS,
    )
    work_patterns = _profile_rule_keywords(
        "non_core_construction_objects",
        NON_CORE_CONSTRUCTION_WORK_PATTERNS,
        field_name="work_keywords",
    )
    object_patterns = _profile_rule_keywords(
        "non_core_construction_objects",
        NON_CORE_BUILDING_OBJECT_PATTERNS,
        field_name="object_keywords",
    )

    return _contains_any(text, direct_patterns) or (
        _contains_any(text, work_patterns) and _contains_any(text, object_patterns)
    )


def _has_target_hardware_main_subject(tender: dict) -> bool:
    title = tender.get("title") or ""
    patterns = _profile_rule_keywords(
        "non_core_transport_security_infrastructure",
        TARGET_HARDWARE_MAIN_SUBJECT_PATTERNS,
        field_name="target_hardware_main_subject_keywords",
    )
    return _contains_any(title, patterns)


def is_non_core_transport_security_infrastructure(tender: dict) -> bool:
    if _has_target_hardware_main_subject(tender):
        return False

    text = _deal_title_summary_text(tender)
    corpus = _corpus(tender)

    subject_patterns = _profile_rule_keywords(
        "non_core_transport_security_infrastructure",
        TRANSPORT_SECURITY_INFRA_SUBJECT_PATTERNS,
        field_name="subject_keywords",
    )
    object_patterns = _profile_rule_keywords(
        "non_core_transport_security_infrastructure",
        TRANSPORT_SECURITY_INFRA_OBJECT_PATTERNS,
        field_name="object_keywords",
    )
    system_patterns = _profile_rule_keywords(
        "non_core_transport_security_infrastructure",
        TRANSPORT_SECURITY_INFRA_SYSTEM_PATTERNS,
        field_name="security_infra_keywords",
    )
    work_patterns = _profile_rule_keywords(
        "non_core_transport_security_infrastructure",
        TRANSPORT_SECURITY_INFRA_WORK_PATTERNS,
        field_name="work_keywords",
    )

    has_subject = _contains_any(text, subject_patterns)
    has_object = _contains_any(text, object_patterns)
    has_system = _contains_any(corpus, system_patterns)
    has_work = _contains_any(corpus, work_patterns)

    transport_security_object = has_subject and (has_object or has_system)
    road_security_object = has_object and _contains_any(corpus, subject_patterns + system_patterns)

    return (transport_security_object or road_security_object) and (has_work or has_system)



def _hardware_title_summary_patterns() -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    supply_words = [
        "поставка",
        "закупка",
        "приобретение",
        "приобретен",
        "договор поставки",
        "предоставление всех необходимых материалов",
    ]

    strong_hardware_supply_phrases = [
        "поставка сервера",
        "поставка серверов",
        "поставка серверного оборудования",
        "поставка системы хранения данных",
        "поставка системы хранения",
        "поставка схд",
        "поставка коммутаторов",
        "поставка коммутатора",
        "поставка маршрутизаторов",
        "поставка маршрутизатора",
        "поставка межсетевого экрана",
        "поставка сетевого оборудования",
        "вычислительный комплекс",
        "право заключения договора поставки системы хранения данных",
    ]

    hardware_context = [
        "схд",
        "система хранения",
        "системы хранения",
        "серверное оборудование",
        "серверного оборудования",
        "вычислительный комплекс",
        "коммутатор",
        "маршрутизатор",
        "межсетевой экран",
        "firewall",
        "программно-аппаратный комплекс",
        "пак",
        "аппаратно-программн",
        "сетевое оборудование",
        "активного сетевого оборудования",
    ]

    service_only_patterns = [
        "оказание услуг",
        "услуг по",
        "эксплуатац",
        "комплексно-техническ",
        "техническ эксплуатац",
        "технической поддержки",
        "техподдержк",
        "сервисн",
        "сопровождени",
        "мониторинг",
        "реагирован",
        "аттестаци",
        "лиценз",
        "неисключительн",
        "право использован",
        "прав использования",
        "сертификат технической поддержки",
        "сертификатов технической поддержки",
        "антивирус",
        "kaspersky",
        "vmmanager",
        "astra linux",
        "программного обеспечения",
        "программное обеспечение",
        "техническое обслуживание",
        "технического обслуживания",
        "материалов для то",
        "материалы для то",
    ]

    infra_project_patterns = [
        "модернизац",
        "внедр",
        "настройк",
        "перенастройк",
        "пуско-налад",
        "пусконалад",
        "монтаж",
        "создани",
        "формирован",
        "реконструкц",
    ]

    return (
        _profile_rule_keywords("hardware_supply_words", supply_words),
        _profile_rule_keywords("strong_hardware_supply", strong_hardware_supply_phrases),
        _profile_rule_keywords("target_hardware_context", hardware_context),
        _profile_rule_keywords("service_only", service_only_patterns),
        _profile_rule_keywords("infra_project_work", infra_project_patterns),
    )


def _has_hardware_supply_intent(tender: dict) -> bool:
    text = _deal_title_summary_text(tender)

    if (
        is_non_core_transport_security_infrastructure(tender)
        or is_non_core_construction_object(tender)
        or is_profile_low_priority_deal(tender)
    ):
        return False

    (
        supply_words,
        strong_hardware_supply_phrases,
        hardware_context,
        service_only_patterns,
        _,
    ) = _hardware_title_summary_patterns()

    if _contains_any(text, service_only_patterns) and not _contains_any(
        text,
        [
            "серверное оборудование",
            "система хранения",
            "схд",
            "коммутатор",
            "маршрутизатор",
            "межсетевой экран",
            "сетевое оборудование",
        ],
    ):
        return False

    return _contains_any(text, strong_hardware_supply_phrases) or (
        _contains_any(text, supply_words) and _contains_any(text, hardware_context)
    )


def is_infra_project(tender: dict) -> bool:
    text = _deal_title_summary_text(tender)

    if (
        is_non_core_transport_security_infrastructure(tender)
        or is_non_core_construction_object(tender)
        or is_profile_low_priority_deal(tender)
    ):
        return False

    (
        _supply_words,
        _strong_hardware_supply_phrases,
        _hardware_context,
        service_only_patterns,
        infra_project_patterns,
    ) = _hardware_title_summary_patterns()

    has_project_work = _contains_any(text, infra_project_patterns)
    has_hardware_supply = _has_hardware_supply_intent(tender)
    pure_service = _contains_any(text, service_only_patterns) and not has_hardware_supply

    return has_project_work and has_hardware_supply and not pure_service


def is_transport_weighing_service_noise(tender: dict) -> bool:
    text = _deal_title_summary_text(tender)
    subject_patterns = _profile_rule_keywords(
        "transport_weighing_service_noise",
        TRANSPORT_WEIGHING_SERVICE_SUBJECT_PATTERNS,
        field_name="subject_keywords",
    )
    service_patterns = _profile_rule_keywords(
        "transport_weighing_service_noise",
        TRANSPORT_WEIGHING_SERVICE_WORD_PATTERNS,
        field_name="service_keywords",
    )

    return _contains_any(text, subject_patterns) and _contains_any(text, service_patterns)


def is_service_noise(tender: dict) -> bool:
    title = _norm_text(tender.get("title") or "")
    text = _deal_title_summary_text(tender)
    corpus = _corpus(tender)

    if is_non_core_transport_security_infrastructure(tender):
        return True

    if is_non_core_construction_object(tender):
        return True

    if _contains_any(title, _profile_excluded_vertical_patterns(EXCLUDED_VERTICAL_PATTERNS)):
        return True

    if is_transport_weighing_service_noise(tender):
        return True

    if is_infra_project(tender):
        return False

    if _has_hardware_supply_intent(tender):
        return False

    (
        _supply_words,
        _strong_hardware_supply_phrases,
        _hardware_context,
        service_only_patterns,
        _infra_project_patterns,
    ) = _hardware_title_summary_patterns()

    if _contains_any(title, _profile_rule_keywords("service_noise", SERVICE_NOISE_PATTERNS)):
        return True

    if _contains_any(text, service_only_patterns):
        return True

    if _contains_any(corpus, _profile_excluded_vertical_patterns(EXCLUDED_VERTICAL_PATTERNS)):
        return True

    if _contains_any(corpus, [
        "автоматизированной информационной системы",
        "информационной системы",
        "платформе гостех",
        "гостех",
        "система мониторинга событий",
        "мониторинг и реагирование",
        "госсопка",
    ]) and not _has_hardware_supply_intent(tender) and not is_infra_project(tender):
        return True

    return False


def has_high_tailoring(tender: dict) -> bool:
    document_result = tender.get("document_risk_result") or {}
    if isinstance(document_result, dict):
        doc_risk = str(document_result.get("tailoring_risk") or "").lower()
        doc_score = _as_int(document_result.get("risk_score"))
        if doc_risk == "high" or (doc_score is not None and doc_score >= 80):
            return True

    report = _llm_report(tender)
    if report and _tailoring_level(report.get("tailoring_risk")) == "high":
        return True

    return False


def is_incumbent_extension(tender: dict) -> bool:
    corpus = _corpus(tender)

    strong_patterns = [
        "продление",
        "сертификат технической поддержки",
        "сертификаты технической поддержки",
        "сертификатов технической поддержки",
        "сертификатов активации",
        "ключей активации",
        "ключи активации",
        "обновления",
        "пакет расширения к лицензии",
        "ваучер на техническую поддержку",
        "ваучер",
        "ваучера",
        "техническую поддержку",
        "техподдержку",
        "сервисная поддержка",
        "сервисную поддержку",
        "renewal",
    ]

    license_patterns = [
        "неисключительных прав",
        "неисключительной лицензии",
        "права использования",
        "прав использования",
        "лицензии права использования",
        "подписк",
    ]

    support_context = [
        "технической поддержки",
        "техподдержк",
        "сопровожд",
        "обновлен",
        "активац",
        "сертификат",
        "сертификатов",
    ]

    if _contains_any(corpus, strong_patterns):
        return True

    return _contains_any(corpus, license_patterns) and _contains_any(corpus, support_context)


def has_domestic_restriction(tender: dict) -> bool:
    corpus = _corpus(tender)

    patterns = [
        "постановления правительства рф от 23.12.2024 n 1875",
        "постановление правительства рф от 23.12.2024 n 1875",
        "пп рф 1875",
        "n 1875",
        "№ 1875",
        "национальный режим",
        "ограничение допуска",
        "запрет допуска",
        "ограничение закупок товаров",
        "российского происхождения",
        "реестровая запись",
        "реестр российской промышленной продукции",
        "реестр отечественного по",
        "минпромторг",
        "заявка приравнивается к заявке, в которой содержится предложение о поставке товаров, происходящих из иностранного государства",
    ]

    return _contains_any(corpus, _profile_rule_keywords("domestic_restrictions", patterns))


def is_target_hardware(tender: dict) -> bool:
    report = _llm_report(tender)
    category = _report_category(report)
    text = _deal_title_summary_text(tender)

    if (
        is_non_core_transport_security_infrastructure(tender)
        or is_non_core_construction_object(tender)
        or is_profile_low_priority_deal(tender)
    ):
        return False

    if category in {"storage", "server", "servers", "network", "security", "security_hardware"}:
        return True

    if is_service_noise(tender) and not is_infra_project(tender):
        return False

    if _has_hardware_supply_intent(tender):
        return True

    if is_infra_project(tender):
        return True

    profile_patterns = _profile_target_category_patterns()
    if profile_patterns and _contains_any(text, profile_patterns):
        return True

    patterns = [
        "схд",
        "система хранения",
        "системы хранения",
        "хранение данных",
        "хранения данных",
        "серверное оборудование",
        "вычислительный комплекс",
        "коммутатор",
        "маршрутизатор",
        "межсетевой экран",
        "firewall",
        "программно-аппаратный комплекс",
        "пак ",
        "сетевое оборудование",
    ]

    return _contains_any(text, patterns)


def is_import_or_exact_hardware(tender: dict) -> bool:
    corpus = _corpus(tender)

    vendor_or_model_patterns = [
        "huawei",
        "oceanstor",
        "dorado",
        "6000v7",
        "6000 v7",
        "5300 v5",
        "hpe",
        "hewlett",
        "dell",
        "lenovo",
        "supermicro",
        "cisco",
        "d-link",
        "hikvision",
        "alfavision",
        "depo",
        "vipnet coordinator",
        "sfp",
        "sfp+",
        "qsfp",
        "raid-tp",
    ]

    return is_target_hardware(tender) and _contains_any(corpus, _profile_rule_keywords("vendor_or_model_signals", vendor_or_model_patterns))


def _profile_commercial_interest_threshold(name: str, default: int) -> int:
    cfg = (_business_profile().get("commercial_interest") or {})
    if not isinstance(cfg, dict):
        return default
    try:
        return int(cfg.get(name, default))
    except (TypeError, ValueError):
        return default


def commercial_interest(tender: dict) -> str:
    price = tender.get("initial_price")
    if isinstance(price, Decimal):
        price = float(price)

    try:
        price_value = float(price or 0)
    except (TypeError, ValueError):
        price_value = 0

    target_high_price = _profile_commercial_interest_threshold("target_category_high_price_rub", 5_000_000)
    target_medium_price = _profile_commercial_interest_threshold("target_category_medium_price_rub", 1_000_000)
    infra_project_high_price = _profile_commercial_interest_threshold("infra_project_high_price_rub", 10_000_000)
    any_medium_price = _profile_commercial_interest_threshold("any_category_medium_price_rub", 15_000_000)

    if is_infra_project(tender) and price_value >= infra_project_high_price:
        return "high"

    if is_infra_project(tender) and price_value >= target_medium_price:
        return "medium"

    if is_target_hardware(tender) and price_value >= target_high_price:
        return "high"

    if is_target_hardware(tender) and price_value >= target_medium_price:
        return "medium"

    if price_value >= any_medium_price:
        return "medium"

    return "low"


def business_assessment(tender: dict) -> dict[str, str]:
    interest = commercial_interest(tender)
    high_tailoring = has_high_tailoring(tender)

    if is_incumbent_extension(tender):
        return {
            "commercial_interest": interest,
            "market_access": "incumbent_extension",
            "tailoring_effect": "negative",
            "action": "skip_incumbent",
        }

    if is_profile_low_priority_deal(tender):
        return {
            "commercial_interest": interest,
            "market_access": "low_priority_deal",
            "tailoring_effect": "negative",
            "action": "skip_low_priority",
        }

    if is_non_core_transport_security_infrastructure(tender):
        return {
            "commercial_interest": interest,
            "market_access": NON_CORE_TRANSPORT_SECURITY_REASON,
            "tailoring_effect": "negative",
            "action": "no_go",
        }

    if is_service_noise(tender):
        return {
            "commercial_interest": interest,
            "market_access": "service_noise",
            "tailoring_effect": "negative",
            "action": "no_go",
        }

    if is_infra_project(tender):
        return {
            "commercial_interest": interest,
            "market_access": "infra_project",
            "tailoring_effect": "caution" if high_tailoring else "neutral",
            "action": "quick_check",
        }

    if has_domestic_restriction(tender):
        return {
            "commercial_interest": interest,
            "market_access": "domestic_restricted",
            "tailoring_effect": "negative" if high_tailoring else "caution",
            "action": "check_domestic_analog",
        }

    if is_import_or_exact_hardware(tender) and high_tailoring:
        return {
            "commercial_interest": interest,
            "market_access": "import_or_exact_hardware",
            "tailoring_effect": "useful_clarity",
            "action": "check_exact_supply",
        }

    if is_target_hardware(tender):
        return {
            "commercial_interest": interest,
            "market_access": "target_hardware",
            "tailoring_effect": "neutral",
            "action": "proceed" if interest == "high" and not high_tailoring else "quick_check",
        }

    return {
        "commercial_interest": interest,
        "market_access": "unknown",
        "tailoring_effect": "neutral",
        "action": "quick_check",
    }


def business_action_text(action: str) -> str:
    return {
        "proceed": "можно передавать в пресейл после обычной проверки",
        "check_exact_supply": "интересно: быстро проверить точную поставляемость и канал",
        "check_domestic_analog": "сначала проверить отечественное ограничение, реестр и аналоги",
        "skip_incumbent": "низкий приоритет: похоже на продление действующего решения",
        "skip_low_priority": "низкий приоритет: комплектующие, запчасти, батареи, диски или похожая неполная поставка",
        "quick_check": "быстрый ручной чек перед пресейлом",
        "no_go": "не тратить время",
    }.get(action, action)


def business_assessment_block(tender: dict) -> str:
    assessment = business_assessment(tender)

    market_access_text = {
        "incumbent_extension": "похоже на продление/поддержку действующего решения",
        "domestic_restricted": "есть признаки отечественного ограничения/нацрежима",
        "import_or_exact_hardware": "похоже на точную импортную или поставляемую конфигурацию",
        "target_hardware": "профильная поставка оборудования",
        "infra_project": "гибридный инфраструктурный проект с оборудованием и работами",
        NON_CORE_TRANSPORT_SECURITY_REASON: "нецелевая вертикаль: транспортная безопасность / дорожная инфраструктура",
        "service_noise": "похоже на сервисную/лицензионную закупку вне текущего фокуса",
        "low_priority_deal": "низкоприоритетная неполная поставка или поддержка",
        "unknown": "доступность сделки не определена",
    }.get(assessment["market_access"], assessment["market_access"])

    tailoring_effect_text = {
        "negative": "скорее минус",
        "caution": "требует осторожной проверки",
        "useful_clarity": "может быть полезной подсказкой по точной конфигурации",
        "neutral": "нейтрально",
    }.get(assessment["tailoring_effect"], assessment["tailoring_effect"])

    interest_text = {
        "high": "высокий",
        "medium": "средний",
        "low": "низкий",
    }.get(assessment["commercial_interest"], assessment["commercial_interest"])

    return "\n".join(
        [
            f"Коммерческий интерес: {interest_text}",
            f"Доступность сделки: {market_access_text}",
            f"Эффект заточки: {tailoring_effect_text}",
            f"Бизнес-действие: {business_action_text(assessment['action'])}",
        ]
    )


def selection_warnings(tender: dict) -> list[str]:
    warnings: list[str] = []

    assessment = business_assessment(tender)
    action = assessment["action"]

    if action == "skip_incumbent":
        warnings.append("Бизнес-логика: похоже на продление/поддержку действующего решения, высокий риск incumbent-поставщика.")
    elif action == "skip_low_priority":
        warnings.append("Бизнес-логика: низкий приоритет, похоже на комплектующие, запчасти, батареи, диски или неполную поставку.")
    elif assessment["market_access"] == NON_CORE_TRANSPORT_SECURITY_REASON:
        warnings.append("Бизнес-логика: нецелевая вертикаль - транспортная безопасность / дорожная инфраструктура.")
    elif action == "no_go":
        warnings.append("Бизнес-логика: сервисная/лицензионная закупка вне текущего фокуса.")
    elif assessment["market_access"] == "infra_project":
        warnings.append("Бизнес-логика: гибридный инфраструктурный проект - нужен быстрый ручной чек состава поставки, работ и материалов.")
    elif action == "check_domestic_analog":
        warnings.append("Бизнес-логика: есть признаки отечественного ограничения/нацрежима, сначала проверить реестр и аналоги.")
    elif action == "check_exact_supply":
        warnings.append("Бизнес-логика: профильное железо с точной конфигурацией, заточка может помогать понять, что именно нужно поставить.")

    document_result = tender.get("document_risk_result") or {}
    if isinstance(document_result, dict):
        doc_risk = str(document_result.get("tailoring_risk") or "").lower()
        doc_score = _as_int(document_result.get("risk_score"))

        if doc_risk == "high":
            warnings.append("Документы: высокий риск заточки.")
        elif doc_risk == "medium":
            warnings.append("Документы: средний риск заточки.")

        if doc_score is not None and doc_score >= 80:
            warnings.append(f"Документы: оценка риска {doc_score}/100.")

    report = _llm_report(tender)
    if report:
        llm_tailoring = _tailoring_level(report.get("tailoring_risk"))
        if llm_tailoring == "high":
            warnings.append("LLM: высокий риск заточки.")
        elif llm_tailoring == "medium":
            warnings.append("LLM: средний риск заточки.")

        decision = _llm_decision(report)
        if decision == "no_go":
            warnings.append("LLM: вердикт no_go.")
        elif decision == "maybe":
            warnings.append("LLM: вердикт maybe, нужна ручная проверка.")

        delivery = report.get("delivery_feasibility") or {}
        if isinstance(delivery, dict):
            assessment_text = str(delivery.get("assessment") or "").lower()
            if assessment_text == "risky":
                warnings.append("LLM: рискованный срок поставки или исполнения.")

    return list(dict.fromkeys(warnings))


def effective_recommendation(tender: dict) -> str:
    base = str(tender.get("recommendation") or "").lower()
    if base == "no_go":
        return "no_go"

    report = _llm_report(tender)
    if report and _llm_decision(report) == "no_go":
        return "no_go"

    assessment = business_assessment(tender)
    action = assessment["action"]

    if action in {"skip_incumbent", "skip_low_priority", "no_go"}:
        return "no_go"

    if action in {"check_exact_supply", "check_domestic_analog"}:
        return "maybe"

    if has_high_tailoring(tender):
        return "maybe"

    if base == "go":
        return "go"

    return "maybe"


def recommendation_explanation_block(tender: dict) -> str:
    warnings = selection_warnings(tender)
    if not warnings:
        return "— критичных предупреждений нет"

    return list_block(warnings)


def digest_sort_key(tender: dict) -> tuple[int, int, int, str]:
    assessment = business_assessment(tender)
    effective = effective_recommendation(tender)

    action_rank = {
        "proceed": 0,
        "check_exact_supply": 1,
        "quick_check": 2,
        "check_domestic_analog": 3,
        "skip_incumbent": 8,
        "skip_low_priority": 8,
        "no_go": 9,
    }.get(assessment["action"], 5)

    recommendation_rank = {
        "go": 0,
        "maybe": 1,
        "no_go": 2,
    }.get(effective, 9)

    interest_rank = {
        "high": 0,
        "medium": 1,
        "low": 2,
    }.get(assessment["commercial_interest"], 9)

    score = _as_int(tender.get("score")) or 0
    deadline = tender.get("deadline_at")
    deadline_text = deadline.isoformat() if hasattr(deadline, "isoformat") else str(deadline or "")

    return (recommendation_rank, action_rank, interest_rank, -score, deadline_text)
