"""Synthetic checks for local atomic state files and one-writer locking."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_radar.state_files import _atomic_text, _private_root, exclusive_lock


class StateFilesTest(unittest.TestCase):
    def test_atomic_replace_keeps_only_complete_text_and_no_staging_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "state.json"
            _atomic_text(target, '{"message": "учебный пример"}')
            self.assertEqual(target.read_text(encoding="utf-8"), '{"message": "учебный пример"}')
            _atomic_text(target, '{"version": 2}')
            self.assertEqual(target.read_text(encoding="utf-8"), '{"version": 2}')
            self.assertEqual(list(root.iterdir()), [target])

    def test_non_file_target_is_rejected_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "occupied"
            target.mkdir()
            with self.assertRaises(ValueError):
                _atomic_text(target, "unwritten")
            self.assertEqual(list(target.iterdir()), [])

    def test_one_writer_is_nonblocking_and_the_inode_survives_release(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "writer.lock"
            with exclusive_lock(lock):
                with self.assertRaises(BlockingIOError):
                    with exclusive_lock(lock):
                        self.fail("a second writer acquired the lock")
            self.assertTrue(lock.is_file())
            with exclusive_lock(lock):
                self.assertTrue(lock.is_file())

    def test_private_root_must_be_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            _private_root(root)
            with self.assertRaises(ValueError):
                _private_root(root / "missing")
            file = root / "ordinary-file"
            file.write_text("synthetic", encoding="utf-8")
            with self.assertRaises(ValueError):
                _private_root(file)


if __name__ == "__main__":
    unittest.main()
