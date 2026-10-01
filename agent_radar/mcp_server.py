"""Stdio MCP read-only broker for *offline* materialized tender snapshots.

The same process has no source API/DB credentials and cannot download or publish.
"""

import argparse
import os
from pathlib import Path
from typing import Any

from agent_radar.bundle import open_verified_bundle
from agent_radar.snapshot import SnapshotStore

try:
    from mcp.types import ToolAnnotations
except ImportError:  # Unit tests without the optional SDK can inject a fake server.
    ToolAnnotations = None  # type: ignore[assignment, misc]

try:
    from mcp.server import MCPServer as Server  # MCP SDK v2 (tested with mcp==2.0.0)
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP as Server  # MCP SDK v1
    except ImportError:  # Unit tests can inject a fake; deployed stdio needs MCP.
        Server = None  # type: ignore[assignment, misc]


def build_server(snapshot_path: str | Path | None = None, *, bundle_path: str | Path | None = None) -> Any:
    if Server is None or ToolAnnotations is None:
        raise RuntimeError("MCP SDK is required for the stdio server (tested mcp==1.12.4 and mcp==2.0.0)")
    if (snapshot_path is None) == (bundle_path is None):
        raise ValueError("choose exactly one bundle or unverified snapshot")
    store = open_verified_bundle(bundle_path)[0] if bundle_path is not None else SnapshotStore(snapshot_path)
    store.list_candidates(limit=1)  # fail closed before exposing MCP tools
    mcp = Server("tender-radar-readonly")
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=readonly)
    def list_candidates(limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Read a bounded page of tender cards; legacy scores are clues, not eligibility gates."""
        return store.list_candidates(limit=limit, offset=offset)

    @mcp.tool(annotations=readonly)
    def get_tender(tender_id: str, snapshot_sha256: str) -> dict[str, Any]:
        """Read an exact card from the pinned source snapshot, with no arbitrary file access."""
        return store.get_tender(tender_id=tender_id, snapshot_sha256=snapshot_sha256)

    @mcp.tool(annotations=readonly)
    def list_documents(tender_id: str, snapshot_sha256: str) -> dict[str, Any]:
        """List extracted text embedded in this operator-provided offline snapshot."""
        return store.list_documents(tender_id=tender_id, snapshot_sha256=snapshot_sha256)

    @mcp.tool(annotations=readonly)
    def read_document_chunk(
        tender_id: str, document_id: str, snapshot_sha256: str, offset: int = 0, limit: int = 4000
    ) -> dict[str, Any]:
        """Read at most 4000 characters of one pinned, tender-scoped document."""
        return store.read_document_chunk(tender_id=tender_id, document_id=document_id, snapshot_sha256=snapshot_sha256, offset=offset, limit=limit)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline-only Tender Radar MCP bundle reader")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", type=Path)
    source.add_argument("--snapshot", type=Path, help="Synthetic/legacy only; requires --allow-unverified-snapshot")
    parser.add_argument("--allow-unverified-snapshot", action="store_true")
    args = parser.parse_args()
    if os.environ.get("AGENT_RADAR_ENABLE_LOCAL_SNAPSHOT") != "1":
        parser.error("disabled by default; AGENT_RADAR_ENABLE_LOCAL_SNAPSHOT=1 required")
    if args.bundle is not None and args.allow_unverified_snapshot:
        parser.error("--allow-unverified-snapshot only applies to --snapshot")
    if args.snapshot is not None and not args.allow_unverified_snapshot:
        parser.error("unverified --snapshot requires --allow-unverified-snapshot (synthetic/legacy only)")
    # build_server validates the pair before registering tools, then rechecks it on each read.
    build_server(args.snapshot, bundle_path=args.bundle).run(transport="stdio")


if __name__ == "__main__":
    main()
