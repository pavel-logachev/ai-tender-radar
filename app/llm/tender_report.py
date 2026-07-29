from __future__ import annotations

import argparse
import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.business_profile import (
    domestic_context_keywords,
    domestic_safe_vendor_examples,
    foreign_vendor_recommendation_keywords,
    load_business_profile,
    normalize as normalize_profile_text,
)
from app.config import settings
from app.document_status import build_document_status
from app.llm.analysis_depth import (
    ANALYSIS_DEPTH_CHOICES,
    ANALYSIS_DEPTH_DEEP,
    ANALYSIS_DEPTH_STANDARD,
    normalize_analysis_depth,
    resolve_analysis_limits,
)
from app.llm.document_selector import select_documents_for_lead_report
from app.llm.context_builder import (
    build_llm_package,
    get_documents,
    get_tender_by_external_id,
    package_to_markdown,
)
from app.llm.base import LLMResponse
from app.llm.factory import create_llm_client
from app.llm.tz_problem_cleanup import (
    DEFAULT_TZ_PROBLEMS_ITEM,
    apply_tz_problems_guardrail,
    clean_problems_in_tz,
)


REPORT_DIR = Path("/app/data/llm_reports")
CONTEXT_DIR = Path("/app/data/llm_contexts")
COMPACT_REPORT_PROVIDER = "routerai"
REPORT_KIND_TECHNICAL = "technical"
REPORT_KIND_LEAD = "lead"
REPORT_KIND_CHOICES = (REPORT_KIND_TECHNICAL, REPORT_KIND_LEAD)
LEAD_DIRECT_PREPARATION_DOCUMENT_LIMIT = 5
LLM_PRESALES_REPORT_ANALYSIS_TYPE = "llm_presales_report"
LLM_CUSTOMER_LEAD_REPORT_ANALYSIS_TYPE = "llm_customer_lead_report"
ROUTERAI_ANALYSIS_FIELDS = (
    "summary",
    "what_is_required",
    "top_risks",
    "delivery_feasibility",
    "potential_offer",
    "problems_in_tz",
    "questions_to_customer",
    "next_action",
    "verdict",
    "confidence",
)
ROUTERAI_DEEP_ANALYSIS_FIELDS = (
    "summary",
    "what_is_procured",
    "supply_composition",
    "critical_tz_requirements",
    "selection_parameters",
    "tailoring_signals",
    "equivalence",
    "domestic_registry",
    "delivery_term_and_feasibility",
    "what_can_be_offered",
    "participation_strategies",
    "what_presales_should_check",
    "questions_to_customer",
    "manager_next_steps",
    "decision",
    "conditions_to_reconsider",
)
ROUTERAI_FINAL_REPORT_FIELDS = (
    "summary",
    "verdict",
    "confidence",
    "what_is_required",
    "supply_composition",
    "critical_tz_requirements",
    "top_risks",
    "delivery_feasibility",
    "potential_offer",
    "what_presales_should_check",
    "problems_in_tz",
    "questions_to_customer",
    "next_action",
)
LEAD_REPORT_FIELDS = (
    "document_status",
    "lead_summary",
    "customer_signal",
    "likely_customer_story",
    "possible_needs",
    "target_roles",
    "procurement_contact_role",
    "target_end_customer_roles",
    "opening_phrase",
    "discovery_questions",
    "handoff_to_manager_when",
    "lead_priority",
    "confidence",
    "why_priority",
    "next_action",
)
LEAD_PRIORITY_RECOMMENDATION = {
    "high": "go",
    "medium": "maybe",
    "low": "maybe",
}
LEAD_DEFAULT_NEXT_ACTION = (
    "\u041f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c "
    "\u0437\u0430\u043a\u0443\u043f\u043e\u0447\u043d\u043e\u043c\u0443 "
    "\u043a\u043e\u043d\u0442\u0430\u043a\u0442\u0443, "
    "\u043f\u043e\u043f\u0440\u043e\u0441\u0438\u0442\u044c "
    "\u0441\u043e\u0435\u0434\u0438\u043d\u0438\u0442\u044c \u0441 "
    "\u0418\u0422-\u043e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u043c "
    "/ \u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u043c "
    "\u0437\u0430\u043a\u0430\u0437\u0447\u0438\u043a\u043e\u043c, "
    "\u0437\u0430\u0444\u0438\u043a\u0441\u0438\u0440\u043e\u0432\u0430\u0442\u044c "
    "\u043a\u043e\u043d\u0442\u0430\u043a\u0442 \u0438 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 "
    "\u0432 \u0440\u0430\u0431\u043e\u0447\u0435\u0439 "
    "\u043e\u0447\u0435\u0440\u0435\u0434\u0438."
)
ROUTERAI_PACKAGING_RETRY_INSTRUCTION = """
Retry instruction:
- Return one valid JSON object only.
- Do not wrap it in markdown fences.
- Do not add commentary before or after the JSON.
""".strip()
ROUTERAI_ANALYSIS_RETRY_INSTRUCTION = """
Retry instruction:
- JSON only.
- Return one valid JSON object only.
- Do not wrap it in markdown fences.
- Do not add commentary before or after the JSON.
- Keep every field compact.
""".strip()
ROUTERAI_ANALYSIS_RETRY_MAX_SPEC_CHARS = 30000
ROUTERAI_ANALYSIS_RETRY_MAX_OTHER_CHARS = 6000
ROUTERAI_DEEP_ANALYSIS_RETRY_MAX_SPEC_CHARS = 120000
ROUTERAI_DEEP_ANALYSIS_RETRY_MAX_OTHER_CHARS = 20000
OUTPUT_LIMIT_FINISH_REASONS = {"length", "max_tokens"}


SYSTEM_PROMPT = """
Ты AI-пресейл-аналитик IT-интегратора.

Твоя задача: анализировать закупки для IT-интегратора по карточке закупки и документации.

Главный фокус:
1. ТЗ / описание объекта закупки / спецификация.
2. Срок поставки и исполнимость.
3. Признаки заточки под конкретного производителя, модель, поставщика или уже имеющуюся инфраструктуру заказчика.

Важные правила:
- Не придумывай факты.
- Не делай категоричный вывод "закупка заточена", если есть только косвенные признаки.
- Если риск заточки есть, укажи конкретные основания из ТЗ.
- Типовые юридические формулировки 44-ФЗ, 223-ФЗ и СМП не считай сильным риском сами по себе.
- Отдельно оцени, что можно предложить интегратору.
- Отдельно сформулируй вопросы заказчику.
- Не пиши общие фразы вроде "предложить оборудование ведущих производителей", если можно привязаться к требованиям ТЗ.
- Если предмет закупки явно СХД, серверы, сетевое оборудование или ПАК/ИБ, не называй категорию неизвестной в тексте анализа.
- Для СХД обязательно проверь: типы накопителей, форм-фактор, контроллеры, кэш/память, порты, масштабирование, полки расширения, лицензии, гарантию, срок поставки, документы при поставке.
- Делай рабочий пресейл-разбор, а не общий консалтинговый текст.
- Для подходящих решений пиши не "точно подходит", а "проверить как кандидата", если нет полного ручного сопоставления с ТЗ.
- Оцени не только заточку, но и поставочные, коммерческие, гарантийные, реестровые и маржинальные риски.
- Если в LLM-пакете обнаружен отечественный/реестровый/российско-платформенный контекст, не предлагай иностранные бренды как основной вариант: используй российское, реестровое или допустимое по ТЗ оборудование, а импортные бренды упоминай только как риск или ограничение.
- Вопросы разделяй по адресатам: заказчику, пресейлу/инженеру, поставщику/дистрибьютору.
- Для маржинальности не выдумывай цену закупки, но оцени, где маржа может сгореть: срок, наличие, гарантия, авторизация, сертификация, точная модель, логистика.
- Если эквивалент не найден или unclear, а требования детальные, не ставь уверенность high без сильных оснований.
- Вердикт go/high разрешен только если ТЗ достаточно разобрано, категория ясна, срок поставки реалистичен и нет существенных коммерческих/поставочных рисков.
- Верни ответ строго в JSON без markdown.
""".strip()


ROUTERAI_ANALYSIS_SYSTEM_PROMPT = """
You are an IT tender presales analyst.

Pass 1: analyze the tender package and return a presales analysis artifact.
Use only facts from the tender package. In deep mode, prioritize a complete technical and commercial presales reading over brevity.
Return strict JSON only.
""".strip()


ROUTERAI_PACKAGING_SYSTEM_PROMPT = """
You are an IT tender report packager.

Pass 2: repackage the given analysis object into the final compact JSON report.
Do not add new facts, do not expand it into a full report, and do not include heavy sections.
Return strict JSON only.
""".strip()


LEAD_REPORT_SYSTEM_PROMPT = """
You are a customer-development analyst for an IT infrastructure integrator.

Treat the tender as a signal of a possible customer infrastructure story.
Focus on relationship-first outreach and account development.
Do not analyze bid participation and do not produce a technical TZ report.
Return strict JSON only.
""".strip()


def normalize_report_kind(value: str | None) -> str:
    raw = str(value or REPORT_KIND_TECHNICAL).strip().lower().replace("_", "-")
    aliases = {
        "": REPORT_KIND_TECHNICAL,
        "technical": REPORT_KIND_TECHNICAL,
        "tech": REPORT_KIND_TECHNICAL,
        "tz": REPORT_KIND_TECHNICAL,
        "presales": REPORT_KIND_TECHNICAL,
        "lead": REPORT_KIND_LEAD,
        "customer-lead": REPORT_KIND_LEAD,
        "customer": REPORT_KIND_LEAD,
    }
    if raw in aliases:
        return aliases[raw]
    raise ValueError(f"Unsupported report_kind: {value}")


def base_analysis_type_for_report_kind(report_kind: str | None) -> str:
    kind = normalize_report_kind(report_kind)
    if kind == REPORT_KIND_LEAD:
        return LLM_CUSTOMER_LEAD_REPORT_ANALYSIS_TYPE
    return LLM_PRESALES_REPORT_ANALYSIS_TYPE


def compact_report_enabled(provider: str) -> bool:
    return provider.strip().lower() == COMPACT_REPORT_PROVIDER


def build_routerai_analysis_user_prompt(
    context_markdown: str,
    *,
    analysis_depth: str = ANALYSIS_DEPTH_STANDARD,
) -> str:
    analysis_depth = normalize_analysis_depth(analysis_depth)
    if analysis_depth == ANALYSIS_DEPTH_DEEP:
        return f"""
Below is an LLM tender package.

Analyze the tender and return a deep presales analysis artifact as strict JSON.

Rules:
- Return exactly these top-level keys and no others: {", ".join(ROUTERAI_DEEP_ANALYSIS_FIELDS)}.
- decision must contain a go/maybe/no_go recommendation and the conditions that would change it.
- Use the tender package only. Do not invent facts, models, quantities, deadlines, registry status, or equivalence.
- If the package says that no technical specification / object description / equipment list was found, mark the analysis as preliminary and make the main action: request or download the technical specification.
- Give priority to the technical specification, object description, equipment list, and price specification; use contract, application, and other documents only for relevant risks and requirements.
- In domestic/registry/russian-platform context, do not recommend Dell, HPE, Lenovo, Huawei, Cisco, Supermicro, Inspur, or other foreign brands as the main offer. Recommend Russian, registry-listed, or tender-compliant equipment; mention foreign brands only as a risk or as an alternative that requires confirmed admissibility.
- Write practical presales content, not generic consulting language.
- Include concrete evidence or cautious "unclear" wording when a fact is not explicit.
- Keep TZ requirements separate from TZ problems: mandatory CPU/RAM/storage/network/software/controller/port/drive/certificate requirements belong to critical_tz_requirements or selection_parameters, not to conditions_to_reconsider.
- conditions_to_reconsider must contain only real contradictions, ambiguity, rejection risks, missing data, impossible or doubtful requirements, conflicting requirements, disputed national-regime/registry status, missing delivery term, no-equivalent restrictions, manufacturer-letter/local-engineer/experience/license/certificate barriers.
- Do not copy or rephrase critical_tz_requirements into conditions_to_reconsider. If no explicit TZ problems are found, say: "{DEFAULT_TZ_PROBLEMS_ITEM}"

Required section intent:
- summary: short human summary.
- what_is_procured: what is being purchased.
- supply_composition: delivery composition by positions, quantities, and equipment groups when available.
- critical_tz_requirements: mandatory TZ requirements that must be fulfilled; do not call them problems just because they are detailed.
- selection_parameters: ordinary technical parameters that influence model selection, including CPU, RAM, storage, network, controllers, ports, drives, software features, certificates, and licenses.
- tailoring_signals: signs of tailoring and how material they are.
- equivalence: allowed, not allowed, or unclear, with basis.
- domestic_registry: national regime, registry, Russian origin, certificates, and admissibility.
- delivery_term_and_feasibility: delivery term, source, and whether it is executable.
- what_can_be_offered: what the integrator can realistically offer.
- participation_strategies: viable participation strategies.
- what_presales_should_check: concrete checks for presales before go/no_go.
- questions_to_customer: what to clarify with the customer.
- manager_next_steps: what the manager can do today.
- conditions_to_reconsider: what must be confirmed for go/no_go to change.

LLM tender package:

{context_markdown}
""".strip()

    return f"""
Below is an LLM tender package.

Analyze the tender and return a compact but substantive JSON analysis object.

Rules:
- Return exactly these top-level keys and no others: {", ".join(ROUTERAI_ANALYSIS_FIELDS)}.
- verdict must be "go", "maybe", or "no_go"; confidence must be "low", "medium", or "high".
- Keep arrays practical and compact: usually 3-7 items.
- Do not include vendor_fit, margin_assessment, risk_map, role-based questions, technical_spec_analysis, commercial_assessment, contact, or other heavy sections.
- Base every field on the tender package. Do not invent facts.
- Write field values in the same language as the tender package when possible.
- next_action must be a concrete working plan for manager/presales.
- If the package contains a domestic/registry/russian-platform context section, do not recommend foreign vendors as the main offer; recommend Russian, registry-listed, or tender-compliant equipment and mention imported brands only as a risk or restriction.
- what_is_required means mandatory TZ parameters that must be fulfilled.
- problems_in_tz means only real TZ problems and uncertainties: contradictions, ambiguity, rejection risks, missing data, impossible/doubtful requirements, conflicting requirements, disputed national-regime/registry status, missing delivery term, no-equivalent restrictions, manufacturer-letter/local-engineer/experience/license/certificate barriers.
- Do not copy CPU/RAM/storage/network/software features, controllers, ports, drives, certificates, or other ordinary requirements into problems_in_tz unless the text explicitly explains a risk, conflict, impossibility, ambiguity, or barrier.
- Do not repeat what_is_required in problems_in_tz. If no explicit TZ problems are found, write: "{DEFAULT_TZ_PROBLEMS_ITEM}"

LLM tender package:

{context_markdown}
""".strip()


def build_routerai_packaging_user_prompt(
    analysis: dict[str, Any],
    *,
    analysis_depth: str = ANALYSIS_DEPTH_STANDARD,
) -> str:
    analysis_depth = normalize_analysis_depth(analysis_depth)
    analysis_json = json.dumps(analysis, ensure_ascii=False, indent=2)
    depth_rules = ""
    if analysis_depth == ANALYSIS_DEPTH_DEEP:
        depth_rules = """
- Preserve the deep analysis substance. Do not collapse concrete supply positions, critical TZ requirements, presales checks, or customer clarifications into one generic sentence.
- supply_composition should contain the delivery composition by positions or equipment groups.
- critical_tz_requirements should contain the most important technical and documentary requirements.
- what_presales_should_check should contain practical checks before a go/no_go decision.
- If a delivery term was found in the technical specification while the tender card had no delivery term, phrase delivery_feasibility cautiously and include "(found in TZ)" or equivalent wording.
""".rstrip()

    return f"""
Below is the pass 1 analysis object.

Package it into the final JSON report for persistence.

Rules:
- Return exactly these top-level keys and no others: {", ".join(ROUTERAI_FINAL_REPORT_FIELDS)}.
- verdict must be "go", "maybe", or "no_go"; confidence must be "low", "medium", or "high".
- Do not include vendor_fit, margin_assessment, risk_map, role-based questions, technical_spec_analysis, commercial_assessment, contact, or other heavy sections.
- Preserve the pass 1 meaning. Do not add facts.
- Keep the report compact and practical.
- Preserve domestic/registry guardrails if they are present: foreign vendors must not become the main offer.
- Put mandatory TZ parameters in critical_tz_requirements. This includes ordinary CPU/RAM/storage/network/software features, controllers, ports, drives, certificates, licenses, warranty, and documentation requirements.
- Put only actual TZ problems into problems_in_tz: contradictions, ambiguity, rejection risks, missing data, impossible/doubtful requirements, conflicting requirements, disputed national-regime/registry status, missing delivery term, no-equivalent restrictions, manufacturer-letter/local-engineer/experience/license/certificate barriers.
- Do not copy or rephrase critical_tz_requirements, what_is_required, supply_composition, or selection_parameters into problems_in_tz unless the item explicitly contains a risk, conflict, impossibility, ambiguity, or barrier.
- If no explicit TZ problems are present, set problems_in_tz to exactly one item: "{DEFAULT_TZ_PROBLEMS_ITEM}"
{depth_rules}

Pass 1 analysis object:

{analysis_json}
""".strip()


