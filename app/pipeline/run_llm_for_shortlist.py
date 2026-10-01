from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.business_profile import (
    category_priority,
    get_price,
    has_generic_equipment_target_hardware_evidence,
    is_generic_equipment_title,
    is_excluded_vertical,
    is_full_deal_for_category,
    is_low_priority_deal,
    load_business_profile,
    match_target_category,
)
from app.business_rules import business_assessment, effective_recommendation
from app.digest import (
    DEFAULT_DEADLINE_MIN_DAYS,
    SALES_EXISTING_CLIENT_STATUS,
    deadline_is_active,
    deadline_is_fresh,
    get_digest_rows,
    hidden_by_existing_client_customer,
    parse_report_datetime,
)
from app.llm.analysis_depth import (
    ANALYSIS_DEPTH_CHOICES,
    ANALYSIS_DEPTH_DEEP,
    ANALYSIS_DEPTH_STANDARD,
    AnalysisLimits,
    normalize_analysis_depth,
    resolve_analysis_limits,
)
from app.llm.transient_network import (
    ROUTERAI_READ_TIMEOUT_NOT_RETRIED_MESSAGE,
    is_read_timeout_error_text,
    is_transient_network_error_text,
    transient_network_error_summary,
)


FAILURE_DETAIL_MAX_CHARS = 500
TARGETED_DOCUMENT_LIMIT = 5
DOCUMENTS_MISSING = "documents_missing"
DOCUMENTS_DOWNLOADED_WITHOUT_TEXT = "documents_downloaded_without_text"
DOCUMENTS_READY_WITH_TEXT = "documents_ready_with_text"
PRIMARY_TECHNICAL_DOCUMENT_MISSING = "primary_technical_document_missing"
PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED = "primary_technical_document_not_processed"
PREPARATION_MISSING_PRIMARY_TECHNICAL_DOCUMENT = (
    "preparation_missing_primary_technical_document"
)
PRIMARY_TECHNICAL_DOCUMENT_READINESS_REASONS = {
    PRIMARY_TECHNICAL_DOCUMENT_MISSING,
    PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED,
    PREPARATION_MISSING_PRIMARY_TECHNICAL_DOCUMENT,
}
FALLBACK_RESULT_LABEL = "routerai_fallback"
FALLBACK_MAX_SPEC_CHARS = 30000
FALLBACK_MAX_OTHER_CHARS = 6000
DEEP_FALLBACK_MAX_SPEC_CHARS = 120000
DEEP_FALLBACK_MAX_OTHER_CHARS = 20000
PREPARATION_REQUEUE_EVENT = "targeted_document_download_requeue"
PREPARATION_BLOCKED_EVENT = "targeted_document_preparation_blocked"
PREPARATION_EXHAUSTION_COOLDOWN_SECONDS = 24 * 60 * 60
SUBPROCESS_NETWORK_MAX_ATTEMPTS = 2
SUBPROCESS_NETWORK_RETRY_BACKOFF_SECONDS = 10
DEADLINE_EXPIRED_SKIP_REASON = "deadline_expired"
DEADLINE_LT_3D_SKIP_REASON = "deadline_lt_3d"
REPORT_KIND_TECHNICAL = "technical"
REPORT_KIND_LEAD = "lead"
REPORT_KIND_CHOICES = (REPORT_KIND_TECHNICAL, REPORT_KIND_LEAD)
NEGATIVE_SALES_FEEDBACK_SKIP_STATUSES = {
    "sales_not_relevant",
    SALES_EXISTING_CLIENT_STATUS,
    "skip",
    "not_interesting",
    "not_our_profile",
    "tailored",
    "bad_region",
    "bad_price",
    "not_profile",
    "service_only",
    "low_value_one_off",
    "bad_customer_fit",
    "bad_timing",
    "duplicate_or_processed",
    "other_reject",
}
TECHNICAL_LLM_REPORT_ANALYSIS_TYPE = "llm_presales_report"
LEAD_LLM_REPORT_ANALYSIS_TYPE = "llm_customer_lead_report"
LEAD_TRIAGE_ANALYSIS_TYPE = "llm_customer_lead_triage"
LEAD_SIGNAL_STRONG = "strong"
LEAD_SIGNAL_WEAK = "weak"
LEAD_SIGNAL_NOISE = "noise"
LEAD_TRIAGE_DECISIONS = ("go", "maybe", "reject")
LEAD_TRIAGE_PRIORITIES = ("high", "medium", "low")
LEAD_TRIAGE_CONFIDENCES = ("high", "medium", "low")
LEAD_TRIAGE_FIELDS = (
    "lead_decision",
    "lead_priority",
    "confidence",
    "lead_summary",
    "likely_customer_story",
    "possible_needs",
    "target_roles",
    "reject_reason",
    "requires_full_lead_report",
)
LEAD_TRIAGE_DEFAULT_LIMIT = 15
LEAD_TRIAGE_CACHE_TTL_HOURS_DEFAULT = 24.0
LEAD_TRIAGE_CACHE_TTL_ENV = "LEAD_TRIAGE_CACHE_TTL_HOURS"
LEAD_TRIAGE_MAX_SPEC_CHARS = 12000
LEAD_TRIAGE_MAX_OTHER_CHARS = 3000
LEAD_TRIAGE_MAX_OUTPUT_TOKENS = 900
LEAD_STRATEGIC_CUSTOMER_PRICE_RUB = 50_000_000
LEAD_DEFAULT_MIN_PRICE_RUB = 1_000_000
LEAD_STORAGE_DATA_CENTER_KEYWORDS = (
    "схд",
    "система хранения данных",
    "системы хранения данных",
    "система хранения",
    "системы хранения",
    "хранилище данных",
    "хранилищ",
    "репозиторий данных",
    "центр хранения",
    "центр обработки данных",
    "центр хранения и обработки данных",
    "хранения и обработки данных",
    "цод",
    "оцод",
    "рцод",
    "дата-центр",
    "дата центр",
    "цомд",
)
LEAD_SERVER_INFRASTRUCTURE_KEYWORDS = (
    "серверное и сетевое оборудование",
    "серверная инфраструктура",
    "серверной инфраструктуры",
    "серверную инфраструктуру",
)
LEAD_STANDALONE_SERVER_KEYWORDS = (
    "серверное оборудование",
    "серверного оборудования",
    "серверным оборудованием",
    "поставка серверов",
    "поставка сервера",
    "поставка серверного",
    "поставка и пнр сервер",
    "обеспечение работы серверов",
    "сопровождение серверов",
    "поддержка серверов",
)
LEAD_NETWORK_CORE_KEYWORDS = (
    "сетевое оборудование",
    "сетевого оборудования",
    "коммутатор",
    "коммутаторы",
    "маршрутизатор",
    "маршрутизаторы",
)
LEAD_NETWORK_PURCHASE_CONTEXT_KEYWORDS = (
    "поставка",
    "закупка",
    "приобретение",
    "пнр",
    "пусконалад",
    "модернизац",
    "расширен",
)
LEAD_ELECTRONIC_QUEUE_BOUND_CONTEXT_KEYWORDS = (
    "электронная очередь",
    "электронной очеред",
    "электронную очередь",
    "система управления электронной очередью",
    "системы управления электронной очередью",
)
LEAD_PERIPHERAL_BOUND_CONTEXT_KEYWORDS = (
    "оргтехник",
    "перифер",
    "мфу",
    "ксерокс",
    "принтер",
    "сканер",
    "картридж",
    "монитор",
    "компьютерная техника",
    "компьютерной техники",
    "вычислительная техника",
    "вычислительной техники",
    "учебно-демонстрацион",
    "учебно демонстрацион",
    "интерактивный учебный класс",
    "интерактивного учебного класса",
    "учебный класс",
    "учебного класса",
    "микроскоп",
)
LEAD_OFFICE_SOFTWARE_CONTEXT_KEYWORDS = (
    "предустановленное по",
    "предустановленного по",
    "офисное по",
    "офисного по",
    "офисных приложений",
    "office",
    "microsoft office",
)
LEAD_SECURITY_HARDWARE_KEYWORDS = (
    "межсетевой экран",
    "firewall",
    "ngfw",
    "средства защиты информации",
    "средство защиты информации",
    "защита информации",
    "защите информации",
    "защиты информации",
    "информационной безопасности",
    "сзи",
    "скзи",
    "госсопка",
    "siem",
    "soc",
    "vipnet",
    "positive",
    "dallas lock",
    "иб-желез",
)
LEAD_SECURITY_TARGET_WORD_KEYWORDS = (
    "иб",
)
LEAD_SECURITY_TARGET_VENDOR_KEYWORDS = (
    "vipnet",
    "positive",
    "dallas lock",
    "dlp",
)
LEAD_PAK_TARGET_KEYWORDS = (
    "программно-аппаратный комплекс",
    "программно-аппаратного комплекса",
    "программно-аппаратных комплексов",
    "аппаратно-программный комплекс",
    "аппаратно-программного комплекса",
    "аппаратно-программных комплексов",
)
LEAD_PLATFORM_MIGRATION_KEYWORDS = (
    "astra linux",
    "астра linux",
    "астра линукс",
    "astra",
    "ред ос",
    "redos",
    "rupost",
    "ru post",
    "руpost",
    "ру пост",
)
LEAD_EXCHANGE_KEYWORDS = (
    "exchange",
    "ms exchange",
    "microsoft exchange",
)
LEAD_MIGRATION_CONTEXT_KEYWORDS = (
    "миграц",
    "переход",
    "перенос",
    "замещ",
    "замен",
    "импортозамещ",
)
LEAD_ENTERPRISE_INFRASTRUCTURE_CONTEXT_KEYWORDS = (
    "ит-инфраструктур",
    "инфраструктур",
    "цод",
    "оцод",
    "рцод",
    "цомд",
    "сервер",
    "серверн",
    "схд",
    "система хранения",
    "виртуализац",
    "платформ",
    "сзи",
    "защита информации",
    "иб",
    "сетевое оборудование",
    "коммутатор",
    "маршрутизатор",
)
LEAD_NON_TARGET_SERVER_BOUND_CONTEXT_KEYWORDS = (
    "сервер системы видеонаблюдения",
    "сервер систем видеонаблюдения",
    "серверов системы видеонаблюдения",
    "серверов систем видеонаблюдения",
    "серверы системы видеонаблюдения",
    "сервер системы весогабаритного контроля",
    "сервер комплекса весогабаритного контроля",
    "почтовый сервер",
    "почтовые серверы",
    "почтовых серверов",
    "mail server",
    "ремонт компьютерной техники",
    "ремонт компьютеров",
    "техническое обслуживание компьютеров",
)
LEAD_VIRTUALIZATION_KEYWORDS = (
    "виртуализац",
    "пак виртуализации",
    "vmmanager",
    "р-виртуализация",
    "р виртуализация",
)
LEAD_CONTAINERIZATION_KEYWORDS = (
    "контейнеризац",
)
LEAD_BACKUP_KEYWORDS = (
    "backup",
    "резервное копирование",
)
LEAD_MEDICAL_DATA_KEYWORDS = (
    "медицинские данные",
    "медицинских данных",
)
LEAD_DATA_STORAGE_CONTEXT_KEYWORDS = (
    "хранен",
    "хранилищ",
    "обработк",
    "репозитор",
    "центр хранения",
    "центр обработки",
    "цод",
    "оцод",
    "рцод",
    "цомд",
)
LEAD_ACTIVITY_KEYWORDS = (
    "модернизац",
    "расширен",
    "обеспечение работы",
    "сопровожден",
    "поддержк",
    "пусконалад",
    "пнр",
)
LEAD_COMPONENT_KEYWORDS = (
    "дисковая полка",
    "дисковые полки",
    "накопител",
    "жестк",
    "жёстк",
    "контроллер",
    "кабел",
    "модул",
    "лицензи",
)
LEAD_NOISE_KEYWORDS_BY_REASON = {
    "video_surveillance": (
        "видеонаблюден",
        "видео наблюден",
        "камера виде",
        "видеофиксац",
        "видео фиксац",
        "видеоаналитик",
        "безопасный город",
    ),
    "electronic_queue": (
        "электронная очередь",
        "электронной очеред",
    ),
    "peripheral_office_equipment": (
        "оргтехник",
        "перифер",
        "мфу",
        "ксерокс",
        "принтер",
        "сканер",
        "картридж",
        "компьютерная техника",
        "компьютерной техники",
        "вычислительная техника",
        "вычислительной техники",
        "учебно-демонстрацион",
        "учебно демонстрацион",
        "приборы учебн",
        "приборов учебн",
        "аппаратура учебн",
        "аппаратуры учебн",
        "устройства учебн",
        "устройств учебн",
        "микроскоп",
    ),
    "construction_or_furniture": (
        "ремонт помещен",
        "строительство",
        "строитель",
        "фап",
        "некапитальн",
        "мебел",
        "канцтовар",
    ),
    "property_sale": (
        "продажа",
        "продать",
        "реализация имущества",
        "аукцион имущества",
        "имущественный комплекс",
    ),
    "interactive_panels": (
        "интерактивн",
        "образовательн",
        "учебно-демонстрацион",
        "учебно демонстрацион",
        "интерактивный учебный класс",
        "интерактивного учебного класса",
        "приборы учебн",
        "приборов учебн",
        "аппаратура учебн",
        "аппаратуры учебн",
        "устройства учебн",
        "устройств учебн",
        "микроскоп",
        "демонстрационн",
    ),
    "access_control": (
        "скуд",
        "охранн",
        "охранное оборудование",
        "охранная система",
        "контроль доступа",
    ),
    "generic_computer_repair": (
        "ремонт компьютерной техники",
        "ремонт компьютеров",
        "техническое обслуживание компьютеров",
    ),
    "generic_antivirus_license": (
        "антивирус",
        "антивирусн",
        "kaspersky",
        "касперск",
    ),
}
LEAD_HARD_NOISE_REASONS = {
    "video_surveillance",
    "electronic_queue",
    "peripheral_office_equipment",
    "construction_or_furniture",
    "road_security",
    "property_sale",
    "generic_antivirus_license",
    "generic_computer_repair",
    "interactive_panels",
}
LEAD_NETWORK_STANDALONE_BLOCK_HARD_NOISE_REASONS = {
    "video_surveillance",
    "peripheral_office_equipment",
    "interactive_panels",
    "electronic_queue",
    "road_security",
    "construction_or_furniture",
}
LEAD_CONTEXT_BOUND_STORAGE_HARD_NOISE_REASONS = {
    "video_surveillance",
    "peripheral_office_equipment",
    "interactive_panels",
    "electronic_queue",
    "road_security",
    "construction_or_furniture",
}
LEAD_CONTEXT_BOUND_SERVER_HARD_NOISE_REASONS = {
    "video_surveillance",
    "peripheral_office_equipment",
    "interactive_panels",
    "electronic_queue",
    "road_security",
    "generic_antivirus_license",
    "generic_computer_repair",
}
LEAD_CONTEXT_BOUND_SECURITY_HARD_NOISE_REASONS = {
    "road_security",
    "generic_antivirus_license",
}
LEAD_EXPLICIT_STORAGE_PLATFORM_KEYWORDS = (
    "схд",
    "дисковый массив",
    "дисковые массивы",
    "storage platform",
    "платформа хранения",
    "репозиторий данных",
    "центр обработки данных",
    "центр хранения и обработки данных",
    "цод",
    "оцод",
    "рцод",
    "цомд",
    "дата-центр",
    "дата центр",
)
LEAD_SECURITY_INFRASTRUCTURE_PROJECT_KEYWORDS = (
    "сзи",
    "скзи",
    "госсопка",
    "siem",
    "soc",
    "dlp",
    "межсетевой экран",
    "firewall",
    "ngfw",
    "vipnet",
    "positive",
    "dallas lock",
    "иб-инфраструктур",
    "инфраструктур информационной безопасности",
    "защита информации объектов информатизации",
    "защите информации объектов информатизации",
    "защиты информации объектов информатизации",
)
LEAD_ROAD_SECURITY_PHRASE_KEYWORDS = (
    "автомобильная дорога",
    "автомобильных дорог",
    "автомобильной дороги",
    "дорожного хозяйства",
    "транспортная безопасность",
    "транспортной безопасности",
    "объект транспортной безопасности",
    "объекты транспортной безопасности",
    "рубеж контроля",
    "фотовидеофиксац",
    "фотовидеофиксация",
    "автоматической фотовидеофиксации",
    "административных правонарушений",
    "фото видео фиксация",
    "дорожного движения",
    "штрафные комплексы",
    "штрафных комплексов",
    "комплекс фиксации нарушений",
    "комплексы фиксации нарушений",
    "фиксации нарушений пдд",
    "весогабаритный контроль",
    "дорожная транспортная безопасность",
    "архимед",
)
LEAD_VIDEO_SURVEILLANCE_BOUND_CONTEXT_KEYWORDS = (
    "для системы видеонаблюдения",
    "для систем видеонаблюдения",
    "серверов системы видеонаблюдения",
    "серверов систем видеонаблюдения",
    "системы видеонаблюдения",
    "систем видеонаблюдения",
    "архив видеонаблюдения",
    "видеофиксац",
    "видео фиксац",
)
LEAD_ROAD_SECURITY_WORD_KEYWORDS = (
    "мост",
    "мосты",
    "мостов",
    "путепровод",
    "путепроводы",
)
LEAD_ENTERPRISE_CUSTOMER_KEYWORDS = (
    "министерство",
    "минздрав",
    "россети",
    "банк",
    "университет",
    "больниц",
    "гбу",
    "фгбу",
    "фгуп",
    "пао",
    " ао ",
    "госкорпорац",
    "администрац",
    "департамент",
    "управлен",
)


@dataclass(frozen=True)
class LLMCandidateFailure:
    external_id: str
    reason: str
    detail: str = ""
    returncode: int | None = None


@dataclass(frozen=True)
class LLMCandidateFallbackAttempt:
    external_id: str
    reason: str
    analysis_depth: str
    max_spec_chars: int
    max_other_chars: int
    max_output_tokens: int
    succeeded: bool


@dataclass(frozen=True)
class CandidateDocumentCounts:
    documents_count: int
    documents_with_text_count: int


@dataclass(frozen=True)
class CandidateDocumentReadiness:
    external_id: str
    docs_before: int | None
    docs_with_text_before: int | None
    docs_after: int | None
    docs_with_text_after: int | None
    targeted_download_triggered: bool
    extraction_retry_triggered: bool
    preparation_status: str
    document_state: str
    preparation_summary: str = ""
    preparation_errors: tuple[str, ...] = ()
    warning_reason: str | None = None
    non_blocking_warning_reason: str | None = None
    check_error: str = ""
    documents_found: int | None = None
    documents_selected: int = 0
    documents_failed: int = 0
    documents_failed_items: tuple[dict[str, Any], ...] = ()
    documents_skipped_due_to_rate_limit: int = 0
    documents_skipped_due_to_rate_limit_items: tuple[dict[str, Any], ...] = ()
    missing_high_value_technical_document: bool = False
    missing_high_value_technical_document_title: str | None = None
    document_download_planner_used: bool = False
    document_download_planner_confidence: str | None = None

    @property
    def llm_readiness(self) -> str:
        if not self.warning_reason and (
            self.document_state == DOCUMENTS_READY_WITH_TEXT
            or bool(self.non_blocking_warning_reason)
        ):
            return "ready_for_llm"
        return "not_ready_for_llm"


@dataclass(frozen=True)
class PreparationRetryEvent:
    payload: dict[str, Any]
    created_at: datetime | None


@dataclass(frozen=True)
class LLMCandidateRunResult:
    external_id: str
    document_readiness: CandidateDocumentReadiness | None = None
    completed: subprocess.CompletedProcess | None = None


class DocumentNotReadyError(RuntimeError):
    def __init__(self, readiness: CandidateDocumentReadiness):
        self.readiness = readiness
        super().__init__(readiness.warning_reason or readiness.document_state)


@dataclass
class DebugSkipLimiter:
    limit: int
    emitted_by_bucket: dict[str, int] = field(default_factory=dict)
    suppressed_by_bucket: dict[str, int] = field(default_factory=dict)

    def emit(self, bucket: str, line: str) -> None:
        emitted = self.emitted_by_bucket.get(bucket, 0)
        if emitted < self.limit:
            print(line, flush=True)
            self.emitted_by_bucket[bucket] = emitted + 1
            return
        self.suppressed_by_bucket[bucket] = (
            self.suppressed_by_bucket.get(bucket, 0) + 1
        )

    @property
    def suppressed_total(self) -> int:
        return sum(self.suppressed_by_bucket.values())


DebugSkipControl = bool | DebugSkipLimiter


