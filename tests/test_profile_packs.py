from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import yaml

from app.business_profile import DEFAULT_BUSINESS_PROFILE_PATH
from app.pipeline.search_profile import DEFAULT_PROFILE_PATH
from app.pipeline.validate_profile_pack import main as validate_profile_pack_main
from app.platform.profile_packs import (
    DEFAULT_CONFIG_ROOT,
    DEFAULT_PROFILE_PACK_PATH,
    PROFILE_PACK_SCHEMA_VERSION,
    ProfilePackError,
    compare_profile_pack_to_legacy,
    load_profile_pack,
)
from app.platform.versioning import sha256_json


def manifest_payload(
    *,
    qualification_profile: str = "qualification.yaml",
    search_profile: str = "search.yaml",
) -> dict[str, object]:
    return {
        "schema_version": PROFILE_PACK_SCHEMA_VERSION,
        "profile_id": "sample-supplier",
        "version": 1,
        "display_name": "Sample supplier",
        "vertical_id": "sample_vertical",
        "status": "shadow",
        "artifacts": {
            "qualification_profile": {
                "path": qualification_profile,
                "content_sha256": "0" * 64,
            },
            "search_profile": {
                "path": search_profile,
                "content_sha256": "0" * 64,
            },
        },
        "ai": {
            "analyst_role": "Supplier opportunity analyst",
            "opportunity_goal": "Find qualified procurement opportunities",
            "buyer_roles": ["Commercial owner", "Technical owner"],
            "prompt_family": "sample-lead-v1",
            "report_contract": "opportunity-assessment-v1",
        },
        "evaluation": {
            "mode": "shadow_only",
            "dataset_ref": None,
            "required_case_tags": ["normal", "boundary"],
        },
        "delivery": {
            "workflow_contract": "sales-lead-v1",
            "channels": ["telegram", "excel"],
        },
    }


