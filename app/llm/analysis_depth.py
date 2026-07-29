from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ANALYSIS_DEPTH_STANDARD = "standard"
ANALYSIS_DEPTH_DEEP = "deep"
ANALYSIS_DEPTH_CHOICES = (ANALYSIS_DEPTH_STANDARD, ANALYSIS_DEPTH_DEEP)


@dataclass(frozen=True)
class AnalysisLimits:
    max_spec_chars: int
    max_other_chars: int
    max_output_tokens: int


STANDARD_ANALYSIS_LIMITS = AnalysisLimits(
    max_spec_chars=90_000,
    max_other_chars=12_000,
    max_output_tokens=4_096,
)
DEEP_ANALYSIS_LIMITS = AnalysisLimits(
    max_spec_chars=240_000,
    max_other_chars=40_000,
    max_output_tokens=16_384,
)


def normalize_analysis_depth(value: Any) -> str:
    depth = str(value or ANALYSIS_DEPTH_DEEP).strip().lower()
    if depth in ANALYSIS_DEPTH_CHOICES:
        return depth
    raise ValueError(
        "analysis_depth must be one of: " + ", ".join(ANALYSIS_DEPTH_CHOICES)
    )


def default_limits_for_depth(analysis_depth: str) -> AnalysisLimits:
    depth = normalize_analysis_depth(analysis_depth)
    if depth == ANALYSIS_DEPTH_STANDARD:
        return STANDARD_ANALYSIS_LIMITS
    return DEEP_ANALYSIS_LIMITS


def resolve_analysis_limits(
    analysis_depth: str,
    *,
    max_spec_chars: int | None = None,
    max_other_chars: int | None = None,
    max_output_tokens: int | None = None,
) -> AnalysisLimits:
    defaults = default_limits_for_depth(analysis_depth)
    return AnalysisLimits(
        max_spec_chars=int(max_spec_chars or defaults.max_spec_chars),
        max_other_chars=int(max_other_chars or defaults.max_other_chars),
        max_output_tokens=int(max_output_tokens or defaults.max_output_tokens),
    )
