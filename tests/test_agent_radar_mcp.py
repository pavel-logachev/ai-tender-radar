"""Real stdio protocol handshake; needs the isolated MCP SDK test environment."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from agent_radar.bundle import materialize_bundle

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None


def _tool_error(result: object) -> bool:
    if hasattr(result, "isError"):
        return bool(result.isError)
    return bool(result.is_error)


@unittest.skipIf(ClientSession is None, "MCP SDK is optional outside the isolated test runtime")
class OfflineMCPHandshakeTest(unittest.TestCase):
    def test_stdio_bundle_reader_refuses_tampered_manifest_before_serving(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "run-synthetic"
                fixture = Path("agent_radar/fixtures/synthetic_source_export.json").resolve()
                manifest = materialize_bundle(fixture, target, max_search_results=10)
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "agent_radar.mcp_server", "--bundle", str(target)],
                    env={**os.environ, "AGENT_RADAR_ENABLE_LOCAL_SNAPSHOT": "1"},
                )
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        page = json.loads((await session.call_tool("list_candidates", {"limit": 10})).content[0].text)
                        self.assertEqual(page["snapshot_sha256"], manifest["snapshot_sha256"])
                        self.assertEqual([item["id"] for item in page["candidates"]],
                                         ["example-source:101", "example-source:102"])
                        sidecar_path = target / "manifest.json"
                        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                        sidecar["search"]["query"] = "altered during session"
                        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
                        refused = await session.call_tool("list_candidates", {"limit": 10})
                        self.assertTrue(_tool_error(refused))
                        self.assertIn("manifest checksum mismatch", refused.content[0].text)

        asyncio.run(run())

    def test_unverified_snapshot_requires_explicit_opt_in(self) -> None:
        from agent_radar.mcp_server import main
        from unittest.mock import patch
        with patch.object(sys, "argv", ["agent_radar.mcp_server", "--snapshot", str(Path("agent_radar/fixtures/synthetic_tenders.json").resolve())]), \
                patch.dict(os.environ, {"AGENT_RADAR_ENABLE_LOCAL_SNAPSHOT": "1"}):
            with self.assertRaises(SystemExit) as refusal:
                main()
        self.assertEqual(refusal.exception.code, 2)

    def test_stdio_tools_expose_only_snapshot_reader(self) -> None:
        async def run() -> None:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "agent_radar.mcp_server", "--snapshot", str(Path("agent_radar/fixtures/synthetic_tenders.json").resolve()), "--allow-unverified-snapshot"],
                env={**os.environ, "AGENT_RADAR_ENABLE_LOCAL_SNAPSHOT": "1"},
            )
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    names = {tool.name for tool in tools}
                    self.assertEqual(names, {"list_candidates", "get_tender", "list_documents", "read_document_chunk"})
                    self.assertTrue(all(tool.annotations is not None and
                                        (getattr(tool.annotations, "read_only_hint", None) is True or
                                         getattr(tool.annotations, "readOnlyHint", None) is True)
                                        for tool in tools))
                    result = await session.call_tool("list_candidates", {"limit": 1})
                    self.assertFalse(_tool_error(result))
                    payload = json.loads(result.content[0].text)
                    self.assertEqual(payload["candidates"][0]["legacy_score"], "no_go")
                    denied = await session.call_tool("read_document_chunk", {
                        "tender_id": "synthetic:102", "document_id": "synthetic-doc-1",
                        "snapshot_sha256": payload["snapshot_sha256"],
                    })
                    self.assertTrue(_tool_error(denied))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