@dataclass
class ShortlistSelectionDiagnostics:
    rule_based_rows: int = 0
    rule_based_passed: int = 0
    eligible_for_llm_before_limit: int = 0
    selected_for_llm: int = 0
    selected_for_lead_triage: int | None = None
    pre_triage_candidates_seen: int | None = None
    skip_counts: dict[str, int] | None = None
    hard_noise_total: int = 0
    hard_noise_strict_skipped: int = 0
    hard_noise_overridden_by_target_signal: int = 0
    hard_noise_overridden_by_existing_go: int = 0
    hard_noise_suspicious_manual_review: int = 0

    def __post_init__(self) -> None:
        if self.skip_counts is None:
            self.skip_counts = {}

    def skip(self, reason: str) -> None:
        assert self.skip_counts is not None
        self.skip_counts[reason] = self.skip_counts.get(reason, 0) + 1

    def skips_with_prefix(self, prefix: str) -> int:
        assert self.skip_counts is not None
        return sum(
            count
            for reason, count in self.skip_counts.items()
            if reason.startswith(prefix)
        )

    @property
    def skipped_before_triage_by_negative_feedback(self) -> int:
        return self.skips_with_prefix("lead_negative_feedback=")

    @property
    def skipped_before_triage_by_existing_report(self) -> int:
        assert self.skip_counts is not None
        return self.skip_counts.get("lead_already_has_report", 0)

    @property
    def skipped_before_triage_by_hard_noise(self) -> int:
        return self.skips_with_prefix("lead_hard_noise=")

    @property
    def business_rules_skipped(self) -> int:
        assert self.skip_counts is not None
        business_prefixes = (
            "business_action=",
            "market_access=",
            "excluded_vertical",
            "no_target_category",
            "low_priority_deal",
            "not_full_deal_for_category",
            "generic_equipment_without_target_hardware",
        )
        return sum(
            count
            for reason, count in self.skip_counts.items()
            if reason.startswith(business_prefixes)
        )

    def record_hard_noise_decision(self, final_decision: str) -> None:
        self.hard_noise_total += 1
        if final_decision == "strict_skip":
            self.hard_noise_strict_skipped += 1
        elif final_decision == "override_to_triage":
            self.hard_noise_overridden_by_target_signal += 1
        elif final_decision == "existing_go_waiting":
            self.hard_noise_overridden_by_existing_go += 1
        elif final_decision == "suspicious_manual_review":
            self.hard_noise_suspicious_manual_review += 1


LEAD_TRIAGE_SOURCE_LLM = "llm"
LEAD_TRIAGE_SOURCE_CACHE = "cache"
LEAD_TRIAGE_SOURCE_EXISTING_REPORT = "existing_report"
LEAD_TRIAGE_SOURCE_DETERMINISTIC = "deterministic"
LEAD_TRIAGE_SOURCE_ERROR_FALLBACK = "error_fallback"


@dataclass(frozen=True)
class LeadTriageLookup:
    triage: dict[str, Any]
    source: str
    created_at: datetime | None = None


@dataclass
class LeadTriageDiagnostics:
    candidates_total: int = 0
    llm_attempted: int = 0
    llm_succeeded: int = 0
    llm_failed: int = 0
    cache_hits: int = 0
    reused_existing: int = 0
    fresh_cache_used: int = 0
    stale_existing_ignored: int = 0
    stale_cache_ignored: int = 0
    deterministic: int = 0
    go: int = 0
    maybe: int = 0
    reject: int = 0
    maybe_deferred: int = 0
    selected_for_report: int = 0
    report_limit_reached: int = 0
    go_waiting_for_report: int = 0
    report_limit_reached_go: int = 0
    go_waiting_for_report_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def retriaged_due_to_stale_ttl(self) -> int:
        return self.stale_existing_ignored + self.stale_cache_ignored

    @property
    def triage_reused_existing(self) -> int:
        return self.cache_hits + self.reused_existing

    def record_source(self, source: str) -> None:
        if source == LEAD_TRIAGE_SOURCE_CACHE:
            self.cache_hits += 1
        elif source == LEAD_TRIAGE_SOURCE_EXISTING_REPORT:
            self.reused_existing += 1
        elif source == LEAD_TRIAGE_SOURCE_DETERMINISTIC:
            self.deterministic += 1

    def record_fresh_cache_used(self, source: str) -> None:
        if source == LEAD_TRIAGE_SOURCE_CACHE:
            self.fresh_cache_used += 1

    def record_stale_reuse_ignored(self, source: str) -> None:
        if source == LEAD_TRIAGE_SOURCE_CACHE:
            self.stale_cache_ignored += 1
        elif source == LEAD_TRIAGE_SOURCE_EXISTING_REPORT:
            self.stale_existing_ignored += 1

    def record_decision(
        self,
        decision: str,
        *,
        row: dict[str, Any] | None = None,
        triage: dict[str, Any] | None = None,
        selected_for_lead_report: bool,
        lead_triage_maybe_deferred: bool,
        report_limit_reached: bool,
    ) -> None:
        if decision == "go":
            self.go += 1
        elif decision == "maybe":
            self.maybe += 1
        elif decision == "reject":
            self.reject += 1

        if lead_triage_maybe_deferred:
            self.maybe_deferred += 1
        if selected_for_lead_report:
            self.selected_for_report += 1
        if report_limit_reached:
            self.report_limit_reached += 1
            if decision == "go":
                self.go_waiting_for_report += 1
                self.report_limit_reached_go += 1
                if row is not None:
                    self.go_waiting_for_report_rows.append(
                        waiting_go_candidate_debug_row(row, triage or {})
                    )


@dataclass
class LeadFullReportDiagnostics:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    existing_skipped: int = 0


def py_module(module: str, *args: object) -> list[str]:
    return [sys.executable, "-m", module, *[str(arg) for arg in args]]


def normalize_report_kind(value: str | None) -> str:
    raw = str(value or REPORT_KIND_TECHNICAL).strip().lower().replace("_", "-")
    if raw in {"", "technical", "tech", "tz", "presales"}:
        return REPORT_KIND_TECHNICAL
    if raw in {"lead", "customer-lead", "customer"}:
        return REPORT_KIND_LEAD
    raise ValueError(f"Unsupported report_kind: {value}")