def build_lead_report_user_prompt(context_markdown: str) -> str:
    return f"""
Below is an LLM tender package.

Create a customer lead report as strict JSON.

Product intent:
- The tender is a signal about the customer, not a bid-participation task.
- Do not write a technical TZ report.
- Do not optimize for go/no_go in the tender procedure.
- Help a sales manager understand why this customer may be worth developing beyond this tender.

Rules:
- Return exactly these top-level keys and no others: {", ".join(LEAD_REPORT_FIELDS)}.
- document_status must be copied from the package document status. Do not invent
  document ids, counts, or preparation diagnostics.
- lead_priority must be one of: high, medium, low.
- confidence must be one of: high, medium, low.
- Use only facts from the tender package. If the package is thin, be explicit that the story is a hypothesis.
- Keep the report practical, compact, and relationship-first.
- Do not sell every tender as a good lead. If the signal is thin or documents
  are sparse, set confidence to low or medium, describe the hypothesis as weak,
  and make next_action cautious.
- If document_status.analysis_basis is "lead_card_only", confidence must be low
  or at most medium. State that conclusions are preliminary, avoid categorical
  claims about supply composition, platform, vendor lock-in, or exact technical
  requirements, and make next_action include checking or obtaining the TZ /
  technical documentation.
- If document_status.analysis_basis is "technical_document", analyze the primary
  technical document deeply: what is really procured; whether this is an
  infrastructure project or one-off supply; account-development signals; current
  platform/vendor; import-substitution or registry limits; servers, storage,
  network, virtualization, backup, and security infrastructure; technical
  customer; what to ask procurement; what to ask IT; and why this is or is not
  a lead.
- Treat the primary technical document as the factual basis for technical and
  platform conclusions when it is present.
- Do not invent a large data center, platform migration, infrastructure
  expansion, or strategic program unless it is supported by the tender package.
- For one-off small supplies, renewals, certificates, repairs, services, or
  peripheral/non-profile subjects, keep lead_priority low or medium and explain
  the limited account-development signal.
- Avoid procedure-first questions about bid submission, registry, equivalence, exact delivery feasibility, required bid documents, or manufacturer letters unless they are directly relevant to the customer story.
- possible_needs, target_roles, target_end_customer_roles, discovery_questions, and handoff_to_manager_when must be arrays.
- opening_phrase must be a soft ready-to-use first-contact phrase for Telegram, not a tender/procedure opener.
- Do not open with "we saw your procurement/procedure" or with modernization of exact server models, serial numbers, or product lines.
- Match opening_phrase to the primary signal: storage/СХД/data repository/ЦОМД -> infrastructure for data storage / СХД / data; network -> network infrastructure; security/ПАК ИБ -> protected infrastructure / ИБ; virtualization/ГосТех/platform/migration -> platform, virtualization, and infrastructure support; generic server -> server infrastructure.
- Put specific tender details into customer_signal or likely_customer_story; do not push them into opening_phrase.
- discovery_questions must be relationship-first, Telegram-facing, and at most 3 items.
- target_roles and target_end_customer_roles must be compact: 3-4 unique roles, no duplicate or overly generic repeats.
- If the customer looks like a procurement center, procurement_contact_role is the entry point; target_end_customer_roles should name the IT responsible person, technical customer, or regional IT curator at the end customer.
- next_action must be call-first by default: "{LEAD_DEFAULT_NEXT_ACTION}"
- Do not make LinkedIn, social networks, or sending a Telegram message the default next_action.
- LinkedIn/social networks may be mentioned only as an additional contact-search source, not as the primary action.

Field intent:
- lead_summary: one short human summary of the lead opportunity.
- customer_signal: what this purchase says about the customer.
- likely_customer_story: the likely IT or infrastructure pain behind the purchase.
- possible_needs: adjacent infrastructure or IT tasks that may be nearby.
- target_roles: who to find inside the organization.
- procurement_contact_role: optional role of the purchasing/procurement contact as an entry point.
- target_end_customer_roles: optional end-customer IT or technical roles to reach, especially for procurement centers.
- opening_phrase: how to start the conversation.
- discovery_questions: what to learn on the first contact.
- handoff_to_manager_when: conditions for sending this to a manager.
- lead_priority: high, medium, or low.
- confidence: high, medium, or low confidence in the lead story.
- why_priority: why this priority was chosen.
- next_action: immediate call-first action for the operator or salesperson.

LLM tender package:

{context_markdown}
""".strip()


def build_user_prompt(context_markdown: str) -> str:
    return f"""
Ниже дан LLM-пакет по закупке.

Проанализируй закупку и верни строго JSON такого вида.

Правила заполнения:
- Все поля должны быть привязаны к данным из LLM-пакета.
- Если в technical_spec_analysis.what_is_required есть конкретные требования, не пиши в problems_in_tz, что технические характеристики не указаны.
- critical_requirements / critical_tz_requirements / technical_spec_analysis.what_is_required - это обязательные требования ТЗ, которые надо выполнить; обычные CPU/RAM/Storage/Network/software features, контроллеры, порты, накопители, сертификаты и лицензии не являются проблемами сами по себе.
- problems_in_tz - только реальные проблемы и неясности ТЗ: противоречия, неясности, риски отклонения, отсутствующие данные, невозможные/сомнительные требования, конфликтующие требования, спорный нацрежим/реестр, отсутствие срока, запрет или неясность эквивалента, письмо производителя, локальный инженер, опыт/лицензии/сертификаты как барьер.
- Не повторяй critical_requirements / what_is_required в problems_in_tz. Если явных проблем нет, пиши один пункт: "{DEFAULT_TZ_PROBLEMS_ITEM}"
- Если точных числовых характеристик мало, пиши так: "в предоставленном фрагменте не хватает части числовых параметров", а не обнуляй весь анализ.
- questions_to_customer должны быть практическими для пресейла: эквивалент, срок поставки, гарантия, комплектация, требуемые документы, возможность аналога, критичные параметры.
- next_action должен быть рабочим планом для менеджера/пресейла, а не общей рекомендацией провести встречу.
- Для СХД в what_presales_should_check добавь проверку поставляемости, совместимости, сроков 30 к.д., маржи, гарантии и сертификатов.
- Если в LLM-пакете обнаружен отечественный/реестровый/российско-платформенный контекст, НЕ предлагай Dell, HPE, HP, Lenovo, Huawei, Cisco, Supermicro, Inspur и другие иностранные бренды как основной вариант. В what_to_offer пиши российское/реестровое оборудование; импортные бренды допустимы только как риск или вариант, требующий подтверждения допустимости иностранного происхождения.

JSON-схема:

{{
  "summary": "Краткое человеческое описание, что закупается",
  "technical_spec_analysis": {{
    "what_is_required": ["..."],
    "key_characteristics": ["..."],
    "mentioned_models_or_vendors": ["..."],
    "equivalent_allowed": "yes | no | unclear",
    "implementation_or_services_required": ["..."]
  }},
  "delivery_feasibility": {{
    "delivery_term": "...",
    "assessment": "realistic | risky | unclear",
    "reasoning": ["..."]
  }},
  "tailoring_risk": {{
    "level": "low | medium | high | unknown",
    "reasons": ["..."],
    "evidence": ["короткие цитаты или пересказ фрагментов из ТЗ"]
  }},
  "potential_offer": {{
    "what_to_offer": ["..."],
    "possible_analogs_or_approach": ["..."],
    "what_presales_should_check": ["..."]
  }},
  "problems_in_tz": ["..."],
  "questions_to_customer": ["..."],
  "contact": {{
    "person": "...",
    "phone": "...",
    "email": "..."
  }},
  "commercial_assessment": {{
    "pros": ["..."],
    "risks": ["..."],
    "fit_for_integrator": "good | medium | poor | unclear"
  }},
  "vendor_fit": {{
    "candidate_vendors_or_lines": [
      {{
        "vendor_or_line": "...",
        "why_may_fit": "...",
        "what_to_verify": ["..."],
        "risk": "low | medium | high"
      }}
    ],
    "notes": ["Не утверждай точное соответствие без ручной проверки ТЗ"]
  }},
  "risk_map": {{
    "tailoring": {{"level": "low | medium | high | unknown", "evidence": ["..."]}},
    "delivery": {{"level": "low | medium | high | unknown", "evidence": ["..."]}},
    "commercial": {{"level": "low | medium | high | unknown", "evidence": ["..."]}},
    "legal_or_registry": {{"level": "low | medium | high | unknown", "evidence": ["..."]}},
    "missing_data": ["..."]
  }},
  "margin_assessment": {{
    "expected_margin": "good | medium | thin | unknown",
    "margin_risks": ["..."],
    "what_to_request_from_supplier": ["..."]
  }},
  "questions": {{
    "to_customer": ["..."],
    "to_presales_engineer": ["..."],
    "to_supplier_or_distributor": ["..."]
  }},
  "recommendation": {{
    "decision": "go | maybe | no_go",
    "confidence": "low | medium | high",
    "reasoning": ["..."]
  }},
  "next_action": ["..."]
}}

LLM-пакет:

{context_markdown}
""".strip()


def extract_json(text: str) -> dict[str, Any]:
    raw = text.strip()

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()

    try:
        return json.loads(raw)
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")

    if start != -1 and end != -1 and end > start:
        return json.loads(raw[start : end + 1])

    raise RuntimeError(f"Cannot parse JSON from LLM response: {text[:2000]}")


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


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
    if isinstance(value, (list, tuple)):
        return [text for item in value if (text := text_value(item))]
    text = text_value(value)
    return [text] if text else []


