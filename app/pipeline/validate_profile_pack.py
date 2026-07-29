"""Validate a Profile Pack v1 and optionally prove legacy shadow parity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from app.business_profile import DEFAULT_BUSINESS_PROFILE_PATH
from app.pipeline.search_profile import DEFAULT_PROFILE_PATH
from app.platform.profile_packs import (
    DEFAULT_CONFIG_ROOT,
    DEFAULT_PROFILE_PACK_PATH,
    ProfilePackError,
    compare_profile_pack_to_legacy,
    load_profile_pack,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a versioned supplier Profile Pack without external side effects",
    )
    parser.add_argument("--pack", default=str(DEFAULT_PROFILE_PACK_PATH))
    parser.add_argument("--allowed-root", default=str(DEFAULT_CONFIG_ROOT))
    parser.add_argument("--shadow-legacy-parity", action="store_true")
    parser.add_argument(
        "--legacy-qualification-profile",
        default=str(DEFAULT_BUSINESS_PROFILE_PATH),
    )
    parser.add_argument("--legacy-search-profile", default=str(DEFAULT_PROFILE_PATH))
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        pack = load_profile_pack(args.pack, allowed_root=args.allowed_root)
        payload: dict[str, object] = {
            "status": "valid",
            "schema_version": pack.manifest.schema_version,
            "profile_id": pack.manifest.profile_id,
            "version": pack.manifest.version,
            "profile_status": pack.manifest.status,
            "fingerprint_sha256": pack.binding.fingerprint_sha256,
            "evaluation_mode": pack.manifest.evaluation.mode,
            "external_side_effects": False,
        }
        exit_code = 0
        if args.shadow_legacy_parity:
            parity = compare_profile_pack_to_legacy(
                pack,
                legacy_qualification_profile=Path(args.legacy_qualification_profile),
                legacy_search_profile=Path(args.legacy_search_profile),
                allowed_root=args.allowed_root,
            )
            payload["shadow_parity"] = parity.model_dump(mode="json")
            if parity.status != "parity":
                payload["status"] = "mismatch"
                exit_code = 2
    except ProfilePackError as exc:
        payload = {
            "status": "invalid",
            "error_class": type(exc).__name__,
            "error": str(exc),
            "external_side_effects": False,
        }
        exit_code = 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Profile Pack validation: {payload['status']}")
        if payload.get("profile_id"):
            print(f"Profile: {payload['profile_id']} v{payload['version']}")
            print(f"Fingerprint: {payload['fingerprint_sha256']}")
        if payload.get("shadow_parity"):
            shadow = payload["shadow_parity"]
            if isinstance(shadow, dict):
                print(f"Legacy shadow parity: {shadow['status']}")
        if payload.get("error"):
            print(f"Error: {payload['error']}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