def truncate_detail(text: str, max_chars: int = FAILURE_DETAIL_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text

    return f"{text[: max_chars - 3]}..."


def output_to_text(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    return str(value)


def compact_failure_detail(output: str) -> str:
    lines = [" ".join(line.split()) for line in output.splitlines() if line.strip()]
    if not lines:
        return ""

    transient_reason = transient_network_error_summary(output)
    if is_transient_network_error_text(output) and transient_reason:
        return truncate_detail(transient_reason)

    if is_read_timeout_error_text(output):
        return truncate_detail(ROUTERAI_READ_TIMEOUT_NOT_RETRIED_MESSAGE)

    for marker in (
        "Cannot parse JSON from LLM response",
        "Cannot parse RouterAI",
        "JSONDecodeError",
    ):
        for line in lines:
            if marker in line:
                return truncate_detail(line)

    return truncate_detail(lines[-1])


def classify_failure_output(output: str) -> str:
    if "documents_missing" in output:
        return DOCUMENTS_MISSING

    if "documents_downloaded_without_text" in output:
        return DOCUMENTS_DOWNLOADED_WITHOUT_TEXT

    if "documents_missing_after_prepare" in output:
        return DOCUMENTS_MISSING

    if "documents_without_text" in output:
        return DOCUMENTS_DOWNLOADED_WITHOUT_TEXT

    if (
        "Cannot parse JSON from LLM response" in output
        or "Cannot parse RouterAI" in output
        or "JSONDecodeError" in output
    ):
        return "non_json_response"

    if is_read_timeout_error_text(output):
        return "read_timeout"

    return "subprocess_failed"


def called_process_output(exc: subprocess.CalledProcessError) -> str:
    return "\n".join(
        part
        for part in (
            output_to_text(exc.stderr),
            output_to_text(exc.stdout),
        )
        if part
    )


def should_retry_subprocess_failure(exc: subprocess.CalledProcessError) -> bool:
    output = called_process_output(exc)
    if classify_failure_output(output) != "subprocess_failed":
        return False

    return is_transient_network_error_text(output)


def describe_subprocess_failure(external_id: str, exc: subprocess.CalledProcessError) -> LLMCandidateFailure:
    output = called_process_output(exc)
    detail = compact_failure_detail(output) or f"returncode={exc.returncode}"

    return LLMCandidateFailure(
        external_id=external_id,
        reason=classify_failure_output(output),
        detail=detail,
        returncode=exc.returncode,
    )


def describe_timeout_failure(external_id: str, exc: subprocess.TimeoutExpired) -> LLMCandidateFailure:
    timeout = exc.timeout
    detail = (
        f"timeout_seconds={timeout:g}"
        if isinstance(timeout, (int, float))
        else f"timeout_seconds={timeout}"
    )

    return LLMCandidateFailure(
        external_id=external_id,
        reason="timeout",
        detail=detail,
    )


def describe_exception_failure(external_id: str, exc: Exception) -> LLMCandidateFailure:
    detail = str(exc) or exc.__class__.__name__
    return LLMCandidateFailure(
        external_id=external_id,
        reason="read_timeout" if is_read_timeout_error_text(detail) else "subprocess_failed",
        detail=truncate_detail(detail),
    )


def describe_document_not_ready_failure(exc: DocumentNotReadyError) -> LLMCandidateFailure:
    readiness = exc.readiness
    detail_parts = [
        f"docs={optional_int_text(readiness.docs_after)}",
        f"docs_with_text={optional_int_text(readiness.docs_with_text_after)}",
        f"extraction_retry_triggered={str(readiness.extraction_retry_triggered).lower()}",
        f"preparation_status={readiness.preparation_status}",
    ]
    if readiness.preparation_summary:
        detail_parts.append(f"summary={truncate_detail(readiness.preparation_summary, 180)}")
    if readiness.preparation_errors:
        detail_parts.append(
            "errors="
            + truncate_detail("; ".join(readiness.preparation_errors[:3]), 220)
        )

    return LLMCandidateFailure(
        external_id=readiness.external_id,
        reason=readiness.warning_reason or readiness.document_state,
        detail=" ".join(detail_parts),
    )


def log_llm_candidate_failure(failure: LLMCandidateFailure) -> None:
    parts = [
        f"LLM candidate failed: external_id={failure.external_id}",
        f"reason={failure.reason}",
    ]

    if failure.returncode is not None:
        parts.append(f"returncode={failure.returncode}")

    if failure.detail:
        parts.append(f"detail={failure.detail}")

    print(" ".join(parts), flush=True)


def log_llm_candidate_network_retry(
    *,
    external_id: str,
    attempt: int,
    max_attempts: int,
    backoff_seconds: int,
    output: str,
) -> None:
    print(
        "LLM candidate transient network failure; retrying "
        f"external_id={external_id} "
        f"attempt={attempt} "
        f"max_attempts={max_attempts} "
        f"backoff_seconds={backoff_seconds} "
        f"reason={transient_network_error_summary(output)}",
        flush=True,
    )


def start_network_retry_event(row: dict[str, Any]) -> dict[str, Any]:
    events = row.setdefault("_llm_network_retry_events", [])
    if not isinstance(events, list):
        events = []
        row["_llm_network_retry_events"] = events

    event = {
        "attempts": 0,
        "succeeded": False,
        "failed": False,
    }
    events.append(event)
    return event


def row_network_retry_counts(row: dict[str, Any]) -> tuple[int, int, int]:
    events = row.get("_llm_network_retry_events")
    if not isinstance(events, list):
        return 0, 0, 0

    attempts = 0
    succeeded = 0
    failed = 0
    for event in events:
        if not isinstance(event, dict):
            continue

        try:
            event_attempts = int(event.get("attempts") or 0)
        except (TypeError, ValueError):
            event_attempts = 0

        if event_attempts <= 0:
            continue

        attempts += event_attempts
        if event.get("succeeded"):
            succeeded += 1
        elif event.get("failed"):
            failed += 1

    return attempts, succeeded, failed


def documents_count_for_candidate(row: dict[str, Any]) -> int:
    return document_counts_for_candidate(row).documents_count


def candidate_has_extracted_document_text(row: dict[str, Any]) -> bool:
    for key in (
        "documents_with_text_count",
        "docs_with_text_count",
        "documents_with_text",
        "docs_with_text",
    ):
        if key in row and _non_negative_int(row.get(key)) > 0:
            return True

    try:
        return document_counts_for_candidate(row).documents_with_text_count > 0
    except Exception:
        return False


def document_counts_for_candidate(row: dict[str, Any]) -> CandidateDocumentCounts:
    import psycopg

    from app.config import settings

    tender_id = row.get("tender_id") or row.get("id")
    external_id = row.get("external_id")

    if tender_id:
        where = "tender_id = %s"
        params = (str(tender_id),)
    elif external_id:
        where = "tender_id = (SELECT id FROM tenders WHERE external_id = %s LIMIT 1)"
        params = (str(external_id),)
    else:
        return CandidateDocumentCounts(documents_count=0, documents_with_text_count=0)

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS documents_count,
                    COUNT(*) FILTER (
                        WHERE extracted_text IS NOT NULL
                          AND btrim(extracted_text) <> ''
                    ) AS documents_with_text_count
                FROM documents
                WHERE {where};
                """,
                params,
            )
            row = cur.fetchone()
            return CandidateDocumentCounts(
                documents_count=int(row[0] or 0),
                documents_with_text_count=int(row[1] or 0),
            )


def candidate_primary_technical_document_status(row: dict[str, Any]) -> dict[str, Any]:
    tender_id = row.get("tender_id") or row.get("id")
    if not tender_id:
        return {"required": False, "ready": True, "reason": None}

    try:
        from app.pipeline.prepare_tender_for_analysis import (
            load_downloaded_documents,
            primary_technical_document_status,
        )

        return primary_technical_document_status(
            row,
            load_downloaded_documents(str(tender_id)),
        )
    except Exception:
        return {"required": False, "ready": True, "reason": None}


def primary_technical_warning_reason(status: dict[str, Any]) -> str | None:
    if status.get("required") and not status.get("ready"):
        reason = str(status.get("reason") or "").strip()
        return reason or PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED
    return None


def optional_int_text(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def document_state_for_counts(
    *,
    docs_count: int | None,
    docs_with_text_count: int | None,
) -> str:
    if docs_count is None or docs_with_text_count is None:
        return "unknown"

    if docs_count <= 0:
        return DOCUMENTS_MISSING

    if docs_with_text_count <= 0:
        return DOCUMENTS_DOWNLOADED_WITHOUT_TEXT

    return DOCUMENTS_READY_WITH_TEXT


def readiness_warning_reason(
    *,
    docs_after: int | None,
    docs_with_text_after: int | None,
) -> str | None:
    state = document_state_for_counts(
        docs_count=docs_after,
        docs_with_text_count=docs_with_text_after,
    )
    if state == "unknown" or state == DOCUMENTS_READY_WITH_TEXT:
        return None

    return state


def preparation_status_value(result: Any) -> str:
    status = getattr(result, "status", None)
    value = getattr(status, "value", status)
    return str(value or "unknown")


def log_document_readiness(readiness: CandidateDocumentReadiness) -> None:
    print(
        "LLM document readiness: "
        f"external_id={readiness.external_id} "
        f"docs_before={optional_int_text(readiness.docs_before)} "
        f"docs_with_text_before={optional_int_text(readiness.docs_with_text_before)} "
        f"docs_after={optional_int_text(readiness.docs_after)} "
        f"docs_with_text_after={optional_int_text(readiness.docs_with_text_after)} "
        f"targeted_download_triggered={str(readiness.targeted_download_triggered).lower()} "
        f"extraction_retry_triggered={str(readiness.extraction_retry_triggered).lower()} "
        f"preparation_status={readiness.preparation_status} "
        f"document_state={readiness.document_state} "
        f"llm_readiness={readiness.llm_readiness}",
        flush=True,
    )

    if readiness.warning_reason:
        print(
            "LLM document readiness warning: "
            f"external_id={readiness.external_id} "
            f"reason={readiness.warning_reason} "
            f"docs_before={optional_int_text(readiness.docs_before)} "
            f"docs_with_text_before={optional_int_text(readiness.docs_with_text_before)} "
            f"docs_after={optional_int_text(readiness.docs_after)} "
            f"docs_with_text_after={optional_int_text(readiness.docs_with_text_after)}",
            flush=True,
        )

    if readiness.non_blocking_warning_reason:
        print(
            "LLM document readiness warning: "
            f"external_id={readiness.external_id} "
            f"reason={readiness.non_blocking_warning_reason} "
            "llm_readiness=ready_for_llm",
            flush=True,
        )

    if readiness.preparation_errors:
        print(
            "LLM document preparation errors: "
            f"external_id={readiness.external_id} "
            f"errors_count={len(readiness.preparation_errors)} "
            f"errors={'; '.join(readiness.preparation_errors[:3])}",
            flush=True,
        )

    if readiness.check_error:
        print(
            "LLM document readiness check error: "
            f"external_id={readiness.external_id} "
            f"error={readiness.check_error}",
            flush=True,
        )


def ensure_targeted_documents_before_llm(
    row: dict[str, Any],
    *,
    dry_run: bool,
    limit_docs: int = TARGETED_DOCUMENT_LIMIT,
) -> CandidateDocumentReadiness:
    external_id = str(row.get("external_id") or "unknown")

    try:
        counts_before = document_counts_for_candidate(row)
    except Exception as exc:
        readiness = CandidateDocumentReadiness(
            external_id=external_id,
            docs_before=None,
            docs_with_text_before=None,
            docs_after=None,
            docs_with_text_after=None,
            targeted_download_triggered=False,
            extraction_retry_triggered=False,
            preparation_status="check_failed",
            document_state="unknown",
            check_error=truncate_detail(str(exc)),
        )
        log_document_readiness(readiness)
        return readiness

    docs_before = counts_before.documents_count
    docs_with_text_before = counts_before.documents_with_text_count
    primary_status_before = candidate_primary_technical_document_status(row)
    primary_warning_before = primary_technical_warning_reason(primary_status_before)
    should_prepare = (
        docs_before == 0
        or docs_with_text_before == 0
        or primary_warning_before is not None
    )

    if not should_prepare:
        readiness = CandidateDocumentReadiness(
            external_id=external_id,
            docs_before=docs_before,
            docs_with_text_before=docs_with_text_before,
            docs_after=docs_before,
            docs_with_text_after=docs_with_text_before,
            targeted_download_triggered=False,
            extraction_retry_triggered=False,
            preparation_status="not_triggered",
            document_state=DOCUMENTS_READY_WITH_TEXT,
        )
        log_document_readiness(readiness)
        return readiness

    if dry_run:
        warning_reason = primary_warning_before or readiness_warning_reason(
            docs_after=docs_before,
            docs_with_text_after=docs_with_text_before,
        )
        readiness = CandidateDocumentReadiness(
            external_id=external_id,
            docs_before=docs_before,
            docs_with_text_before=docs_with_text_before,
            docs_after=docs_before,
            docs_with_text_after=docs_with_text_before,
            targeted_download_triggered=True,
            extraction_retry_triggered=docs_before > 0 and docs_with_text_before == 0,
            preparation_status="dry_run_skipped",
            document_state=(
                primary_warning_before
                or document_state_for_counts(
                    docs_count=docs_before,
                    docs_with_text_count=docs_with_text_before,
                )
            ),
            warning_reason=warning_reason,
        )
        log_document_readiness(readiness)
        return readiness

    try:
        from app.pipeline.prepare_tender_for_analysis import prepare_tender_for_analysis

        result = prepare_tender_for_analysis(
            external_id=external_id,
            tender_context=row,
            limit_docs=limit_docs,
        )
        counts_after = document_counts_for_candidate(row)
        docs_after = counts_after.documents_count
        docs_with_text_after = counts_after.documents_with_text_count
        preparation_status = preparation_status_value(result)
        preparation_summary = str(getattr(result, "summary", "") or "")
        preparation_document_readiness = str(
            getattr(result, "document_readiness", "") or ""
        )
        targeted_download_triggered = bool(getattr(result, "document_download_was_run", True))
        steps = getattr(result, "steps", {}) or {}
        extraction_step = steps.get("text_extraction") if isinstance(steps, dict) else None
        download_step = (
            steps.get("targeted_document_download") if isinstance(steps, dict) else None
        )
        download_details = getattr(download_step, "details", {}) or {}
        extraction_retry_triggered = (
            docs_before > 0
            and docs_with_text_before == 0
        ) or bool(getattr(extraction_step, "attempted", False))
        result_errors = tuple(str(item) for item in (getattr(result, "errors", []) or []))
    except Exception as exc:
        try:
            counts_after = document_counts_for_candidate(row)
            docs_after = counts_after.documents_count
            docs_with_text_after = counts_after.documents_with_text_count
        except Exception:
            docs_after = None
            docs_with_text_after = None

        readiness = CandidateDocumentReadiness(
            external_id=external_id,
            docs_before=docs_before,
            docs_with_text_before=docs_with_text_before,
            docs_after=docs_after,
            docs_with_text_after=docs_with_text_after,
            targeted_download_triggered=True,
            extraction_retry_triggered=docs_before > 0 and docs_with_text_before == 0,
            preparation_status="failed",
            document_state=document_state_for_counts(
                docs_count=docs_after,
                docs_with_text_count=docs_with_text_after,
            ),
            preparation_summary=truncate_detail(str(exc)),
            preparation_errors=(truncate_detail(str(exc)),),
            warning_reason=readiness_warning_reason(
                docs_after=docs_after,
                docs_with_text_after=docs_with_text_after,
            ),
            check_error=truncate_detail(str(exc)),
        )
        log_document_readiness(readiness)
        return readiness

    document_state = document_state_for_counts(
        docs_count=docs_after,
        docs_with_text_count=docs_with_text_after,
    )
    primary_warning_after = (
        preparation_document_readiness
        if preparation_document_readiness in PRIMARY_TECHNICAL_DOCUMENT_READINESS_REASONS
        else None
    )
    if primary_warning_after:
        document_state = primary_warning_after

    partial_rate_limit_warning = None
    if int(download_details.get("documents_skipped_due_to_rate_limit") or 0) > 0:
        partial_rate_limit_warning = "partial_documents_rate_limited_429"

    readiness = CandidateDocumentReadiness(
        external_id=external_id,
        docs_before=docs_before,
        docs_with_text_before=docs_with_text_before,
        docs_after=docs_after,
        docs_with_text_after=docs_with_text_after,
        targeted_download_triggered=targeted_download_triggered,
        extraction_retry_triggered=extraction_retry_triggered,
        preparation_status=preparation_status,
        document_state=document_state,
        preparation_summary=preparation_summary,
        preparation_errors=result_errors,
        warning_reason=primary_warning_after or readiness_warning_reason(
            docs_after=docs_after,
            docs_with_text_after=docs_with_text_after,
        ),
        non_blocking_warning_reason=(
            partial_rate_limit_warning
            if document_state == DOCUMENTS_READY_WITH_TEXT
            else None
        ),
        documents_found=(
            int(download_details.get("documents_found"))
            if download_details.get("documents_found") is not None
            else None
        ),
        documents_selected=int(download_details.get("documents_selected") or 0),
        documents_failed=int(download_details.get("documents_failed") or 0),
        documents_failed_items=tuple(
            item
            for item in (download_details.get("documents_failed_items") or [])
            if isinstance(item, dict)
        ),
        documents_skipped_due_to_rate_limit=int(
            download_details.get("documents_skipped_due_to_rate_limit") or 0
        ),
        documents_skipped_due_to_rate_limit_items=tuple(
            item
            for item in (download_details.get("documents_skipped_due_to_rate_limit_items") or [])
            if isinstance(item, dict)
        ),
        missing_high_value_technical_document=bool(
            download_details.get("missing_high_value_technical_document")
        ),
        missing_high_value_technical_document_title=(
            download_details.get("missing_high_value_technical_document_title")
        ),
        document_download_planner_used=bool(
            download_details.get("document_download_planner_used")
        ),
        document_download_planner_confidence=(
            download_details.get("document_download_planner_confidence")
        ),
    )
    log_document_readiness(readiness)
    return readiness


def readiness_metadata(readiness: CandidateDocumentReadiness) -> dict[str, Any]:
    return {
        "external_id": readiness.external_id,
        "docs_before": readiness.docs_before,
        "docs_with_text_before": readiness.docs_with_text_before,
        "docs_after": readiness.docs_after,
        "docs_with_text_after": readiness.docs_with_text_after,
        "targeted_download_triggered": readiness.targeted_download_triggered,
        "extraction_retry_triggered": readiness.extraction_retry_triggered,
        "preparation_status": readiness.preparation_status,
        "document_state": readiness.document_state,
        "preparation_summary": readiness.preparation_summary,
        "preparation_errors": list(readiness.preparation_errors),
        "warning_reason": readiness.warning_reason,
        "non_blocking_warning_reason": readiness.non_blocking_warning_reason,
        "check_error": readiness.check_error,
        "documents_found": readiness.documents_found,
        "documents_selected": readiness.documents_selected,
        "documents_failed": readiness.documents_failed,
        "documents_failed_items": list(readiness.documents_failed_items),
        "documents_skipped_due_to_rate_limit": (
            readiness.documents_skipped_due_to_rate_limit
        ),
        "documents_skipped_due_to_rate_limit_items": list(
            readiness.documents_skipped_due_to_rate_limit_items
        ),
        "missing_high_value_technical_document": (
            readiness.missing_high_value_technical_document
        ),
        "missing_high_value_technical_document_title": (
            readiness.missing_high_value_technical_document_title
        ),
        "document_download_planner_used": readiness.document_download_planner_used,
        "document_download_planner_confidence": (
            readiness.document_download_planner_confidence
        ),
        "llm_readiness": readiness.llm_readiness,
    }


def readiness_has_explicit_blocked_or_no_valid_status(
    readiness: CandidateDocumentReadiness,
) -> bool:
    text = " ".join(
        str(value or "").lower()
        for value in (
            readiness.preparation_status,
            readiness.document_state,
            readiness.warning_reason,
            readiness.non_blocking_warning_reason,
            readiness.preparation_summary,
            readiness.check_error,
        )
    )
    return any(
        marker in text
        for marker in (
            "blocked_by_marketplace_auth",
            "marketplace_auth",
            "external_marketplace_auth_required",
            "no_valid_documents",
            "preparation_no_valid_documents",
        )
    )


def lead_card_only_warning_reason(readiness: CandidateDocumentReadiness) -> str:
    if readiness_has_explicit_blocked_or_no_valid_status(readiness):
        return "lead_documents_blocked_or_no_valid_allowed"
    if readiness.docs_after is not None and readiness.docs_after <= 0:
        return "lead_documents_missing_allowed_after_prepare"
    if readiness.docs_with_text_after is not None and readiness.docs_with_text_after <= 0:
        return "lead_documents_without_text_allowed_after_prepare"
    return readiness.warning_reason or readiness.document_state or "lead_card_only_allowed_after_prepare"


def lead_document_preparation_was_attempted(readiness: CandidateDocumentReadiness) -> bool:
    if readiness.targeted_download_triggered or readiness.extraction_retry_triggered:
        return readiness.preparation_status != "dry_run_skipped"
    return readiness.preparation_status not in {
        "not_triggered",
        "dry_run_skipped",
        "check_failed",
    }


def ensure_lead_documents_before_llm(
    row: dict[str, Any],
    *,
    dry_run: bool,
    limit_docs: int = TARGETED_DOCUMENT_LIMIT,
) -> CandidateDocumentReadiness:
    readiness = ensure_targeted_documents_before_llm(
        row,
        dry_run=dry_run,
        limit_docs=limit_docs,
    )
    if readiness.llm_readiness == "ready_for_llm":
        return readiness

    actual_attempt = lead_document_preparation_was_attempted(readiness)
    explicit_blocked = readiness_has_explicit_blocked_or_no_valid_status(readiness)
    if not actual_attempt and not explicit_blocked:
        return readiness

    allowed = replace(
        readiness,
        warning_reason=None,
        non_blocking_warning_reason=lead_card_only_warning_reason(readiness),
    )
    log_document_readiness(allowed)
    return allowed


def lead_card_context_readiness_before_llm(
    row: dict[str, Any],
    *,
    dry_run: bool,
) -> CandidateDocumentReadiness:
    return ensure_lead_documents_before_llm(row, dry_run=dry_run)


def print_llm_summary(
    successful_external_ids: list[str],
    failures: list[LLMCandidateFailure],
    document_warnings: list[CandidateDocumentReadiness] | None = None,
    document_readiness_events: list[CandidateDocumentReadiness] | None = None,
    *,
    analysis_depth: str | None = None,
    limits: AnalysisLimits | None = None,
    fallback_succeeded: int = 0,
    fallback_failed: int = 0,
    fallback_attempts: list[LLMCandidateFallbackAttempt] | None = None,
    network_retry_attempts: int = 0,
    network_retry_succeeded: int = 0,
    network_retry_failed: int = 0,
    retry_external_ids: list[str] | None = None,
    lead_selector_used: int = 0,
    lead_selector_failed: int = 0,
    lead_triage_diagnostics: LeadTriageDiagnostics | None = None,
    lead_full_report_diagnostics: LeadFullReportDiagnostics | None = None,
    duplicate_operational_lead_report_rows: list[dict[str, Any]] | None = None,
    report_kind: str = REPORT_KIND_TECHNICAL,
) -> None:
    document_warnings = document_warnings or []
    document_readiness_events = document_readiness_events or []
    fallback_attempts = fallback_attempts or []
    retry_external_ids = retry_external_ids or []

    print()
    print("LLM shortlist summary:", flush=True)
    if analysis_depth:
        print(f"- analysis_depth={analysis_depth}", flush=True)
    if limits:
        print(f"- max_spec_chars={limits.max_spec_chars}", flush=True)
        print(f"- max_other_chars={limits.max_other_chars}", flush=True)
        print(f"- max_output_tokens={limits.max_output_tokens}", flush=True)
        print(
            "- fallback=enabled_on_non_json_response "
            "reason=non_json_response",
            flush=True,
        )
    print(f"- succeeded: {len(successful_external_ids)}", flush=True)
    print(f"- failed: {len(failures)}", flush=True)
    print(f"- fallback_succeeded: {fallback_succeeded}", flush=True)
    print(f"- fallback_failed: {fallback_failed}", flush=True)
    print(f"- network_retry_attempts: {network_retry_attempts}", flush=True)
    print(f"- network_retry_succeeded: {network_retry_succeeded}", flush=True)
    print(f"- network_retry_failed: {network_retry_failed}", flush=True)
    if lead_triage_diagnostics is not None:
        print_lead_triage_summary(lead_triage_diagnostics)
    if lead_full_report_diagnostics is not None:
        print("- lead full report summary:", flush=True)
        print(
            f"- lead_full_report_attempted: {lead_full_report_diagnostics.attempted}",
            flush=True,
        )
        print(
            f"- lead_full_report_succeeded: {lead_full_report_diagnostics.succeeded}",
            flush=True,
        )
        print(
            f"- full_reports_created: {lead_full_report_diagnostics.succeeded}",
            flush=True,
        )
        print(
            f"- lead_full_report_failed: {lead_full_report_diagnostics.failed}",
            flush=True,
        )
        print(
            "- lead_full_report_existing_skipped: "
            f"{lead_full_report_diagnostics.existing_skipped}",
            flush=True,
        )
        print_duplicate_operational_lead_report_audit(
            duplicate_operational_lead_report_rows
        )
    read_timeout_external_ids = [
        failure.external_id for failure in failures if failure.reason == "read_timeout"
    ]
    print(f"- read_timeout_not_retried: {len(read_timeout_external_ids)}", flush=True)
    for attempt in fallback_attempts:
        status = "succeeded" if attempt.succeeded else "failed"
        print(
            "- fallback_attempt: "
            f"external_id={attempt.external_id} "
            f"reason={attempt.reason} "
            f"status={status} "
            f"analysis_depth={attempt.analysis_depth} "
            f"max_spec_chars={attempt.max_spec_chars} "
            f"max_other_chars={attempt.max_other_chars} "
            f"max_output_tokens={attempt.max_output_tokens}",
            flush=True,
        )
    print(f"- document_readiness_warnings: {len(document_warnings)}", flush=True)
    if normalize_report_kind(report_kind) == REPORT_KIND_LEAD and document_readiness_events:
        lead_docprep_triggered = sum(
            1 for item in document_readiness_events if item.targeted_download_triggered
        )
        lead_docprep_succeeded = sum(
            1
            for item in document_readiness_events
            if item.targeted_download_triggered
            and (item.docs_with_text_after or 0) > 0
        )
        lead_docprep_card_only = sum(
            1
            for item in document_readiness_events
            if item.non_blocking_warning_reason
            and str(item.non_blocking_warning_reason).startswith("lead_")
        )
        lead_primary_doc_found = sum(
            1
            for item in document_readiness_events
            if (item.docs_with_text_after or 0) > 0
            and item.warning_reason not in PRIMARY_TECHNICAL_DOCUMENT_READINESS_REASONS
            and item.non_blocking_warning_reason not in PRIMARY_TECHNICAL_DOCUMENT_READINESS_REASONS
        )
        lead_primary_doc_missing = sum(
            1
            for item in document_readiness_events
            if item.warning_reason in PRIMARY_TECHNICAL_DOCUMENT_READINESS_REASONS
            or item.non_blocking_warning_reason in PRIMARY_TECHNICAL_DOCUMENT_READINESS_REASONS
            or (
                item.non_blocking_warning_reason
                and "lead_" in item.non_blocking_warning_reason
            )
        )
        print(f"- lead_docprep_triggered: {lead_docprep_triggered}", flush=True)
        print(f"- lead_docprep_succeeded: {lead_docprep_succeeded}", flush=True)
        print(f"- lead_docprep_card_only: {lead_docprep_card_only}", flush=True)
        print(f"- lead_primary_doc_found: {lead_primary_doc_found}", flush=True)
        print(f"- lead_primary_doc_missing: {lead_primary_doc_missing}", flush=True)
        print(f"- lead_selector_used: {lead_selector_used}", flush=True)
        print(f"- lead_selector_failed: {lead_selector_failed}", flush=True)

    if successful_external_ids:
        print(f"- succeeded_external_ids: {', '.join(successful_external_ids)}", flush=True)

    if retry_external_ids:
        print(f"- retry_external_ids: {', '.join(retry_external_ids)}", flush=True)

    if read_timeout_external_ids:
        print(
            f"- read_timeout_external_ids: {', '.join(read_timeout_external_ids)}",
            flush=True,
        )

    if failures:
        failed_external_ids = ", ".join(f"{failure.external_id}({failure.reason})" for failure in failures)
        print(f"- failed_external_ids: {failed_external_ids}", flush=True)

    if document_warnings:
        print("Document warnings:", flush=True)
        for item in document_warnings:
            print(
                "- "
                f"external_id={item.external_id} "
                f"reason={item.warning_reason or item.non_blocking_warning_reason} "
                f"document_state={item.document_state} "
                f"docs_after={item.docs_after} "
                f"docs_with_text_after={item.docs_with_text_after}",
                flush=True,
            )
        warning_external_ids = ", ".join(
            f"{item.external_id}({item.warning_reason or item.non_blocking_warning_reason})"
            for item in document_warnings
        )
        print(f"- document_warning_external_ids: {warning_external_ids}", flush=True)
    run_status = "success"
    if failures and successful_external_ids:
        run_status = "partial_failure"
    elif failures:
        run_status = "failure"
    print("Final run status:", flush=True)
    print(f"- run_status: {run_status}", flush=True)


def print_lead_triage_summary(diagnostics: LeadTriageDiagnostics) -> None:
    print("Lead triage summary:", flush=True)
    print(f"- lead_triage_candidates_total: {diagnostics.candidates_total}", flush=True)
    print(f"- sent_to_paid_triage: {diagnostics.llm_attempted}", flush=True)
    print(f"- lead_triage_llm_attempted: {diagnostics.llm_attempted}", flush=True)
    print(f"- lead_triage_llm_succeeded: {diagnostics.llm_succeeded}", flush=True)
    print(f"- lead_triage_llm_failed: {diagnostics.llm_failed}", flush=True)
    print(f"- triage_reused_existing: {diagnostics.triage_reused_existing}", flush=True)
    print(f"- lead_triage_cache_hits: {diagnostics.cache_hits}", flush=True)
    print(f"- lead_triage_reused_existing: {diagnostics.reused_existing}", flush=True)
    print(f"- lead_triage_fresh_cache_used: {diagnostics.fresh_cache_used}", flush=True)
    print(f"- retriaged_due_to_stale_ttl: {diagnostics.retriaged_due_to_stale_ttl}", flush=True)
    print(
        f"- lead_triage_stale_existing_ignored: {diagnostics.stale_existing_ignored}",
        flush=True,
    )
    print(
        f"- lead_triage_stale_cache_ignored: {diagnostics.stale_cache_ignored}",
        flush=True,
    )
    print(f"- lead_triage_deterministic: {diagnostics.deterministic}", flush=True)
    print(f"- lead_triage_go: {diagnostics.go}", flush=True)
    print(f"- lead_triage_maybe: {diagnostics.maybe}", flush=True)
    print(f"- lead_triage_reject: {diagnostics.reject}", flush=True)
    print(f"- lead_triage_maybe_deferred: {diagnostics.maybe_deferred}", flush=True)
    print(
        f"- lead_triage_selected_for_report: {diagnostics.selected_for_report}",
        flush=True,
    )
    print(
        f"- lead_triage_report_limit_reached: {diagnostics.report_limit_reached}",
        flush=True,
    )
    print(
        f"- lead_triage_go_waiting_for_report: {diagnostics.go_waiting_for_report}",
        flush=True,
    )
    print(
        f"- lead_report_limit_reached_go: {diagnostics.report_limit_reached_go}",
        flush=True,
    )
    print(f"- go_waiting_for_full_report: {diagnostics.go_waiting_for_report}", flush=True)
    if diagnostics.go_waiting_for_report_rows:
        print("Go candidates waiting for full report:", flush=True)
        for item in diagnostics.go_waiting_for_report_rows:
            print(
                "- "
                f"external_id={item.get('external_id') or 'unknown'} | "
                f"price={int(item.get('price') or 0):,} руб. | "
                f"category={item.get('category') or ''} | "
                f"triage_priority={item.get('triage_priority') or ''} | "
                f"title={item.get('title') or ''}",
                flush=True,
            )


def print_debug_skip_limit_summary(debug_skips: DebugSkipControl) -> None:
    if not isinstance(debug_skips, DebugSkipLimiter) or not debug_skips.suppressed_by_bucket:
        return
    print("Debug skip output limit:", flush=True)
    print(f"- debug_skips_limit: {debug_skips.limit}", flush=True)
    print(f"- debug_skip_lines_suppressed: {debug_skips.suppressed_total}", flush=True)
    formatted = ", ".join(
        f"{bucket}={count}"
        for bucket, count in sorted(
            debug_skips.suppressed_by_bucket.items(),
            key=lambda item: (-item[1], item[0]),
        )
    )
    print(f"- suppressed_by_bucket: {formatted}", flush=True)


def selection_empty_reason(diagnostics: ShortlistSelectionDiagnostics) -> str:
    skip_counts = diagnostics.skip_counts or {}
    if diagnostics.rule_based_rows == 0:
        return "no_rule_based_rows_from_digest"
    if diagnostics.rule_based_passed == 0:
        return "all_rows_filtered_by_effective_recommendation"
    if diagnostics.eligible_for_llm_before_limit == 0:
        if not skip_counts:
            return "no_eligible_rows_after_selection_filters"
        top_reason = max(skip_counts.items(), key=lambda item: item[1])[0]
        return f"all_rows_filtered_after_rule_based_top_reason={top_reason}"
    if diagnostics.selected_for_llm == 0:
        return "eligible_rows_exist_but_limit_selected_zero"
    return "not_empty"


def print_selection_diagnostics(diagnostics: ShortlistSelectionDiagnostics) -> None:
    print()
    print("LLM shortlist selection diagnostics:", flush=True)
    print(f"- rule_based_rows: {diagnostics.rule_based_rows}", flush=True)
    print(f"- rule_based_passed: {diagnostics.rule_based_passed}", flush=True)
    print(f"- business_rules_skipped: {diagnostics.business_rules_skipped}", flush=True)
    print(
        f"- eligible_for_llm_before_limit: {diagnostics.eligible_for_llm_before_limit}",
        flush=True,
    )
    print(f"- selected_for_llm: {diagnostics.selected_for_llm}", flush=True)
    if diagnostics.selected_for_lead_triage is not None:
        pre_triage_seen = (
            diagnostics.pre_triage_candidates_seen
            if diagnostics.pre_triage_candidates_seen is not None
            else diagnostics.rule_based_rows
        )
        print(f"- pre_triage_candidates_seen: {pre_triage_seen}", flush=True)
        print(
            "- skipped_before_triage_by_negative_feedback: "
            f"{diagnostics.skipped_before_triage_by_negative_feedback}",
            flush=True,
        )
        print(
            "- skipped_before_triage_by_existing_report: "
            f"{diagnostics.skipped_before_triage_by_existing_report}",
            flush=True,
        )
        print(
            "- skipped_before_triage_by_hard_noise: "
            f"{diagnostics.skipped_before_triage_by_hard_noise}",
            flush=True,
        )
        print(f"- hard_noise_total: {diagnostics.hard_noise_total}", flush=True)
        print(
            f"- hard_noise_strict_skipped: {diagnostics.hard_noise_strict_skipped}",
            flush=True,
        )
        print(
            "- hard_noise_overridden_by_target_signal: "
            f"{diagnostics.hard_noise_overridden_by_target_signal}",
            flush=True,
        )
        print(
            "- hard_noise_overridden_by_existing_go: "
            f"{diagnostics.hard_noise_overridden_by_existing_go}",
            flush=True,
        )
        print(
            "- hard_noise_suspicious_manual_review: "
            f"{diagnostics.hard_noise_suspicious_manual_review}",
            flush=True,
        )
        print(
            f"- selected_for_lead_triage: {diagnostics.selected_for_lead_triage}",
            flush=True,
        )
    print(f"- empty_reason: {selection_empty_reason(diagnostics)}", flush=True)

    skip_counts = diagnostics.skip_counts or {}
    if skip_counts:
        print("Skip summary by reason:", flush=True)
        for reason, count in sorted(
            skip_counts.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            print(f"- {reason}: {count}", flush=True)
        formatted = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(
                skip_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        print(f"- skip_counts: {formatted}", flush=True)
    else:
        print("Skip summary by reason:", flush=True)
        print("- none", flush=True)

    if diagnostics.selected_for_lead_triage is not None:
        print("Hard-noise summary:", flush=True)
        print(f"- hard_noise_total: {diagnostics.hard_noise_total}", flush=True)
        print(
            f"- strict_skipped: {diagnostics.hard_noise_strict_skipped}",
            flush=True,
        )
        print(
            f"- overridden_by_target_signal: {diagnostics.hard_noise_overridden_by_target_signal}",
            flush=True,
        )
        print(
            f"- overridden_by_existing_go: {diagnostics.hard_noise_overridden_by_existing_go}",
            flush=True,
        )
        print(
            f"- suspicious_manual_review: {diagnostics.hard_noise_suspicious_manual_review}",
            flush=True,
        )
        print("Existing report skipped count:", flush=True)
        print(f"- {diagnostics.skipped_before_triage_by_existing_report}", flush=True)
        print("Negative feedback skipped count:", flush=True)
        print(f"- {diagnostics.skipped_before_triage_by_negative_feedback}", flush=True)


def has_llm_report(row: dict[str, Any]) -> bool:
    result = row.get("llm_report_result")
    if not isinstance(result, dict) or not result.get("report"):
        return False

    try:
        from app.digest import llm_report_is_stale_after_primary_technical_document

        if llm_report_is_stale_after_primary_technical_document(row, result):
            return False
    except Exception:
        pass

    return True


def safe_analysis_label(value: str | None) -> str:
    label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip())
    return label.strip("_.-")


def analysis_type_for_candidate_report(
    report_kind: str,
    result_label: str | None = None,
) -> str:
    base = (
        LEAD_LLM_REPORT_ANALYSIS_TYPE
        if normalize_report_kind(report_kind) == REPORT_KIND_LEAD
        else TECHNICAL_LLM_REPORT_ANALYSIS_TYPE
    )
    label = safe_analysis_label(result_label)
    return f"{base}_{label}" if label else base


def result_has_report(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("report"))


def row_report_result_for_analysis_type(
    row: dict[str, Any],
    analysis_type: str,
) -> Any:
    if (
        row.get("llm_report_analysis_type") == analysis_type
        and row.get("llm_report_result") is not None
    ):
        return row.get("llm_report_result")

    if analysis_type == LEAD_LLM_REPORT_ANALYSIS_TYPE:
        for key in ("lead_llm_report_result", "llm_customer_lead_report_result"):
            if row.get(key) is not None:
                return row.get(key)

    results_by_type = row.get("llm_report_results_by_type")
    if isinstance(results_by_type, dict) and analysis_type in results_by_type:
        return results_by_type.get(analysis_type)

    return None


def analysis_result_exists_for_candidate(
    row: dict[str, Any],
    analysis_type: str,
    *,
    allow_external_id_fallback: bool = False,
) -> bool:
    tender_id = row.get("tender_id") or row.get("id")
    external_id = row.get("external_id")

    if tender_id:
        where = "tender_id = %s"
        params = (str(tender_id), analysis_type)
    elif external_id and allow_external_id_fallback:
        where = "tender_id = (SELECT id FROM tenders WHERE external_id = %s LIMIT 1)"
        params = (str(external_id), analysis_type)
    else:
        return False

    try:
        import psycopg

        from app.config import settings

        with psycopg.connect(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT result
                    FROM analysis_results
                    WHERE {where}
                      AND analysis_type = %s
                    ORDER BY created_at DESC
                    LIMIT 1;
                    """,
                    params,
                )
                row_result = cur.fetchone()
    except Exception:
        return False

    return bool(row_result and result_has_report(row_result[0]))


def operational_lead_report_exists_for_candidate(
    row: dict[str, Any],
    *,
    allow_external_id_fallback: bool = True,
) -> bool:
    row_result = row_report_result_for_analysis_type(row, LEAD_LLM_REPORT_ANALYSIS_TYPE)
    if row_result is not None:
        return result_has_report(row_result)

    if "operational_lead_report_analysis_type" in row:
        return False

    return analysis_result_exists_for_candidate(
        row,
        LEAD_LLM_REPORT_ANALYSIS_TYPE,
        allow_external_id_fallback=allow_external_id_fallback,
    )


def has_llm_report_for_kind(
    row: dict[str, Any],
    *,
    report_kind: str,
    result_label: str | None = None,
    allow_external_id_fallback: bool = False,
) -> bool:
    kind = normalize_report_kind(report_kind)
    if kind == REPORT_KIND_TECHNICAL:
        return has_llm_report(row)

    analysis_type = analysis_type_for_candidate_report(kind, result_label)
    row_result = row_report_result_for_analysis_type(row, analysis_type)
    if row_result is not None:
        return result_has_report(row_result)

    if (
        kind == REPORT_KIND_LEAD
        and analysis_type == LEAD_LLM_REPORT_ANALYSIS_TYPE
        and "operational_lead_report_analysis_type" in row
    ):
        return False

    return analysis_result_exists_for_candidate(
        row,
        analysis_type,
        allow_external_id_fallback=allow_external_id_fallback,
    )


def get_duplicate_operational_lead_report_rows(
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    try:
        import psycopg
        from psycopg.rows import dict_row

        from app.config import settings

        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) OVER() AS duplicate_groups_total,
                        COALESCE(NULLIF(t.external_id, ''), a.tender_id::text) AS external_id,
                        COUNT(*) AS reports_count,
                        max(a.created_at) AS latest_report_created_at
                    FROM analysis_results a
                    LEFT JOIN tenders t ON t.id = a.tender_id
                    WHERE a.analysis_type = %s
                    GROUP BY COALESCE(NULLIF(t.external_id, ''), a.tender_id::text)
                    HAVING COUNT(*) > 1
                    ORDER BY COUNT(*) DESC, max(a.created_at) DESC
                    LIMIT %s;
                    """,
                    (LEAD_LLM_REPORT_ANALYSIS_TYPE, limit),
                )
                return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []


def print_duplicate_operational_lead_report_audit(
    rows: list[dict[str, Any]] | None = None,
) -> None:
    duplicates = (
        get_duplicate_operational_lead_report_rows()
        if rows is None
        else rows
    )
    total = 0
    if duplicates:
        try:
            total = int(duplicates[0].get("duplicate_groups_total") or len(duplicates))
        except (TypeError, ValueError):
            total = len(duplicates)

    print(f"- duplicate_operational_lead_reports: {total}", flush=True)
    if duplicates:
        external_ids = ", ".join(
            f"{row.get('external_id')}({row.get('reports_count')})"
            for row in duplicates
            if row.get("external_id")
        )
        if external_ids:
            print(f"- duplicate_external_ids: {external_ids}", flush=True)


def parse_preparation_retry_payload(message: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(message or "{}"))
    except json.JSONDecodeError:
        return {}

    return payload if isinstance(payload, dict) else {}


def latest_preparation_processing_event(
    row: dict[str, Any],
    event_type: str,
) -> PreparationRetryEvent | None:
    import psycopg

    from app.config import settings

    tender_id = row.get("tender_id") or row.get("id")
    external_id = row.get("external_id")

    if tender_id:
        where = "tender_id = %s"
        params = (str(tender_id), event_type)
    elif external_id:
        where = "tender_id = (SELECT id FROM tenders WHERE external_id = %s LIMIT 1)"
        params = (str(external_id), event_type)
    else:
        return None

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT message, created_at
                FROM processing_events
                WHERE {where}
                  AND event_type = %s
                ORDER BY created_at DESC
                LIMIT 1;
                """,
                params,
            )
            event = cur.fetchone()

    if not event:
        return None

    payload = parse_preparation_retry_payload(event[0])
    if not payload:
        return None

    return PreparationRetryEvent(payload=payload, created_at=event[1])