def offer_list(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return string_list(value)

    result: list[str] = []
    for key in (
        "what_to_offer",
        "possible_analogs_or_approach",
        "what_to_verify",
        "what_presales_should_check",
    ):
        result.extend(string_list(value.get(key)))

    return result or string_list(value)


def delivery_text(value: Any) -> str:
    if not isinstance(value, dict):
        return text_value(value)

    parts = [
        text_value(value.get("delivery_term")),
        text_value(value.get("assessment")),
        "; ".join(string_list(value.get("reasoning"))),
    ]
    return " | ".join(part for part in parts if part)


def _get_report_value(report: dict[str, Any], fallback: dict[str, Any], field: str) -> Any:
    value = report.get(field)
    return fallback.get(field) if value is None else value


def has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def first_report_value(*values: Any) -> Any:
    for value in values:
        if has_value(value):
            return value
    return None


def normalize_routerai_analysis(
    report: dict[str, Any],
    *,
    analysis_depth: str = ANALYSIS_DEPTH_STANDARD,
) -> dict[str, Any]:
    recommendation = report.get("recommendation")
    recommendation = recommendation if isinstance(recommendation, dict) else {}

    decision = report.get("decision")
    if isinstance(decision, dict):
        decision_value = decision.get("decision") or decision.get("verdict")
        decision_confidence = decision.get("confidence")
    else:
        decision_value = decision
        decision_confidence = None

    analysis = {
        "summary": report.get("summary") or "",
        "what_is_required": ensure_list(
            first_report_value(
                report.get("what_is_required"),
                report.get("critical_tz_requirements"),
                report.get("what_is_procured"),
            )
        ),
        "top_risks": ensure_list(
            first_report_value(
                report.get("top_risks"),
                report.get("tailoring_signals"),
                report.get("domestic_registry"),
            )
        ),
        "delivery_feasibility": first_report_value(
            report.get("delivery_feasibility"),
            report.get("delivery_term_and_feasibility"),
        ) or {},
        "potential_offer": first_report_value(
            report.get("potential_offer"),
            report.get("what_can_be_offered"),
        ) or {},
        "problems_in_tz": ensure_list(
            first_report_value(
                report.get("problems_in_tz"),
                report.get("selection_parameters"),
                report.get("conditions_to_reconsider"),
            )
        ),
        "questions_to_customer": ensure_list(report.get("questions_to_customer")),
        "next_action": ensure_list(
            first_report_value(report.get("next_action"), report.get("manager_next_steps"))
        ),
        "verdict": report.get("verdict") or recommendation.get("decision") or decision_value or "maybe",
        "confidence": (
            report.get("confidence")
            or recommendation.get("confidence")
            or decision_confidence
            or "low"
        ),
    }

    if normalize_analysis_depth(analysis_depth) == ANALYSIS_DEPTH_STANDARD:
        return {field: analysis[field] for field in ROUTERAI_ANALYSIS_FIELDS}

    deep_analysis = dict(report)
    deep_analysis.update({field: analysis[field] for field in ROUTERAI_ANALYSIS_FIELDS})
    for field in ROUTERAI_DEEP_ANALYSIS_FIELDS:
        deep_analysis.setdefault(field, [] if field not in {"summary", "decision"} else "")
    return deep_analysis


def normalize_routerai_final_report(
    report: dict[str, Any],
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fallback = fallback or {}
    recommendation = report.get("recommendation")
    recommendation = recommendation if isinstance(recommendation, dict) else {}

    verdict = (
        report.get("verdict")
        or recommendation.get("decision")
        or fallback.get("verdict")
        or "maybe"
    )
    confidence = (
        report.get("confidence")
        or recommendation.get("confidence")
        or fallback.get("confidence")
        or "low"
    )

    compact = {
        "summary": text_value(_get_report_value(report, fallback, "summary")),
        "verdict": text_value(verdict),
        "confidence": text_value(confidence),
        "what_is_required": string_list(_get_report_value(report, fallback, "what_is_required")),
        "supply_composition": string_list(
            first_report_value(
                report.get("supply_composition"),
                fallback.get("supply_composition"),
            )
        ),
        "critical_tz_requirements": string_list(
            first_report_value(
                report.get("critical_tz_requirements"),
                fallback.get("critical_tz_requirements"),
                report.get("what_is_required"),
                fallback.get("what_is_required"),
            )
        ),
        "top_risks": string_list(_get_report_value(report, fallback, "top_risks")),
        "delivery_feasibility": delivery_text(_get_report_value(report, fallback, "delivery_feasibility")),
        "potential_offer": offer_list(_get_report_value(report, fallback, "potential_offer")),
        "what_presales_should_check": string_list(
            first_report_value(
                report.get("what_presales_should_check"),
                fallback.get("what_presales_should_check"),
                report.get("presales_checks"),
                fallback.get("presales_checks"),
            )
        ),
        "problems_in_tz": string_list(_get_report_value(report, fallback, "problems_in_tz")),
        "questions_to_customer": string_list(_get_report_value(report, fallback, "questions_to_customer")),
        "next_action": string_list(_get_report_value(report, fallback, "next_action")),
    }

    return {field: compact[field] for field in ROUTERAI_FINAL_REPORT_FIELDS}


def normalize_lead_priority(value: Any) -> str:
    priority = text_value(value).strip().lower()
    if priority in {"high", "medium", "low"}:
        return priority
    return "medium"


def lead_recommendation_from_priority(value: Any) -> str:
    return LEAD_PRIORITY_RECOMMENDATION[normalize_lead_priority(value)]


def normalize_lead_confidence(value: Any, *, lead_priority: str | None = None) -> str:
    confidence = text_value(value).strip().lower()
    if confidence in {"high", "medium", "low"}:
        return confidence
    return "low" if normalize_lead_priority(lead_priority) == "low" else "medium"


def lead_package_is_card_only(package: dict[str, Any] | None) -> bool:
    if not package:
        return False
    meta = package.get("meta") or {}
    if not isinstance(meta, dict):
        return False
    document_status = package.get("document_status") or meta.get("document_status") or {}
    if isinstance(document_status, dict):
        if document_status.get("analysis_basis") == "lead_card_only":
            return True
    return meta.get("lead_context_mode") == "card_only" or bool(
        meta.get("lead_documents_missing_allowed")
    )


def downgrade_lead_confidence_for_light_context(confidence: str) -> str:
    return {
        "high": "medium",
        "medium": "low",
        "low": "low",
    }.get(confidence, "low")


def lead_document_status_from_package(package: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(package, dict):
        return build_document_status()
    status = package.get("document_status")
    if isinstance(status, dict) and status.get("code"):
        return status
    meta = package.get("meta") or {}
    meta = meta if isinstance(meta, dict) else {}
    return build_document_status(
        document_selection=package.get("document_selection") or meta.get("document_selection"),
        document_preparation=meta.get("document_preparation"),
        documents_summary=package.get("documents_summary") or [],
    )


def parse_document_readiness_json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cannot parse document readiness JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Document readiness JSON must be an object")
    return parsed


def _lead_normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", text_value(value).casefold().replace("\u0451", "\u0435")).strip()


def _lead_string_corpus(*values: Any, limit: int = 30000) -> str:
    parts: list[str] = []

    def collect(value: Any) -> None:
        if len(" ".join(parts)) > limit:
            return
        if isinstance(value, dict):
            for nested in value.values():
                collect(nested)
            return
        if isinstance(value, (list, tuple, set)):
            for nested in value:
                collect(nested)
            return
        text = text_value(value).strip()
        if text:
            parts.append(text)

    for value in values:
        collect(value)

    return _lead_normalized_text(" ".join(parts)[:limit])


def lead_category_from_context(
    report: dict[str, Any],
    package: dict[str, Any] | None = None,
) -> str:
    package = package or {}
    tender = package.get("tender") or {}
    docs = package.get("technical_spec_documents") or []
    corpus = _lead_string_corpus(
        tender.get("title"),
        tender.get("customer_name"),
        report,
        docs[:3] if isinstance(docs, list) else docs,
    )

    groups = {
        "storage": (
            "\u0441\u0445\u0434",
            "\u0445\u0440\u0430\u043d\u0435\u043d\u0438\u0435 \u0434\u0430\u043d\u043d\u044b\u0445",
            "\u0445\u0440\u0430\u043d\u0435\u043d\u0438\u044e \u0434\u0430\u043d\u043d\u044b\u0445",
            "\u0440\u0435\u043f\u043e\u0437\u0438\u0442\u043e\u0440\u0438\u0439 \u0434\u0430\u043d\u043d\u044b\u0445",
            "\u0440\u0435\u043f\u043e\u0437\u0438\u0442\u043e\u0440\u0438\u044f \u0434\u0430\u043d\u043d\u044b\u0445",
            "\u0446\u043e\u043c\u0434",
            "\u0446\u0435\u043d\u0442\u0440 \u0445\u0440\u0430\u043d\u0435\u043d\u0438\u044f",
            "\u0446\u0435\u043d\u0442\u0440 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438 \u0434\u0430\u043d\u043d\u044b\u0445",
            "storage",
            "data repository",
            "san",
            "nas",
        ),
        "network": (
            "\u0441\u0435\u0442\u0435\u0432\u0430\u044f \u0438\u043d\u0444\u0440\u0430\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0430",
            "\u0441\u0435\u0442\u0435\u0432\u043e\u0435 \u043e\u0431\u043e\u0440\u0443\u0434\u043e\u0432\u0430\u043d\u0438\u0435",
            "\u043a\u043e\u043c\u043c\u0443\u0442\u0430\u0442\u043e\u0440",
            "\u043c\u0430\u0440\u0448\u0440\u0443\u0442\u0438\u0437\u0430\u0442\u043e\u0440",
            "network",
            "switch",
            "router",
        ),
        "security": (
            "\u0438\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u043e\u043d\u043d\u0430\u044f \u0431\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u043e\u0441\u0442\u044c",
            "\u0438\u0431",
            "\u043c\u0435\u0436\u0441\u0435\u0442\u0435\u0432\u043e\u0439 \u044d\u043a\u0440\u0430\u043d",
            "\u0441\u0437\u0438",
            "security",
            "firewall",
            "ngfw",
            "vpn",
            "\u043f\u0430\u043a \u0438\u0431",
            "\u0441\u043a\u0437\u0438",
        ),
        "platform": (
            "\u0432\u0438\u0440\u0442\u0443\u0430\u043b\u0438\u0437\u0430\u0446",
            "\u0433\u043e\u0441\u0442\u0435\u0445",
            "\u0433\u043e\u0441 \u0442\u0435\u0445",
            "\u043f\u043b\u0430\u0442\u0444\u043e\u0440\u043c",
            "\u043c\u0438\u0433\u0440\u0430\u0446",
            "virtualization",
            "virtualisation",
            "vmware",
            "kubernetes",
            "openshift",
            "migration",
        ),
        "server": (
            "\u0441\u0435\u0440\u0432\u0435\u0440",
            "\u0441\u0435\u0440\u0432\u0435\u0440\u043d",
            "\u0432\u044b\u0447\u0438\u0441\u043b\u0438\u0442\u0435\u043b\u044c\u043d",
            "server",
            "depo storm",
        ),
    }

    title = _lead_normalized_text(tender.get("title"))
    for category in ("storage", "network", "security", "platform", "server"):
        if any(marker in title for marker in groups[category]):
            return category

    scores = {
        category: sum(corpus.count(marker) for marker in markers)
        for category, markers in groups.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "generic"


def lead_opening_fallback(category: str) -> str:
    templates = {
        "server": (
            "Добрый день. Подскажите, пожалуйста, кто у вас отвечает за "
            "серверную инфраструктуру и ее развитие? Мы занимаемся "
            "инфраструктурными проектами, поддержкой и модернизацией "
            "серверных платформ, хотели бы коротко познакомиться и понять, "
            "можем ли быть полезны по будущим задачам."
        ),
        "storage": (
            "Добрый день. Подскажите, пожалуйста, кто у вас отвечает за "
            "инфраструктуру хранения данных и развитие СХД? Мы занимаемся "
            "инфраструктурными проектами, поддержкой и модернизацией "
            "платформ хранения, хотели бы коротко познакомиться и понять, "
            "можем ли быть полезны по будущим задачам."
        ),
        "network": (
            "Добрый день. Подскажите, пожалуйста, кто у вас отвечает за "
            "сетевую инфраструктуру и ее развитие? Мы занимаемся "
            "инфраструктурными проектами, поддержкой и модернизацией "
            "сетевых платформ, хотели бы коротко познакомиться и понять, "
            "можем ли быть полезны по будущим задачам."
        ),
        "security": (
            "Добрый день. Подскажите, пожалуйста, кто у вас отвечает за "
            "защищенную инфраструктуру и ИБ? Мы занимаемся "
            "инфраструктурными проектами, защитой и поддержкой "
            "ИТ-платформ, хотели бы коротко познакомиться и понять, "
            "можем ли быть полезны по будущим задачам."
        ),
        "platform": (
            "Добрый день. Подскажите, пожалуйста, кто у вас отвечает за "
            "платформу, виртуализацию и инфраструктурное сопровождение? "
            "Мы занимаемся инфраструктурными проектами, поддержкой и "
            "миграцией платформ, хотели бы коротко познакомиться и понять, "
            "можем ли быть полезны по будущим задачам."
        ),
        "generic": (
            "Добрый день. Подскажите, пожалуйста, кто у вас отвечает за "
            "ИТ-инфраструктуру и ее развитие? Мы занимаемся "
            "инфраструктурными проектами, поддержкой и модернизацией "
            "ИТ-платформ, хотели бы коротко познакомиться и понять, "
            "можем ли быть полезны по будущим задачам."
        ),
    }
    return templates.get(category, templates["generic"])


def lead_opening_looks_procedure_first(value: Any) -> bool:
    text = _lead_normalized_text(value)
    if not text:
        return True
    markers = (
        "\u043f\u043e \u0432\u0430\u0448\u0435\u0439 \u0437\u0430\u043a\u0443\u043f\u043a\u0435",
        "\u043f\u043e \u0437\u0430\u043a\u0443\u043f\u043a\u0435",
        "\u043f\u043e \u043f\u0440\u043e\u0446\u0435\u0434\u0443\u0440\u0435",
        "\u0432\u0430\u0448\u0443 \u0437\u0430\u043a\u0443\u043f\u043a\u0443",
        "\u0443\u0432\u0438\u0434\u0435\u043b\u0438 \u0432\u0430\u0448\u0443 \u0437\u0430\u043a\u0443\u043f\u043a\u0443",
        "your procurement",
        "your tender",
        "this tender",
        "this procedure",
        "depo storm",
        "\u0441\u0435\u0440\u0438\u0439\u043d",
        "serial number",
    )
    if any(marker in text for marker in markers):
        return True
    if (
        "\u043f\u043b\u0430\u043d\u0438\u0440" in text
        and "\u043c\u043e\u0434\u0435\u0440\u043d\u0438\u0437\u0430\u0446" in text
        and "\u0441\u0435\u0440\u0432\u0435\u0440" in text
    ):
        return True
    return bool(re.search(r"\b(?:\d{6,}|[a-z]+-\d{3,})\b", text) and "\u0437\u0430\u043a\u0443\u043f" in text)


def lead_opening_looks_too_generic_for_category(value: Any, *, category: str) -> bool:
    if category in {"", "generic", "server"}:
        return False

    text = _lead_normalized_text(value)
    if not text:
        return True

    category_markers = {
        "storage": (
            "\u0441\u0445\u0434",
            "\u0445\u0440\u0430\u043d\u0435\u043d",
            "\u0434\u0430\u043d\u043d",
            "storage",
            "repository",
        ),
        "network": (
            "\u0441\u0435\u0442\u0435\u0432",
            "\u043a\u043e\u043c\u043c\u0443\u0442\u0430\u0442",
            "\u043c\u0430\u0440\u0448\u0440\u0443\u0442",
            "network",
        ),
        "security": (
            "\u0438\u0431",
            "\u0431\u0435\u0437\u043e\u043f\u0430\u0441",
            "\u0437\u0430\u0449\u0438\u0449",
            "security",
            "firewall",
        ),
        "platform": (
            "\u043f\u043b\u0430\u0442\u0444\u043e\u0440\u043c",
            "\u0432\u0438\u0440\u0442\u0443\u0430\u043b",
            "\u043c\u0438\u0433\u0440\u0430\u0446",
            "\u0433\u043e\u0441\u0442\u0435\u0445",
            "platform",
            "virtual",
            "migration",
        ),
    }.get(category, ())
    if any(marker in text for marker in category_markers):
        return False

    generic_markers = (
        "\u0441\u0435\u0440\u0432\u0435\u0440\u043d",
        "\u0438\u0442-\u0438\u043d\u0444\u0440\u0430",
        "\u0438\u0442 \u0438\u043d\u0444\u0440\u0430",
        "it infrastructure",
        "infrastructure",
    )
    return any(marker in text for marker in generic_markers)


def clean_lead_opening_phrase(
    value: Any,
    *,
    category: str,
) -> str:
    phrase = text_value(value).strip()
    if (
        lead_opening_looks_procedure_first(phrase)
        or lead_opening_looks_too_generic_for_category(phrase, category=category)
    ):
        return lead_opening_fallback(category)
    return phrase


def lead_question_looks_procedure_first(value: Any) -> bool:
    text = _lead_normalized_text(value)
    markers = (
        "\u0441\u0440\u043e\u043a \u043f\u043e\u0441\u0442\u0430\u0432\u043a",
        "\u043f\u043e\u0434\u0430\u0447\u0430 \u0437\u0430\u044f\u0432\u043a",
        "\u043f\u043e\u0434\u0430\u0447\u0438 \u0437\u0430\u044f\u0432\u043a",
        "\u0437\u0430\u044f\u0432\u043a\u0443 \u043d\u0430 \u0443\u0447\u0430\u0441\u0442\u0438\u0435",
        "\u0440\u0435\u0435\u0441\u0442\u0440",
        "\u043f\u043f 616",
        "\u043f\u043f 719",
        "\u043f\u043f 1875",
        "\u043f\u0438\u0441\u044c\u043c\u043e \u043f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0438\u0442\u0435\u043b",
        "\u0441\u0435\u0440\u0438\u0439\u043d",
        "\u043a\u0442\u043e \u0431\u0443\u0434\u0435\u0442 \u043f\u0440\u0438\u043d\u0438\u043c\u0430\u0442\u044c \u0440\u0430\u0431\u043e\u0442",
        "\u043f\u0440\u0438\u0435\u043c\u043a",
        "\u043f\u0440\u0438\u0435\u043c\u043a\u0430",
        "bid submission",
        "manufacturer letter",
        "serial number",
        "delivery term",
    )
    if any(marker in text for marker in markers):
        return True
    if "\u0441\u0440\u043e\u043a" in text and "\u043f\u043e\u0441\u0442\u0430\u0432" in text:
        return True
    if "\u043f\u043e\u0434\u0430\u0447" in text and "\u0437\u0430\u044f\u0432" in text:
        return True
    return re.search(r"\b\u043f\u043f\s*(616|719|1875)\b", text) is not None


def lead_discovery_question_fallbacks(category: str) -> list[str]:
    subject = {
        "server": "\u0441\u0435\u0440\u0432\u0435\u0440\u043d\u0443\u044e \u0438\u043d\u0444\u0440\u0430\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0443",
        "storage": "\u0445\u0440\u0430\u043d\u0435\u043d\u0438\u0435 \u0434\u0430\u043d\u043d\u044b\u0445 \u0438 \u0421\u0425\u0414",
        "network": "\u0441\u0435\u0442\u0435\u0432\u0443\u044e \u0438\u043d\u0444\u0440\u0430\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0443",
        "security": "\u0437\u0430\u0449\u0438\u0449\u0435\u043d\u043d\u0443\u044e \u0438\u043d\u0444\u0440\u0430\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0443 / \u0418\u0411",
        "platform": "\u043f\u043b\u0430\u0442\u0444\u043e\u0440\u043c\u0443, \u0432\u0438\u0440\u0442\u0443\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u044e \u0438 \u0438\u043d\u0444\u0440\u0430\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u043d\u043e\u0435 \u0441\u043e\u043f\u0440\u043e\u0432\u043e\u0436\u0434\u0435\u043d\u0438\u0435",
    }.get(category, "\u0418\u0422-\u0438\u043d\u0444\u0440\u0430\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0443")
    return [
        f"\u041a\u0442\u043e \u0432\u043d\u0443\u0442\u0440\u0438 \u043e\u0440\u0433\u0430\u043d\u0438\u0437\u0430\u0446\u0438\u0438 \u043e\u0442\u0432\u0435\u0447\u0430\u0435\u0442 \u0437\u0430 {subject}?",
        "\u0415\u0441\u0442\u044c \u043b\u0438 \u0440\u044f\u0434\u043e\u043c \u043f\u043b\u0430\u043d\u044b \u043f\u043e \u0440\u0430\u0437\u0432\u0438\u0442\u0438\u044e, \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044e \u0438\u043b\u0438 \u0441\u043e\u043f\u0440\u043e\u0432\u043e\u0436\u0434\u0435\u043d\u0438\u044e \u044d\u0442\u043e\u0439 \u0438\u043d\u0444\u0440\u0430\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u044b?",
        "\u041c\u043e\u0436\u043d\u043e \u043b\u0438 \u043a\u043e\u0440\u043e\u0442\u043a\u043e \u043f\u043e\u0437\u043d\u0430\u043a\u043e\u043c\u0438\u0442\u044c\u0441\u044f \u0441 \u0418\u0422-\u043e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u043c \u0438\u043b\u0438 \u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u043c \u0437\u0430\u043a\u0430\u0437\u0447\u0438\u043a\u043e\u043c?",
    ]


def clean_lead_discovery_questions(value: Any, *, category: str) -> list[str]:
    questions: list[str] = []
    seen: set[str] = set()
    for question in string_list(value):
        if lead_question_looks_procedure_first(question):
            continue
        text = question.strip()
        if not text:
            continue
        if text[-1] not in ".!?":
            text += "?"
        key = _lead_normalized_text(text)
        if key in seen:
            continue
        seen.add(key)
        questions.append(text)
        if len(questions) >= 3:
            break
    return questions or lead_discovery_question_fallbacks(category)


def _lead_role_key(value: Any) -> str:
    text = _lead_normalized_text(value)
    text = text.replace("information technology", "it")
    text = re.sub(r"[\s/|,;:().]+", " ", text)
    return text.strip()


def clean_lead_roles(value: Any, *, limit: int = 4) -> list[str]:
    raw_roles = [role.strip(" \t\r\n-.,;:") for role in string_list(value)]
    compact: list[tuple[str, str]] = []
    seen: set[str] = set()
    for role in raw_roles:
        if not role:
            continue
        key = _lead_role_key(role)
        if not key or key in seen:
            continue
        seen.add(key)
        compact.append((role, key))

    keys = [key for _, key in compact]
    generic_keys = {
        "it",
        "ит",
        "заказчик",
        "контакт",
        "ответственный",
        "закупки",
        "закупочный контакт",
        "procurement",
        "procurement contact",
        "customer",
        "technical contact",
    }

    filtered: list[str] = []
    for role, key in compact:
        has_more_specific = any(
            other != key and key in other
            for other in keys
        )
        if key in generic_keys and has_more_specific:
            continue
        if len(key) < 18 and has_more_specific:
            continue
        filtered.append(role)
        if len(filtered) >= limit:
            break

    return filtered


def lead_next_action_looks_non_call_first(value: Any) -> bool:
    text = _lead_normalized_text(value)
    if not text:
        return True

    first_clause = re.split(r"[.;\n]", text, maxsplit=1)[0][:180]
    call_markers = (
        "позвон",
        "звон",
        "созвон",
        "call",
        "phone",
        "telephone",
        "закупочн",
    )
    search_markers = (
        "linkedin",
        "линкедин",
        "соцсет",
        "social network",
        "telegram",
        "телеграм",
    )
    telegram_message_markers = (
        "telegram-сообщ",
        "telegram сообщ",
        "сообщение в telegram",
        "сообщение в телеграм",
        "написать в telegram",
        "написать в телеграм",
        "отправить telegram",
        "отправить телеграм",
        "send telegram",
        "telegram message",
    )

    first_clause_has_call = any(marker in first_clause for marker in call_markers)
    if any(marker in first_clause for marker in search_markers) and not first_clause_has_call:
        return True
    if any(marker in text for marker in search_markers) and not first_clause_has_call:
        return True
    if any(marker in first_clause for marker in telegram_message_markers):
        return True
    return False


def clean_lead_next_action(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        action = "; ".join(string_list(value)).strip()
    else:
        action = text_value(value).strip()

    if lead_next_action_looks_non_call_first(action):
        return LEAD_DEFAULT_NEXT_ACTION
    return action


def _append_unique_strings(items: list[str], additions: list[str]) -> list[str]:
    result = list(items)
    seen = {_lead_normalized_text(item) for item in result if item.strip()}
    for addition in additions:
        key = _lead_normalized_text(addition)
        if key and key not in seen:
            result.append(addition)
            seen.add(key)
    return result


def lead_customer_looks_procurement_center(
    report: dict[str, Any],
    package: dict[str, Any] | None = None,
) -> bool:
    package = package or {}
    tender = package.get("tender") or {}
    text = _lead_string_corpus(
        tender.get("customer_name"),
        tender.get("title"),
        report.get("procurement_contact_role"),
        report.get("target_roles"),
        report.get("customer_signal"),
    )
    if "procurement center" in text or "purchasing department" in text:
        return True
    procurement_markers = (
        "\u043a\u043e\u043c\u0438\u0442\u0435\u0442",
        "\u0446\u0435\u043d\u0442\u0440",
        "\u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d",
        "\u0434\u0435\u043f\u0430\u0440\u0442\u0430\u043c\u0435\u043d\u0442",
        "\u0441\u043b\u0443\u0436\u0431\u0430",
        "\u0434\u0438\u0440\u0435\u043a\u0446",
        "\u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u043d",
        "\u0433\u043e\u0441\u0437\u0430\u043a\u0443\u043f",
    )
    return "\u0437\u0430\u043a\u0443\u043f" in text and any(marker in text for marker in procurement_markers)


def normalize_lead_report(
    report: dict[str, Any],
    *,
    package: dict[str, Any] | None = None,
) -> dict[str, Any]:
    category = lead_category_from_context(report, package)
    document_status = (
        report.get("document_status")
        if isinstance(report.get("document_status"), dict)
        and report.get("document_status", {}).get("code")
        else lead_document_status_from_package(package)
    )
    priority = normalize_lead_priority(report.get("lead_priority"))
    recommendation = report.get("recommendation")
    recommendation = recommendation if isinstance(recommendation, dict) else {}
    confidence = normalize_lead_confidence(
        first_report_value(report.get("confidence"), recommendation.get("confidence")),
        lead_priority=priority,
    )
    if lead_package_is_card_only(package):
        confidence = downgrade_lead_confidence_for_light_context(confidence)
    normalized = {
        "document_status": document_status,
        "lead_summary": text_value(first_report_value(report.get("lead_summary"), report.get("summary"))),
        "customer_signal": text_value(report.get("customer_signal")),
        "likely_customer_story": text_value(report.get("likely_customer_story")),
        "possible_needs": string_list(report.get("possible_needs")),
        "target_roles": clean_lead_roles(report.get("target_roles"), limit=4),
        "procurement_contact_role": text_value(report.get("procurement_contact_role")),
        "target_end_customer_roles": clean_lead_roles(
            report.get("target_end_customer_roles"),
            limit=4,
        ),
        "opening_phrase": clean_lead_opening_phrase(
            first_report_value(report.get("opening_phrase"), report.get("first_phrase")),
            category=category,
        ),
        "discovery_questions": clean_lead_discovery_questions(
            report.get("discovery_questions"),
            category=category,
        ),
        "handoff_to_manager_when": string_list(report.get("handoff_to_manager_when")),
        "lead_priority": priority,
        "confidence": confidence,
        "why_priority": text_value(report.get("why_priority")),
        "next_action": clean_lead_next_action(report.get("next_action")),
    }

    if lead_customer_looks_procurement_center(report, package):
        if not normalized["procurement_contact_role"]:
            normalized["procurement_contact_role"] = (
                "\u0437\u0430\u043a\u0443\u043f\u043e\u0447\u043d\u044b\u0439 "
                "\u043a\u043e\u043d\u0442\u0430\u043a\u0442 - \u0432\u0445\u043e\u0434; "
                "\u0446\u0435\u043b\u044c - \u0432\u044b\u0439\u0442\u0438 \u043d\u0430 "
                "\u0418\u0422-\u043e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d\u043e\u0433\u043e "
                "\u043a\u043e\u043d\u0435\u0447\u043d\u043e\u0433\u043e "
                "\u043f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u044f"
            )
        normalized["target_end_customer_roles"] = clean_lead_roles(
            _append_unique_strings(
                normalized["target_end_customer_roles"],
                [
                "\u0418\u0422-\u043e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u0439 "
                "\u043a\u043e\u043d\u0435\u0447\u043d\u043e\u0433\u043e "
                "\u043f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u044f",
                "\u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0437\u0430\u043a\u0430\u0437\u0447\u0438\u043a",
                "\u0440\u0435\u0433\u0438\u043e\u043d\u0430\u043b\u044c\u043d\u044b\u0439 "
                "\u0418\u0422-\u043a\u0443\u0440\u0430\u0442\u043e\u0440",
                ],
            ),
            limit=4,
        )

    return {field: normalized[field] for field in LEAD_REPORT_FIELDS}





def normalize_detected_category(value: Any) -> str:
    category = str(value or "").strip().lower()

    aliases = {
        "servers": "server",
        "security_hardware": "security",
        "unknown": "",
        "": "",
        "none": "",
        "null": "",
    }

    category = aliases.get(category, category)

    if category in {"storage", "server", "network", "security"}:
        return category

    return ""

def detect_tender_category(package: dict[str, Any]) -> str:
    tender = package.get("tender", {}) or {}
    title = str(tender.get("title") or "").lower().replace("ё", "е")

    doc_texts: list[str] = []
    for doc in package.get("technical_spec_documents", []):
        doc_texts.append(str(doc.get("filename") or "").lower().replace("ё", "е"))
        doc_texts.append(str(doc.get("text") or "")[:25000].lower().replace("ё", "е"))

    joined = chr(10).join([title] + doc_texts)

    title_patterns = {
        "storage": [
            "схд",
            "система хранения",
            "система хранения данных",
            "системы хранения данных",
            "хранения данных",
            "хранение данных",
            "дисковая полка",
        ],
        "security": [
            "межсетевой экран",
            "firewall",
            "ngfw",
            "шлюз безопасности",
            "vpn-шлюз",
            "vpn шлюз",
            "средство защиты",
        ],
        "network": [
            "коммутатор",
            "маршрутизатор",
            "точка доступа",
            "сетевое оборудование",
        ],
        "server": [
            "сервер",
            "вычислительный комплекс",
        ],
    }

    # Сильный сигнал из названия закупки важнее характеристик.
    # Например, сервер может иметь SFP/Ethernet, но это не делает закупку сетевой.
    for category, patterns in title_patterns.items():
        if any(pattern in title for pattern in patterns):
            return category

    keyword_groups = {
        "storage": [
            "схд",
            "система хранения",
            "storage",
            "дисковая полка",
            "san",
            "nas",
            "fibre channel",
            "iscsi",
            "lun",
            "snapshot",
            "репликац",
            "дедупликац",
        ],
        "security": [
            "межсетевой экран",
            "firewall",
            "ngfw",
            "vpn",
            "шлюз безопасности",
            "фстэк",
            "фсб",
            "скзи",
            "utm",
            "ips",
            "ids",
        ],
        "network": [
            "коммутатор",
            "маршрутизатор",
            "switch",
            "router",
            "poe",
            "vlan",
            "стекир",
            "stacking",
            "qsfp",
        ],
        "server": [
            "сервер",
            "вычислительный комплекс",
            "процессор",
            "оперативной памяти",
            "ddr",
            "bios",
            "bmc",
            "raid",
            "стоечный",
            "модулей оперативной памяти",
            "установленных процессоров",
        ],
    }

    scores = {
        category: sum(joined.count(word) for word in words)
        for category, words in keyword_groups.items()
    }

    best_category = max(scores, key=scores.get)
    if scores[best_category] <= 0:
        return "unknown"

    return best_category

def format_supply_item(item: dict[str, Any]) -> str:
    name = item.get("name") or "не указано"
    unit = item.get("unit") or "не указано"
    quantity = item.get("quantity") or "не указано"

    return (
        f"Позиция: {name}; "
        f"ед. изм.: {unit}; "
        f"количество: {quantity}; "
        "важные ограничения: соответствие характеристикам ТЗ и указание точных параметров в заявке"
    )


def format_characteristic(item: dict[str, Any]) -> str:
    name = item.get("name") or "не указано"
    value = item.get("value") or "не указано"
    unit = item.get("unit") or ""

    if unit:
        return f"{name}: {value} {unit}"

    return f"{name}: {value}"


def _append_unique_text(items: list[Any], additions: list[str]) -> list[Any]:
    existing = {str(item).strip().lower() for item in items if str(item).strip()}

    for addition in additions:
        key = addition.strip().lower()
        if key and key not in existing:
            items.append(addition)
            existing.add(key)

    return items


def _load_guardrail_business_profile() -> dict[str, Any]:
    try:
        return load_business_profile()
    except Exception:
        return {}


def _iter_text_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_iter_text_values(item))
        return result

    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_iter_text_values(item))
        return result

    if value is None:
        return []

    return [str(value)]


def _keyword_in_text(text: str, keyword: str) -> bool:
    normalized = normalize_profile_text(text)
    pattern = normalize_profile_text(keyword).strip()
    if not pattern:
        return False

    if re.fullmatch(r"[a-z0-9][a-z0-9 .+\-]*", pattern):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])",
            normalized,
        ) is not None

    return pattern in normalized


