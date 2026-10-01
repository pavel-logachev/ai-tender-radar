"""Offline review utilities. Never invokes a model, network API or Telegram."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_radar.bundle import materialize_bundle, open_verified_bundle
from agent_radar.review_queue import ReviewQueue
from agent_radar.snapshot import SnapshotStore
from agent_radar.source_export import materialize_source_export


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Tender Radar review tools")
    commands = parser.add_subparsers(dest="command", required=True)
    materialize = commands.add_parser("materialize", help="Turn an offline operator export into a validated new snapshot")
    materialize.add_argument("--source-export", required=True, type=Path)
    materialize.add_argument("--snapshot", required=True, type=Path)
    materialize.add_argument("--manifest", required=True, type=Path)
    materialize.add_argument("--max-search-results", required=True, type=int)
    bundle = commands.add_parser("materialize-bundle", help="Publish an offline snapshot and manifest as one visible bundle")
    bundle.add_argument("--source-export", required=True, type=Path)
    bundle.add_argument("--bundle", required=True, type=Path)
    bundle.add_argument("--max-search-results", required=True, type=int)
    verify = commands.add_parser("verify-bundle", help="Verify a published bundle snapshot/manifest hash")
    verify.add_argument("--bundle", required=True, type=Path)
    preview = commands.add_parser("preview", help="Read a bounded page from a verified local bundle")
    preview_input = preview.add_mutually_exclusive_group(required=True)
    preview_input.add_argument("--bundle", type=Path)
    preview_input.add_argument("--snapshot", type=Path, help="Synthetic/legacy only; requires --allow-unverified-snapshot")
    preview.add_argument("--allow-unverified-snapshot", action="store_true")
    preview.add_argument("--limit", default=20, type=int)
    submit = commands.add_parser("submit", help="Validate a proposal against a verified local bundle")
    submit_input = submit.add_mutually_exclusive_group(required=True)
    submit_input.add_argument("--bundle", type=Path)
    submit_input.add_argument("--snapshot", type=Path, help="Synthetic/legacy only; requires --allow-unverified-snapshot")
    submit.add_argument("--allow-unverified-snapshot", action="store_true")
    submit.add_argument("--proposal", required=True, type=Path)
    submit.add_argument("--queue", required=True, type=Path)
    pending = commands.add_parser("pending", help="List queued suggestions for human review")
    pending.add_argument("--queue", required=True, type=Path)
    review = commands.add_parser("review", help="Record an operator-asserted manual decision; never deliver")
    review.add_argument("--queue", required=True, type=Path)
    review.add_argument("--id", required=True)
    review.add_argument("--verdict", choices=("approved", "rejected"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--decided-at", required=True, help="Explicit UTC ISO timestamp")
    detail = commands.add_parser("show-review", help="Read a suggestion and optional review decision")
    detail.add_argument("--queue", required=True, type=Path)
    detail.add_argument("--id", required=True)
    args = parser.parse_args()
    if args.command == "materialize":
        result = materialize_source_export(args.source_export, args.snapshot, args.manifest, max_search_results=args.max_search_results)
    elif args.command == "materialize-bundle":
        result = materialize_bundle(args.source_export, args.bundle, max_search_results=args.max_search_results)
    elif args.command == "verify-bundle":
        _, result = open_verified_bundle(args.bundle)
    elif args.command in ("preview", "submit"):
        if args.bundle is not None:
            if args.allow_unverified_snapshot:
                parser.error("--allow-unverified-snapshot only applies to --snapshot")
            store, _ = open_verified_bundle(args.bundle)
        else:
            if not args.allow_unverified_snapshot:
                parser.error("unverified --snapshot requires --allow-unverified-snapshot (synthetic/legacy only)")
            store = SnapshotStore(args.snapshot)
        if args.command == "preview":
            result = store.list_candidates(limit=args.limit)
        else:
            result = ReviewQueue(args.queue).submit(json.loads(args.proposal.read_text(encoding="utf-8")), store)
    elif args.command == "pending":
        result = ReviewQueue(args.queue).list_pending()
    elif args.command == "review":
        if args.reviewer.startswith("telegram:user:"):
            parser.error("Telegram reviewer identity is reserved for authenticated bot commands")
        result = ReviewQueue(args.queue).record_decision(
            review_id=args.id, verdict=args.verdict, reviewer=args.reviewer,
            reason=args.reason, decided_at=args.decided_at,
        )
    else:
        result = ReviewQueue(args.queue).get_review(args.id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
