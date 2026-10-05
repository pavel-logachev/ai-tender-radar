from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


CHECKER = Path(__file__).resolve().parents[1] / "tools" / "check_public_boundary.py"


class PublicBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent = Path(directory.name)
        self.root = self.parent / "public"
        (self.root / "tools").mkdir(parents=True)
        self.checker = self.root / "tools" / CHECKER.name
        shutil.copyfile(CHECKER, self.checker)
        self.patterns = self.parent / "private-patterns.txt"
        self.environment = os.environ.copy()
        self.environment.pop("PUBLIC_BOUNDARY_PRIVATE_PATTERNS", None)

    def run_checker(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.checker)],
            env=self.environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def use_private_patterns(self, content: str) -> None:
        self.patterns.write_text(content, encoding="utf-8")
        self.environment["PUBLIC_BOUNDARY_PRIVATE_PATTERNS"] = str(self.patterns)

    def test_no_private_file_is_required_in_ci(self) -> None:
        (self.root / "fixture.txt").write_text("Synthetic example", encoding="utf-8")
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("text_files=2 findings=0", result.stdout)

    def test_private_patterns_file_blocks_a_local_marker_without_printing_it(self) -> None:
        (self.root / "fixture.txt").write_text("Example-only local marker", encoding="utf-8")
        self.use_private_patterns("# local-only deny-list\n\n(?i)example-only local marker\n")
        result = self.run_checker()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("private-pattern-3\tfixture.txt", result.stdout)
        self.assertNotIn("local marker", result.stdout + result.stderr)

    def test_private_patterns_also_scan_the_checker_itself(self) -> None:
        with self.checker.open("a", encoding="utf-8") as stream:
            stream.write("\n# Example-only local marker\n")
        self.use_private_patterns("(?i)example-only local marker\n")
        result = self.run_checker()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("private-pattern-1\ttools/check_public_boundary.py", result.stdout)
        self.assertNotIn("local marker", result.stdout + result.stderr)

    def test_configured_missing_file_fails_closed_without_printing_its_path(self) -> None:
        self.environment["PUBLIC_BOUNDARY_PRIVATE_PATTERNS"] = str(self.patterns)
        result = self.run_checker()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cannot read configured UTF-8 file", result.stderr)
        self.assertNotIn(str(self.patterns), result.stdout + result.stderr)

    def test_invalid_regex_fails_closed_without_printing_the_expression(self) -> None:
        self.use_private_patterns("# local-only deny-list\nexample-only local marker[\n")
        result = self.run_checker()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("invalid regex at line 2", result.stderr)
        self.assertNotIn("local marker", result.stdout + result.stderr)

    def test_invalid_utf8_fails_closed(self) -> None:
        self.patterns.write_bytes(b"\xff\xfe")
        self.environment["PUBLIC_BOUNDARY_PRIVATE_PATTERNS"] = str(self.patterns)
        result = self.run_checker()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cannot read configured UTF-8 file", result.stderr)

    def test_private_patterns_file_inside_public_tree_is_rejected(self) -> None:
        self.patterns = self.root / "private-patterns.txt"
        self.use_private_patterns("example-only local marker\n")
        result = self.run_checker()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("configured file must be outside the public tree", result.stderr)
        self.assertNotIn("local marker", result.stdout + result.stderr)

    def test_builtin_secret_patterns_still_apply_without_private_file(self) -> None:
        (self.root / "fixture.txt").write_text("ghp_" + "A" * 24, encoding="utf-8")
        result = self.run_checker()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("github-token\tfixture.txt", result.stdout)

    def test_forbidden_filenames_still_apply_without_private_file(self) -> None:
        (self.root / "customer_contact_hints.json").write_text("{}", encoding="utf-8")
        result = self.run_checker()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("forbidden-file\tcustomer_contact_hints.json", result.stdout)


if __name__ == "__main__":
    unittest.main()