def _value_contains_keyword(value: Any, keywords: list[str]) -> bool:
    return any(
        _keyword_in_text(text, keyword)
        for text in _iter_text_values(value)
        for keyword in keywords
    )


def _matched_keywords(value: Any, keywords: list[str], *, limit: int = 12) -> list[str]:
    matches: list[str] = []
    for keyword in keywords:
        if _value_contains_keyword(value, [keyword]):
            matches.append(keyword)
        if len(matches) >= limit:
            break
    return matches


def domestic_registry_context_from_package(
    package: dict[str, Any] | None,
    profile: dict[str, Any],
) -> dict[str, Any]:
    if not package:
        return {"detected": False, "matched_signals": []}

    context = package.get("domestic_registry_context") or {}
    if isinstance(context, dict) and context.get("detected"):
        return {
            "detected": True,
            "matched_signals": string_list(context.get("matched_signals")),
        }

    keywords = domestic_context_keywords(profile)
    context_fields = {
        "tender": package.get("tender"),
        "current_document_risk_analysis": package.get("current_document_risk_analysis"),
        "spec_facts": package.get("spec_facts"),
        "technical_spec_documents": package.get("technical_spec_documents"),
        "application_requirements_short": package.get("application_requirements_short"),
        "contract_documents_short": package.get("contract_documents_short"),
        "other_documents_short": package.get("other_documents_short"),
    }
    matches = _matched_keywords(context_fields, keywords)
    return {"detected": bool(matches), "matched_signals": matches}


def domestic_registry_context_for_report(
    report: dict[str, Any],
    package: dict[str, Any] | None,
    profile: dict[str, Any],
) -> dict[str, Any]:
    context = domestic_registry_context_from_package(package, profile)
    if context.get("detected"):
        return context

    embedded = report.get("domestic_registry_context") or {}
    if isinstance(embedded, dict) and embedded.get("detected"):
        return {
            "detected": True,
            "matched_signals": string_list(embedded.get("matched_signals")),
        }

    keywords = domestic_context_keywords(profile)
    matches = _matched_keywords(report, keywords)
    return {"detected": bool(matches), "matched_signals": matches}


def foreign_vendor_offer_detected(report: dict[str, Any], profile: dict[str, Any]) -> bool:
    keywords = foreign_vendor_recommendation_keywords(profile)
    offer = report.get("potential_offer")
    values: list[Any] = []

    if isinstance(offer, dict):
        for key in ("what_to_offer", "possible_analogs_or_approach"):
            values.append(offer.get(key))
    else:
        values.append(offer)

    vendor_fit = report.get("vendor_fit") or {}
    if isinstance(vendor_fit, dict):
        for item in vendor_fit.get("candidate_vendors_or_lines") or []:
            if isinstance(item, dict):
                values.append(item.get("vendor_or_line"))
                values.append(item.get("why_may_fit"))
            else:
                values.append(item)

    return _value_contains_keyword(values, keywords)


def safe_domestic_offer_lines(profile: dict[str, Any]) -> list[str]:
    vendors = ", ".join(domestic_safe_vendor_examples(profile))
    return [
        "Российское или реестровое оборудование, соответствующее ТЗ и действующим требованиям нацрежима.",
        f"{vendors} или другой производитель - только при подтверждении соответствия ТЗ, реестровой записи и канала поставки.",
        "Конкретный вендор и конфигурацию выбирать только после проверки ТЗ, реестра и допустимости происхождения.",
    ]


def apply_domestic_vendor_guardrail(
    report: dict[str, Any],
    package: dict[str, Any] | None = None,
) -> bool:
    profile = _load_guardrail_business_profile()
    context = domestic_registry_context_for_report(report, package, profile)
    if not context.get("detected") or not foreign_vendor_offer_detected(report, profile):
        return False

    safe_offer = safe_domestic_offer_lines(profile)
    registry_check = (
        "Проверить нацрежим, реестр, реестровую запись и допустимость иностранного происхождения до выбора вендора."
    )

    offer = report.get("potential_offer")
    if isinstance(offer, dict):
        offer["what_to_offer"] = safe_offer
        offer["possible_analogs_or_approach"] = [
            "Подбирать российское, реестровое или прямо допустимое по ТЗ оборудование.",
            "Импортные бренды рассматривать только как риск или ограничение без подтверждения допустимости иностранного происхождения.",
        ]
        checks = offer.setdefault("what_presales_should_check", [])
        if isinstance(checks, list):
            _append_unique_text(checks, [registry_check])
        else:
            offer["what_presales_should_check"] = [text_value(checks), registry_check]
    else:
        report["potential_offer"] = safe_offer

    vendor_fit = report.get("vendor_fit")
    if isinstance(vendor_fit, dict):
        vendor_fit["candidate_vendors_or_lines"] = [
            {
                "vendor_or_line": "Российское или реестровое оборудование",
                "why_may_fit": "Безопасный базовый подход при отечественном/реестровом контексте закупки.",
                "what_to_verify": [
                    "соответствие ТЗ",
                    "реестровая запись",
                    "канал поставки",
                    "допустимость происхождения",
                ],
                "risk": "medium",
            }
        ]
        notes = vendor_fit.setdefault("notes", [])
        if isinstance(notes, list):
            _append_unique_text(
                notes,
                [
                    "Иностранные бренды нельзя выводить как основной вариант без подтверждения допустимости иностранного происхождения.",
                ],
            )

    risks = report.get("top_risks")
    if isinstance(risks, list):
        _append_unique_text(risks, [registry_check])

    next_action = report.get("next_action")
    if isinstance(next_action, list):
        _append_unique_text(
            next_action,
            [
                "Сначала проверить ТЗ, нацрежим, реестр и канал поставки для российского/реестрового оборудования.",
            ],
        )

    commercial = report.get("commercial_assessment")
    if isinstance(commercial, dict):
        commercial_risks = commercial.setdefault("risks", [])
        if isinstance(commercial_risks, list):
            _append_unique_text(commercial_risks, [registry_check])

    risk_map = report.get("risk_map")
    if isinstance(risk_map, dict):
        legal = risk_map.setdefault("legal_or_registry", {})
        if isinstance(legal, dict):
            if str(legal.get("level") or "unknown").lower() in {"", "unknown", "low"}:
                legal["level"] = "medium"
            evidence = legal.setdefault("evidence", [])
            if isinstance(evidence, list):
                _append_unique_text(evidence, [registry_check])

    return True


def apply_presales_guardrails(
    report: dict[str, Any],
    package: dict[str, Any] | None = None,
) -> None:
    category = str(report.get("detected_category") or "").lower()
    facts = report.get("extracted_spec_facts") or {}
    tech = report.get("technical_spec_analysis") or {}
    offer = report.setdefault("potential_offer", {})
    rec = report.setdefault("recommendation", {})

    supply_count = int(facts.get("supply_items_count") or 0)
    characteristic_count = int(facts.get("technical_characteristics_count") or 0)
    equivalent_allowed = str(tech.get("equivalent_allowed") or "").lower()

    if category == "storage":
        checks = offer.setdefault("what_presales_should_check", [])
        if isinstance(checks, list):
            _append_unique_text(
                checks,
                [
                    "Проверить точную поставляемость СХД по всем обязательным параметрам ТЗ",
                    "Проверить срок поставки 30 календарных дней с учетом наличия на складе и логистики",
                    "Проверить комплектацию: контроллеры, кэш/память, диски, полки расширения, порты и лицензии",
                    "Проверить гарантию, сертификаты соответствия и техническую документацию на русском языке",
                    "Проверить маржу после подтверждения цены у поставщика или дистрибьютора",
                ],
            )

    if isinstance(rec, dict):
        decision = str(rec.get("decision") or "").lower()
        confidence = str(rec.get("confidence") or "").lower()
        reasoning = rec.setdefault("reasoning", [])

        should_downgrade_high_confidence = (
            decision == "go"
            and confidence == "high"
            and (
                equivalent_allowed in {"unclear", "no", "неясно", "нет"}
                or (supply_count == 0 and characteristic_count == 0)
            )
        )

        if should_downgrade_high_confidence:
            rec["confidence"] = "medium"
            if isinstance(reasoning, list):
                _append_unique_text(
                    reasoning,
                    [
                        "Уверенность снижена автоматически: эквивалент не подтвержден явно или структурные характеристики не извлечены полностью",
                    ],
                )

    if "risk_map" not in report:
        tailoring = report.get("tailoring_risk") or {}
        delivery = report.get("delivery_feasibility") or {}
        commercial = report.get("commercial_assessment") or {}

        report["risk_map"] = {
            "tailoring": {
                "level": tailoring.get("level") if isinstance(tailoring, dict) else "unknown",
                "evidence": tailoring.get("evidence") if isinstance(tailoring, dict) else [],
            },
            "delivery": {
                "level": delivery.get("assessment") if isinstance(delivery, dict) else "unknown",
                "evidence": delivery.get("reasoning") if isinstance(delivery, dict) else [],
            },
            "commercial": {
                "level": "medium",
                "evidence": commercial.get("risks") if isinstance(commercial, dict) else [],
            },
            "legal_or_registry": {
                "level": "unknown",
                "evidence": [],
            },
            "missing_data": report.get("problems_in_tz") or [],
        }

    if "margin_assessment" not in report:
        report["margin_assessment"] = {
            "expected_margin": "unknown",
            "margin_risks": [
                "Маржу нельзя оценить без цены поставщика, срока поставки, условий гарантии и подтвержденной комплектации",
            ],
            "what_to_request_from_supplier": [
                "Цена и срок поставки",
                "Наличие на складе или срок производства",
                "Гарантийные условия",
                "Сертификаты и документы поставки",
                "Возможность авторизации или письма производителя при необходимости",
            ],
        }

    if "questions" not in report:
        report["questions"] = {
            "to_customer": report.get("questions_to_customer") or [],
            "to_presales_engineer": [
                "Какие 2-3 конфигурации можно быстро проверить под требования ТЗ?",
                "Есть ли параметры, которые ведут к конкретному вендору или модели?",
                "Какие характеристики являются стоп-факторами для наших поставщиков?",
            ],
            "to_supplier_or_distributor": [
                "Есть ли подходящая конфигурация в наличии или с поставкой в требуемый срок?",
                "Какая цена, гарантия и комплект документов?",
                "Есть ли ограничения по лицензиям, дискам, полкам расширения или поддержке?",
            ],
        }

    apply_domestic_vendor_guardrail(report, package)


