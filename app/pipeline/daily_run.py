from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from app.digest import business_assessment, effective_recommendation, get_digest_rows


DEFAULT_PROFILE_PATH = Path("config/search_profile.yaml")


def load_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Search profile not found: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Search profile must be a mapping: {path}")

    return data


def run_cmd(args: list[str], *, dry_run: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    printable = " ".join(args)
    print(f"\n$ {printable}", flush=True)

    if dry_run:
        return subprocess.CompletedProcess(args=args, returncode=0)

    return subprocess.run(args, check=check)


def run_cmd_capture(args: list[str], output_path: Path, *, dry_run: bool = False) -> None:
    printable = " ".join(args)
    print(f"\n$ {printable} > {output_path}", flush=True)

    if dry_run:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as fh:
        subprocess.run(args, check=True, stdout=fh, text=True)


def py_module(module: str, *args: object) -> list[str]:
    return [sys.executable, "-m", module, *[str(arg) for arg in args]]


def collect_tenders(profile: dict[str, Any], *, dry_run: bool) -> None:
    queries = profile.get("queries") or []
    if not queries:
        raise ValueError("search_profile.yaml must contain non-empty queries list")

    days_back = int(profile.get("days_back", 14))
    min_price = int(profile.get("min_price", 500000))
    limit_full = int(profile.get("limit_full_per_query", 25))

    for query in queries:
        run_cmd(
            py_module(
                "app.collector.zakupki360",
                "--query",
                query,
                "--days-back",
                days_back,
                "--limit-full",
                limit_full,
                "--min-price",
                min_price,
            ),
            dry_run=dry_run,
        )


def run_documents_stage(profile: dict[str, Any], *, dry_run: bool) -> None:
    docs_cfg = profile.get("documents") or {}
    limit_tenders = int(docs_cfg.get("limit_tenders", 50))
    limit_docs = int(docs_cfg.get("limit_docs", 6))

    run_cmd(
        py_module(
            "app.collector.documents",
            "--limit-tenders",
            limit_tenders,
            "--limit-docs",
            limit_docs,
        ),
        dry_run=dry_run,
    )


def run_text_extraction(profile: dict[str, Any], *, dry_run: bool) -> None:
    cfg = profile.get("text_extraction") or {}
    limit = int(cfg.get("limit", 200))

    run_cmd(
        py_module("app.document_text_extractor", "--limit", limit),
        dry_run=dry_run,
    )


def run_document_risk(profile: dict[str, Any], *, dry_run: bool) -> None:
    cfg = profile.get("document_risk") or {}
    limit_tenders = int(cfg.get("limit_tenders", 50))

    run_cmd(
        py_module("app.document_risk_analyzer", "--limit-tenders", limit_tenders),
        dry_run=dry_run,
    )


def select_llm_candidate_ids(limit: int) -> list[str]:
    rows = get_digest_rows(limit=50)

    candidates: list[str] = []

    for row in sorted(rows, key=lambda item: item.get("score") or 0, reverse=True):
        if row.get("llm_report_result"):
            continue

        effective = effective_recommendation(row)
        if effective == "no_go":
            continue

        assessment = business_assessment(row)
        if assessment.get("action") == "skip_incumbent":
            continue

        external_id = row.get("external_id")
        if external_id:
            candidates.append(str(external_id))

        if len(candidates) >= limit:
            break

    return candidates


def run_llm_stage(profile: dict[str, Any], *, dry_run: bool, cli_enabled: bool, cli_limit: int | None) -> None:
    llm_cfg = profile.get("llm") or {}

    enabled = bool(llm_cfg.get("enabled", False)) or cli_enabled
    if not enabled:
        print("\nLLM stage skipped. Use --run-llm to enable.", flush=True)
        return

    limit = int(cli_limit or llm_cfg.get("limit", 5))
    max_spec_chars = int(llm_cfg.get("max_spec_chars", 90000))
    max_other_chars = int(llm_cfg.get("max_other_chars", 12000))
    max_output_tokens = int(llm_cfg.get("max_output_tokens", 4096))

    candidate_ids = select_llm_candidate_ids(limit)

    if not candidate_ids:
        print("\nNo LLM candidates selected.", flush=True)
        return

    print(f"\nSelected LLM candidates: {', '.join(candidate_ids)}", flush=True)

    for external_id in candidate_ids:
        try:
            run_cmd(
                py_module(
                    "app.llm.tender_report",
                    "--external-id",
                    external_id,
                    "--max-spec-chars",
                    max_spec_chars,
                    "--max-other-chars",
                    max_other_chars,
                    "--max-output-tokens",
                    max_output_tokens,
                ),
                dry_run=dry_run,
            )
        except subprocess.CalledProcessError as exc:
            print(f"LLM report failed for {external_id}: {exc}", flush=True)


def run_digest(profile: dict[str, Any], *, dry_run: bool) -> None:
    digest_cfg = profile.get("digest") or {}
    output_path = Path(digest_cfg.get("output_path", "data/digests/latest.md"))

    run_cmd_capture(
        py_module("app.digest"),
        output_path,
        dry_run=dry_run,
    )

    print(f"\nDigest saved: {output_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AI Tender Radar daily pipeline")
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE_PATH), help="Path to search profile YAML")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them")
    parser.add_argument("--skip-collect", action="store_true", help="Skip Z360 collection")
    parser.add_argument("--skip-documents", action="store_true", help="Skip document downloading")
    parser.add_argument("--skip-llm", action="store_true", help="Skip LLM even if enabled in profile")
    parser.add_argument("--run-llm", action="store_true", help="Run LLM for selected top candidates")
    parser.add_argument("--llm-limit", type=int, default=None, help="Override LLM candidate limit")
    args = parser.parse_args()

    profile = load_profile(Path(args.profile))

    print("AI Tender Radar daily pipeline")
    print(f"Profile: {args.profile}")
    print(f"Dry run: {args.dry_run}")

    if not args.skip_collect:
        collect_tenders(profile, dry_run=args.dry_run)
    else:
        print("\nCollect stage skipped.", flush=True)

    run_cmd(py_module("app.load_company_profile"), dry_run=args.dry_run)

    run_cmd(py_module("app.scorer"), dry_run=args.dry_run)

    if not args.skip_documents:
        run_documents_stage(profile, dry_run=args.dry_run)
        run_text_extraction(profile, dry_run=args.dry_run)
        run_document_risk(profile, dry_run=args.dry_run)
        run_cmd(py_module("app.scorer"), dry_run=args.dry_run)
    else:
        print("\nDocuments stage skipped.", flush=True)

    if not args.skip_llm:
        run_llm_stage(
            profile,
            dry_run=args.dry_run,
            cli_enabled=args.run_llm,
            cli_limit=args.llm_limit,
        )
    else:
        print("\nLLM stage skipped by --skip-llm.", flush=True)

    run_digest(profile, dry_run=args.dry_run)

    print("\nDaily pipeline finished.", flush=True)


if __name__ == "__main__":
    main()
