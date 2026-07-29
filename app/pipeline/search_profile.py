from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


DEFAULT_PROFILE_PATH = Path("config/search_profile.yaml")


@dataclass(frozen=True)
class QuerySpec:
    pack: str
    query: str


@dataclass
class QueryRunSummary:
    pack: str
    query: str
    collected: int = 0
    external_ids: set[str] = field(default_factory=set)
    new_external_ids: set[str] = field(default_factory=set)
    saved_or_updated: int = 0
    skipped_existing: int = 0
    full_load_attempts: int = 0
    failed_full_loads: int = 0
    scored_external_ids: set[str] = field(default_factory=set)
    rule_based_passed_external_ids: set[str] = field(default_factory=set)

    @property
    def unique(self) -> int:
        return len(self.external_ids)

    @property
    def new(self) -> int:
        return len(self.new_external_ids)

    @property
    def scored(self) -> int:
        return len(self.scored_external_ids)

    @property
    def rule_based_passed(self) -> int:
        return len(self.rule_based_passed_external_ids)


@dataclass
class AggregateRunSummary:
    label: str
    collected: int = 0
    external_ids: set[str] = field(default_factory=set)
    new_external_ids: set[str] = field(default_factory=set)
    saved_or_updated: int = 0
    skipped_existing: int = 0
    full_load_attempts: int = 0
    failed_full_loads: int = 0
    scored_external_ids: set[str] = field(default_factory=set)
    rule_based_passed_external_ids: set[str] = field(default_factory=set)

    @property
    def unique(self) -> int:
        return len(self.external_ids)

    @property
    def new(self) -> int:
        return len(self.new_external_ids)

    @property
    def scored(self) -> int:
        return len(self.scored_external_ids)

    @property
    def rule_based_passed(self) -> int:
        return len(self.rule_based_passed_external_ids)


def load_search_profile(path: Path = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    import yaml

    if not path.exists():
        raise FileNotFoundError(f"Search profile not found: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Search profile must be a mapping: {path}")

    validate_profile(data)
    return data


def validate_profile(profile: dict[str, Any]) -> None:
    packs = profile.get("packs")
    if not isinstance(packs, dict) or not packs:
        raise ValueError("search profile must contain non-empty 'packs' mapping")

    for pack_name, pack_cfg in packs.items():
        if not isinstance(pack_name, str) or not pack_name:
            raise ValueError("pack name must be a non-empty string")
        if not isinstance(pack_cfg, dict):
            raise ValueError(f"pack '{pack_name}' must be a mapping")

        queries = pack_cfg.get("queries")
        if not isinstance(queries, list) or not queries:
            raise ValueError(f"pack '{pack_name}' must contain non-empty queries list")

        bad_queries = [
            query
            for query in queries
            if not isinstance(query, str) or not query.strip()
        ]
        if bad_queries:
            raise ValueError(f"pack '{pack_name}' contains empty or non-string queries")

    groups = profile.get("groups") or {}
    if not isinstance(groups, dict):
        raise ValueError("'groups' must be a mapping when present")

    for group_name, group_cfg in groups.items():
        group_packs = group_pack_names(group_cfg)
        if not group_packs:
            raise ValueError(f"group '{group_name}' must contain non-empty packs list")
        for pack_name in group_packs:
            if pack_name not in packs:
                raise ValueError(f"group '{group_name}' references unknown pack '{pack_name}'")


def group_pack_names(group_cfg: Any) -> list[str]:
    if isinstance(group_cfg, list):
        return [str(value) for value in group_cfg if value]

    if isinstance(group_cfg, dict):
        values = group_cfg.get("packs") or []
        if isinstance(values, list):
            return [str(value) for value in values if value]

    return []


def list_pack_names(profile: dict[str, Any]) -> list[str]:
    return list((profile.get("packs") or {}).keys())


def list_group_names(profile: dict[str, Any]) -> list[str]:
    return list((profile.get("groups") or {}).keys())


def selected_pack_names(
    profile: dict[str, Any],
    *,
    packs: Iterable[str] | None = None,
    groups: Iterable[str] | None = None,
) -> list[str]:
    available_packs = profile.get("packs") or {}
    available_groups = profile.get("groups") or {}

    selected: list[str] = []

    for group_name in groups or []:
        if group_name not in available_groups:
            raise ValueError(f"Unknown group: {group_name}")
        selected.extend(group_pack_names(available_groups[group_name]))

    for pack_name in packs or []:
        if pack_name not in available_packs:
            raise ValueError(f"Unknown pack: {pack_name}")
        selected.append(pack_name)

    if not selected:
        raise ValueError("Select at least one --pack or --group, or use --list-packs")

    return list(dict.fromkeys(selected))


def query_specs_for_packs(profile: dict[str, Any], pack_names: Iterable[str]) -> list[QuerySpec]:
    packs = profile.get("packs") or {}
    specs: list[QuerySpec] = []

    for pack_name in pack_names:
        if pack_name not in packs:
            raise ValueError(f"Unknown pack: {pack_name}")

        queries = packs[pack_name].get("queries") or []
        for query in queries:
            query_text = str(query).strip()
            if query_text:
                specs.append(QuerySpec(pack=pack_name, query=query_text))

    return specs


def defaults(profile: dict[str, Any]) -> dict[str, Any]:
    values = dict(profile.get("defaults") or {})

    for key in ("days_back", "min_price", "limit_full_per_query"):
        if key in profile and key not in values:
            values[key] = profile[key]

    if "max_short_results_per_query" not in values:
        budget = profile.get("collector_budget") or {}
        if isinstance(budget, dict) and budget.get("recommended_max_short_results_per_query") is not None:
            values["max_short_results_per_query"] = budget["recommended_max_short_results_per_query"]

    return values


def aggregate_summaries(
    label: str,
    summaries: Iterable[QueryRunSummary],
) -> AggregateRunSummary:
    aggregate = AggregateRunSummary(label=label)

    for summary in summaries:
        aggregate.collected += summary.collected
        aggregate.external_ids.update(summary.external_ids)
        aggregate.new_external_ids.update(summary.new_external_ids)
        aggregate.saved_or_updated += summary.saved_or_updated
        aggregate.skipped_existing += summary.skipped_existing
        aggregate.full_load_attempts += summary.full_load_attempts
        aggregate.failed_full_loads += summary.failed_full_loads
        aggregate.scored_external_ids.update(summary.scored_external_ids)
        aggregate.rule_based_passed_external_ids.update(summary.rule_based_passed_external_ids)

    return aggregate


def aggregate_by_pack(summaries: Iterable[QueryRunSummary]) -> list[AggregateRunSummary]:
    by_pack: dict[str, list[QueryRunSummary]] = {}

    for summary in summaries:
        by_pack.setdefault(summary.pack, []).append(summary)

    return [
        aggregate_summaries(pack_name, pack_summaries)
        for pack_name, pack_summaries in by_pack.items()
    ]


def aggregate_total(summaries: Iterable[QueryRunSummary]) -> AggregateRunSummary:
    return aggregate_summaries("TOTAL", summaries)