def enrich_report_with_extracted_facts(report: dict[str, Any], package: dict[str, Any]) -> dict[str, Any]:
    spec_facts = package.get("spec_facts") or {}

    raw_category = normalize_detected_category(spec_facts.get("category"))
    detected_category = normalize_detected_category(detect_tender_category(package))
    category = raw_category or detected_category or "unknown"

    report["detected_category"] = category

    tech = report.setdefault("technical_spec_analysis", {})
    if not isinstance(tech, dict):
        return report

    supply_items = spec_facts.get("supply_items") or []
    characteristics = spec_facts.get("technical_characteristics") or []

    if supply_items:
        tech["what_is_required"] = [format_supply_item(item) for item in supply_items]

    if characteristics:
        tech["key_characteristics"] = [format_characteristic(item) for item in characteristics]

    report["extracted_spec_facts"] = {
        "category": category,
        "raw_category": spec_facts.get("category"),
        "detected_category_from_text": detected_category or "unknown",
        "supply_items_count": len(supply_items),
        "technical_characteristics_count": len(characteristics),
    }

    apply_presales_guardrails(report, package)
    return report


def enrich_routerai_report_with_package_facts(
    report: dict[str, Any],
    package: dict[str, Any],
) -> dict[str, Any]:
    spec_facts = package.get("spec_facts") or {}
    if not isinstance(spec_facts, dict):
        return report

    supply_items = spec_facts.get("supply_items") or []
    characteristics = spec_facts.get("technical_characteristics") or []

    if supply_items and not report.get("supply_composition"):
        report["supply_composition"] = [
            format_supply_item(item)
            for item in supply_items
            if isinstance(item, dict)
        ]

    if characteristics and not report.get("critical_tz_requirements"):
        report["critical_tz_requirements"] = [
            format_characteristic(item)
            for item in characteristics[:20]
            if isinstance(item, dict)
        ]

    if supply_items and not report.get("what_is_required"):
        report["what_is_required"] = [
            format_supply_item(item)
            for item in supply_items[:12]
            if isinstance(item, dict)
        ]

    return report


def persistence_recommendation_and_confidence(
    parsed_report: dict[str, Any],
    *,
    analysis_type: str,
) -> tuple[Any, Any]:
    recommendation = parsed_report.get("recommendation")
    recommendation = recommendation if isinstance(recommendation, dict) else {}

    if analysis_type.startswith(LLM_CUSTOMER_LEAD_REPORT_ANALYSIS_TYPE):
        priority = normalize_lead_priority(parsed_report.get("lead_priority"))
        confidence = normalize_lead_confidence(
            first_report_value(parsed_report.get("confidence"), recommendation.get("confidence")),
            lead_priority=priority,
        )
        return lead_recommendation_from_priority(priority), confidence

    decision = (
        recommendation.get("decision")
        or parsed_report.get("verdict")
    )
    confidence = (
        recommendation.get("confidence")
        or parsed_report.get("confidence")
    )
    return decision, confidence


def save_llm_report(
    *,
    tender_id: str,
    provider: str,
    model: str,
    parsed_report: dict[str, Any],
    raw_response: str,
    context_chars: int,
    analysis_type: str = "llm_presales_report",
    metadata: dict[str, Any] | None = None,
) -> None:
    decision, confidence = persistence_recommendation_and_confidence(
        parsed_report,
        analysis_type=analysis_type,
    )

    result = {
        "provider": provider,
        "model": model,
        "created_at": datetime.now().isoformat(),
        "context_chars": context_chars,
        "meta": metadata or {},
        "report": parsed_report,
        "raw_response": raw_response,
    }

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM analysis_results
                WHERE tender_id = %s
                  AND analysis_type = %s;
                """,
                (tender_id, analysis_type),
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
                    analysis_type,
                    model,
                    Jsonb(result),
                    None,
                    decision,
                    confidence,
                ),
            )

        conn.commit()


def safe_result_label(value: str | None) -> str:
    label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip())
    return label.strip("_.-")


def analysis_type_for_label(
    result_label: str | None,
    *,
    report_kind: str = REPORT_KIND_TECHNICAL,
) -> str:
    base_analysis_type = base_analysis_type_for_report_kind(report_kind)
    safe_label = safe_result_label(result_label)
    if not safe_label:
        return base_analysis_type

    return f"{base_analysis_type}_{safe_label}"


def output_stem(
    external_id: str,
    result_label: str | None,
    *,
    report_kind: str = REPORT_KIND_TECHNICAL,
) -> str:
    kind = normalize_report_kind(report_kind)
    safe_label = safe_result_label(result_label)
    if kind == REPORT_KIND_LEAD:
        if safe_label:
            return f"{external_id}.lead.{safe_label}"
        return f"{external_id}.lead"

    if not safe_label:
        return external_id

    return f"{external_id}.{safe_label}"


def llm_response_metadata(
    response: LLMResponse,
    *,
    parse_status: str,
    json_mode: bool,
    prompt_chars: int | None = None,
    context_chars: int | None = None,
    output_token_cap: int | None = None,
    parse_error_reason: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "provider": response.provider,
        "model": response.model,
        "parse_status": parse_status,
        "json_mode": json_mode,
    }

    if prompt_chars is not None:
        metadata["prompt_chars"] = prompt_chars

    if context_chars is not None:
        metadata["context_chars"] = context_chars

    if output_token_cap is not None:
        metadata["output_token_cap"] = output_token_cap

    finish_reason = llm_finish_reason(response)
    if finish_reason:
        metadata["finish_reason"] = finish_reason

    if response.response_id:
        metadata["response_id"] = response.response_id

    if response.latency_seconds is not None:
        metadata["latency_seconds"] = round(response.latency_seconds, 3)

    if response.usage:
        metadata["usage"] = response.usage

    if parse_error_reason:
        metadata["parse_error_reason"] = parse_error_reason

    return metadata


def llm_context_quality_metadata(package: dict[str, Any]) -> dict[str, Any]:
    package_meta = package.get("meta") or {}
    package_meta = package_meta if isinstance(package_meta, dict) else {}
    documents_summary = package.get("documents_summary") or []
    documents_summary = documents_summary if isinstance(documents_summary, list) else []
    technical_spec_documents = package.get("technical_spec_documents") or []
    technical_spec_documents = (
        technical_spec_documents if isinstance(technical_spec_documents, list) else []
    )
    spec_facts = package.get("spec_facts") or {}
    spec_facts = spec_facts if isinstance(spec_facts, dict) else {}

    supply_items = spec_facts.get("supply_items") or []
    characteristics = spec_facts.get("technical_characteristics") or []
    supply_items = supply_items if isinstance(supply_items, list) else []
    characteristics = characteristics if isinstance(characteristics, list) else []

    documents_with_text = 0
    compact_documents = []
    technical_spec_found_inside_count = 0
    for doc in documents_summary:
        if not isinstance(doc, dict):
            continue
        try:
            text_len = int(float(doc.get("text_len") or 0))
        except (TypeError, ValueError):
            text_len = 0
        if text_len > 0:
            documents_with_text += 1
        doc_type_reason = doc.get("doc_type_reason")
        if doc_type_reason in {
            "technical_section_found_inside_document",
            "content_contains_technical_spec",
            "content_contains_supply_table",
            "price_doc_contains_supply_spec",
        }:
            technical_spec_found_inside_count += 1
        compact_documents.append(
            {
                "filename": doc.get("filename"),
                "doc_type": doc.get("doc_type"),
                "doc_type_reason": doc_type_reason,
                "technical_spec_note": doc.get("technical_spec_note"),
                "technical_spec_reference_note": doc.get("technical_spec_reference_note"),
                "text_len": text_len,
            }
        )

    technical_spec_documents_count = len(technical_spec_documents)
    preliminary_no_technical_spec = (
        documents_with_text > 0
        and technical_spec_documents_count == 0
        and len(supply_items) == 0
        and len(characteristics) == 0
    )

    return {
        "documents_with_text": documents_with_text,
        "analysis_depth": package_meta.get("analysis_depth"),
        "context_limits": package_meta.get("context_limits"),
        "document_status": package.get("document_status") or package_meta.get("document_status"),
        "document_preparation": package_meta.get("document_preparation"),
        "document_selection": package.get("document_selection") or package_meta.get("document_selection"),
        "documents_summary": compact_documents[:20],
        "technical_spec_documents_count": technical_spec_documents_count,
        "technical_spec_found_inside_count": technical_spec_found_inside_count,
        "spec_facts": {
            "category": spec_facts.get("category"),
            "supply_items_count": len(supply_items),
            "technical_characteristics_count": len(characteristics),
        },
        "preliminary_no_technical_spec": preliminary_no_technical_spec,
    }


def llm_finish_reason(response: LLMResponse) -> str | None:
    choices = response.raw_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    first = choices[0]
    if not isinstance(first, dict):
        return None

    value = first.get("finish_reason")
    return str(value) if value else None


def usage_int(usage: dict[str, Any] | None, *path: str) -> int | None:
    current: Any = usage
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)

    try:
        return int(current)
    except (TypeError, ValueError):
        return None


def reasoning_tokens(usage: dict[str, Any] | None) -> int | None:
    return (
        usage_int(usage, "reasoning_tokens")
        or usage_int(usage, "completion_tokens_details", "reasoning_tokens")
        or usage_int(usage, "completion_tokens_details", "reasoning")
    )


def diagnose_empty_llm_response(response: LLMResponse) -> str | None:
    if response.text is not None and response.text.strip():
        return None

    completion_tokens = usage_int(response.usage, "completion_tokens")
    used_reasoning_tokens = reasoning_tokens(response.usage)

    if (
        completion_tokens
        and used_reasoning_tokens
        and used_reasoning_tokens >= completion_tokens
    ):
        return "completion budget exhausted by reasoning, no final answer"

    return "empty final answer from LLM provider"


def raw_response_excerpt(text: str | None, max_chars: int = 4000) -> str | None:
    if text is None:
        return None

    return text[:max_chars]


def save_llm_error_metadata(
    *,
    external_id: str,
    result_label: str | None,
    report_kind: str = REPORT_KIND_TECHNICAL,
    response: LLMResponse,
    context_chars: int,
    prompt_chars: int | None = None,
    llm_context_chars: int | None = None,
    output_token_cap: int | None = None,
    json_mode: bool,
    error: Exception,
    parse_error_reason: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{output_stem(external_id, result_label, report_kind=report_kind)}.error.json"
    metadata = llm_response_metadata(
        response,
        parse_status="failed",
        json_mode=json_mode,
        prompt_chars=prompt_chars,
        context_chars=llm_context_chars,
        output_token_cap=output_token_cap,
        parse_error_reason=parse_error_reason,
    )
    if extra_meta:
        metadata.update(extra_meta)

    payload = {
        "provider": response.provider,
        "model": response.model,
        "created_at": datetime.now().isoformat(),
        "context_chars": context_chars,
        "meta": metadata,
        "error": str(error),
        "raw_excerpt": raw_response_excerpt(response.text),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def generate_chat_completion_with_thinking(
    client: Any,
    *,
    thinking_enabled: bool | None,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> LLMResponse:
    missing = object()
    previous = getattr(client, "thinking_enabled", missing)

    if thinking_enabled is not None and previous is not missing:
        setattr(client, "thinking_enabled", thinking_enabled)

    try:
        return client.generate_chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
    finally:
        if previous is not missing:
            setattr(client, "thinking_enabled", previous)


def save_routerai_analysis_artifact(
    *,
    external_id: str,
    result_label: str | None,
    response: LLMResponse,
    analysis: dict[str, Any],
    context_chars: int,
    prompt_chars: int,
    llm_context_chars: int,
    output_token_cap: int,
    json_mode: bool,
) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{output_stem(external_id, result_label)}.analysis.json"
    metadata = llm_response_metadata(
        response,
        parse_status="ok",
        json_mode=json_mode,
        prompt_chars=prompt_chars,
        context_chars=llm_context_chars,
        output_token_cap=output_token_cap,
    )
    metadata.update(
        {
            "pass_mode": "routerai_two_pass",
            "pass_stage": "analysis",
            "analysis_finish_reason": llm_finish_reason(response),
        }
    )

    payload = {
        "provider": response.provider,
        "model": response.model,
        "created_at": datetime.now().isoformat(),
        "context_chars": context_chars,
        "meta": metadata,
        "analysis": analysis,
        "raw_response": response.text,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def parse_routerai_pass_json(
    *,
    external_id: str,
    result_label: str | None,
    response: LLMResponse,
    pass_stage: str,
    user_prompt: str,
    prompt_chars: int,
    llm_context_chars: int,
    output_token_cap: int,
    json_mode: bool,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return parse_routerai_pass_json_payload(response=response, pass_stage=pass_stage)
    except Exception as exc:
        parse_error_reason = diagnose_empty_llm_response(response)
        metadata = {
            "pass_mode": "routerai_two_pass",
            "pass_stage": pass_stage,
        }
        if extra_meta:
            metadata.update(extra_meta)

        error_path = save_llm_error_metadata(
            external_id=external_id,
            result_label=result_label,
            response=response,
            context_chars=len(user_prompt),
            prompt_chars=prompt_chars,
            llm_context_chars=llm_context_chars,
            output_token_cap=output_token_cap,
            json_mode=json_mode,
            error=exc,
            parse_error_reason=parse_error_reason,
            extra_meta=metadata,
        )
        reason_part = f" reason={parse_error_reason}" if parse_error_reason else ""
        raise RuntimeError(
            f"Cannot parse RouterAI {pass_stage} pass JSON response: "
            f"provider={response.provider} model={response.model} "
            f"error_metadata={error_path}{reason_part} "
            f"raw_excerpt={raw_response_excerpt(response.text, 2000)}"
        ) from exc


def parse_routerai_pass_json_payload(
    *,
    response: LLMResponse,
    pass_stage: str,
) -> dict[str, Any]:
    parse_error_reason = diagnose_empty_llm_response(response)

    if parse_error_reason:
        raise RuntimeError(parse_error_reason)

    parsed = extract_json(response.text)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{pass_stage} pass returned non-object JSON")

    return parsed


def package_text_lengths(
    package: dict[str, Any],
    section: str,
    field: str,
) -> list[int]:
    return [
        len(str(doc.get(field) or ""))
        for doc in package.get(section, [])
        if isinstance(doc, dict) and doc.get(field)
    ]


def package_text_cap(
    package: dict[str, Any],
    section: str,
    field: str,
    fallback: int,
) -> int:
    values = package_text_lengths(package, section, field)
    current = max(values) if values else fallback
    return min(current, fallback)


def package_context_limit(package: dict[str, Any], key: str, fallback: int) -> int:
    meta = package.get("meta") or {}
    context_limits = meta.get("context_limits") if isinstance(meta, dict) else {}
    if not isinstance(context_limits, dict):
        return fallback

    try:
        value = int(context_limits.get(key) or fallback)
    except (TypeError, ValueError):
        return fallback

    return value if value > 0 else fallback


def routerai_analysis_retry_cap_defaults(analysis_depth: str) -> tuple[int, int]:
    if normalize_analysis_depth(analysis_depth) == ANALYSIS_DEPTH_DEEP:
        return (
            ROUTERAI_DEEP_ANALYSIS_RETRY_MAX_SPEC_CHARS,
            ROUTERAI_DEEP_ANALYSIS_RETRY_MAX_OTHER_CHARS,
        )

    return (
        ROUTERAI_ANALYSIS_RETRY_MAX_SPEC_CHARS,
        ROUTERAI_ANALYSIS_RETRY_MAX_OTHER_CHARS,
    )


def routerai_analysis_retry_caps(
    package: dict[str, Any],
    *,
    analysis_depth: str,
) -> tuple[int, int]:
    retry_max_spec_chars, retry_max_other_chars = routerai_analysis_retry_cap_defaults(
        analysis_depth
    )
    spec_chars = package_text_cap(
        package,
        "technical_spec_documents",
        "text",
        retry_max_spec_chars,
    )
    other_values = (
        package_text_lengths(package, "price_documents", "text")
        + package_text_lengths(package, "other_documents_short", "text_preview")
    )
    current_other = max(other_values) if other_values else retry_max_other_chars
    other_chars = min(current_other, retry_max_other_chars)
    return spec_chars, other_chars


def output_limit_finish_reason(finish_reason: str | None) -> bool:
    return str(finish_reason or "").strip().lower() in OUTPUT_LIMIT_FINISH_REASONS


def increased_deep_retry_output_tokens(
    analysis_depth: str,
    max_output_tokens: int,
    finish_reason: str | None,
) -> int | None:
    if normalize_analysis_depth(analysis_depth) != ANALYSIS_DEPTH_DEEP:
        return None
    if not output_limit_finish_reason(finish_reason):
        return None

    deep_default = resolve_analysis_limits(ANALYSIS_DEPTH_DEEP).max_output_tokens
    if max_output_tokens >= deep_default:
        return None

    return deep_default


def truncate_retry_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def build_routerai_analysis_retry_context(
    package: dict[str, Any],
    *,
    max_spec_chars: int,
    max_other_chars: int,
) -> str:
    retry_package = copy.deepcopy(package)

    for doc in retry_package.get("technical_spec_documents", []):
        if isinstance(doc, dict):
            doc["text"] = truncate_retry_text(doc.get("text"), max_spec_chars)

    for doc in retry_package.get("price_documents", []):
        if isinstance(doc, dict):
            doc["text"] = truncate_retry_text(doc.get("text"), max_other_chars)

    for doc in retry_package.get("other_documents_short", []):
        if isinstance(doc, dict):
            doc["text_preview"] = truncate_retry_text(
                doc.get("text_preview"),
                max_other_chars,
            )

    return package_to_markdown(retry_package)


def report_to_markdown(report: dict[str, Any]) -> str:
    rec = report.get("recommendation", {})
    tailoring = report.get("tailoring_risk", {})
    delivery = report.get("delivery_feasibility", {})
    contact = report.get("contact", {})
    commercial = report.get("commercial_assessment", {})
    tech = report.get("technical_spec_analysis", {})
    offer = report.get("potential_offer", {})
    vendor_fit = report.get("vendor_fit", {}) or {}
    risk_map = report.get("risk_map", {}) or {}
    margin = report.get("margin_assessment", {}) or {}
    role_questions = report.get("questions", {}) or {}

    def lines(items):
        if not items:
            return "- не указано"
        if not isinstance(items, list):
            items = [items]
        return "\n".join(f"- {json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item}" for item in items)

    if "verdict" in report and "top_risks" in report and "recommendation" not in report:
        return f"""