def latest_preparation_retry_event(row: dict[str, Any]) -> PreparationRetryEvent | None:
    return latest_preparation_processing_event(row, PREPARATION_REQUEUE_EVENT)


def latest_preparation_blocked_event(row: dict[str, Any]) -> PreparationRetryEvent | None:
    return latest_preparation_processing_event(row, PREPARATION_BLOCKED_EVENT)


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0

    return max(0, parsed)


def preparation_event_in_cooldown(
    created_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    if created_at is None:
        return True

    now_value = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None and now_value.tzinfo is not None:
        now_value = now_value.replace(tzinfo=None)
    elif created_at.tzinfo is not None and now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=timezone.utc)

    return (now_value - created_at).total_seconds() < (
        PREPARATION_EXHAUSTION_COOLDOWN_SECONDS
    )


def preparation_exhaustion_skip_reason(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str | None:
    try:
        event = latest_preparation_retry_event(row)
    except Exception:
        return None

    if event is None:
        return None

    payload = event.payload
    if not payload.get("exhausted"):
        return None

    if not preparation_event_in_cooldown(event.created_at, now=now):
        return None

    if candidate_has_extracted_document_text(row):
        return None

    return preparation_exhaustion_payload_skip_reason(payload)


def preparation_exhaustion_payload_skip_reason(payload: dict[str, Any]) -> str | None:
    documents_downloaded = _non_negative_int(payload.get("documents_downloaded"))
    documents_with_text = _non_negative_int(payload.get("documents_with_text"))

    if documents_downloaded <= 0:
        return "preparation_exhausted=documents_missing"

    if documents_with_text <= 0:
        return "preparation_exhausted=not_ready_for_llm"

    return None


def preparation_blocked_payload_skip_reason(payload: dict[str, Any]) -> str | None:
    status = str(payload.get("status") or "").lower()
    reason = str(payload.get("reason") or "").lower()

    if status == "blocked_by_marketplace_auth" or reason in {
        "marketplace_auth",
        "external_marketplace_auth_required",
    }:
        return "preparation_blocked=marketplace_auth"

    if status == "no_valid_documents" or reason == "no_valid_documents":
        return "preparation_no_valid_documents"

    return None


def preparation_blocked_skip_reason(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str | None:
    try:
        event = latest_preparation_blocked_event(row)
    except Exception:
        return None

    if event is None:
        return None

    if not preparation_event_in_cooldown(event.created_at, now=now):
        return None

    if candidate_has_extracted_document_text(row):
        return None

    return preparation_blocked_payload_skip_reason(event.payload)


def _direct_preparation_values(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "preparation_status",
        "preparation_state",
        "preparation_reason",
        "document_state",
        "warning_reason",
        "skip_reason",
        "manual_document_reason",
    ):
        if row.get(key):
            values.append(str(row.get(key)))

    result = row.get("result") or {}
    if isinstance(result, dict):
        for key in (
            "preparation_status",
            "preparation_state",
            "preparation_reason",
            "document_state",
            "warning_reason",
            "skip_reason",
        ):
            if result.get(key):
                values.append(str(result.get(key)))

    return values


def direct_preparation_skip_reason(row: dict[str, Any]) -> str | None:
    if candidate_has_extracted_document_text(row):
        return None

    blocked_payload = parse_preparation_retry_payload(row.get("preparation_blocked_event"))
    if blocked_payload:
        blocked_reason = preparation_blocked_payload_skip_reason(blocked_payload)
        if blocked_reason:
            return blocked_reason

    requeue_payload = parse_preparation_retry_payload(row.get("preparation_requeue_event"))
    if requeue_payload and requeue_payload.get("exhausted"):
        exhausted_reason = preparation_exhaustion_payload_skip_reason(requeue_payload)
        if exhausted_reason:
            return exhausted_reason

    direct_values = {
        value.strip().lower()
        for value in _direct_preparation_values(row)
        if value.strip()
    }
    direct_text = " ".join(direct_values)

    if (
        "preparation_blocked=marketplace_auth" in direct_values
        or "blocked_by_marketplace_auth" in direct_values
        or "marketplace_auth" in direct_values
        or "blocked_by_marketplace_auth" in direct_text
    ):
        return "preparation_blocked=marketplace_auth"

    if (
        "preparation_no_valid_documents" in direct_values
        or "no_valid_documents" in direct_values
        or "preparation_no_valid_documents" in direct_text
        or "no_valid_documents" in direct_text
    ):
        return "preparation_no_valid_documents"

    if (
        "preparation_exhausted=documents_missing" in direct_values
        or "documents_missing_after_prepare" in direct_values
        or "preparation_exhausted=documents_missing" in direct_text
        or "documents_missing_after_prepare" in direct_text
    ):
        return "preparation_exhausted=documents_missing"

    return None


def debug_skip_bucket(prefix: str, reason: str) -> str:
    normalized = str(reason or "unknown").strip() or "unknown"
    normalized = normalized.split()[0]
    normalized = normalized.replace(":", "=")
    return f"{prefix}:{normalized}"


def debug_emit(enabled: DebugSkipControl, bucket: str, line: str) -> None:
    if not enabled:
        return
    if isinstance(enabled, DebugSkipLimiter):
        enabled.emit(bucket, line)
        return
    print(line, flush=True)


def print_debug_skip(row: dict[str, Any], reason: str, enabled: DebugSkipControl) -> None:
    if not enabled:
        return
    external_id = row.get("external_id") or "unknown"
    title = row.get("title") or ""
    debug_emit(
        enabled,
        debug_skip_bucket("llm_skip", reason),
        f"Skip LLM candidate {external_id}: {reason} | {title}",
    )


def record_selection_skip(
    diagnostics: ShortlistSelectionDiagnostics | None,
    row: dict[str, Any],
    reason: str,
    debug_skips: DebugSkipControl,
) -> None:
    if diagnostics:
        diagnostics.skip(reason)
    print_debug_skip(row, reason, debug_skips)


def sales_feedback_selection_skip_reason(row: dict[str, Any]) -> str | None:
    if hidden_by_existing_client_customer(row):
        return f"sales_feedback={SALES_EXISTING_CLIENT_STATUS}"

    status = str(
        row.get("latest_sales_status")
        or row.get("sales_status")
        or row.get("sales_workflow_status")
        or ""
    ).strip()
    if status in NEGATIVE_SALES_FEEDBACK_SKIP_STATUSES:
        return f"sales_feedback={status}"

    return None


def deadline_selection_skip_reason(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str | None:
    current_time = now or datetime.now(timezone.utc)
    if not deadline_is_active(row, now=current_time):
        return DEADLINE_EXPIRED_SKIP_REASON

    if not deadline_is_fresh(
        row,
        DEFAULT_DEADLINE_MIN_DAYS,
        now=current_time,
    ):
        return DEADLINE_LT_3D_SKIP_REASON

    return None


def candidate_priority(row: dict[str, Any], profile: dict[str, Any]) -> tuple[int, int, int, str]:
    _, category_cfg = match_target_category(row, profile)
    priority = category_priority(category_cfg)
    score = int(row.get("score") or 0)

    deadline = row.get("deadline_at")
    deadline_text = deadline.isoformat() if hasattr(deadline, "isoformat") else str(deadline or "")

    return (priority, -get_price(row), -score, deadline_text)


def lead_candidate_priority(row: dict[str, Any], profile: dict[str, Any]) -> tuple[int, int, int, int, str]:
    signal = str(row.get("_lead_signal") or LEAD_SIGNAL_WEAK)
    waiting_rank = 0 if row.get("_lead_waiting_for_full_report") else 1
    signal_rank = 0 if signal == LEAD_SIGNAL_STRONG else 1
    score = int(row.get("score") or 0)
    deadline = row.get("deadline_at")
    deadline_text = deadline.isoformat() if hasattr(deadline, "isoformat") else str(deadline or "")
    return (waiting_rank, signal_rank, -get_price(row), -score, deadline_text)


def normalize_lead_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _collect_lead_text(value: Any, parts: list[str], *, limit: int = 30000) -> None:
    if sum(len(part) for part in parts) > limit:
        return
    if value is None:
        return
    if isinstance(value, dict):
        for nested in value.values():
            _collect_lead_text(nested, parts, limit=limit)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            _collect_lead_text(nested, parts, limit=limit)
        return
    text = str(value).strip()
    if text:
        parts.append(text)


def lead_signal_corpus(
    row: dict[str, Any],
    assessment: dict[str, Any] | None = None,
) -> str:
    parts: list[str] = []
    for key in (
        "title",
        "customer_name",
        "region",
        "procedure_type",
        "law",
        "result",
        "rule_result",
        "document_risk_result",
        "raw",
    ):
        _collect_lead_text(row.get(key), parts)
    if assessment:
        _collect_lead_text(assessment, parts)
    return normalize_lead_text(" ".join(parts)[:30000])


def lead_keyword_in_text(text: str, keyword: str) -> bool:
    pattern = normalize_lead_text(keyword)
    if not pattern:
        return False
    if re.fullmatch(r"[a-z0-9а-я]+", pattern) and len(pattern) <= 3:
        return re.search(
            rf"(?<![a-z0-9а-я]){re.escape(pattern)}(?![a-z0-9а-я])",
            text,
        ) is not None
    return pattern in text


def lead_text_has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(lead_keyword_in_text(text, keyword) for keyword in keywords)


def lead_text_has_word(text: str, word: str) -> bool:
    pattern = normalize_lead_text(word)
    if not pattern:
        return False
    return re.search(
        rf"(?<![a-z0-9а-я]){re.escape(pattern)}(?![a-z0-9а-я])",
        text,
    ) is not None


def lead_has_road_security_noise(text: str) -> bool:
    return lead_text_has_any(text, LEAD_ROAD_SECURITY_PHRASE_KEYWORDS) or any(
        lead_text_has_word(text, keyword)
        for keyword in LEAD_ROAD_SECURITY_WORD_KEYWORDS
    )


def lead_has_storage_data_center_signal(text: str) -> bool:
    if lead_text_has_any(text, LEAD_STORAGE_DATA_CENTER_KEYWORDS):
        return True
    return (
        lead_text_has_any(text, LEAD_MEDICAL_DATA_KEYWORDS)
        and lead_text_has_any(text, LEAD_DATA_STORAGE_CONTEXT_KEYWORDS)
    )


def lead_has_network_core_signal(text: str) -> bool:
    if lead_text_has_any(text, ("сетевое оборудование", "сетевого оборудования")):
        return True
    return (
        lead_text_has_any(text, ("коммутатор", "коммутаторы", "маршрутизатор", "маршрутизаторы"))
        and lead_text_has_any(text, LEAD_NETWORK_PURCHASE_CONTEXT_KEYWORDS)
        and "mesh" not in text
    )


def lead_has_named_network_device_signal(text: str) -> bool:
    return (
        lead_text_has_any(
            text,
            ("коммутатор", "коммутаторы", "маршрутизатор", "маршрутизаторы"),
        )
        and "mesh" not in text
    )


def lead_has_virtualization_or_backup_core_signal(text: str) -> bool:
    context = (
        lead_has_storage_data_center_signal(text)
        or lead_text_has_any(text, LEAD_SERVER_INFRASTRUCTURE_KEYWORDS)
        or lead_text_has_any(text, ("сервер", "серверн"))
        or "ит-инфраструктур" in text
        or "инфраструктурн" in text
    )
    if lead_text_has_any(text, ("пак виртуализации",)):
        return True
    if lead_text_has_any(text, LEAD_VIRTUALIZATION_KEYWORDS) and context:
        return True
    if lead_text_has_any(text, LEAD_CONTAINERIZATION_KEYWORDS) and context:
        return True
    return lead_text_has_any(text, LEAD_BACKUP_KEYWORDS) and context


def lead_has_platform_migration_signal(text: str) -> bool:
    has_platform = lead_text_has_any(text, LEAD_PLATFORM_MIGRATION_KEYWORDS)
    has_migration_context = lead_text_has_any(text, LEAD_MIGRATION_CONTEXT_KEYWORDS)
    has_exchange_migration = lead_text_has_any(
        text,
        LEAD_EXCHANGE_KEYWORDS,
    ) and has_migration_context
    return (has_platform and has_migration_context) or has_exchange_migration


def lead_has_enterprise_infra_modernization_signal(text: str) -> bool:
    return lead_text_has_any(text, LEAD_ACTIVITY_KEYWORDS + LEAD_MIGRATION_CONTEXT_KEYWORDS) and lead_text_has_any(
        text,
        (
            "ит-инфраструктур",
            "информационной инфраструктур",
            "информационно-технологическ",
            "серверная инфраструктура",
            "серверной инфраструктуры",
            "сетевая инфраструктура",
            "сетевой инфраструктуры",
        ),
    )


def lead_has_core_infrastructure_signal(text: str) -> bool:
    return (
        lead_has_storage_data_center_signal(text)
        or lead_text_has_any(text, LEAD_SERVER_INFRASTRUCTURE_KEYWORDS)
        or lead_has_network_core_signal(text)
        or lead_has_virtualization_or_backup_core_signal(text)
        or lead_has_platform_migration_signal(text)
        or lead_has_enterprise_infra_modernization_signal(text)
    )


def lead_has_explicit_server_equipment_or_datacenter_signal(text: str) -> bool:
    return lead_text_has_any(
        text,
        LEAD_SERVER_INFRASTRUCTURE_KEYWORDS
        + (
            "серверное оборудование",
            "серверного оборудования",
            "серверным оборудованием",
            "поставка серверов",
            "поставка серверного",
            "вычислительный комплекс",
            "вычислительного комплекса",
            "дата-центр",
            "дата центр",
            "центр обработки данных",
            "цод",
            "оцод",
            "рцод",
            "цомд",
        ),
    )


def lead_has_server_or_datacenter_target_signal(text: str) -> bool:
    if lead_has_explicit_server_equipment_or_datacenter_signal(text):
        return True

    return (
        lead_text_has_any(text, ("сервер", "серверы", "серверов", "серверн"))
        and lead_text_has_any(
            text,
            LEAD_NETWORK_PURCHASE_CONTEXT_KEYWORDS
            + LEAD_ACTIVITY_KEYWORDS
            + ("оборудование",),
        )
        and not lead_text_has_any(text, LEAD_NON_TARGET_SERVER_BOUND_CONTEXT_KEYWORDS)
        and not lead_text_has_any(text, LEAD_VIDEO_SURVEILLANCE_BOUND_CONTEXT_KEYWORDS)
        and not lead_text_has_any(text, LEAD_ELECTRONIC_QUEUE_BOUND_CONTEXT_KEYWORDS)
        and not lead_text_has_any(text, LEAD_PERIPHERAL_BOUND_CONTEXT_KEYWORDS)
        and not lead_has_road_security_noise(text)
    )


def lead_has_security_target_signal(text: str) -> bool:
    return (
        lead_has_security_hardware_signal(text)
        or lead_text_has_any(text, LEAD_SECURITY_TARGET_VENDOR_KEYWORDS)
        or any(lead_text_has_word(text, keyword) for keyword in LEAD_SECURITY_TARGET_WORD_KEYWORDS)
    )


def lead_has_pak_target_signal(text: str) -> bool:
    return lead_text_has_any(text, LEAD_PAK_TARGET_KEYWORDS) or lead_text_has_word(text, "пак")


def lead_has_explicit_storage_platform_target_signal(text: str) -> bool:
    return lead_text_has_any(text, LEAD_EXPLICIT_STORAGE_PLATFORM_KEYWORDS)


def lead_has_security_infrastructure_project_signal(text: str) -> bool:
    return lead_has_pak_target_signal(text) or lead_text_has_any(
        text,
        LEAD_SECURITY_INFRASTRUCTURE_PROJECT_KEYWORDS,
    )


def lead_target_signal_matches_from_corpus(text: str) -> tuple[str, ...]:
    matches: list[str] = []
    if lead_has_storage_data_center_signal(text) or lead_text_has_any(
        text,
        ("дисковый массив", "дисковые массивы", "storage"),
    ):
        matches.append("storage")
    if lead_has_server_or_datacenter_target_signal(text):
        matches.append("server_or_datacenter")
    if lead_has_pak_target_signal(text):
        matches.append("pak")
    if lead_has_security_target_signal(text):
        matches.append("security")
    if lead_has_platform_migration_signal(text):
        matches.append("platform_migration")
    if lead_has_virtualization_or_backup_core_signal(text):
        matches.append("virtualization_or_backup")
    if lead_has_network_core_signal(text):
        matches.append("network")
    if lead_has_enterprise_infra_modernization_signal(text):
        matches.append("enterprise_infra_modernization")
    return tuple(dict.fromkeys(matches))


def tighten_target_signal_matches_for_hard_noise(
    text: str,
    reason: str,
    matches: list[str],
) -> tuple[str, ...]:
    if (
        "storage" in matches
        and reason in LEAD_CONTEXT_BOUND_STORAGE_HARD_NOISE_REASONS
        and not lead_has_explicit_storage_platform_target_signal(text)
    ):
        matches.remove("storage")

    if (
        "server_or_datacenter" in matches
        and reason in LEAD_CONTEXT_BOUND_SERVER_HARD_NOISE_REASONS
        and not lead_has_explicit_server_equipment_or_datacenter_signal(text)
    ):
        matches.remove("server_or_datacenter")

    if (
        "security" in matches
        and reason in LEAD_CONTEXT_BOUND_SECURITY_HARD_NOISE_REASONS
        and not lead_has_security_infrastructure_project_signal(text)
    ):
        matches.remove("security")

    if (
        "network" in matches
        and reason in LEAD_NETWORK_STANDALONE_BLOCK_HARD_NOISE_REASONS
        and not any(match != "network" for match in matches)
    ):
        matches.remove("network")

    return tuple(dict.fromkeys(matches))


def lead_target_signal_matches_for_hard_noise(
    text: str,
    reason: str,
) -> tuple[str, ...]:
    if reason == "property_sale":
        return ()

    matches = list(lead_target_signal_matches_from_corpus(text))
    matches = [match for match in matches if match != "pak"]

    if reason == "video_surveillance":
        matches = [
            match
            for match in matches
            if match
            in {
                "storage",
                "server_or_datacenter",
                "security",
                "platform_migration",
                "virtualization_or_backup",
                "network",
                "enterprise_infra_modernization",
            }
        ]
        if "network" in matches and not lead_has_named_network_device_signal(text):
            matches.remove("network")

    elif reason == "electronic_queue":
        matches = [
            match
            for match in matches
            if match
            in {
                "storage",
                "server_or_datacenter",
                "security",
                "platform_migration",
                "virtualization_or_backup",
                "enterprise_infra_modernization",
            }
        ]
        if (
            "server_or_datacenter" in matches
            and not lead_has_explicit_server_equipment_or_datacenter_signal(text)
        ):
            matches.remove("server_or_datacenter")

    elif reason == "peripheral_office_equipment":
        matches = [
            match
            for match in matches
            if match
            in {
                "storage",
                "server_or_datacenter",
                "security",
                "platform_migration",
                "virtualization_or_backup",
                "network",
                "enterprise_infra_modernization",
            }
        ]
        if "network" in matches and not lead_has_named_network_device_signal(text):
            matches.remove("network")
        if (
            "server_or_datacenter" in matches
            and lead_text_has_any(text, LEAD_PERIPHERAL_BOUND_CONTEXT_KEYWORDS)
            and not lead_has_explicit_server_equipment_or_datacenter_signal(text)
        ):
            matches.remove("server_or_datacenter")
        if (
            "platform_migration" in matches
            and lead_text_has_any(text, LEAD_OFFICE_SOFTWARE_CONTEXT_KEYWORDS)
            and not lead_text_has_any(text, LEAD_EXCHANGE_KEYWORDS)
        ):
            matches.remove("platform_migration")

    elif reason == "road_security":
        matches = [
            match
            for match in matches
            if match
            in {
                "storage",
                "server_or_datacenter",
                "security",
                "platform_migration",
                "virtualization_or_backup",
                "enterprise_infra_modernization",
            }
        ]
        if (
            "server_or_datacenter" in matches
            and not lead_has_explicit_server_equipment_or_datacenter_signal(text)
        ):
            matches.remove("server_or_datacenter")

    elif reason == "construction_or_furniture":
        matches = [
            match
            for match in matches
            if match
            in {
                "storage",
                "server_or_datacenter",
                "security",
                "platform_migration",
                "virtualization_or_backup",
                "network",
                "enterprise_infra_modernization",
            }
        ]
        if "network" in matches and not lead_has_named_network_device_signal(text):
            matches.remove("network")

    elif reason == "generic_antivirus_license":
        matches = [
            match
            for match in matches
            if match
            in {
                "storage",
                "server_or_datacenter",
                "security",
                "platform_migration",
                "virtualization_or_backup",
                "network",
                "enterprise_infra_modernization",
            }
        ]
        if (
            "platform_migration" in matches
            and lead_text_has_any(
                text,
                LEAD_NOISE_KEYWORDS_BY_REASON["generic_antivirus_license"]
                + LEAD_OFFICE_SOFTWARE_CONTEXT_KEYWORDS,
            )
            and not (
                lead_text_has_any(text, LEAD_EXCHANGE_KEYWORDS)
                and lead_text_has_any(text, LEAD_MIGRATION_CONTEXT_KEYWORDS)
            )
        ):
            matches.remove("platform_migration")
        if "network" in matches and not lead_has_named_network_device_signal(text):
            matches.remove("network")

    elif reason in {"generic_computer_repair", "interactive_panels"}:
        matches = [
            match
            for match in matches
            if match in {"storage", "server_or_datacenter", "security"}
        ]
        if (
            "server_or_datacenter" in matches
            and not lead_has_explicit_server_equipment_or_datacenter_signal(text)
        ):
            matches.remove("server_or_datacenter")

    return tighten_target_signal_matches_for_hard_noise(text, reason, matches)


def lead_has_noise_override_core_signal(text: str, reason: str) -> bool:
    if reason == "property_sale":
        return False

    if reason == "video_surveillance":
        matches = lead_target_signal_matches_for_hard_noise(text, reason)
        if not matches:
            return False
        if lead_text_has_any(text, LEAD_VIDEO_SURVEILLANCE_BOUND_CONTEXT_KEYWORDS):
            return any(
                match
                in {
                    "storage",
                    "server_or_datacenter",
                    "security",
                    "network",
                    "platform_migration",
                    "enterprise_infra_modernization",
                }
                for match in matches
            )
        return True

    return bool(lead_target_signal_matches_for_hard_noise(text, reason))


def lead_has_standalone_server_signal(text: str) -> bool:
    return lead_text_has_any(text, LEAD_STANDALONE_SERVER_KEYWORDS)


def lead_has_security_hardware_signal(text: str) -> bool:
    return lead_text_has_any(text, LEAD_SECURITY_HARDWARE_KEYWORDS)


def lead_has_component_infra_signal(text: str) -> bool:
    if not lead_text_has_any(text, LEAD_COMPONENT_KEYWORDS):
        return False
    return (
        lead_has_storage_data_center_signal(text)
        or lead_text_has_any(text, ("сервер", "серверн", "ит-инфраструктур"))
        or lead_text_has_any(text, ("дисковая полка", "дисковые полки", "жестк", "жёстк", "накопител"))
    )


def lead_explicit_noise_reason_from_corpus(text: str) -> str | None:
    if lead_has_road_security_noise(text):
        return "road_security"

    for reason, keywords in LEAD_NOISE_KEYWORDS_BY_REASON.items():
        if reason not in LEAD_HARD_NOISE_REASONS:
            continue
        if not lead_text_has_any(text, keywords):
            continue
        return reason
    return None


def lead_hard_noise_reason_from_corpus(text: str) -> str | None:
    reason = lead_explicit_noise_reason_from_corpus(text)
    if not reason:
        return None
    if lead_has_noise_override_core_signal(text, reason):
        return None
    return reason


def lead_noise_reason_from_corpus(text: str) -> str | None:
    return lead_hard_noise_reason_from_corpus(text)


def lead_hard_noise_reason_for_row(
    row: dict[str, Any],
    assessment: dict[str, Any] | None = None,
) -> str | None:
    return lead_hard_noise_reason_from_corpus(lead_signal_corpus(row, assessment))


def lead_explicit_hard_noise_reason_for_row(
    row: dict[str, Any],
    assessment: dict[str, Any] | None = None,
) -> str | None:
    return lead_explicit_noise_reason_from_corpus(lead_signal_corpus(row, assessment))


def lead_target_signal_matches_for_row(
    row: dict[str, Any],
    assessment: dict[str, Any] | None = None,
    *,
    hard_noise_reason: str | None = None,
) -> tuple[str, ...]:
    text = lead_signal_corpus(row, assessment)
    if hard_noise_reason:
        return lead_target_signal_matches_for_hard_noise(text, hard_noise_reason)
    return lead_target_signal_matches_from_corpus(text)


def classify_lead_signal(
    row: dict[str, Any],
    assessment: dict[str, Any] | None = None,
) -> str:
    text = lead_signal_corpus(row, assessment)
    if lead_hard_noise_reason_from_corpus(text):
        return LEAD_SIGNAL_NOISE

    if (
        lead_has_core_infrastructure_signal(text)
        or lead_has_standalone_server_signal(text)
        or lead_has_security_hardware_signal(text)
    ):
        return LEAD_SIGNAL_STRONG

    if (
        lead_text_has_any(text, LEAD_ACTIVITY_KEYWORDS)
        and (
            lead_has_storage_data_center_signal(text)
            or lead_has_standalone_server_signal(text)
            or lead_has_network_core_signal(text)
            or lead_has_security_hardware_signal(text)
        )
    ):
        return LEAD_SIGNAL_STRONG

    if lead_has_component_infra_signal(text) and get_price(row) >= LEAD_DEFAULT_MIN_PRICE_RUB:
        return LEAD_SIGNAL_STRONG

    market_access = str((assessment or {}).get("market_access") or "").strip()
    if market_access in {
        "target_hardware",
        "infra_project",
        "domestic_restricted",
        "low_priority_deal",
    }:
        return LEAD_SIGNAL_WEAK

    return LEAD_SIGNAL_WEAK


def lead_category_from_signal(
    row: dict[str, Any],
    assessment: dict[str, Any] | None = None,
) -> tuple[str, str]:
    text = lead_signal_corpus(row, assessment)
    category_keywords = {
        "storage": (
            "схд",
            "система хранения",
            "системы хранения",
            "хранилищ",
            "репозиторий данных",
            "цомд",
            "дисковая полка",
        ),
        "servers": (
            "сервер",
            "серверн",
            "вычислительн",
        ),
        "network": (
            "сетевое оборудование",
            "коммутатор",
            "маршрутизатор",
        ),
        "security_hardware": (
            "межсетевой экран",
            "firewall",
            "ngfw",
            "средства защиты информации",
            "сзи",
            "скзи",
            "иб-желез",
        ),
    }
    labels = {
        "storage": "Storage",
        "servers": "Servers",
        "network": "Network",
        "security_hardware": "Security",
    }
    for category, keywords in category_keywords.items():
        if lead_text_has_any(text, keywords):
            return category, labels[category]
    return "lead_signal", "Lead signal"


def waiting_go_candidate_debug_row(
    row: dict[str, Any],
    triage: dict[str, Any],
) -> dict[str, Any]:
    category = (
        row.get("_llm_category_label")
        or row.get("_llm_category")
        or lead_category_from_signal(row)[1]
    )
    return {
        "external_id": row.get("external_id") or "unknown",
        "price": get_price(row),
        "category": category,
        "title": row.get("title") or "",
        "triage_priority": triage.get("lead_priority") or "",
    }


def lead_has_enterprise_customer_signal(row: dict[str, Any]) -> bool:
    text = normalize_lead_text(
        " ".join(
            str(row.get(key) or "")
            for key in ("customer_name", "title", "region")
        )
    )
    return lead_text_has_any(text, LEAD_ENTERPRISE_CUSTOMER_KEYWORDS)


def lead_negative_feedback_skip_reason(row: dict[str, Any]) -> str | None:
    reason = sales_feedback_selection_skip_reason(row)
    if not reason:
        return None
    _, _, status = reason.partition("=")
    return f"lead_negative_feedback={status or 'unknown'}"


def lead_deadline_allowed_note(row: dict[str, Any], *, now: datetime | None = None) -> str | None:
    reason = deadline_selection_skip_reason(row, now=now)
    if reason == DEADLINE_EXPIRED_SKIP_REASON:
        return "lead_deadline_expired_allowed"
    if reason == DEADLINE_LT_3D_SKIP_REASON:
        return "lead_deadline_lt_3d_allowed"
    return None


def print_debug_lead_note(row: dict[str, Any], reason: str, enabled: DebugSkipControl) -> None:
    if not enabled:
        return
    external_id = row.get("external_id") or "unknown"
    title = row.get("title") or ""
    debug_emit(
        enabled,
        debug_skip_bucket("lead_note", reason),
        f"Lead LLM candidate {external_id}: {reason} | {title}",
    )


def print_debug_pre_triage_note(row: dict[str, Any], reason: str, enabled: DebugSkipControl) -> None:
    if not enabled:
        return
    external_id = row.get("external_id") or "unknown"
    title = row.get("title") or ""
    debug_emit(
        enabled,
        debug_skip_bucket("pre_triage_note", reason),
        f"Pre-triage lead candidate {external_id}: {reason} | {title}",
    )


def print_debug_pre_triage_skip(row: dict[str, Any], reason: str, enabled: DebugSkipControl) -> None:
    if not enabled:
        return
    external_id = row.get("external_id") or "unknown"
    title = row.get("title") or ""
    debug_emit(
        enabled,
        debug_skip_bucket("pre_triage_skip", reason),
        f"Skip before lead triage {external_id}: {reason} | {title}",
    )


def print_debug_hard_noise_gate(
    row: dict[str, Any],
    *,
    hard_noise_reason: str,
    target_signals: tuple[str, ...],
    final_decision: str,
    enabled: DebugSkipControl,
) -> None:
    if not enabled:
        return
    external_id = row.get("external_id") or "unknown"
    title = row.get("title") or ""
    target_signal = ",".join(target_signals) if target_signals else "none"
    debug_emit(
        enabled,
        f"hard_noise_gate:{final_decision}:{hard_noise_reason}",
        "Hard-noise gate "
        f"{external_id}: "
        f"hard_noise_reason={hard_noise_reason} "
        f"target_signal_matched={target_signal} "
        f"final_decision={final_decision} | {title}",
    )


def record_hard_noise_gate_decision(
    diagnostics: ShortlistSelectionDiagnostics | None,
    row: dict[str, Any],
    *,
    hard_noise_reason: str,
    target_signals: tuple[str, ...],
    final_decision: str,
    debug_skips: DebugSkipControl,
) -> None:
    if diagnostics is not None:
        diagnostics.record_hard_noise_decision(final_decision)
    print_debug_hard_noise_gate(
        row,
        hard_noise_reason=hard_noise_reason,
        target_signals=target_signals,
        final_decision=final_decision,
        enabled=debug_skips,
    )


def record_lead_pre_triage_skip(
    diagnostics: ShortlistSelectionDiagnostics | None,
    row: dict[str, Any],
    reason: str,
    debug_skips: DebugSkipControl,
) -> None:
    record_selection_skip(diagnostics, row, reason, debug_skips)
    print_debug_pre_triage_skip(row, reason, debug_skips)


LEAD_TRIAGE_SYSTEM_PROMPT = """
You are a B2B infrastructure lead triage analyst.
Your job is to decide whether a tender creates a commercial reason to call the
customer's IT owner for future account development. Do not decide whether to
participate in the tender procedure.
Return one JSON object only.
""".strip()


def build_lead_triage_user_prompt(context_markdown: str) -> str:
    fields = ", ".join(LEAD_TRIAGE_FIELDS)
    return f"""
Question:
Is there a commercial reason to call this customer for future account
development, based on the tender, customer, amount, category, title, and
documents/summary if available?

Return exactly these top-level keys and no others: {fields}.

JSON contract:
- lead_decision: "go", "maybe", or "reject".
- lead_priority: "high", "medium", or "low".
- confidence: "high", "medium", or "low".
- lead_summary, likely_customer_story, reject_reason: short strings.
- possible_needs and target_roles: arrays of short strings.
- requires_full_lead_report: boolean.

This triage is a strict paid-report gate. Do not be afraid to reject: the goal
is to avoid spending a full paid customer lead report on weak account-development
hypotheses.
Triage decides whether it is worth attempting a full lead report. It is not the
final commercial verdict. A later full report may lower confidence or priority
after targeted document preparation and technical-document analysis.
Do not pretend that you have seen the TZ or technical documentation unless the
provided context explicitly contains it. If the package is card-only or document
status is unclear, treat go as "worth preparing documents and checking", not as
a confirmed opportunity.

Decision principle:
- Decide whether the tender creates a commercial account-development reason,
  not whether the text merely contains words like server, storage, СХД, data
  center, or license.
- A good lead has a plausible customer owner, business context, and next
  conversation beyond the procurement itself.
- Deadline is warning-only in lead mode. An expired or close tender deadline
  must not by itself cause reject; it only means the current procedure may be
  useful mainly as an account-development reason to call.
- Price is a priority signal, not a hard ban. Prefer normal NMCK and larger
  enterprise infrastructure projects, but a smaller purchase can still be go
  when it exposes a strong installed base, strategic customer, or future
  infrastructure roadmap.

Use go when at least one explicit account-development story is visible:
- storage/СХД, servers, network infrastructure, security hardware/ПАК/СЗИ, or
  data-center infrastructure with normal NMCK and a concrete customer signal;
- a tender with TZ/documents or a clear infrastructure signal that is useful
  for document preparation and a full customer lead report;
- modernization of a data center, ЦОД/ОЦОД/РЦОД/ЦОМД, or regional/federal IT estate;
- expansion of storage, server room, server fleet, or network infrastructure
  with signs of growth, migration, resilience, or capacity pressure;
- virtualization, containerization, backup, DR, or import substitution of
  infrastructure/platforms;
- critical government, medical, energy, regional, or regulated IT systems;
- modernization, expansion, implementation, rollout, support, SLA, or PNR of
  enterprise infrastructure;
- a large or strategic customer where the purchase is a credible signal of
  infrastructure development.
- a case where current tender participation may be imperfect, but the tender
  clearly shows installed base or future infrastructure tasks.

Use maybe when the signal is useful but thin:
- one server or a small supply without enough history or scale;
- support, renewal, license, or certificate context where infrastructure
  relevance is possible but not proven;
- a plausible IT owner exists, but the next commercial reason is still a
  hypothesis.
- Be cautious with maybe: when there is a strong account-development reason,
  prefer go with medium priority over maybe_deferred.

Use reject when any of these is true:
- it is a one-off small supply without account history or expansion signal;
- it looks like a one-off supply without a long-term infrastructure story;
- you cannot infer who the IT owner might be or what future task could be
  discussed;
- it is software, development, or services without an infrastructure entry point;
- it is a service/support renewal without credible account expansion potential;
- it is a narrow certificate, license, consumable, accessory, or small component
  supply without signs of infrastructure renewal;
- it is an obvious license renewal/prolongation without infrastructure
  development;
- it is small spare parts/consumables/peripherals without expansion or installed
  base signal;
- it is repair, maintenance, peripherals, office equipment, video surveillance,
  traffic enforcement, property sale, construction, or other non-profile subject;
- it is video surveillance, access control/СКУД, road security, traffic
  enforcement, office peripherals, or similar non-core infrastructure;
- it has no identifiable customer and no clear account-development reason;
- latest negative feedback in the package says the tender was rejected for this
  exact procedure;
- it is outside the system integrator profile for servers, storage, network,
  cybersecurity infrastructure, data centers, virtualization, or backup.

Use go only when there is a clear customer story and a plausible conversation
for the next 6-18 months: data center modernization, storage, server fleet,
network, virtualization, backup/DR, infrastructure import substitution,
security infrastructure, SLA/service contract, or regional/federal IT estate.

Use maybe only when there is probable but insufficiently confirmed
infrastructure potential. Maybe is deferred by default and should not assume a
full report will be generated.
Use maybe_deferred only when customer/context/documents/scale are genuinely too
thin for a full lead report now.

Set requires_full_lead_report=true only for go, or for a maybe that would be
worth manual escalation with --include-maybe-leads. For reject, set it to false
and explain reject_reason.
Use Russian for free-text values.

LLM tender package:

{context_markdown}
""".strip()


def _normalize_choice(value: Any, allowed: tuple[str, ...], fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def normalize_lead_triage_decision(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"reject", "rejected", "no_go", "nogo", "no"}:
        return "reject"
    if normalized in {"go", "maybe"}:
        return normalized
    return "reject"


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _string_list(value: Any, *, limit: int = 8) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        text = _string_value(item)
        if text:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _bool_value(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "y", "1"}:
        return True
    if text in {"false", "no", "n", "0"}:
        return False
    return default


def clean_lead_triage_reject_reason(value: Any, *, decision: str) -> str:
    text = _string_value(value)
    if decision == "go":
        return ""

    normalized = normalize_lead_text(text).strip(" .,:;!-")
    if decision == "maybe" and normalized in {
        "",
        "не применимо",
        "неприменимо",
        "n/a",
        "na",
        "not applicable",
        "нет",
        "нет причины",
    }:
        return ""

    return text


def normalize_lead_triage(raw: Any) -> dict[str, Any]:
    source = raw.get("report") if isinstance(raw, dict) and isinstance(raw.get("report"), dict) else raw
    source = source if isinstance(source, dict) else {}

    decision = normalize_lead_triage_decision(
        source.get("lead_decision") or source.get("decision")
    )
    priority_fallback = "low" if decision == "reject" else "medium"
    priority = _normalize_choice(
        source.get("lead_priority"),
        LEAD_TRIAGE_PRIORITIES,
        priority_fallback,
    )
    confidence = _normalize_choice(
        source.get("confidence"),
        LEAD_TRIAGE_CONFIDENCES,
        "low",
    )
    requires_report = _bool_value(
        source.get("requires_full_lead_report"),
        default=decision in {"go", "maybe"},
    )
    if decision == "reject":
        requires_report = False

    normalized = {
        "lead_decision": decision,
        "lead_priority": priority,
        "confidence": confidence,
        "lead_summary": _string_value(
            source.get("lead_summary")
            or source.get("customer_story")
            or source.get("why_call")
            or source.get("summary")
        ),
        "likely_customer_story": _string_value(
            source.get("likely_customer_story")
            or source.get("customer_story")
        ),
        "possible_needs": _string_list(
            source.get("possible_needs")
            if source.get("possible_needs") is not None
            else source.get("likely_needs")
        ),
        "target_roles": _string_list(source.get("target_roles")),
        "reject_reason": clean_lead_triage_reject_reason(
            source.get("reject_reason"),
            decision=decision,
        ),
        "requires_full_lead_report": requires_report,
    }
    return {field: normalized[field] for field in LEAD_TRIAGE_FIELDS}


def unwrap_lead_triage_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    source = value.get("report") if isinstance(value.get("report"), dict) else value
    if not isinstance(source, dict) or "lead_decision" not in source:
        return None
    return normalize_lead_triage(source)


def first_report_datetime(*values: Any) -> datetime | None:
    for value in values:
        parsed = parse_report_datetime(value)
        if parsed is not None:
            return parsed
    return None


def lead_triage_created_at_from_result(value: Any) -> datetime | None:
    if not isinstance(value, dict):
        return None
    meta = value.get("meta") if isinstance(value.get("meta"), dict) else {}
    report = value.get("report") if isinstance(value.get("report"), dict) else {}
    return first_report_datetime(
        value.get("created_at"),
        value.get("_analysis_created_at"),
        meta.get("created_at"),
        report.get("created_at"),
    )


def lead_triage_created_at_from_row(
    row: dict[str, Any],
    raw_result: Any,
    *row_keys: str,
) -> datetime | None:
    row_values = [row.get(key) for key in row_keys]
    return first_report_datetime(
        *row_values,
        lead_triage_created_at_from_result(raw_result),
    )


def analysis_result_for_candidate(
    row: dict[str, Any],
    analysis_type: str,
) -> Any:
    tender_id = row.get("tender_id") or row.get("id")
    external_id = row.get("external_id")

    if tender_id:
        where = "tender_id = %s"
        params = (str(tender_id), analysis_type)
    elif external_id:
        where = "tender_id = (SELECT id FROM tenders WHERE external_id = %s LIMIT 1)"
        params = (str(external_id), analysis_type)
    else:
        return None

    try:
        import psycopg

        from app.config import settings

        with psycopg.connect(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT result, created_at
                    FROM analysis_results
                    WHERE {where}
                      AND analysis_type = %s
                    ORDER BY created_at DESC
                    LIMIT 1;
                    """,
                    params,
                )
                db_row = cur.fetchone()
    except Exception:
        return None

    if not db_row:
        return None

    result = db_row[0]
    created_at = db_row[1] if len(db_row) > 1 else None
    if isinstance(result, dict) and created_at is not None:
        result = dict(result)
        result.setdefault("created_at", created_at)
        result["_analysis_created_at"] = created_at
    return result


def lead_triage_result_lookups_for_candidate(
    row: dict[str, Any],
    *,
    include_db_lookup: bool = True,
) -> list[LeadTriageLookup]:
    lookups: list[LeadTriageLookup] = []
    for key in ("lead_triage_result", "llm_customer_lead_triage_result"):
        raw_result = row.get(key)
        triage = unwrap_lead_triage_result(raw_result)
        if triage:
            lookups.append(
                LeadTriageLookup(
                    triage=triage,
                    source=LEAD_TRIAGE_SOURCE_EXISTING_REPORT,
                    created_at=lead_triage_created_at_from_row(
                        row,
                        raw_result,
                        "lead_triage_created_at",
                        "llm_customer_lead_triage_created_at",
                    ),
                )
            )

    if row.get("llm_report_analysis_type") == LEAD_TRIAGE_ANALYSIS_TYPE:
        raw_result = row.get("llm_report_result")
        triage = unwrap_lead_triage_result(raw_result)
        if triage:
            lookups.append(
                LeadTriageLookup(
                    triage=triage,
                    source=LEAD_TRIAGE_SOURCE_EXISTING_REPORT,
                    created_at=lead_triage_created_at_from_row(
                        row,
                        raw_result,
                        "lead_triage_created_at",
                        "llm_report_created_at",
                    ),
                )
            )

    results_by_type = row.get("llm_report_results_by_type")
    if isinstance(results_by_type, dict):
        raw_result = results_by_type.get(LEAD_TRIAGE_ANALYSIS_TYPE)
        triage = unwrap_lead_triage_result(raw_result)
        if triage:
            lookups.append(
                LeadTriageLookup(
                    triage=triage,
                    source=LEAD_TRIAGE_SOURCE_EXISTING_REPORT,
                    created_at=lead_triage_created_at_from_row(
                        row,
                        raw_result,
                        "lead_triage_created_at",
                    ),
                )
            )

    if include_db_lookup:
        raw_result = analysis_result_for_candidate(row, LEAD_TRIAGE_ANALYSIS_TYPE)
        triage = unwrap_lead_triage_result(raw_result)
        if triage:
            lookups.append(
                LeadTriageLookup(
                    triage=triage,
                    source=LEAD_TRIAGE_SOURCE_CACHE,
                    created_at=lead_triage_created_at_from_result(raw_result),
                )
            )

    return lookups


def lead_triage_result_lookup_for_candidate(row: dict[str, Any]) -> LeadTriageLookup | None:
    lookups = lead_triage_result_lookups_for_candidate(row)
    return lookups[0] if lookups else None


def lead_triage_result_for_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    lookup = lead_triage_result_lookup_for_candidate(row)
    return lookup.triage if lookup else None


def save_lead_triage_result(
    *,
    tender_id: str,
    provider: str,
    model: str,
    triage: dict[str, Any],
    raw_response: str,
    context_chars: int,
    metadata: dict[str, Any] | None = None,
) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    from app.config import settings

    recommendation = {
        "go": "go",
        "maybe": "maybe",
        "reject": "no_go",
    }.get(normalize_lead_triage_decision(triage.get("lead_decision")), "no_go")
    confidence = (
        triage.get("confidence")
        or triage.get("lead_priority")
        or "low"
    )
    result = {
        "provider": provider,
        "model": model,
        "created_at": datetime.now().isoformat(),
        "context_chars": context_chars,
        "meta": metadata or {},
        "report": triage,
        "raw_response": raw_response,
    }
    result.update({field: triage.get(field) for field in LEAD_TRIAGE_FIELDS})

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM analysis_results
                WHERE tender_id = %s
                  AND analysis_type = %s;
                """,
                (tender_id, LEAD_TRIAGE_ANALYSIS_TYPE),
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
                    LEAD_TRIAGE_ANALYSIS_TYPE,
                    model,
                    Jsonb(result),
                    None,
                    recommendation,
                    confidence,
                ),
            )
        conn.commit()


def run_lead_triage_for_candidate(
    row: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    json_mode: bool | None = None,
) -> dict[str, Any]:
    external_id = str(row.get("external_id") or "")
    if not external_id:
        raise RuntimeError("Cannot run lead triage without external_id")

    from app.config import settings
    from app.llm.contracts import (
        LEAD_TRIAGE_PROMPT_VERSION,
        LEAD_TRIAGE_SCHEMA_VERSION,
        LeadTriageOutput,
        inspect_lead_triage_output,
    )
    from app.llm.factory import create_llm_client
    from app.llm.tender_report import (
        build_package_from_database,
        extract_json,
        package_to_markdown,
    )
    from app.platform.versioning import build_ai_version_binding

    tender_id, package = build_package_from_database(
        external_id=external_id,
        max_spec_chars=LEAD_TRIAGE_MAX_SPEC_CHARS,
        max_other_chars=LEAD_TRIAGE_MAX_OTHER_CHARS,
        analysis_depth=ANALYSIS_DEPTH_STANDARD,
        report_kind=REPORT_KIND_LEAD,
        use_llm_document_selector=False,
    )
    context_markdown = package_to_markdown(package)
    client = create_llm_client(provider=provider, model=model)
    effective_json_mode = settings.llm_json_mode if json_mode is None else json_mode
    request_json_mode = bool(effective_json_mode and client.provider == "routerai")
    system_prompt = LEAD_TRIAGE_SYSTEM_PROMPT
    user_prompt = build_lead_triage_user_prompt(context_markdown)

    response = client.generate_chat_completion(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.1,
        max_tokens=LEAD_TRIAGE_MAX_OUTPUT_TOKENS,
        json_mode=request_json_mode,
    )
    raw_response = response.text or ""
    parsed_response = extract_json(raw_response)
    triage = normalize_lead_triage(parsed_response)
    strict_validation = inspect_lead_triage_output(parsed_response)
    version_binding = build_ai_version_binding(
        prompt_version=LEAD_TRIAGE_PROMPT_VERSION,
        prompt_template=(
            LEAD_TRIAGE_SYSTEM_PROMPT
            + "\n\n"
            + build_lead_triage_user_prompt("{{CONTEXT_MARKDOWN}}")
        ),
        model_provider=response.provider,
        model_id=response.model,
        model_config={
            "temperature": 0.1,
            "max_tokens": LEAD_TRIAGE_MAX_OUTPUT_TOKENS,
            "json_mode": request_json_mode,
        },
        schema_version=LEAD_TRIAGE_SCHEMA_VERSION,
        schema=LeadTriageOutput.model_json_schema(),
        context=context_markdown,
        input_payload={
            "external_id": external_id,
            "context_markdown": context_markdown,
        },
    )
    metadata = {
        "analysis_type": LEAD_TRIAGE_ANALYSIS_TYPE,
        "report_kind": REPORT_KIND_LEAD,
        "json_mode": request_json_mode,
        "prompt_chars": len(system_prompt) + len(user_prompt),
        "context_chars": len(context_markdown),
        "output_token_cap": LEAD_TRIAGE_MAX_OUTPUT_TOKENS,
        "strict_validation": strict_validation,
        "version_binding": version_binding.model_dump(mode="json"),
    }
    if response.response_id:
        metadata["response_id"] = response.response_id
    if response.latency_seconds is not None:
        metadata["latency_seconds"] = round(response.latency_seconds, 3)
    if response.usage:
        metadata["usage"] = response.usage

    save_lead_triage_result(
        tender_id=tender_id,
        provider=response.provider,
        model=response.model,
        triage=triage,
        raw_response=raw_response,
        context_chars=len(context_markdown),
        metadata=metadata,
    )
    return triage


def lead_is_strategic_customer_candidate(row: dict[str, Any]) -> bool:
    return (
        lead_has_enterprise_customer_signal(row)
        or get_price(row) >= LEAD_STRATEGIC_CUSTOMER_PRICE_RUB
    )


def lead_triage_requires_full_report(
    row: dict[str, Any],
    triage: dict[str, Any],
    *,
    include_maybe_leads: bool = False,
) -> bool:
    decision = normalize_lead_triage_decision(triage.get("lead_decision"))
    if decision == "go":
        return _bool_value(triage.get("requires_full_lead_report"))

    if decision == "maybe":
        return include_maybe_leads and _bool_value(
            triage.get("requires_full_lead_report")
        )

    if decision == "reject":
        return False

    return False


def lead_go_waiting_for_full_report(row: dict[str, Any]) -> bool:
    if lead_negative_feedback_skip_reason(row):
        return False
    has_stable_tender_identity = bool(row.get("tender_id") or row.get("id"))
    if operational_lead_report_exists_for_candidate(
        row,
        allow_external_id_fallback=has_stable_tender_identity,
    ):
        return False

    has_embedded_triage_snapshot = any(
        key in row
        for key in (
            "lead_triage_result",
            "llm_customer_lead_triage_result",
            "lead_triage_analysis_type",
        )
    )
    lookups = lead_triage_result_lookups_for_candidate(
        row,
        include_db_lookup=(
            has_stable_tender_identity and not has_embedded_triage_snapshot
        ),
    )
    lookup = lookups[0] if lookups else None
    if not lookup:
        return False

    triage = lookup.triage
    return (
        normalize_lead_triage_decision(triage.get("lead_decision")) == "go"
        and _bool_value(triage.get("requires_full_lead_report"))
    )


def print_lead_triage_note(row: dict[str, Any], note: str) -> None:
    external_id = row.get("external_id") or "unknown"
    title = row.get("title") or ""
    print(f"Lead triage candidate {external_id}: {note} | {title}", flush=True)


def lead_triage_note_text(*parts: object) -> str:
    return " ".join(str(part).strip() for part in parts if str(part).strip())


def format_lead_triage_ttl_hours(value: float) -> str:
    return f"{value:g}"


def lead_triage_lookup_is_fresh(
    lookup: LeadTriageLookup,
    *,
    ttl_hours: float,
    now: datetime | None = None,
) -> bool:
    created_at = lookup.created_at
    if created_at is None:
        return False

    current_time = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None and current_time.tzinfo is not None:
        created_at = created_at.replace(tzinfo=current_time.tzinfo)
    elif created_at.tzinfo is not None and current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=created_at.tzinfo)

    ttl_seconds = max(0.0, ttl_hours) * 60 * 60
    return (current_time - created_at).total_seconds() <= ttl_seconds


def lead_triage_selection_skip_reason(
    row: dict[str, Any],
    *,
    ttl_hours: float,
    include_maybe_leads: bool,
    force_lead_triage: bool,
    now: datetime | None = None,
) -> str | None:
    if force_lead_triage:
        return None

    has_embedded_triage_snapshot = any(
        key in row
        for key in (
            "lead_triage_result",
            "llm_customer_lead_triage_result",
            "lead_triage_analysis_type",
        )
    )
    has_stable_tender_identity = bool(row.get("tender_id") or row.get("id"))
    lookups = lead_triage_result_lookups_for_candidate(
        row,
        include_db_lookup=(
            has_stable_tender_identity and not has_embedded_triage_snapshot
        ),
    )
    for lookup in lookups:
        if not lead_triage_lookup_is_fresh(
            lookup,
            ttl_hours=ttl_hours,
            now=now,
        ):
            continue
        if lead_triage_requires_full_report(
            row,
            lookup.triage,
            include_maybe_leads=include_maybe_leads,
        ):
            return None
        return "lead_triage_fresh_non_actionable"

    return None


def print_lead_triage_stale_reuse_ignored(
    row: dict[str, Any],
    lookup: LeadTriageLookup,
    *,
    ttl_hours: float,
) -> None:
    marker = (
        "lead_triage_stale_cache_ignored"
        if lookup.source == LEAD_TRIAGE_SOURCE_CACHE
        else "lead_triage_stale_existing_ignored"
    )
    created_at = lookup.created_at.isoformat() if lookup.created_at else "missing"
    print_lead_triage_note(
        row,
        lead_triage_note_text(
            marker,
            f"lead_triage_source={lookup.source}",
            f"lead_triage_created_at={created_at}",
            f"lead_triage_ttl_hours={format_lead_triage_ttl_hours(ttl_hours)}",
        ),
    )


def print_lead_triage_decision(
    row: dict[str, Any],
    triage: dict[str, Any],
    *,
    source: str,
    selected_for_lead_report: bool,
    lead_triage_maybe_deferred: bool,
    skipped_by_lead_triage: bool,
) -> None:
    print_lead_triage_note(
        row,
        lead_triage_note_text(
            f"lead_triage_source={source}",
            f"lead_triage_decision={triage.get('lead_decision')}",
            f"lead_triage_priority={triage.get('lead_priority')}",
            f"selected_for_lead_report={str(selected_for_lead_report).lower()}",
            f"lead_triage_maybe_deferred={str(lead_triage_maybe_deferred).lower()}",
            f"skipped_by_lead_triage={str(skipped_by_lead_triage).lower()}",
            f"reject_reason={triage.get('reject_reason') or ''}",
            f"lead_triage_reject_reason={triage.get('reject_reason') or ''}",
        ),
    )


def lead_triage_error_fallback(exc: Exception) -> dict[str, Any]:
    detail = truncate_detail(f"{type(exc).__name__}: {exc}", 240)
    return normalize_lead_triage(
        {
            "lead_decision": "reject",
            "lead_priority": "low",
            "confidence": "low",
            "lead_summary": "",
            "likely_customer_story": "",
            "possible_needs": [],
            "target_roles": [],
            "reject_reason": f"lead triage failed: {detail}",
            "requires_full_lead_report": False,
        }
    )


def apply_lead_triage(
    candidates: list[dict[str, Any]],
    *,
    dry_run: bool,
    provider: str | None,
    model: str | None,
    json_mode: bool | None,
    full_report_limit: int,
    include_maybe_leads: bool = False,
    force_lead_triage: bool = False,
    lead_triage_cache_ttl_hours: float = LEAD_TRIAGE_CACHE_TTL_HOURS_DEFAULT,
    diagnostics: LeadTriageDiagnostics | None = None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    if diagnostics is not None:
        diagnostics.candidates_total += len(candidates)

    for row in candidates:
        lookup: LeadTriageLookup | None = None
        has_operational_full_report: bool | None = None
        if not force_lead_triage:
            for candidate_lookup in lead_triage_result_lookups_for_candidate(row):
                if not lead_triage_lookup_is_fresh(
                    candidate_lookup,
                    ttl_hours=lead_triage_cache_ttl_hours,
                ):
                    if has_operational_full_report is None:
                        has_operational_full_report = (
                            operational_lead_report_exists_for_candidate(row)
                        )
                    if not has_operational_full_report:
                        if diagnostics is not None:
                            diagnostics.record_stale_reuse_ignored(
                                candidate_lookup.source
                            )
                        print_lead_triage_stale_reuse_ignored(
                            row,
                            candidate_lookup,
                            ttl_hours=lead_triage_cache_ttl_hours,
                        )
                        continue

                lookup = candidate_lookup
                break
        if lookup:
            triage = lookup.triage
            source = lookup.source
            if diagnostics is not None:
                diagnostics.record_source(source)
                if lead_triage_lookup_is_fresh(
                    lookup,
                    ttl_hours=lead_triage_cache_ttl_hours,
                ):
                    diagnostics.record_fresh_cache_used(source)
        elif dry_run:
            source = LEAD_TRIAGE_SOURCE_DETERMINISTIC
            if diagnostics is not None:
                diagnostics.record_source(source)
            print_lead_triage_note(
                row,
                lead_triage_note_text("lead_triage_pending", f"lead_triage_source={source}"),
            )
            continue
        else:
            source = LEAD_TRIAGE_SOURCE_LLM
            print_lead_triage_note(
                row,
                lead_triage_note_text("lead_triage_pending", f"lead_triage_source={source}"),
            )
            if diagnostics is not None:
                diagnostics.llm_attempted += 1
            try:
                triage = run_lead_triage_for_candidate(
                    row,
                    provider=provider,
                    model=model,
                    json_mode=json_mode,
                )
            except Exception as exc:
                source = LEAD_TRIAGE_SOURCE_ERROR_FALLBACK
                triage = lead_triage_error_fallback(exc)
                if diagnostics is not None:
                    diagnostics.llm_failed += 1
                print_lead_triage_note(
                    row,
                    lead_triage_note_text(
                        f"lead_triage_source={source}",
                        f"lead_triage_error={truncate_detail(str(exc), 240)}",
                    ),
                )
            else:
                if diagnostics is not None:
                    diagnostics.llm_succeeded += 1

        row = dict(row)
        row["_lead_triage"] = triage
        triage_allows_full_report = lead_triage_requires_full_report(
            row,
            triage,
            include_maybe_leads=include_maybe_leads,
        )
        decision = normalize_lead_triage_decision(triage.get("lead_decision"))
        lead_triage_maybe_deferred = decision == "maybe" and not triage_allows_full_report
        skipped_by_lead_triage = not triage_allows_full_report
        selected_for_lead_report = (
            triage_allows_full_report and len(selected) < full_report_limit
        )
        lead_triage_report_limit_reached = (
            triage_allows_full_report and not selected_for_lead_report
        )
        if diagnostics is not None:
            diagnostics.record_decision(
                decision,
                row=row,
                triage=triage,
                selected_for_lead_report=selected_for_lead_report,
                lead_triage_maybe_deferred=lead_triage_maybe_deferred,
                report_limit_reached=lead_triage_report_limit_reached,
            )
        print_lead_triage_decision(
            row,
            triage,
            source=source,
            selected_for_lead_report=selected_for_lead_report,
            lead_triage_maybe_deferred=lead_triage_maybe_deferred,
            skipped_by_lead_triage=skipped_by_lead_triage,
        )
        if selected_for_lead_report:
            print_lead_triage_note(row, "selected_for_lead_report")
            selected.append(row)
        elif lead_triage_report_limit_reached:
            print_lead_triage_note(row, "lead_report_limit_reached")
            if decision == "go":
                print_lead_triage_note(row, "go_waiting_for_full_report")
        else:
            if lead_triage_maybe_deferred:
                print_lead_triage_note(row, "lead_triage_maybe_deferred")
            print_lead_triage_note(row, "skipped_by_lead_triage")

    return selected


def _as_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None

    return parsed if parsed >= 0 else None


def _as_non_negative_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None

    return parsed if parsed >= 0 else None


def resolve_lead_triage_cache_ttl_hours(
    *,
    cli_value: float | None,
    llm_cfg: dict[str, Any],
) -> float:
    cli_ttl = _as_non_negative_float(cli_value)
    if cli_ttl is not None:
        return cli_ttl

    env_ttl = _as_non_negative_float(os.environ.get(LEAD_TRIAGE_CACHE_TTL_ENV))
    if env_ttl is not None:
        return env_ttl

    configured_ttl = _as_non_negative_float(
        llm_cfg.get("lead_triage_cache_ttl_hours")
    )
    if configured_ttl is not None:
        return configured_ttl

    return LEAD_TRIAGE_CACHE_TTL_HOURS_DEFAULT


def llm_min_price_for_candidate(
    *,
    profile: dict[str, Any],
    category_name: str,
    assessment: dict[str, Any],
    fallback_min_price: int,
    min_price_is_override: bool = False,
) -> int:
    if min_price_is_override:
        return fallback_min_price

    llm_cfg = profile.get("llm_selection") or {}
    if not isinstance(llm_cfg, dict):
        return fallback_min_price

    thresholds = llm_cfg.get("category_min_price_rub") or llm_cfg.get("min_price_by_category") or {}
    if not isinstance(thresholds, dict):
        thresholds = {}

    threshold_key = "infra_project" if assessment.get("market_access") == "infra_project" else category_name
    aliases = {
        "server": "servers",
        "security": "security_hardware",
    }

    for key in (threshold_key, aliases.get(threshold_key, "")):
        if not key:
            continue
        threshold = _as_positive_int(thresholds.get(key))
        if threshold is not None:
            return threshold

    for key in ("default_min_price_rub", "fallback_min_price_rub", "min_price_rub"):
        threshold = _as_positive_int(llm_cfg.get(key))
        if threshold is not None:
            return threshold

    return fallback_min_price


def select_lead_candidates(
    *,
    profile: dict[str, Any],
    limit: int,
    pool_limit: int,
    force: bool,
    min_price: int,
    include_low_priority: bool,
    include_non_full_deals: bool,
    include_domestic_restricted: bool,
    debug_skips: DebugSkipControl,
    min_price_is_override: bool = False,
    diagnostics: ShortlistSelectionDiagnostics | None = None,
    now: datetime | None = None,
    result_label: str | None = None,
    include_maybe_leads: bool = False,
    force_lead_triage: bool = False,
    lead_triage_cache_ttl_hours: float = LEAD_TRIAGE_CACHE_TTL_HOURS_DEFAULT,
) -> list[dict[str, Any]]:
    del include_domestic_restricted
    candidates: list[dict[str, Any]] = []
    selection_time = now or datetime.now(timezone.utc)

    page_offset = 0
    seen_row_keys: set[str] = set()
    while limit > 0 and pool_limit > 0 and len(candidates) < limit:
        page = get_digest_rows(limit=pool_limit, offset=page_offset)
        if not page:
            break

        unique_rows_in_page = 0
        for page_index, row in enumerate(page):
            stable_identity = row.get("tender_id") or row.get("id") or row.get("external_id")
            row_key = (
                f"stable:{stable_identity}"
                if stable_identity
                else f"page:{page_offset + page_index}"
            )
            if row_key in seen_row_keys:
                continue
            seen_row_keys.add(row_key)
            unique_rows_in_page += 1
            if diagnostics:
                diagnostics.rule_based_rows += 1
                diagnostics.pre_triage_candidates_seen = diagnostics.rule_based_rows

            assessment = business_assessment(row)
            hard_noise_reason = lead_explicit_hard_noise_reason_for_row(row, assessment)
            target_signals = (
                lead_target_signal_matches_for_row(
                    row,
                    assessment,
                    hard_noise_reason=hard_noise_reason,
                )
                if hard_noise_reason
                else ()
            )
            signal = classify_lead_signal(row, assessment)
            print_debug_pre_triage_note(
                row,
                f"pre_triage_candidate_seen lead_signal={signal}",
                debug_skips,
            )

            negative_feedback_reason = lead_negative_feedback_skip_reason(row)
            if negative_feedback_reason:
                if hard_noise_reason:
                    record_hard_noise_gate_decision(
                        diagnostics,
                        row,
                        hard_noise_reason=hard_noise_reason,
                        target_signals=target_signals,
                        final_decision="skipped_by_feedback",
                        debug_skips=debug_skips,
                    )
                record_lead_pre_triage_skip(
                    diagnostics,
                    row,
                    negative_feedback_reason,
                    debug_skips,
                )
                continue

            if has_llm_report_for_kind(
                row,
                report_kind=REPORT_KIND_LEAD,
                result_label=result_label,
            ) and not force:
                if hard_noise_reason:
                    record_hard_noise_gate_decision(
                        diagnostics,
                        row,
                        hard_noise_reason=hard_noise_reason,
                        target_signals=target_signals,
                        final_decision="skipped_by_existing_report",
                        debug_skips=debug_skips,
                    )
                record_lead_pre_triage_skip(
                    diagnostics,
                    row,
                    "lead_already_has_report",
                    debug_skips,
                )
                continue

            waiting_for_full_report = lead_go_waiting_for_full_report(row)
            if hard_noise_reason:
                if waiting_for_full_report:
                    record_hard_noise_gate_decision(
                        diagnostics,
                        row,
                        hard_noise_reason=hard_noise_reason,
                        target_signals=target_signals,
                        final_decision="existing_go_waiting",
                        debug_skips=debug_skips,
                    )
                elif target_signals:
                    record_hard_noise_gate_decision(
                        diagnostics,
                        row,
                        hard_noise_reason=hard_noise_reason,
                        target_signals=target_signals,
                        final_decision="override_to_triage",
                        debug_skips=debug_skips,
                    )
                else:
                    record_hard_noise_gate_decision(
                        diagnostics,
                        row,
                        hard_noise_reason=hard_noise_reason,
                        target_signals=target_signals,
                        final_decision="strict_skip",
                        debug_skips=debug_skips,
                    )
                    record_lead_pre_triage_skip(
                        diagnostics,
                        row,
                        f"lead_hard_noise={hard_noise_reason}",
                        debug_skips,
                    )
                    continue

            triage_skip_reason = lead_triage_selection_skip_reason(
                row,
                ttl_hours=lead_triage_cache_ttl_hours,
                include_maybe_leads=include_maybe_leads,
                force_lead_triage=force_lead_triage,
                now=selection_time,
            )
            if triage_skip_reason:
                record_lead_pre_triage_skip(
                    diagnostics,
                    row,
                    triage_skip_reason,
                    debug_skips,
                )
                continue

            deadline_note = lead_deadline_allowed_note(row, now=selection_time)
            if deadline_note:
                print_debug_pre_triage_note(row, deadline_note, debug_skips)

            if diagnostics:
                diagnostics.rule_based_passed += 1
            if waiting_for_full_report:
                print_debug_pre_triage_note(row, "go_waiting_for_full_report", debug_skips)
            print_debug_pre_triage_note(
                row,
                f"eligible_for_paid_triage lead_signal={signal}",
                debug_skips,
            )

            category_name, category_cfg = match_target_category(row, profile)
            if not category_name or not category_cfg:
                category_name, category_label = lead_category_from_signal(row, assessment)
                category_cfg = {"label": category_label}

            row = dict(row)
            row["_lead_signal"] = signal
            row["_lead_waiting_for_full_report"] = waiting_for_full_report
            row["_llm_category"] = category_name
            row["_llm_category_label"] = category_cfg.get("label") or category_name
            candidates.append(row)

        if len(page) < pool_limit or unique_rows_in_page == 0:
            break
        page_offset += len(page)

    if diagnostics:
        diagnostics.eligible_for_llm_before_limit = len(candidates)

    selected = sorted(candidates, key=lambda row: lead_candidate_priority(row, profile))[:limit]
    if diagnostics:
        diagnostics.selected_for_llm = len(selected)
        diagnostics.selected_for_lead_triage = len(selected)

    return selected


def select_candidates(
    *,
    profile: dict[str, Any],
    limit: int,
    pool_limit: int,
    force: bool,
    min_price: int,
    include_low_priority: bool,
    include_non_full_deals: bool,
    include_domestic_restricted: bool,
    debug_skips: DebugSkipControl,
    min_price_is_override: bool = False,
    diagnostics: ShortlistSelectionDiagnostics | None = None,
    now: datetime | None = None,
    report_kind: str = REPORT_KIND_TECHNICAL,
    result_label: str | None = None,
    include_maybe_leads: bool = False,
    force_lead_triage: bool = False,
    lead_triage_cache_ttl_hours: float = LEAD_TRIAGE_CACHE_TTL_HOURS_DEFAULT,
) -> list[dict[str, Any]]:
    report_kind = normalize_report_kind(report_kind)
    if report_kind == REPORT_KIND_LEAD:
        return select_lead_candidates(
            profile=profile,
            limit=limit,
            pool_limit=pool_limit,
            force=force,
            min_price=min_price,
            include_low_priority=include_low_priority,
            include_non_full_deals=include_non_full_deals,
            include_domestic_restricted=include_domestic_restricted,
            debug_skips=debug_skips,
            min_price_is_override=min_price_is_override,
            diagnostics=diagnostics,
            now=now,
            result_label=result_label,
            include_maybe_leads=include_maybe_leads,
            force_lead_triage=force_lead_triage,
            lead_triage_cache_ttl_hours=lead_triage_cache_ttl_hours,
        )

    rows = get_digest_rows(limit=pool_limit)
    if diagnostics:
        diagnostics.rule_based_rows = len(rows)

    candidates: list[dict[str, Any]] = []
    selection_time = now or datetime.now(timezone.utc)

    for row in rows:
        if effective_recommendation(row) == "no_go":
            record_selection_skip(
                diagnostics,
                row,
                "effective_recommendation=no_go",
                debug_skips,
            )
            continue
        if diagnostics:
            diagnostics.rule_based_passed += 1

        sales_feedback_reason = sales_feedback_selection_skip_reason(row)
        if sales_feedback_reason:
            record_selection_skip(
                diagnostics,
                row,
                sales_feedback_reason,
                debug_skips,
            )
            continue

        assessment = business_assessment(row)
        if assessment.get("action") in {"skip_incumbent", "skip_low_priority", "no_go"}:
            record_selection_skip(
                diagnostics,
                row,
                f"business_action={assessment.get('action')}",
                debug_skips,
            )
            continue

        if (
            assessment.get("market_access") == "unknown"
            and is_generic_equipment_title(row, profile)
            and not has_generic_equipment_target_hardware_evidence(row, profile)
        ):
            record_selection_skip(
                diagnostics,
                row,
                "generic_equipment_without_target_hardware",
                debug_skips,
            )
            continue

        if assessment.get("market_access") == "domestic_restricted" and not include_domestic_restricted:
            record_selection_skip(
                diagnostics,
                row,
                "market_access=domestic_restricted",
                debug_skips,
            )
            continue

        if is_excluded_vertical(row, profile):
            record_selection_skip(diagnostics, row, "excluded_vertical", debug_skips)
            continue

        if has_llm_report(row) and not force:
            record_selection_skip(diagnostics, row, "already_has_llm_report", debug_skips)
            continue

        deadline_reason = deadline_selection_skip_reason(row, now=selection_time)
        if deadline_reason:
            record_selection_skip(diagnostics, row, deadline_reason, debug_skips)
            continue

        if not force:
            direct_preparation_reason = direct_preparation_skip_reason(row)
            if direct_preparation_reason:
                record_selection_skip(
                    diagnostics,
                    row,
                    direct_preparation_reason,
                    debug_skips,
                )
                continue

            blocked_reason = preparation_blocked_skip_reason(row)
            if blocked_reason:
                record_selection_skip(
                    diagnostics,
                    row,
                    blocked_reason,
                    debug_skips,
                )
                continue

            exhausted_reason = preparation_exhaustion_skip_reason(row)
            if exhausted_reason:
                record_selection_skip(
                    diagnostics,
                    row,
                    exhausted_reason,
                    debug_skips,
                )
                continue

        category_name, category_cfg = match_target_category(row, profile)
        if not category_name or not category_cfg:
            record_selection_skip(diagnostics, row, "no_target_category", debug_skips)
            continue

        if is_low_priority_deal(row, profile) and not include_low_priority:
            record_selection_skip(diagnostics, row, "low_priority_deal", debug_skips)
            continue

        if not include_non_full_deals and not is_full_deal_for_category(row, profile, category_cfg):
            record_selection_skip(
                diagnostics,
                row,
                "not_full_deal_for_category",
                debug_skips,
            )
            continue

        candidate_min_price = llm_min_price_for_candidate(
            profile=profile,
            category_name=category_name,
            assessment=assessment,
            fallback_min_price=min_price,
            min_price_is_override=min_price_is_override,
        )
        if get_price(row) < candidate_min_price:
            record_selection_skip(
                diagnostics,
                row,
                f"price_below_min_price={candidate_min_price}",
                debug_skips,
            )
            continue

        row = dict(row)
        row["_llm_category"] = category_name
        row["_llm_category_label"] = category_cfg.get("label") or category_name
        candidates.append(row)

    if diagnostics:
        diagnostics.eligible_for_llm_before_limit = len(candidates)

    selected = sorted(candidates, key=lambda row: candidate_priority(row, profile))[:limit]
    if diagnostics:
        diagnostics.selected_for_llm = len(selected)

    return selected


def run_llm_for_candidate(
    row: dict[str, Any],
    *,
    dry_run: bool,
    max_spec_chars: int,
    max_other_chars: int,
    max_output_tokens: int,
    timeout_seconds: int | None,
    analysis_depth: str = ANALYSIS_DEPTH_DEEP,
    report_kind: str = REPORT_KIND_TECHNICAL,
    provider: str | None = None,
    model: str | None = None,
    json_mode: bool = False,
    result_label: str | None = None,
) -> LLMCandidateRunResult | None:
    external_id = row.get("external_id")
    title = row.get("title")

    if not external_id:
        print("Skip candidate without external_id")
        return

    analysis_depth = normalize_analysis_depth(analysis_depth)
    report_kind = normalize_report_kind(report_kind)
    args = py_module(
        "app.llm.tender_report",
        "--external-id",
        external_id,
        "--report-kind",
        report_kind,
        "--analysis-depth",
        analysis_depth,
        "--max-spec-chars",
        max_spec_chars,
        "--max-other-chars",
        max_other_chars,
        "--max-output-tokens",
        max_output_tokens,
    )

    if provider:
        args.extend(["--provider", provider])

    if model:
        args.extend(["--model", model])

    if json_mode:
        args.append("--json-mode")

    if result_label:
        args.extend(["--result-label", result_label])

    print()
    print(f"===== LLM candidate {external_id}: {title} =====")
    print("$ " + " ".join(args), flush=True)

    network_retry_event = start_network_retry_event(row)
    if report_kind == REPORT_KIND_LEAD:
        document_readiness = ensure_lead_documents_before_llm(row, dry_run=dry_run)
    else:
        document_readiness = ensure_targeted_documents_before_llm(row, dry_run=dry_run)
    row["_llm_document_readiness"] = document_readiness

    if report_kind == REPORT_KIND_LEAD:
        args.extend(
            [
                "--document-readiness-json",
                json.dumps(readiness_metadata(document_readiness), ensure_ascii=True),
            ]
        )

    if document_readiness.llm_readiness != "ready_for_llm":
        reason = document_readiness.warning_reason or document_readiness.document_state
        print(
            "Skip LLM candidate: "
            f"external_id={external_id} "
            f"reason={reason} "
            f"docs={optional_int_text(document_readiness.docs_after)} "
            f"docs_with_text={optional_int_text(document_readiness.docs_with_text_after)} "
            f"extraction_retry_triggered={str(document_readiness.extraction_retry_triggered).lower()}",
            flush=True,
        )
        if not dry_run:
            raise DocumentNotReadyError(document_readiness)

    if dry_run:
        return LLMCandidateRunResult(
            external_id=str(external_id),
            document_readiness=document_readiness,
        )

    for attempt in range(1, SUBPROCESS_NETWORK_MAX_ATTEMPTS + 1):
        try:
            completed = subprocess.run(
                args,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.CalledProcessError as exc:
            if attempt < SUBPROCESS_NETWORK_MAX_ATTEMPTS and should_retry_subprocess_failure(exc):
                output = called_process_output(exc)
                network_retry_event["attempts"] = int(network_retry_event["attempts"]) + 1
                log_llm_candidate_network_retry(
                    external_id=str(external_id),
                    attempt=attempt,
                    max_attempts=SUBPROCESS_NETWORK_MAX_ATTEMPTS,
                    backoff_seconds=SUBPROCESS_NETWORK_RETRY_BACKOFF_SECONDS,
                    output=output,
                )
                time.sleep(SUBPROCESS_NETWORK_RETRY_BACKOFF_SECONDS)
                continue

            if int(network_retry_event["attempts"]) > 0:
                network_retry_event["failed"] = True
            raise
        else:
            if int(network_retry_event["attempts"]) > 0:
                network_retry_event["succeeded"] = True
            break
    else:
        raise RuntimeError("LLM candidate retry loop ended unexpectedly")

    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr, flush=True)

    return LLMCandidateRunResult(
        external_id=str(external_id),
        document_readiness=document_readiness,
        completed=completed,
    )


def document_readiness_for_row(row: dict[str, Any]) -> CandidateDocumentReadiness | None:
    readiness = row.get("_llm_document_readiness")
    return readiness if isinstance(readiness, CandidateDocumentReadiness) else None


def fallback_context_limits(
    *,
    analysis_depth: str,
    max_spec_chars: int,
    max_other_chars: int,
) -> tuple[int, int]:
    depth = normalize_analysis_depth(analysis_depth)
    if depth == ANALYSIS_DEPTH_DEEP:
        return (
            min(max_spec_chars, DEEP_FALLBACK_MAX_SPEC_CHARS),
            min(max_other_chars, DEEP_FALLBACK_MAX_OTHER_CHARS),
        )

    return (
        min(max_spec_chars, FALLBACK_MAX_SPEC_CHARS),
        min(max_other_chars, FALLBACK_MAX_OTHER_CHARS),
    )


def fallback_analysis_depth(primary_analysis_depth: str) -> str:
    depth = normalize_analysis_depth(primary_analysis_depth)
    if depth == ANALYSIS_DEPTH_DEEP:
        return ANALYSIS_DEPTH_DEEP
    return ANALYSIS_DEPTH_STANDARD


def run_llm_fallback_for_candidate(
    row: dict[str, Any],
    *,
    primary_failure: LLMCandidateFailure,
    dry_run: bool,
    analysis_depth: str,
    max_spec_chars: int,
    max_other_chars: int,
    max_output_tokens: int,
    timeout_seconds: int | None,
    provider: str | None = None,
    model: str | None = None,
    json_mode: bool = False,
    report_kind: str = REPORT_KIND_TECHNICAL,
) -> LLMCandidateFailure | None:
    external_id = str(row.get("external_id") or "unknown")
    report_kind = normalize_report_kind(report_kind)
    retry_analysis_depth = fallback_analysis_depth(analysis_depth)
    fallback_spec_chars, fallback_other_chars = fallback_context_limits(
        analysis_depth=analysis_depth,
        max_spec_chars=max_spec_chars,
        max_other_chars=max_other_chars,
    )

    print(
        "LLM fallback retry: "
        f"external_id={external_id} "
        f"primary_reason={primary_failure.reason} "
        f"analysis_depth={retry_analysis_depth} "
        f"max_spec_chars={fallback_spec_chars} "
        f"max_other_chars={fallback_other_chars} "
        f"max_output_tokens={max_output_tokens} "
        f"report_kind={report_kind} "
        f"result_label={FALLBACK_RESULT_LABEL}",
        flush=True,
    )

    try:
        run_llm_for_candidate(
            row,
            dry_run=dry_run,
            analysis_depth=retry_analysis_depth,
            report_kind=report_kind,
            max_spec_chars=fallback_spec_chars,
            max_other_chars=fallback_other_chars,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            provider=provider,
            model=model,
            json_mode=json_mode,
            result_label=FALLBACK_RESULT_LABEL,
        )
    except DocumentNotReadyError as exc:
        failure = describe_document_not_ready_failure(exc)
    except subprocess.TimeoutExpired as exc:
        failure = describe_timeout_failure(external_id, exc)
    except subprocess.CalledProcessError as exc:
        failure = describe_subprocess_failure(external_id, exc)
    except Exception as exc:
        failure = describe_exception_failure(external_id, exc)
    else:
        print(
            "LLM fallback succeeded: "
            f"external_id={external_id} "
            f"result_label={FALLBACK_RESULT_LABEL}",
            flush=True,
        )
        return None

    detail = " ".join(
        part
        for part in (
            "fallback_after=non_json_response",
            failure.detail,
        )
        if part
    )
    failure = LLMCandidateFailure(
        external_id=failure.external_id,
        reason=failure.reason,
        detail=truncate_detail(detail),
        returncode=failure.returncode,
    )
    print(
        "LLM fallback failed: "
        f"external_id={external_id} "
        f"reason={failure.reason} "
        f"result_label={FALLBACK_RESULT_LABEL}",
        flush=True,
    )
    return failure


def main() -> None:
    parser = argparse.ArgumentParser(description="Run LLM presales reports for cleaned shortlist")
    parser.add_argument("--profile", default="config/business_profile.yaml")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--pool-limit", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Run LLM even if report already exists")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug-skips", action="store_true", help="Print why tenders were skipped during LLM shortlist selection")
    parser.add_argument(
        "--debug-skips-limit",
        type=int,
        default=None,
        help=(
            "When --debug-skips is enabled, cap detailed debug lines per skip "
            "reason/bucket while keeping full counters in summaries"
        ),
    )
    parser.add_argument("--min-price", type=int, default=None)
    parser.add_argument("--include-low-priority", action="store_true")
    parser.add_argument("--include-non-full-deals", action="store_true")
    parser.add_argument("--include-domestic-restricted", action="store_true")
    parser.add_argument(
        "--analysis-depth",
        choices=ANALYSIS_DEPTH_CHOICES,
        default=ANALYSIS_DEPTH_DEEP,
    )
    parser.add_argument("--max-spec-chars", type=int, default=None)
    parser.add_argument("--max-other-chars", type=int, default=None)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--candidate-timeout-seconds", type=int, default=None)
    parser.add_argument("--provider", help="Override LLM provider for this shortlist run")
    parser.add_argument("--model", help="Override LLM model for this shortlist run")
    parser.add_argument("--json-mode", action="store_true", help="Request response_format=json_object from compatible providers")
    parser.add_argument("--result-label", help="Store shortlist LLM reports under a comparison label")
    parser.add_argument(
        "--lead-triage-enabled",
        dest="lead_triage_enabled",
        action="store_true",
        default=None,
        help="Run lightweight lead triage before full lead reports",
    )
    parser.add_argument(
        "--no-lead-triage",
        dest="lead_triage_enabled",
        action="store_false",
        help="Run lead reports directly after hard-noise filtering",
    )
    parser.add_argument(
        "--lead-triage-limit",
        type=int,
        default=None,
        help="Maximum number of lead candidates sent to lightweight triage",
    )
    parser.add_argument(
        "--force-lead-triage",
        action="store_true",
        help="Ignore cached lead triage decisions and rerun lightweight triage",
    )
    parser.add_argument(
        "--lead-triage-cache-ttl-hours",
        type=float,
        default=None,
        help=(
            "Maximum age in hours for reusing llm_customer_lead_triage "
            f"(env: {LEAD_TRIAGE_CACHE_TTL_ENV}, default: "
            f"{format_lead_triage_ttl_hours(LEAD_TRIAGE_CACHE_TTL_HOURS_DEFAULT)})"
        ),
    )
    parser.add_argument(
        "--force-lead-report",
        action="store_true",
        help=(
            "Dangerous diagnostic/manual lead mode: rerun full customer lead "
            "report even when an operational lead report already exists"
        ),
    )
    parser.add_argument(
        "--include-maybe-leads",
        action="store_true",
        help="Allow maybe lead triage decisions to run full lead reports",
    )
    parser.add_argument(
        "--report-kind",
        choices=REPORT_KIND_CHOICES,
        default=REPORT_KIND_TECHNICAL,
        help="Report mode to pass to app.llm.tender_report",
    )
    args = parser.parse_args()

    profile = load_business_profile(Path(args.profile))
    llm_cfg = profile.get("llm_selection") or {}
    analysis_depth = normalize_analysis_depth(args.analysis_depth)
    report_kind = normalize_report_kind(args.report_kind)
    limits = resolve_analysis_limits(
        analysis_depth,
        max_spec_chars=args.max_spec_chars,
        max_other_chars=args.max_other_chars,
        max_output_tokens=args.max_output_tokens,
    )

    limit = int(args.limit or llm_cfg.get("default_limit", 5))
    pool_limit = int(args.pool_limit or llm_cfg.get("default_pool_limit", 150))
    min_price = (
        _as_positive_int(args.min_price)
        if args.min_price is not None
        else (
            _as_positive_int(llm_cfg.get("fallback_min_price_rub"))
            or _as_positive_int(llm_cfg.get("min_price_rub"))
            or 5_000_000
        )
    )
    if min_price is None:
        min_price = 0
    lead_triage_enabled = (
        report_kind == REPORT_KIND_LEAD
        if args.lead_triage_enabled is None
        else bool(args.lead_triage_enabled)
    )
    if args.lead_triage_limit is not None:
        lead_triage_limit = max(0, int(args.lead_triage_limit))
    else:
        configured_triage_limit = _as_positive_int(llm_cfg.get("lead_triage_limit"))
        lead_triage_limit = configured_triage_limit or max(
            LEAD_TRIAGE_DEFAULT_LIMIT,
            limit * 3,
        )
    lead_triage_cache_ttl_hours = resolve_lead_triage_cache_ttl_hours(
        cli_value=args.lead_triage_cache_ttl_hours,
        llm_cfg=llm_cfg if isinstance(llm_cfg, dict) else {},
    )
    selection_limit = (
        lead_triage_limit
        if report_kind == REPORT_KIND_LEAD and lead_triage_enabled
        else limit
    )
    debug_skips: DebugSkipControl = bool(args.debug_skips)
    if args.debug_skips and args.debug_skips_limit is not None:
        debug_skips = DebugSkipLimiter(limit=max(0, int(args.debug_skips_limit)))

    selection_diagnostics = ShortlistSelectionDiagnostics()
    candidates = select_candidates(
        profile=profile,
        limit=selection_limit,
        pool_limit=pool_limit,
        force=args.force,
        min_price=min_price,
        include_low_priority=args.include_low_priority,
        include_non_full_deals=args.include_non_full_deals,
        include_domestic_restricted=args.include_domestic_restricted,
        debug_skips=debug_skips,
        min_price_is_override=args.min_price is not None,
        diagnostics=selection_diagnostics,
        report_kind=report_kind,
        result_label=args.result_label,
        include_maybe_leads=args.include_maybe_leads,
        force_lead_triage=args.force_lead_triage,
        lead_triage_cache_ttl_hours=lead_triage_cache_ttl_hours,
    )
    print_selection_diagnostics(selection_diagnostics)
    print_debug_skip_limit_summary(debug_skips)

    if not candidates:
        print("No LLM candidates selected.")
        if report_kind == REPORT_KIND_LEAD:
            print_duplicate_operational_lead_report_audit()
        print("Final run status:", flush=True)
        print("- run_status: success_no_candidates", flush=True)
        return

    if report_kind == REPORT_KIND_LEAD and lead_triage_enabled:
        print("Selected lead triage candidates:")
    else:
        print("Selected LLM candidates:")
    for index, row in enumerate(candidates, start=1):
        assessment = business_assessment(row)
        print(
            f"{index}. {row.get('external_id')} | "
            f"{get_price(row):,} руб. | "
            f"{row.get('_llm_category_label')} | "
            f"{assessment.get('market_access')} | "
            f"{row.get('title')}"
        )

    lead_triage_diagnostics: LeadTriageDiagnostics | None = None
    if report_kind == REPORT_KIND_LEAD and lead_triage_enabled:
        lead_triage_diagnostics = LeadTriageDiagnostics()
        candidates = apply_lead_triage(
            candidates,
            dry_run=args.dry_run,
            provider=args.provider,
            model=args.model,
            json_mode=args.json_mode,
            full_report_limit=limit,
            include_maybe_leads=args.include_maybe_leads,
            force_lead_triage=args.force_lead_triage,
            lead_triage_cache_ttl_hours=lead_triage_cache_ttl_hours,
            diagnostics=lead_triage_diagnostics,
        )
        if not candidates:
            print()
            print_lead_triage_summary(lead_triage_diagnostics)
            print("No lead candidates approved by triage.")
            print_duplicate_operational_lead_report_audit()
            print("Final run status:", flush=True)
            print("- run_status: success_no_full_report_candidates", flush=True)
            return

        print("Selected LLM candidates:")
        for index, row in enumerate(candidates, start=1):
            triage = row.get("_lead_triage") or {}
            print(
                f"{index}. {row.get('external_id')} | "
                f"{get_price(row):,} руб. | "
                f"lead_triage_decision={triage.get('lead_decision')} | "
                f"lead_triage_priority={triage.get('lead_priority')} | "
                f"{row.get('title')}"
            )

    timeout_seconds = (
        args.candidate_timeout_seconds
        if args.candidate_timeout_seconds and args.candidate_timeout_seconds > 0
        else None
    )
    successful_external_ids: list[str] = []
    failures: list[LLMCandidateFailure] = []
    document_warnings: list[CandidateDocumentReadiness] = []
    fallback_succeeded = 0
    fallback_failed = 0
    fallback_attempts: list[LLMCandidateFallbackAttempt] = []
    network_retry_attempts = 0
    network_retry_succeeded = 0
    network_retry_failed = 0
    retry_external_ids: list[str] = []
    document_readiness_events: list[CandidateDocumentReadiness] = []
    lead_selector_used = 0
    lead_selector_failed = 0
    lead_full_report_diagnostics = (
        LeadFullReportDiagnostics()
        if report_kind == REPORT_KIND_LEAD
        else None
    )

    for row in candidates:
        external_id = str(row.get("external_id") or "unknown")
        failure: LLMCandidateFailure | None = None
        run_result: LLMCandidateRunResult | None = None

        if (
            report_kind == REPORT_KIND_LEAD
            and not args.force_lead_report
            and operational_lead_report_exists_for_candidate(row)
        ):
            if lead_full_report_diagnostics is not None:
                lead_full_report_diagnostics.existing_skipped += 1
            print(
                f"Skip full lead report {external_id}: "
                "lead_full_report_already_exists",
                flush=True,
            )
            continue

        if lead_full_report_diagnostics is not None:
            lead_full_report_diagnostics.attempted += 1

        try:
            run_result = run_llm_for_candidate(
                row,
                dry_run=args.dry_run,
                analysis_depth=analysis_depth,
                report_kind=report_kind,
                max_spec_chars=limits.max_spec_chars,
                max_other_chars=limits.max_other_chars,
                max_output_tokens=limits.max_output_tokens,
                timeout_seconds=timeout_seconds,
                provider=args.provider,
                model=args.model,
                json_mode=args.json_mode,
                result_label=args.result_label,
            )
        except DocumentNotReadyError as exc:
            failure = describe_document_not_ready_failure(exc)
        except subprocess.TimeoutExpired as exc:
            failure = describe_timeout_failure(external_id, exc)
        except subprocess.CalledProcessError as exc:
            failure = describe_subprocess_failure(external_id, exc)
        except Exception as exc:
            failure = describe_exception_failure(external_id, exc)
        else:
            readiness = (
                run_result.document_readiness
                if isinstance(run_result, LLMCandidateRunResult)
                else document_readiness_for_row(row)
            )
            if (
                args.dry_run
                and readiness
                and readiness.llm_readiness != "ready_for_llm"
            ):
                failure = describe_document_not_ready_failure(DocumentNotReadyError(readiness))
            else:
                successful_external_ids.append(external_id)
                if lead_full_report_diagnostics is not None:
                    lead_full_report_diagnostics.succeeded += 1
                if (
                    isinstance(run_result, LLMCandidateRunResult)
                    and run_result.completed
                    and run_result.completed.stdout
                ):
                    stdout = run_result.completed.stdout
                    if "lead_selector_used=true" in stdout:
                        lead_selector_used += 1
                    if "lead_selector_failed=true" in stdout:
                        lead_selector_failed += 1

        readiness = document_readiness_for_row(row)
        if readiness:
            document_readiness_events.append(readiness)
        if readiness and (
            readiness.warning_reason or readiness.non_blocking_warning_reason
        ):
            document_warnings.append(readiness)

        if failure and failure.reason == "non_json_response":
            retry_analysis_depth = fallback_analysis_depth(analysis_depth)
            fallback_spec_chars, fallback_other_chars = fallback_context_limits(
                analysis_depth=analysis_depth,
                max_spec_chars=limits.max_spec_chars,
                max_other_chars=limits.max_other_chars,
            )
            fallback_failure = run_llm_fallback_for_candidate(
                row,
                primary_failure=failure,
                dry_run=args.dry_run,
                analysis_depth=analysis_depth,
                max_spec_chars=limits.max_spec_chars,
                max_other_chars=limits.max_other_chars,
                max_output_tokens=limits.max_output_tokens,
                timeout_seconds=timeout_seconds,
                provider=args.provider,
                model=args.model,
                json_mode=args.json_mode,
                report_kind=report_kind,
            )
            fallback_attempts.append(
                LLMCandidateFallbackAttempt(
                    external_id=external_id,
                    reason=failure.reason,
                    analysis_depth=retry_analysis_depth,
                    max_spec_chars=fallback_spec_chars,
                    max_other_chars=fallback_other_chars,
                    max_output_tokens=limits.max_output_tokens,
                    succeeded=fallback_failure is None,
                )
            )
            if fallback_failure is None:
                fallback_succeeded += 1
                successful_external_ids.append(f"{external_id}({FALLBACK_RESULT_LABEL})")
                if lead_full_report_diagnostics is not None:
                    lead_full_report_diagnostics.succeeded += 1
                failure = None
            else:
                fallback_failed += 1
                failure = fallback_failure

        if failure:
            if lead_full_report_diagnostics is not None:
                lead_full_report_diagnostics.failed += 1
            failures.append(failure)
            log_llm_candidate_failure(failure)

        row_retry_attempts, row_retry_succeeded, row_retry_failed = row_network_retry_counts(row)
        if row_retry_attempts:
            network_retry_attempts += row_retry_attempts
            network_retry_succeeded += row_retry_succeeded
            network_retry_failed += row_retry_failed
            retry_external_ids.append(external_id)

    print_llm_summary(
        successful_external_ids,
        failures,
        document_warnings,
        analysis_depth=analysis_depth,
        limits=limits,
        fallback_succeeded=fallback_succeeded,
        fallback_failed=fallback_failed,
        fallback_attempts=fallback_attempts,
        network_retry_attempts=network_retry_attempts,
        network_retry_succeeded=network_retry_succeeded,
        network_retry_failed=network_retry_failed,
        retry_external_ids=retry_external_ids,
        document_readiness_events=document_readiness_events,
        lead_selector_used=lead_selector_used,
        lead_selector_failed=lead_selector_failed,
        lead_triage_diagnostics=lead_triage_diagnostics,
        lead_full_report_diagnostics=lead_full_report_diagnostics,
        duplicate_operational_lead_report_rows=(
            get_duplicate_operational_lead_report_rows()
            if report_kind == REPORT_KIND_LEAD
            else None
        ),
        report_kind=report_kind,
    )


if __name__ == "__main__":
    main()
