"""Deterministic lead-triage classification and hard-gate evaluation."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from math import ceil
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.llm.contracts import validate_lead_triage_output
from app.platform.versioning import sha256_json


Decision = Literal["go", "maybe", "reject"]


class LeadTriageGoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    case_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    provenance: str = Field(min_length=1, max_length=500)
    privacy_class: Literal["synthetic", "internal_sanitized", "sensitive_local_only"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_decision: Decision
    business_weight: Decimal = Field(gt=0)
    critical_wrong_decisions: list[Decision] = Field(default_factory=list, max_length=3)
    tags: list[str] = Field(default_factory=list, max_length=20)
    is_real_case: bool


class LeadTriageDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset_version: str = Field(min_length=1, max_length=100)
    task_type: Literal["classification"] = "classification"
    cases: list[LeadTriageGoldenCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_cases(self) -> "LeadTriageDataset":
        identities = [case.case_id for case in self.cases]
        if len(identities) != len(set(identities)):
            raise ValueError("case_id values must be unique")
        return self

    @property
    def dataset_sha256(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


class LeadTriagePrediction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    case_id: str = Field(min_length=1, max_length=120)
    status: Literal["completed", "abstained", "failed"]
    output: dict[str, Any] | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    cost_units: Decimal | None = Field(default=None, ge=0)
    fallback_used: bool = False
    human_review_required: bool = False
    human_review_completed: bool = False
    grounding_review: Literal["passed", "failed", "not_reviewed"] = "not_reviewed"

    @model_validator(mode="after")
    def validate_output_presence(self) -> "LeadTriagePrediction":
        if self.status == "completed" and self.output is None:
            raise ValueError("completed prediction requires output")
        if self.status != "completed" and self.output is not None:
            raise ValueError("non-completed prediction must not include output")
        if self.human_review_completed and not self.human_review_required:
            raise ValueError("human_review_completed requires human_review_required")
        return self


class LeadTriageEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_cases: int = Field(ge=1)
    real_case_count: int = Field(ge=0)
    structured_valid_rate: float = Field(ge=0, le=1)
    decision_accuracy: float = Field(ge=0, le=1)
    business_loss_score: float = Field(ge=0, le=1)
    critical_error_count: int = Field(ge=0)
    abstention_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    human_review_rate: float = Field(ge=0, le=1)
    human_review_completion_rate: float = Field(ge=0, le=1)
    fallback_rate: float = Field(ge=0, le=1)
    grounding_review_pass_rate: float = Field(ge=0, le=1)
    latency_coverage_rate: float = Field(ge=0, le=1)
    p95_latency_ms: int | None = Field(default=None, ge=0)
    cost_coverage_rate: float = Field(ge=0, le=1)
    average_cost_units: float | None = Field(default=None, ge=0)
    confusion: dict[str, int]
    operational_thresholds_bound: bool
    release_evidence_eligible: bool
    release_passed: bool


def evaluate_lead_triage(
    dataset: LeadTriageDataset,
    predictions: list[LeadTriagePrediction],
    *,
    semantic_threshold: float = 0.8,
    structured_threshold: float = 1.0,
    business_loss_threshold: float = 0.2,
    max_p95_latency_ms: int | None = None,
    max_average_cost_units: Decimal | None = None,
    max_fallback_rate: float | None = None,
    max_human_review_rate: float | None = None,
) -> LeadTriageEvaluationResult:
    if max_p95_latency_ms is not None and max_p95_latency_ms < 0:
        raise ValueError("max_p95_latency_ms must be non-negative")
    if max_average_cost_units is not None and max_average_cost_units < 0:
        raise ValueError("max_average_cost_units must be non-negative")
    for name, threshold in (
        ("max_fallback_rate", max_fallback_rate),
        ("max_human_review_rate", max_human_review_rate),
    ):
        if threshold is not None and not 0 <= threshold <= 1:
            raise ValueError(f"{name} must be between 0 and 1")

    by_case = {prediction.case_id: prediction for prediction in predictions}
    known_ids = {case.case_id for case in dataset.cases}
    unknown_ids = sorted(set(by_case) - known_ids)
    if unknown_ids:
        raise ValueError(f"predictions contain unknown case ids: {', '.join(unknown_ids)}")
    if len(by_case) != len(predictions):
        raise ValueError("prediction case_id values must be unique")

    valid_outputs = 0
    correct = 0
    critical_errors = 0
    weighted_errors = Decimal("0")
    total_weight = sum((case.business_weight for case in dataset.cases), Decimal("0"))
    abstentions = 0
    failures = 0
    human_review = 0
    human_review_completed = 0
    fallbacks = 0
    grounding_passed = 0
    latencies: list[int] = []
    costs: list[Decimal] = []
    confusion: Counter[str] = Counter()

    for case in dataset.cases:
        prediction = by_case.get(case.case_id)
        if prediction is None:
            predicted = "missing"
            failures += 1
        elif prediction.status == "abstained":
            predicted = "abstained"
            abstentions += 1
        elif prediction.status == "failed":
            predicted = "failed"
            failures += 1
        else:
            try:
                validated = validate_lead_triage_output(prediction.output)
            except ValueError:
                predicted = "invalid"
            else:
                valid_outputs += 1
                predicted = str(validated["lead_decision"])

        if prediction is not None:
            human_review += int(prediction.human_review_required)
            human_review_completed += int(prediction.human_review_completed)
            fallbacks += int(prediction.fallback_used)
            grounding_passed += int(prediction.grounding_review == "passed")
            if prediction.latency_ms is not None:
                latencies.append(prediction.latency_ms)
            if prediction.cost_units is not None:
                costs.append(prediction.cost_units)
        confusion[f"{case.expected_decision}->{predicted}"] += 1
        if predicted == case.expected_decision:
            correct += 1
            continue
        weighted_errors += case.business_weight
        if predicted in case.critical_wrong_decisions or predicted in {"invalid", "missing"}:
            critical_errors += 1

    total = len(dataset.cases)
    structured_rate = valid_outputs / total
    accuracy = correct / total
    business_loss = float(weighted_errors / total_weight)
    real_cases = sum(1 for case in dataset.cases if case.is_real_case)
    latency_coverage = len(latencies) / total
    cost_coverage = len(costs) / total
    sorted_latencies = sorted(latencies)
    p95_latency = (
        sorted_latencies[max(0, ceil(0.95 * len(sorted_latencies)) - 1)]
        if sorted_latencies
        else None
    )
    average_cost = float(sum(costs, Decimal("0")) / len(costs)) if costs else None
    human_review_rate = human_review / total
    fallback_rate = fallbacks / total
    grounding_pass_rate = grounding_passed / total
    operational_thresholds_bound = all(
        threshold is not None
        for threshold in (
            max_p95_latency_ms,
            max_average_cost_units,
            max_fallback_rate,
            max_human_review_rate,
        )
    )
    operational_metrics_complete = latency_coverage == 1.0 and cost_coverage == 1.0
    review_evidence_complete = (
        grounding_pass_rate == 1.0 and human_review_completed == human_review
    )
    release_eligible = (
        real_cases > 0
        and operational_thresholds_bound
        and operational_metrics_complete
        and review_evidence_complete
    )
    release_passed = (
        release_eligible
        and structured_rate >= structured_threshold
        and accuracy >= semantic_threshold
        and business_loss <= business_loss_threshold
        and critical_errors == 0
        and p95_latency is not None
        and p95_latency <= max_p95_latency_ms
        and average_cost is not None
        and average_cost <= float(max_average_cost_units)
        and fallback_rate <= max_fallback_rate
        and human_review_rate <= max_human_review_rate
    )
    return LeadTriageEvaluationResult(
        dataset_version=dataset.dataset_version,
        dataset_sha256=dataset.dataset_sha256,
        total_cases=total,
        real_case_count=real_cases,
        structured_valid_rate=structured_rate,
        decision_accuracy=accuracy,
        business_loss_score=business_loss,
        critical_error_count=critical_errors,
        abstention_count=abstentions,
        failed_count=failures,
        human_review_rate=human_review_rate,
        human_review_completion_rate=(
            human_review_completed / human_review if human_review else 1.0
        ),
        fallback_rate=fallback_rate,
        grounding_review_pass_rate=grounding_pass_rate,
        latency_coverage_rate=latency_coverage,
        p95_latency_ms=p95_latency,
        cost_coverage_rate=cost_coverage,
        average_cost_units=average_cost,
        confusion=dict(sorted(confusion.items())),
        operational_thresholds_bound=operational_thresholds_bound,
        release_evidence_eligible=release_eligible,
        release_passed=release_passed,
    )