# LLM compact presales report

## Summary

{report.get("summary", "not specified")}

## Verdict

Decision: {report.get("verdict", "not specified")}
Confidence: {report.get("confidence", "not specified")}

## Top risks

{lines(report.get("top_risks"))}

## Supply composition

{lines(report.get("supply_composition"))}

## Critical TZ requirements

{lines(report.get("critical_tz_requirements") or report.get("what_is_required"))}

## What can be offered

{lines(report.get("potential_offer"))}

## What presales should check

{lines(report.get("what_presales_should_check"))}

## Customer clarifications

{lines(report.get("questions_to_customer"))}

## Next action

{lines(report.get("next_action"))}
""".strip()

    def vendor_lines(items):
        if not items:
            return "- не указано"
        result = []
        for item in items:
            if isinstance(item, dict):
                result.append(
                    "- "
                    + str(item.get("vendor_or_line") or "не указано")
                    + ": "
                    + str(item.get("why_may_fit") or "проверить соответствие")
                    + "; проверить: "
                    + ", ".join(str(x) for x in (item.get("what_to_verify") or []))
                    + "; риск: "
                    + str(item.get("risk") or "unknown")
                )
            else:
                result.append(f"- {item}")
        return "\n".join(result)

    def risk_line(value):
        if isinstance(value, dict):
            evidence = value.get("evidence") or []
            return f"{value.get('level', 'unknown')} - " + "; ".join(str(x) for x in evidence[:3])
        return "unknown"

    return f"""
# LLM-пресейл-отчёт

## Резюме

{report.get("summary", "не указано")}

## Вердикт

Решение: {rec.get("decision", "не указано")}
Уверенность: {rec.get("confidence", "не указана")}

Обоснование:
{lines(rec.get("reasoning"))}

## Анализ ТЗ

Что требуется:
{lines(tech.get("what_is_required"))}

Ключевые характеристики:
{lines(tech.get("key_characteristics"))}

Модели и вендоры:
{lines(tech.get("mentioned_models_or_vendors"))}

Эквивалент допускается: {tech.get("equivalent_allowed", "не указано")}

Работы/услуги:
{lines(tech.get("implementation_or_services_required"))}

## Сроки поставки и исполнимость

Срок: {delivery.get("delivery_term", "не указан")}
Оценка: {delivery.get("assessment", "не указана")}

Обоснование:
{lines(delivery.get("reasoning"))}

## Признаки заточки

Риск: {tailoring.get("level", "не указан")}

Причины:
{lines(tailoring.get("reasons"))}

Основания:
{lines(tailoring.get("evidence"))}

## Что можно предложить

Что предложить:
{lines(offer.get("what_to_offer"))}

Подход к аналогам:
{lines(offer.get("possible_analogs_or_approach"))}

Что проверить пресейлу:
{lines(offer.get("what_presales_should_check"))}

## Подходящие вендоры и подход

{vendor_lines(vendor_fit.get("candidate_vendors_or_lines"))}

## Карта рисков

Заточка: {risk_line(risk_map.get("tailoring"))}
Поставка: {risk_line(risk_map.get("delivery"))}
Коммерция: {risk_line(risk_map.get("commercial"))}
Право/реестр: {risk_line(risk_map.get("legal_or_registry"))}

Недостающие данные:
{lines(risk_map.get("missing_data"))}

## Маржинальность и коммерческий смысл

Ожидаемая маржа: {margin.get("expected_margin", "не указано")}

Риски маржи:
{lines(margin.get("margin_risks"))}

Что запросить у поставщика:
{lines(margin.get("what_to_request_from_supplier"))}

## Проблемы и неясности ТЗ

{lines(report.get("problems_in_tz"))}

## Что уточнить у заказчика

{lines(report.get("questions_to_customer"))}

## Вопросы по ролям

Заказчику:
{lines(role_questions.get("to_customer"))}

Пресейлу / инженеру:
{lines(role_questions.get("to_presales_engineer"))}

Поставщику / дистрибьютору:
{lines(role_questions.get("to_supplier_or_distributor"))}

## Контакт

ФИО: {contact.get("person", "не указано")}
Телефон: {contact.get("phone", "не указан")}
Email: {contact.get("email", "не указан")}

## Коммерческая оценка

Плюсы:
{lines(commercial.get("pros"))}

Риски:
{lines(commercial.get("risks"))}

Fit для интегратора: {commercial.get("fit_for_integrator", "не указан")}

## Следующее действие

{lines(report.get("next_action"))}
""".strip()


def lead_report_to_markdown(report: dict[str, Any], package: dict[str, Any]) -> str:
    tender = package.get("tender") or {}
    document_status = report.get("document_status") or package.get("document_status") or {}

    def lines(items: Any) -> str:
        values = string_list(items)
        return "\n".join(f"- {item}" for item in values) if values else "- not specified"

    card_lines = [
        f"Customer: {tender.get('customer_name') or 'not specified'}",
        f"Purchase: {tender.get('title') or 'not specified'}",
        f"Price: {tender.get('initial_price_text') or tender.get('initial_price') or 'not specified'}",
        f"Deadline: {tender.get('deadline_at') or 'not specified'}",
    ]

    return f"""
# LLM customer lead report

## Tender signal

{chr(10).join(card_lines)}

## Document status

{document_status.get("label") if isinstance(document_status, dict) else "not specified"}

## Lead summary

{report.get("lead_summary") or "not specified"}

## Customer signal

{report.get("customer_signal") or "not specified"}

## Likely customer story

{report.get("likely_customer_story") or "not specified"}

## Possible needs

{lines(report.get("possible_needs"))}

## Target roles

{lines(report.get("target_roles"))}

## Procurement contact role

{report.get("procurement_contact_role") or "not specified"}

## Target end-customer roles

{lines(report.get("target_end_customer_roles"))}

## Opening phrase

{report.get("opening_phrase") or "not specified"}

## Discovery questions

{lines(report.get("discovery_questions"))}

## Handoff to manager when

{lines(report.get("handoff_to_manager_when"))}

## Lead priority

Priority: {report.get("lead_priority") or "not specified"}
Confidence: {report.get("confidence") or "not specified"}
Why: {report.get("why_priority") or "not specified"}

## Next action

{report.get("next_action") or "not specified"}
""".strip()


def routerai_two_pass_report_to_markdown(
    report: dict[str, Any],
    package: dict[str, Any],
) -> str:
    tender = package.get("tender") or {}
    contact = package.get("contact") or {}

    def lines(items: Any) -> str:
        values = string_list(items)
        return "\n".join(f"- {item}" for item in values) if values else "- не указано"

    card_lines = [
        f"Заказчик: {tender.get('customer_name') or 'не указан'}",
        f"НМЦК: {tender.get('initial_price_text') or tender.get('initial_price') or 'не указана'}",
        f"Срок подачи: {tender.get('deadline_at') or 'не указан'}",
    ]
    contact_lines = [
        line
        for line in (
            f"ФИО: {contact.get('person')}" if contact.get("person") else "",
            f"Телефон: {contact.get('phone')}" if contact.get("phone") else "",
            f"Email: {contact.get('email')}" if contact.get("email") else "",
        )
        if line
    ]
    if contact_lines:
        card_lines.extend(["", "Контакт:", *contact_lines])

    return f"""
# LLM presales report (RouterAI two-pass)

## Карточка закупки

{chr(10).join(card_lines)}

## Summary

{report.get("summary") or "not specified"}

## Verdict

Decision: {report.get("verdict") or "not specified"}
Confidence: {report.get("confidence") or "not specified"}

## What is required

{lines(report.get("what_is_required"))}

## Supply composition

{lines(report.get("supply_composition"))}

## Critical TZ requirements

{lines(report.get("critical_tz_requirements"))}

## Top risks

{lines(report.get("top_risks"))}

## Delivery feasibility

{report.get("delivery_feasibility") or "not specified"}

## Potential offer

{lines(report.get("potential_offer"))}

## What presales should check

{lines(report.get("what_presales_should_check"))}

## Проблемы и неясности ТЗ

{lines(report.get("problems_in_tz"))}

## Customer clarifications

{lines(report.get("questions_to_customer"))}

## Next action