def write_sample_pack(root: Path, *, payload: dict[str, object] | None = None) -> Path:
    qualification = {
        "vertical": {"name": "sample_vertical", "label": "Sample"},
        "target_categories": {
            "supplies": {
                "label": "Supplies",
                "keywords": ["supply"],
            }
        },
    }
    search = {
        "name": "sample-search",
        "groups": {"daily": {"packs": ["supplies"]}},
        "packs": {"supplies": {"queries": ["supply"]}},
    }
    (root / "qualification.yaml").write_text(
        yaml.safe_dump(qualification, allow_unicode=True),
        encoding="utf-8",
    )
    (root / "search.yaml").write_text(
        yaml.safe_dump(search, allow_unicode=True),
        encoding="utf-8",
    )
    selected_payload = payload or manifest_payload()
    artifacts = selected_payload.get("artifacts")
    if isinstance(artifacts, dict):
        qualification_ref = artifacts.get("qualification_profile")
        search_ref = artifacts.get("search_profile")
        if isinstance(qualification_ref, dict):
            qualification_ref["content_sha256"] = sha256_json(qualification)
        if isinstance(search_ref, dict):
            search_ref["content_sha256"] = sha256_json(search)
    manifest = root / "profile.yaml"
    manifest.write_text(
        yaml.safe_dump(selected_payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return manifest


class CurrentProfilePackTest(unittest.TestCase):
    def test_current_it_pack_has_exact_legacy_shadow_parity(self) -> None:
        pack = load_profile_pack(DEFAULT_PROFILE_PACK_PATH)
        report = compare_profile_pack_to_legacy(
            pack,
            legacy_qualification_profile=DEFAULT_BUSINESS_PROFILE_PATH,
            legacy_search_profile=DEFAULT_PROFILE_PATH,
        )

        self.assertEqual(report.status, "parity")
        self.assertTrue(report.qualification_profile_equal)
        self.assertTrue(report.search_profile_equal)
        self.assertGreater(report.target_category_count, 0)
        self.assertGreater(report.search_pack_count, 0)
        self.assertGreater(report.search_query_count, 0)
        self.assertRegex(report.fingerprint_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(pack.manifest.status, "shadow")

    def test_cli_reports_parity_without_external_side_effects(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = validate_profile_pack_main(
                [
                    "--pack",
                    str(DEFAULT_PROFILE_PACK_PATH),
                    "--allowed-root",
                    str(DEFAULT_CONFIG_ROOT),
                    "--shadow-legacy-parity",
                    "--json",
                ]
            )
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "valid")
        self.assertEqual(payload["shadow_parity"]["status"], "parity")
        self.assertFalse(payload["external_side_effects"])
        self.assertNotIn("qualification_profile", payload)
        self.assertNotIn("search_profile", payload)


class ProfilePackContractTest(unittest.TestCase):
    def test_versioned_artifact_hash_rejects_drift_and_changes_after_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_sample_pack(root)
            first = load_profile_pack(manifest, allowed_root=root)
            second = load_profile_pack(manifest, allowed_root=root)
            self.assertEqual(first.binding, second.binding)

            qualification_path = root / "qualification.yaml"
            qualification = yaml.safe_load(qualification_path.read_text(encoding="utf-8"))
            qualification["target_categories"]["supplies"]["keywords"].append("delivery")
            qualification_path.write_text(
                yaml.safe_dump(qualification, allow_unicode=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProfilePackError, "content hash does not match"):
                load_profile_pack(manifest, allowed_root=root)

            payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            payload["version"] = 2
            payload["artifacts"]["qualification_profile"]["content_sha256"] = sha256_json(
                qualification
            )
            manifest.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            changed = load_profile_pack(manifest, allowed_root=root)

        self.assertNotEqual(
            first.binding.qualification_profile_sha256,
            changed.binding.qualification_profile_sha256,
        )
        self.assertNotEqual(first.binding.fingerprint_sha256, changed.binding.fingerprint_sha256)

    def test_absolute_and_outside_root_artifact_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            absolute_payload = manifest_payload(
                qualification_profile=str(Path(outside) / "qualification.yaml")
            )
            manifest = write_sample_pack(root, payload=absolute_payload)
            with self.assertRaisesRegex(ProfilePackError, "must be relative"):
                load_profile_pack(manifest, allowed_root=root)

            outside_payload = manifest_payload(qualification_profile="../outside.yaml")
            manifest.write_text(
                yaml.safe_dump(outside_payload, allow_unicode=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProfilePackError, "outside allowed_root"):
                load_profile_pack(manifest, allowed_root=root)

    def test_secret_like_keys_are_rejected_without_echoing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_sample_pack(root)
            qualification_path = root / "qualification.yaml"
            qualification = yaml.safe_load(qualification_path.read_text(encoding="utf-8"))
            qualification["connector"] = {"api_token": "do-not-print-this-value"}
            qualification_path.write_text(
                yaml.safe_dump(qualification, allow_unicode=True),
                encoding="utf-8",
            )

            with self.assertRaises(ProfilePackError) as raised:
                load_profile_pack(manifest, allowed_root=root)

        self.assertIn("secret-like key", str(raised.exception))
        self.assertNotIn("do-not-print-this-value", str(raised.exception))

    def test_prefixed_api_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_sample_pack(root)
            qualification_path = root / "qualification.yaml"
            qualification = yaml.safe_load(qualification_path.read_text(encoding="utf-8"))
            qualification["connector"] = {"openai_api_key": "not-a-real-key"}
            qualification_path.write_text(
                yaml.safe_dump(qualification, allow_unicode=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ProfilePackError, "secret-like key"):
                load_profile_pack(manifest, allowed_root=root)

    def test_recursive_yaml_alias_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_sample_pack(root)
            (root / "qualification.yaml").write_text(
                "vertical:\n  name: sample_vertical\n"
                "target_categories: &categories\n"
                "  supplies:\n"
                "    keywords: [supply]\n"
                "recursive: &recursive [*recursive]\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ProfilePackError, "recursive YAML alias"):
                load_profile_pack(manifest, allowed_root=root)

    def test_shared_yaml_alias_is_rejected_before_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_sample_pack(root)
            (root / "qualification.yaml").write_text(
                "vertical:\n  name: sample_vertical\n"
                "shared: &shared\n  keywords: [supply]\n"
                "target_categories:\n"
                "  supplies: *shared\n"
                "  services: *shared\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ProfilePackError, "YAML alias"):
                load_profile_pack(manifest, allowed_root=root)

    def test_duplicate_and_non_string_yaml_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_sample_pack(root)
            qualification_path = root / "qualification.yaml"
            qualification_path.write_text(
                "vertical:\n"
                "  name: sample_vertical\n"
                "  name: replaced_vertical\n"
                "target_categories:\n  supplies:\n    keywords: [supply]\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProfilePackError, "invalid YAML"):
                load_profile_pack(manifest, allowed_root=root)

            qualification_path.write_text(
                "vertical:\n  name: sample_vertical\n"
                "target_categories:\n"
                "  1:\n    keywords: [supply]\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProfilePackError, "non-string key"):
                load_profile_pack(manifest, allowed_root=root)

    def test_legacy_parity_paths_cannot_escape_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            pack = load_profile_pack(write_sample_pack(root), allowed_root=root)

            with self.assertRaisesRegex(ProfilePackError, "outside allowed_root"):
                compare_profile_pack_to_legacy(
                    pack,
                    legacy_qualification_profile=Path(outside) / "qualification.yaml",
                    legacy_search_profile=root / "search.yaml",
                    allowed_root=root,
                )

    def test_unknown_fields_and_unsupported_versions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = manifest_payload()
            payload["unexpected"] = True
            manifest = write_sample_pack(root, payload=payload)
            with self.assertRaisesRegex(ProfilePackError, "manifest failed validation"):
                load_profile_pack(manifest, allowed_root=root)

            payload = manifest_payload()
            payload["schema_version"] = "profile-pack-v2"
            manifest.write_text(
                yaml.safe_dump(payload, allow_unicode=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProfilePackError, "schema_version"):
                load_profile_pack(manifest, allowed_root=root)

    def test_reviewed_evaluation_mode_requires_dataset_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = manifest_payload()
            payload["evaluation"] = {
                "mode": "reviewed",
                "dataset_ref": None,
                "required_case_tags": ["normal"],
            }
            manifest = write_sample_pack(root, payload=payload)

            with self.assertRaisesRegex(ProfilePackError, "evaluation"):
                load_profile_pack(manifest, allowed_root=root)

    def test_manifest_vertical_must_match_qualification_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = manifest_payload()
            payload["vertical_id"] = "different_vertical"
            manifest = write_sample_pack(root, payload=payload)

            with self.assertRaisesRegex(ProfilePackError, "vertical does not match"):
                load_profile_pack(manifest, allowed_root=root)


if __name__ == "__main__":
    unittest.main()
