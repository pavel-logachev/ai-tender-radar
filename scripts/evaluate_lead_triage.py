"""Validate a lead-triage golden dataset and optionally score predictions."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from pydantic import TypeAdapter

from app.evaluation.lead_triage import (
    LeadTriageDataset,
    LeadTriagePrediction,
    evaluate_lead_triage,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--max-p95-latency-ms", type=int)
    parser.add_argument("--max-average-cost-units", type=Decimal)
    parser.add_argument("--max-fallback-rate", type=float)
    parser.add_argument("--max-human-review-rate", type=float)
    args = parser.parse_args()

    dataset = LeadTriageDataset.model_validate_json(
        args.dataset.read_text(encoding="utf-8"),
        strict=True,
    )
    if args.predictions is None:
        print(
            f"dataset_valid version={dataset.dataset_version} "
            f"cases={len(dataset.cases)} real_cases="
            f"{sum(1 for case in dataset.cases if case.is_real_case)} "
            f"sha256={dataset.dataset_sha256}"
        )
        return 0

    predictions = TypeAdapter(list[LeadTriagePrediction]).validate_json(
        args.predictions.read_text(encoding="utf-8"),
        strict=True,
    )
    result = evaluate_lead_triage(
        dataset,
        predictions,
        max_p95_latency_ms=args.max_p95_latency_ms,
        max_average_cost_units=args.max_average_cost_units,
        max_fallback_rate=args.max_fallback_rate,
        max_human_review_rate=args.max_human_review_rate,
    )
    print(result.model_dump_json(indent=2))
    return 0 if result.release_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
