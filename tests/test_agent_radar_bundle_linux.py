"""Linux no-replace directory publishing test; run only in an offline container."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_radar.bundle import _publish_directory, open_verified_bundle
from tests.radar_fixtures import publish


@unittest.skipUnless(os.name == "posix", "Linux-only no-replace path")
class LinuxBundlePublicationTest(unittest.TestCase):
    def test_existing_empty_directory_cannot_be_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / ".staging"
            stage.mkdir()
            (stage / "owned").write_text("our stage", encoding="utf-8")
            target = root / "published"
            target.mkdir()
            with self.assertRaises(FileExistsError):
                _publish_directory(stage, target)
            self.assertTrue((stage / "owned").is_file())
            self.assertEqual(list(target.iterdir()), [])

    def test_generated_bundle_has_private_files_and_valid_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "published"
            publish(target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            for file in target.iterdir():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            store, manifest = open_verified_bundle(target)
            self.assertEqual(store.list_candidates()["snapshot_sha256"], manifest["snapshot_sha256"])
            commit = json.loads((target / "commit.json").read_text(encoding="utf-8"))
            self.assertEqual(commit["schema_version"], "agent-radar-bundle-v1")


if __name__ == "__main__":
    unittest.main()