{lines(report.get("next_action"))}
""".strip()



def raw_full_from_tender(tender: dict[str, Any]) -> dict[str, Any]:
    raw = tender.get("raw") or {}
    if not isinstance(raw, dict):
        return {}
    full = raw.get("full") or {}
    return full if isinstance(full, dict) else {}


def available_document_titles_from_tender(tender: dict[str, Any]) -> list[dict[str, str]]:
    full = raw_full_from_tender(tender)
    raw_documents = full.get("documents") or []
    if not isinstance(raw_documents, list):
        return []

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for doc in raw_documents:
        if not isinstance(doc, dict):
            continue
        title = str(doc.get("title") or doc.get("name") or doc.get("filename") or "").strip()
        filename = str(doc.get("filename") or doc.get("fileName") or "").strip()
        doc_id = str(doc.get("id") or doc.get("documentId") or "").strip()
        if not title and not filename:
            continue
        key = (doc_id, title or filename)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "id": doc_id,
                "title": title or filename,
                "filename": filename,
            }
        )
    return result[:30]


def annotate_lead_package_context(
    package: dict[str, Any],
    *,
    tender: dict[str, Any],
    documents: list[dict[str, Any]],
    primary_status: dict[str, Any],
    document_selection: dict[str, Any] | None = None,
    document_readiness: dict[str, Any] | None = None,
) -> None:
    documents_with_text = sum(
        1
        for doc in documents
        if str(doc.get("extracted_text") or "").strip()
    )
    context_mode = "document_context" if documents_with_text > 0 else "card_only"
    document_preparation = dict(document_readiness or {})
    if not document_preparation:
        document_preparation = {
            "docs_before": len(documents),
            "docs_with_text_before": documents_with_text,
            "docs_after": len(documents),
            "docs_with_text_after": documents_with_text,
            "targeted_download_triggered": False,
            "preparation_status": "not_provided",
        }
    document_status = build_document_status(
        document_selection=document_selection,
        document_preparation=document_preparation,
        documents_summary=package.get("documents_summary") or [],
    )
    if document_status.get("analysis_basis") == "lead_card_only":
        context_mode = "card_only"
    elif document_status.get("analysis_basis") == "documents_without_primary":
        context_mode = "documents_without_primary"
    else:
        context_mode = "technical_document"
    meta = package.setdefault("meta", {})
    if isinstance(meta, dict):
        meta.update(
            {
                "report_kind": REPORT_KIND_LEAD,
                "lead_context_mode": context_mode,
                "lead_card_only_allowed": context_mode == "card_only",
                "lead_documents_missing_allowed": (
                    document_status.get("analysis_basis") == "lead_card_only"
                ),
                "lead_confidence_policy": (
                    "downgrade_if_card_only"
                    if document_status.get("analysis_basis") == "lead_card_only"
                    else "normal"
                ),
                "primary_technical_document_required": bool(primary_status.get("required")),
                "primary_technical_document_ready": bool(primary_status.get("ready")),
                "primary_technical_document_reason": primary_status.get("reason"),
                "document_preparation": document_preparation,
                "document_selection": {
                    key: value
                    for key, value in (document_selection or {}).items()
                    if key not in {"candidates", "usage"}
                },
                "document_status": document_status,
            }
        )
    package["document_status"] = document_status

    available_titles = available_document_titles_from_tender(tender)
    if available_titles:
        package["available_document_titles"] = available_titles


def documents_with_text_count(documents: list[dict[str, Any]]) -> int:
    return sum(1 for doc in documents if str(doc.get("extracted_text") or "").strip())


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _step_attempted(steps: Any, key: str) -> bool:
    if not isinstance(steps, dict):
        return False
    step = steps.get(key)
    if isinstance(step, dict):
        return bool(step.get("attempted"))
    return bool(getattr(step, "attempted", False))


def _lead_direct_warning_reason(
    *,
    docs_after: int,
    docs_with_text_after: int,
    primary_status: dict[str, Any],
    fallback: str | None = None,
) -> str | None:
    if docs_with_text_after > 0 and not (
        primary_status.get("required") and not primary_status.get("ready")
    ):
        return None
    if docs_after <= 0:
        return "documents_missing"
    if docs_with_text_after <= 0:
        return "documents_downloaded_without_text"
    return str(primary_status.get("reason") or fallback or "primary_technical_document_not_processed")


def _lead_direct_card_only_reason(
    *,
    docs_after: int,
    docs_with_text_after: int,
    warning_reason: str | None,
) -> str | None:
    if not warning_reason:
        return None
    if docs_after <= 0:
        return "lead_documents_missing_allowed_after_prepare"
    if docs_with_text_after <= 0:
        return "lead_documents_without_text_allowed_after_prepare"
    return warning_reason


def prepare_lead_documents_for_report(
    *,
    external_id: str,
    tender: dict[str, Any],
    documents: list[dict[str, Any]],
    primary_status: dict[str, Any],
    limit_docs: int = LEAD_DIRECT_PREPARATION_DOCUMENT_LIMIT,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    docs_before = len(documents)
    docs_with_text_before = documents_with_text_count(documents)
    primary_missing = bool(primary_status.get("required")) and not bool(primary_status.get("ready"))
    needs_preparation = docs_with_text_before <= 0 or primary_missing
    if not needs_preparation:
        return (
            {
                "external_id": external_id,
                "docs_before": docs_before,
                "docs_with_text_before": docs_with_text_before,
                "docs_after": docs_before,
                "docs_with_text_after": docs_with_text_before,
                "targeted_download_triggered": False,
                "extraction_retry_triggered": False,
                "preparation_status": "not_triggered",
                "document_state": "documents_ready_with_text",
                "preparation_summary": "",
                "preparation_errors": [],
                "warning_reason": None,
                "non_blocking_warning_reason": None,
                "check_error": None,
                "llm_readiness": "ready_for_llm",
            },
            documents,
        )

    preparation_status = "failed"
    preparation_summary = ""
    preparation_errors: list[str] = []
    targeted_download_triggered = True
    extraction_retry_triggered = False
    document_state = "documents_missing"
    download_details: dict[str, Any] = {}

    try:
        from app.pipeline.prepare_tender_for_analysis import prepare_tender_for_analysis

        result = prepare_tender_for_analysis(
            external_id=external_id,
            tender_context=tender,
            limit_docs=limit_docs,
        )
        preparation_status = _status_value(getattr(result, "status", None)) or "completed"
        preparation_summary = str(getattr(result, "summary", "") or "")
        preparation_errors = [str(item) for item in (getattr(result, "errors", None) or [])]
        targeted_download_triggered = bool(
            getattr(result, "document_download_was_run", False)
            or _step_attempted(getattr(result, "steps", None), "targeted_document_download")
        )
        steps = getattr(result, "steps", {}) or {}
        download_step = (
            steps.get("targeted_document_download") if isinstance(steps, dict) else None
        )
        download_details = dict(getattr(download_step, "details", {}) or {})
        extraction_retry_triggered = _step_attempted(getattr(result, "steps", None), "text_extraction")
        document_state = str(getattr(result, "document_readiness", "") or "")
    except Exception as exc:
        preparation_errors = [str(exc)]
        preparation_summary = f"lead document preparation failed: {exc}"

    try:
        prepared_documents = get_documents(str(tender["id"]))
    except Exception:
        prepared_documents = documents

    docs_after = len(prepared_documents)
    docs_with_text_after = documents_with_text_count(prepared_documents)
    warning_reason = _lead_direct_warning_reason(
        docs_after=docs_after,
        docs_with_text_after=docs_with_text_after,
        primary_status=primary_status,
        fallback=document_state,
    )
    non_blocking_warning_reason = _lead_direct_card_only_reason(
        docs_after=docs_after,
        docs_with_text_after=docs_with_text_after,
        warning_reason=warning_reason,
    )

    return (
        {
            "external_id": external_id,
            "docs_before": docs_before,
            "docs_with_text_before": docs_with_text_before,
            "docs_after": docs_after,
            "docs_with_text_after": docs_with_text_after,
            "targeted_download_triggered": targeted_download_triggered,
            "extraction_retry_triggered": extraction_retry_triggered,
            "preparation_status": preparation_status,
            "document_state": document_state,
            "preparation_summary": preparation_summary,
            "preparation_errors": preparation_errors,
            "warning_reason": warning_reason,
            "non_blocking_warning_reason": non_blocking_warning_reason,
            "documents_found": (
                int(download_details.get("documents_found"))
                if download_details.get("documents_found") is not None
                else None
            ),
            "documents_selected": int(download_details.get("documents_selected") or 0),
            "documents_failed": int(download_details.get("documents_failed") or 0),
            "documents_failed_items": (
                download_details.get("documents_failed_items") or []
            ),
            "documents_skipped_due_to_rate_limit": int(
                download_details.get("documents_skipped_due_to_rate_limit") or 0
            ),
            "documents_skipped_due_to_rate_limit_items": (
                download_details.get("documents_skipped_due_to_rate_limit_items") or []
            ),
            "missing_high_value_technical_document": bool(
                download_details.get("missing_high_value_technical_document")
            ),
            "missing_high_value_technical_document_title": (
                download_details.get("missing_high_value_technical_document_title")
            ),
            "document_download_planner_used": bool(
                download_details.get("document_download_planner_used")
            ),
            "document_download_planner_confidence": (
                download_details.get("document_download_planner_confidence")
            ),
            "check_error": None,
            "llm_readiness": "ready_for_llm",
        },
        prepared_documents,
    )


def build_package_from_database(
    *,
    external_id: str,
    max_spec_chars: int,
    max_other_chars: int,
    analysis_depth: str = ANALYSIS_DEPTH_STANDARD,
    report_kind: str = REPORT_KIND_TECHNICAL,
    document_readiness: dict[str, Any] | None = None,
    use_llm_document_selector: bool = True,
) -> tuple[str, dict[str, Any]]:
    report_kind = normalize_report_kind(report_kind)
    tender = get_tender_by_external_id(external_id)

    if not tender:
        raise RuntimeError(f"Tender not found: {external_id}")

    documents = get_documents(str(tender["id"]))
    try:
        from app.pipeline.prepare_tender_for_analysis import (
            PREPARATION_MISSING_PRIMARY_TECHNICAL_DOCUMENT,
            primary_technical_document_status,
        )

        primary_status = primary_technical_document_status(tender, documents)
    except Exception:
        primary_status = {"required": False, "ready": True, "reason": None}

    if (
        report_kind != REPORT_KIND_LEAD
        and primary_status.get("required")
        and not primary_status.get("ready")
    ):
        reason = primary_status.get("reason") or PREPARATION_MISSING_PRIMARY_TECHNICAL_DOCUMENT
        raise RuntimeError(
            "Primary technical document is not ready for LLM: "
            f"external_id={external_id} reason={reason}"
        )

    document_selection: dict[str, Any] | None = None
    if report_kind == REPORT_KIND_LEAD:
        if document_readiness is None:
            document_readiness, documents = prepare_lead_documents_for_report(
                external_id=external_id,
                tender=tender,
                documents=documents,
                primary_status=primary_status,
            )
            try:
                from app.pipeline.prepare_tender_for_analysis import (
                    primary_technical_document_status,
                )

                primary_status = primary_technical_document_status(tender, documents)
            except Exception:
                pass
            if isinstance(document_readiness, dict):
                docs_after = int(document_readiness.get("docs_after") or len(documents))
                docs_with_text_after = int(
                    document_readiness.get("docs_with_text_after")
                    or documents_with_text_count(documents)
                )
                warning_reason = _lead_direct_warning_reason(
                    docs_after=docs_after,
                    docs_with_text_after=docs_with_text_after,
                    primary_status=primary_status,
                    fallback=str(document_readiness.get("document_state") or ""),
                )
                document_readiness["warning_reason"] = warning_reason
                document_readiness["non_blocking_warning_reason"] = _lead_direct_card_only_reason(
                    docs_after=docs_after,
                    docs_with_text_after=docs_with_text_after,
                    warning_reason=warning_reason,
                )
        document_selection = select_documents_for_lead_report(
            tender,
            documents,
            use_llm_selector=use_llm_document_selector,
        )

    package = build_llm_package(
        tender,
        documents,
        max_spec_chars=max_spec_chars,
        max_other_chars=max_other_chars,
        analysis_depth=analysis_depth,
        document_selection=document_selection,
    )
    if report_kind == REPORT_KIND_LEAD:
        annotate_lead_package_context(
            package,
            tender=tender,
            documents=documents,
            primary_status=primary_status,
            document_selection=document_selection,
            document_readiness=document_readiness,
        )

    return str(tender["id"]), package


def load_package_from_context_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def package_external_id(package: dict[str, Any]) -> str:
    tender = package.get("tender") or {}
    external_id = tender.get("external_id")

    if not external_id:
        raise RuntimeError("Context package does not contain tender.external_id")

    return str(external_id)


def package_tender_id(package: dict[str, Any]) -> str:
    tender = package.get("tender") or {}
    tender_id = tender.get("id")

    if not tender_id:
        raise RuntimeError("Context package does not contain tender.id")

    return str(tender_id)


def write_context_files(
    *,
    external_id: str,
    package: dict[str, Any],
    context_markdown: str,
) -> tuple[Path, Path]:
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    context_path = CONTEXT_DIR / f"{external_id}.md"
    context_json_path = CONTEXT_DIR / f"{external_id}.json"
    context_path.write_text(context_markdown, encoding="utf-8")
    context_json_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    return context_path, context_json_path


def run_routerai_two_pass_report(
    *,
    external_id: str,
    tender_id: str,
    package: dict[str, Any],
    context_markdown: str,
    context_path: Path,
    client: Any,
    max_output_tokens: int,
    effective_json_mode: bool,
    result_label: str | None,
    save_to_db: bool,
    analysis_depth: str = ANALYSIS_DEPTH_STANDARD,
) -> dict[str, Any]:
    request_json_mode = bool(effective_json_mode)
    analysis_depth = normalize_analysis_depth(analysis_depth)
    thinking_enabled_for_analysis = analysis_depth == ANALYSIS_DEPTH_DEEP

    analysis_user_prompt = build_routerai_analysis_user_prompt(
        context_markdown,
        analysis_depth=analysis_depth,
    )
    analysis_prompt_chars = len(ROUTERAI_ANALYSIS_SYSTEM_PROMPT) + len(analysis_user_prompt)

    analysis_retry_used = False
    analysis_retry_reason: str | None = None
    analysis_retry_max_spec_chars: int | None = None
    analysis_retry_max_other_chars: int | None = None
    analysis_retry_max_output_tokens: int | None = None
    analysis_retry_context_strategy: str | None = None
    analysis_retry_finish_reason: str | None = None
    analysis_metadata_prompt = analysis_user_prompt
    analysis_metadata_prompt_chars = analysis_prompt_chars
    analysis_metadata_context = context_markdown
    analysis_metadata_output_token_cap = max_output_tokens

    analysis_response = generate_chat_completion_with_thinking(
        client,
        thinking_enabled=thinking_enabled_for_analysis,
        system_prompt=ROUTERAI_ANALYSIS_SYSTEM_PROMPT,
        user_prompt=analysis_user_prompt,
        temperature=0.1,
        max_tokens=max_output_tokens,
        json_mode=request_json_mode,
    )
    analysis_initial_finish_reason = llm_finish_reason(analysis_response)

    try:
        raw_analysis = parse_routerai_pass_json_payload(
            response=analysis_response,
            pass_stage="analysis",
        )
    except Exception as exc:
        analysis_retry_used = True
        analysis_retry_reason = str(exc)
        analysis_retry_max_output_tokens = increased_deep_retry_output_tokens(
            analysis_depth,
            max_output_tokens,
            analysis_initial_finish_reason,
        )
        if analysis_retry_max_output_tokens:
            analysis_retry_context_strategy = "same_context_increased_output_tokens"
            analysis_retry_max_spec_chars = package_context_limit(
                package,
                "max_spec_chars",
                resolve_analysis_limits(analysis_depth).max_spec_chars,
            )
            analysis_retry_max_other_chars = package_context_limit(
                package,
                "max_other_chars",
                resolve_analysis_limits(analysis_depth).max_other_chars,
            )
            retry_context_markdown = context_markdown
        else:
            analysis_retry_context_strategy = "reduced_context"
            analysis_retry_max_output_tokens = max_output_tokens
            (
                analysis_retry_max_spec_chars,
                analysis_retry_max_other_chars,
            ) = routerai_analysis_retry_caps(package, analysis_depth=analysis_depth)
            retry_context_markdown = build_routerai_analysis_retry_context(
                package,
                max_spec_chars=analysis_retry_max_spec_chars,
                max_other_chars=analysis_retry_max_other_chars,
            )
        retry_analysis_user_prompt = (
            f"{build_routerai_analysis_user_prompt(retry_context_markdown, analysis_depth=analysis_depth)}"
            f"\n\n{ROUTERAI_ANALYSIS_RETRY_INSTRUCTION}"
        )
        retry_analysis_prompt_chars = (
            len(ROUTERAI_ANALYSIS_SYSTEM_PROMPT) + len(retry_analysis_user_prompt)
        )
        print(
            "RouterAI analysis pass parse failed; retrying analysis pass once: "
            f"external_id={external_id} reason=non_json_response "
            f"retry_strategy={analysis_retry_context_strategy} "
            f"max_spec_chars={analysis_retry_max_spec_chars} "
            f"max_other_chars={analysis_retry_max_other_chars} "
            f"max_output_tokens={analysis_retry_max_output_tokens}",
            flush=True,
        )

        analysis_response = generate_chat_completion_with_thinking(
            client,
            thinking_enabled=thinking_enabled_for_analysis,
            system_prompt=ROUTERAI_ANALYSIS_SYSTEM_PROMPT,
            user_prompt=retry_analysis_user_prompt,
            temperature=0.1,
            max_tokens=analysis_retry_max_output_tokens,
            json_mode=request_json_mode,
        )
        analysis_retry_finish_reason = llm_finish_reason(analysis_response)
        analysis_metadata_prompt = retry_analysis_user_prompt
        analysis_metadata_prompt_chars = retry_analysis_prompt_chars
        analysis_metadata_context = retry_context_markdown
        analysis_metadata_output_token_cap = analysis_retry_max_output_tokens
        raw_analysis = parse_routerai_pass_json(
            external_id=external_id,
            result_label=result_label,
            response=analysis_response,
            pass_stage="analysis",
            user_prompt=retry_analysis_user_prompt,
            prompt_chars=retry_analysis_prompt_chars,
            llm_context_chars=len(retry_context_markdown),
            output_token_cap=analysis_retry_max_output_tokens,
            json_mode=request_json_mode,
            extra_meta={
                "analysis_retry_used": analysis_retry_used,
                "analysis_retry_reason": raw_response_excerpt(analysis_retry_reason, 1000),
                "analysis_retry_max_spec_chars": analysis_retry_max_spec_chars,
                "analysis_retry_max_other_chars": analysis_retry_max_other_chars,
                "analysis_retry_max_output_tokens": analysis_retry_max_output_tokens,
                "analysis_retry_context_strategy": analysis_retry_context_strategy,
                "analysis_initial_finish_reason": analysis_initial_finish_reason,
                "analysis_retry_finish_reason": analysis_retry_finish_reason,
            },
        )
    analysis = normalize_routerai_analysis(raw_analysis, analysis_depth=analysis_depth)

    analysis_path = save_routerai_analysis_artifact(
        external_id=external_id,
        result_label=result_label,
        response=analysis_response,
        analysis=analysis,
        context_chars=len(analysis_metadata_prompt),
        prompt_chars=analysis_metadata_prompt_chars,
        llm_context_chars=len(analysis_metadata_context),
        output_token_cap=analysis_metadata_output_token_cap,
        json_mode=request_json_mode,
    )

    packaging_user_prompt = build_routerai_packaging_user_prompt(
        analysis,
        analysis_depth=analysis_depth,
    )
    packaging_prompt_chars = len(ROUTERAI_PACKAGING_SYSTEM_PROMPT) + len(packaging_user_prompt)

    try:
        packaging_response = generate_chat_completion_with_thinking(
            client,
            thinking_enabled=False,
            system_prompt=ROUTERAI_PACKAGING_SYSTEM_PROMPT,
            user_prompt=packaging_user_prompt,
            temperature=0.1,
            max_tokens=max_output_tokens,
            json_mode=request_json_mode,
        )
    except Exception as exc:
        raise RuntimeError(
            f"RouterAI packaging pass failed after analysis artifact was saved: "
            f"analysis_artifact={analysis_path} error={exc}"
        ) from exc

    packaging_retry_count = 0
    packaging_first_parse_error: str | None = None
    packaging_first_finish_reason: str | None = None
    packaging_metadata_prompt = packaging_user_prompt
    packaging_metadata_prompt_chars = packaging_prompt_chars

    try:
        raw_report = parse_routerai_pass_json_payload(
            response=packaging_response,
            pass_stage="packaging",
        )
    except Exception as exc:
        packaging_retry_count = 1
        packaging_first_parse_error = str(exc)
        packaging_first_finish_reason = llm_finish_reason(packaging_response)
        retry_packaging_user_prompt = (
            f"{packaging_user_prompt}\n\n{ROUTERAI_PACKAGING_RETRY_INSTRUCTION}"
        )
        retry_packaging_prompt_chars = (
            len(ROUTERAI_PACKAGING_SYSTEM_PROMPT) + len(retry_packaging_user_prompt)
        )
        print(
            "RouterAI packaging pass parse failed; retrying packaging pass once: "
            f"external_id={external_id} reason=non_json_response "
            f"analysis_artifact={analysis_path}",
            flush=True,
        )

        try:
            packaging_response = generate_chat_completion_with_thinking(
                client,
                thinking_enabled=False,
                system_prompt=ROUTERAI_PACKAGING_SYSTEM_PROMPT,
                user_prompt=retry_packaging_user_prompt,
                temperature=0.1,
                max_tokens=max_output_tokens,
                json_mode=request_json_mode,
            )
        except Exception as retry_exc:
            raise RuntimeError(
                f"RouterAI packaging retry failed after analysis artifact was saved: "
                f"analysis_artifact={analysis_path} "
                f"first_parse_error={packaging_first_parse_error} "
                f"error={retry_exc}"
            ) from retry_exc

        packaging_metadata_prompt = retry_packaging_user_prompt
        packaging_metadata_prompt_chars = retry_packaging_prompt_chars
        raw_report = parse_routerai_pass_json(
            external_id=external_id,
            result_label=result_label,
            response=packaging_response,
            pass_stage="packaging",
            user_prompt=retry_packaging_user_prompt,
            prompt_chars=retry_packaging_prompt_chars,
            llm_context_chars=len(retry_packaging_user_prompt),
            output_token_cap=max_output_tokens,
            json_mode=request_json_mode,
            extra_meta={
                "analysis_artifact_path": str(analysis_path),
                "analysis_finish_reason": llm_finish_reason(analysis_response),
                "packaging_retry_count": packaging_retry_count,
                "packaging_first_finish_reason": packaging_first_finish_reason,
                "packaging_first_parse_error": raw_response_excerpt(
                    packaging_first_parse_error,
                    1000,
                ),
            },
        )
    parsed = normalize_routerai_final_report(raw_report, fallback=analysis)
    enrich_routerai_report_with_package_facts(parsed, package)
    apply_domestic_vendor_guardrail(parsed, package)
    apply_tz_problems_guardrail(parsed)

    metadata = llm_response_metadata(
        packaging_response,
        parse_status="ok",
        json_mode=request_json_mode,
        prompt_chars=packaging_metadata_prompt_chars,
        context_chars=len(packaging_metadata_prompt),
        output_token_cap=max_output_tokens,
    )
    metadata.update(
        {
            "pass_mode": "routerai_two_pass",
            "analysis_depth": analysis_depth,
            "thinking_enabled_for_analysis": thinking_enabled_for_analysis,
            "analysis_finish_reason": llm_finish_reason(analysis_response),
            "analysis_retry_used": analysis_retry_used,
            "analysis_retry_reason": raw_response_excerpt(analysis_retry_reason, 1000),
            "analysis_retry_max_spec_chars": analysis_retry_max_spec_chars,
            "analysis_retry_max_other_chars": analysis_retry_max_other_chars,
            "analysis_retry_max_output_tokens": analysis_retry_max_output_tokens,
            "analysis_retry_context_strategy": analysis_retry_context_strategy,
            "analysis_initial_finish_reason": analysis_initial_finish_reason,
            "packaging_finish_reason": llm_finish_reason(packaging_response),
            "analysis_artifact_path": str(analysis_path),
            "analysis_prompt_chars": analysis_metadata_prompt_chars,
            "packaging_prompt_chars": packaging_metadata_prompt_chars,
            "analysis_output_token_cap": analysis_metadata_output_token_cap,
            "packaging_retry_count": packaging_retry_count,
        }
    )
    metadata.update(llm_context_quality_metadata(package))
    metadata["analysis_depth"] = analysis_depth
    metadata["report_kind"] = REPORT_KIND_TECHNICAL
    metadata["thinking_enabled_for_analysis"] = thinking_enabled_for_analysis
    if analysis_retry_finish_reason:
        metadata["analysis_retry_finish_reason"] = analysis_retry_finish_reason
    if packaging_first_finish_reason:
        metadata["packaging_first_finish_reason"] = packaging_first_finish_reason
    if packaging_first_parse_error:
        metadata["packaging_first_parse_error"] = raw_response_excerpt(
            packaging_first_parse_error,
            1000,
        )
    if analysis_response.usage:
        metadata["analysis_usage"] = analysis_response.usage

    analysis_type = analysis_type_for_label(result_label)
    total_context_chars = len(analysis_metadata_prompt) + len(packaging_metadata_prompt)

    if save_to_db:
        save_llm_report(
            tender_id=tender_id,
            provider=packaging_response.provider,
            model=packaging_response.model,
            parsed_report=parsed,
            raw_response=packaging_response.text or "",
            context_chars=total_context_chars,
            analysis_type=analysis_type,
            metadata=metadata,
        )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    stem = output_stem(external_id, result_label)
    json_path = REPORT_DIR / f"{stem}.json"
    md_path = REPORT_DIR / f"{stem}.md"

    json_path.write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown = routerai_two_pass_report_to_markdown(parsed, package)
    md_path.write_text(markdown, encoding="utf-8")

    return {
        "external_id": external_id,
        "provider": packaging_response.provider,
        "model": packaging_response.model,
        "analysis_type": analysis_type,
        "report_kind": REPORT_KIND_TECHNICAL,
        "analysis_depth": analysis_depth,
        "context_chars": total_context_chars,
        "llm_metadata": metadata,
        "report": parsed,
        "markdown": markdown,
        "json_path": str(json_path),
        "md_path": str(md_path),
        "analysis_path": str(analysis_path),
        "context_path": str(context_path),
    }


def run_lead_report(
    *,
    external_id: str,
    tender_id: str,
    package: dict[str, Any],
    max_output_tokens: int,
    provider: str | None = None,
    model: str | None = None,
    json_mode: bool | None = None,
    result_label: str | None = None,
    save_to_db: bool = True,
) -> dict[str, Any]:
    context_markdown = package_to_markdown(package)
    context_path, _ = write_context_files(
        external_id=external_id,
        package=package,
        context_markdown=context_markdown,
    )

    effective_json_mode = settings.llm_json_mode if json_mode is None else json_mode
    client = create_llm_client(provider=provider, model=model)
    user_prompt = build_lead_report_user_prompt(context_markdown)
    system_prompt = LEAD_REPORT_SYSTEM_PROMPT
    prompt_chars = len(system_prompt) + len(user_prompt)
    request_json_mode = bool(effective_json_mode and client.provider == "routerai")

    response = client.generate_chat_completion(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.1,
        max_tokens=max_output_tokens,
        json_mode=request_json_mode,
    )
    parse_error_reason = diagnose_empty_llm_response(response)

    try:
        if parse_error_reason:
            raise RuntimeError(parse_error_reason)
        raw_report = extract_json(response.text)
        parsed = normalize_lead_report(raw_report, package=package)
    except Exception as exc:
        error_path = save_llm_error_metadata(
            external_id=external_id,
            result_label=result_label,
            report_kind=REPORT_KIND_LEAD,
            response=response,
            context_chars=len(user_prompt),
            prompt_chars=prompt_chars,
            llm_context_chars=len(context_markdown),
            output_token_cap=max_output_tokens,
            json_mode=request_json_mode,
            error=exc,
            parse_error_reason=parse_error_reason,
            extra_meta={"report_kind": REPORT_KIND_LEAD},
        )
        reason_part = f" reason={parse_error_reason}" if parse_error_reason else ""
        raise RuntimeError(
            "Cannot parse JSON from LLM response: "
            f"provider={response.provider} model={response.model} "
            f"error_metadata={error_path}{reason_part} "
            f"raw_excerpt={raw_response_excerpt(response.text, 2000)}"
        ) from exc

    metadata = llm_response_metadata(
        response,
        parse_status="ok",
        json_mode=request_json_mode,
        prompt_chars=prompt_chars,
        context_chars=len(context_markdown),
        output_token_cap=max_output_tokens,
    )
    metadata.update(llm_context_quality_metadata(package))
    metadata["report_kind"] = REPORT_KIND_LEAD
    selection = metadata.get("document_selection") or {}
    if isinstance(selection, dict):
        print(
            "Lead document selector: "
            f"external_id={external_id} "
            f"lead_selector_used={str(bool(selection.get('selector_used'))).lower()} "
            f"lead_selector_failed={str(bool(selection.get('selector_failed'))).lower()} "
            f"selector_status={selection.get('selector_status') or selection.get('status') or ''} "
            f"primary_document_confidence={selection.get('primary_document_confidence') or ''} "
            f"primary_document_source_kind={selection.get('primary_document_source_kind') or ''} "
            f"primary_document_is_full_technical_spec={str(bool(selection.get('primary_document_is_full_technical_spec'))).lower()} "
            f"primary_document_title={selection.get('primary_document_title') or ''} "
            f"primary_section_hint={selection.get('primary_section_hint') or ''} "
            f"docs_count={len(package.get('documents_summary') or [])} "
            f"docs_with_text_count={metadata.get('documents_with_text') or 0}",
            flush=True,
        )

    analysis_type = analysis_type_for_label(
        result_label,
        report_kind=REPORT_KIND_LEAD,
    )
    if save_to_db:
        save_llm_report(
            tender_id=tender_id,
            provider=response.provider,
            model=response.model,
            parsed_report=parsed,
            raw_response=response.text,
            context_chars=len(user_prompt),
            analysis_type=analysis_type,
            metadata=metadata,
        )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    stem = output_stem(external_id, result_label, report_kind=REPORT_KIND_LEAD)
    json_path = REPORT_DIR / f"{stem}.json"
    md_path = REPORT_DIR / f"{stem}.md"
    markdown = lead_report_to_markdown(parsed, package)

    json_path.write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(markdown, encoding="utf-8")

    return {
        "external_id": external_id,
        "provider": response.provider,
        "model": response.model,
        "analysis_type": analysis_type,
        "report_kind": REPORT_KIND_LEAD,
        "context_chars": len(user_prompt),
        "llm_metadata": metadata,
        "report": parsed,
        "markdown": markdown,
        "json_path": str(json_path),
        "md_path": str(md_path),
        "context_path": str(context_path),
    }


def run_presales_report(
    *,
    external_id: str,
    tender_id: str,
    package: dict[str, Any],
    max_output_tokens: int,
    analysis_depth: str = ANALYSIS_DEPTH_STANDARD,
    report_kind: str = REPORT_KIND_TECHNICAL,
    provider: str | None = None,
    model: str | None = None,
    json_mode: bool | None = None,
    result_label: str | None = None,
    save_to_db: bool = True,
) -> dict[str, Any]:
    analysis_depth = normalize_analysis_depth(analysis_depth)
    report_kind = normalize_report_kind(report_kind)
    if report_kind == REPORT_KIND_LEAD:
        return run_lead_report(
            external_id=external_id,
            tender_id=tender_id,
            package=package,
            max_output_tokens=max_output_tokens,
            provider=provider,
            model=model,
            json_mode=json_mode,
            result_label=result_label,
            save_to_db=save_to_db,
        )

    context_markdown = package_to_markdown(package)

    context_path, _ = write_context_files(
        external_id=external_id,
        package=package,
        context_markdown=context_markdown,
    )

    effective_json_mode = settings.llm_json_mode if json_mode is None else json_mode
    client = create_llm_client(provider=provider, model=model)
    compact_report = compact_report_enabled(client.provider)
    if compact_report:
        return run_routerai_two_pass_report(
            external_id=external_id,
            tender_id=tender_id,
            package=package,
            context_markdown=context_markdown,
            context_path=context_path,
            client=client,
            max_output_tokens=max_output_tokens,
            effective_json_mode=bool(effective_json_mode),
            result_label=result_label,
            save_to_db=save_to_db,
            analysis_depth=analysis_depth,
        )

    user_prompt = build_user_prompt(context_markdown)
    system_prompt = SYSTEM_PROMPT
    prompt_chars = len(system_prompt) + len(user_prompt)
    request_json_mode = bool(effective_json_mode and client.provider == "routerai")
    response = client.generate_chat_completion(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.1,
        max_tokens=max_output_tokens,
        json_mode=request_json_mode,
    )

    parse_error_reason = diagnose_empty_llm_response(response)

    try:
        if parse_error_reason:
            raise RuntimeError(parse_error_reason)

        raw_report = extract_json(response.text)
        parsed = enrich_report_with_extracted_facts(raw_report, package)
    except Exception as exc:
        error_path = save_llm_error_metadata(
            external_id=external_id,
            result_label=result_label,
            response=response,
            context_chars=len(user_prompt),
            prompt_chars=prompt_chars,
            llm_context_chars=len(context_markdown),
            output_token_cap=max_output_tokens,
            json_mode=request_json_mode,
            error=exc,
            parse_error_reason=parse_error_reason,
        )
        reason_part = f" reason={parse_error_reason}" if parse_error_reason else ""
        raise RuntimeError(
            "Cannot parse JSON from LLM response: "
            f"provider={response.provider} model={response.model} "
            f"error_metadata={error_path}{reason_part} "
            f"raw_excerpt={raw_response_excerpt(response.text, 2000)}"
        ) from exc

    metadata = llm_response_metadata(
        response,
        parse_status="ok",
        json_mode=request_json_mode,
        prompt_chars=prompt_chars,
        context_chars=len(context_markdown),
        output_token_cap=max_output_tokens,
    )
    metadata["analysis_depth"] = analysis_depth
    metadata.update(llm_context_quality_metadata(package))
    metadata["analysis_depth"] = analysis_depth
    metadata["report_kind"] = REPORT_KIND_TECHNICAL
    analysis_type = analysis_type_for_label(result_label)

    if save_to_db:
        save_llm_report(
            tender_id=tender_id,
            provider=response.provider,
            model=response.model,
            parsed_report=parsed,
            raw_response=response.text,
            context_chars=len(user_prompt),
            analysis_type=analysis_type,
            metadata=metadata,
        )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    stem = output_stem(external_id, result_label)
    json_path = REPORT_DIR / f"{stem}.json"
    md_path = REPORT_DIR / f"{stem}.md"

    json_path.write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(report_to_markdown(parsed), encoding="utf-8")

    return {
        "external_id": external_id,
        "provider": response.provider,
        "model": response.model,
        "analysis_type": analysis_type,
        "report_kind": REPORT_KIND_TECHNICAL,
        "analysis_depth": analysis_depth,
        "context_chars": len(user_prompt),
        "llm_metadata": metadata,
        "report": parsed,
        "markdown": report_to_markdown(parsed),
        "json_path": str(json_path),
        "md_path": str(md_path),
        "context_path": str(context_path),
    }


def generate_presales_report(
    *,
    external_id: str,
    analysis_depth: str = ANALYSIS_DEPTH_DEEP,
    report_kind: str = REPORT_KIND_TECHNICAL,
    max_spec_chars: int | None = None,
    max_other_chars: int | None = None,
    max_output_tokens: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    json_mode: bool | None = None,
    result_label: str | None = None,
    save_to_db: bool = True,
) -> dict[str, Any]:
    analysis_depth = normalize_analysis_depth(analysis_depth)
    report_kind = normalize_report_kind(report_kind)
    limits = resolve_analysis_limits(
        analysis_depth,
        max_spec_chars=max_spec_chars,
        max_other_chars=max_other_chars,
        max_output_tokens=max_output_tokens,
    )
    tender_id, package = build_package_from_database(
        external_id=external_id,
        max_spec_chars=limits.max_spec_chars,
        max_other_chars=limits.max_other_chars,
        analysis_depth=analysis_depth,
        report_kind=report_kind,
    )

    return run_presales_report(
        external_id=external_id,
        tender_id=tender_id,
        package=package,
        max_output_tokens=limits.max_output_tokens,
        analysis_depth=analysis_depth,
        report_kind=report_kind,
        provider=provider,
        model=model,
        json_mode=json_mode,
        result_label=result_label,
        save_to_db=save_to_db,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate LLM presales report")
    parser.add_argument("--external-id")
    parser.add_argument("--context-json-path", help="Use an already frozen LLM context package JSON")
    parser.add_argument("--provider", help="Override LLM provider for this run: gigachat or routerai")
    parser.add_argument("--model", help="Override LLM model for this run")
    parser.add_argument("--json-mode", action="store_true", help="Request response_format=json_object from compatible providers")
    parser.add_argument("--result-label", help="Write DB/file result under a comparison label without replacing the default report")
    parser.add_argument("--no-save-db", action="store_true", help="Write report files only")
    parser.add_argument(
        "--report-kind",
        choices=REPORT_KIND_CHOICES,
        default=REPORT_KIND_TECHNICAL,
        help="Report mode: technical presales report or customer lead report",
    )
    parser.add_argument(
        "--analysis-depth",
        choices=ANALYSIS_DEPTH_CHOICES,
        default=ANALYSIS_DEPTH_DEEP,
    )
    parser.add_argument("--max-spec-chars", type=int, default=None)
    parser.add_argument("--max-other-chars", type=int, default=None)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--document-readiness-json", help=argparse.SUPPRESS)
    args = parser.parse_args()
    analysis_depth = normalize_analysis_depth(args.analysis_depth)
    report_kind = normalize_report_kind(args.report_kind)
    limits = resolve_analysis_limits(
        analysis_depth,
        max_spec_chars=args.max_spec_chars,
        max_other_chars=args.max_other_chars,
        max_output_tokens=args.max_output_tokens,
    )

    if args.context_json_path:
        context_json_path = Path(args.context_json_path)
        package = load_package_from_context_json(context_json_path)
        external_id = args.external_id or package_external_id(package)
        tender_id = package_tender_id(package)
    else:
        if not args.external_id:
            parser.error("--external-id is required unless --context-json-path is used")

        external_id = args.external_id
        build_kwargs = {
            "external_id": external_id,
            "max_spec_chars": limits.max_spec_chars,
            "max_other_chars": limits.max_other_chars,
            "analysis_depth": analysis_depth,
            "report_kind": report_kind,
        }
        document_readiness = parse_document_readiness_json(args.document_readiness_json)
        if document_readiness is not None:
            build_kwargs["document_readiness"] = document_readiness
        tender_id, package = build_package_from_database(**build_kwargs)

    result = run_presales_report(
        external_id=external_id,
        tender_id=tender_id,
        package=package,
        max_output_tokens=limits.max_output_tokens,
        analysis_depth=analysis_depth,
        report_kind=report_kind,
        provider=args.provider,
        model=args.model,
        json_mode=args.json_mode or settings.llm_json_mode,
        result_label=args.result_label,
        save_to_db=not args.no_save_db,
    )

    print(f"Context chars: {result['context_chars']}")
    print(f"Report kind: {report_kind}")
    print(f"Analysis depth: {analysis_depth}")
    print(f"Max spec chars: {limits.max_spec_chars}")
    print(f"Max other chars: {limits.max_other_chars}")
    print(f"Max output tokens: {limits.max_output_tokens}")
    print(f"Provider: {result['provider']}")
    print(f"Model: {result['model']}")
    print(f"Analysis type: {result['analysis_type']}")
    print("LLM report created:")
    print(f"- {result['json_path']}")
    print(f"- {result['md_path']}")
    if result.get("analysis_path"):
        print(f"- {result['analysis_path']}")
    print(f"- {result['context_path']}")
    print()
    print("Markdown report saved. Use cat to view it.")


if __name__ == "__main__":
    main()
