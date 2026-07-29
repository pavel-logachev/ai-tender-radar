"""Strict, portable contracts for versioned supplier profile packs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.business_profile import load_business_profile
from app.pipeline.search_profile import load_search_profile, validate_profile
from app.platform.contracts import SENSITIVE_KEYS
from app.platform.versioning import sha256_json


PROFILE_PACK_SCHEMA_VERSION = "profile-pack-v1"
DEFAULT_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
DEFAULT_PROFILE_PACK_PATH = DEFAULT_CONFIG_ROOT / "profile_packs" / "it_infrastructure.v1.yaml"
MAX_MANIFEST_BYTES = 128 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_DATA_NODES = 50_000
MAX_DATA_DEPTH = 40
MAX_SCALAR_CHARS = 200_000
IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9_.-]*$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    str_strip_whitespace=True,
)


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that also rejects ambiguous duplicate mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "expected a mapping node",
                node.start_mark,
            )
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found a duplicate key",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class ProfilePackError(ValueError):
    """Raised when a profile pack crosses its schema or filesystem boundary."""


def _as_tuple(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(value)
    return value


class ProfilePackArtifactReference(BaseModel):
    model_config = MODEL_CONFIG

    path: str = Field(min_length=1, max_length=300)
    content_sha256: str = Field(pattern=SHA256_PATTERN)


class ProfilePackArtifacts(BaseModel):
    model_config = MODEL_CONFIG

    qualification_profile: ProfilePackArtifactReference
    search_profile: ProfilePackArtifactReference


class ProfilePackAIContract(BaseModel):
    model_config = MODEL_CONFIG

    analyst_role: str = Field(min_length=1, max_length=500)
    opportunity_goal: str = Field(min_length=1, max_length=1_000)
    buyer_roles: tuple[str, ...] = Field(min_length=1, max_length=20)
    prompt_family: str = Field(min_length=1, max_length=100, pattern=IDENTIFIER_PATTERN)
    report_contract: str = Field(min_length=1, max_length=100, pattern=IDENTIFIER_PATTERN)

    @field_validator("buyer_roles", mode="before")
    @classmethod
    def normalize_buyer_roles(cls, value: Any) -> Any:
        return _as_tuple(value)

    @field_validator("buyer_roles")
    @classmethod
    def validate_buyer_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not all(isinstance(role, str) for role in value):
            raise ValueError("buyer_roles must contain strings")
        normalized = tuple(role.strip() for role in value if role.strip())
        if len(normalized) != len(value):
            raise ValueError("buyer_roles must contain non-empty strings")
        if len(set(role.casefold() for role in normalized)) != len(normalized):
            raise ValueError("buyer_roles must be unique")
        return normalized


class ProfilePackEvaluationPolicy(BaseModel):
    model_config = MODEL_CONFIG

    mode: Literal["shadow_only", "reviewed"]
    dataset_ref: str | None = Field(default=None, max_length=200, pattern=IDENTIFIER_PATTERN)
    required_case_tags: tuple[str, ...] = Field(default_factory=tuple, max_length=30)

    @field_validator("required_case_tags", mode="before")
    @classmethod
    def normalize_required_case_tags(cls, value: Any) -> Any:
        return _as_tuple(value)

    @field_validator("required_case_tags")
    @classmethod
    def validate_required_case_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not all(isinstance(tag, str) for tag in value):
            raise ValueError("required_case_tags must contain strings")
        for tag in value:
            if not tag or len(tag) > 80 or not re.fullmatch(IDENTIFIER_PATTERN, tag):
                raise ValueError("required_case_tags must contain portable identifiers")
        if len(set(value)) != len(value):
            raise ValueError("required_case_tags must be unique")
        return value

    @model_validator(mode="after")
    def reviewed_mode_requires_dataset(self) -> "ProfilePackEvaluationPolicy":
        if self.mode == "reviewed" and not self.dataset_ref:
            raise ValueError("reviewed evaluation mode requires dataset_ref")
        return self


class ProfilePackDeliveryContract(BaseModel):
    model_config = MODEL_CONFIG

    workflow_contract: str = Field(min_length=1, max_length=100, pattern=IDENTIFIER_PATTERN)
    channels: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("channels", mode="before")
    @classmethod
    def normalize_channels(cls, value: Any) -> Any:
        return _as_tuple(value)

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not all(isinstance(channel, str) for channel in value):
            raise ValueError("channels must contain strings")
        for channel in value:
            if not channel or len(channel) > 80:
                raise ValueError("channels must contain short non-empty identifiers")
            if not channel.replace("-", "").replace("_", "").isalnum():
                raise ValueError("channels must contain portable identifiers")
        if len(set(value)) != len(value):
            raise ValueError("channels must be unique")
        return value


class ProfilePackManifest(BaseModel):
    model_config = MODEL_CONFIG

    schema_version: Literal[PROFILE_PACK_SCHEMA_VERSION]
    profile_id: str = Field(min_length=1, max_length=100, pattern=IDENTIFIER_PATTERN)
    version: int = Field(gt=0, strict=True)
    display_name: str = Field(min_length=1, max_length=200)
    vertical_id: str = Field(min_length=1, max_length=100, pattern=IDENTIFIER_PATTERN)
    status: Literal["draft", "shadow", "active", "retired"]
    artifacts: ProfilePackArtifacts
    ai: ProfilePackAIContract
    evaluation: ProfilePackEvaluationPolicy
    delivery: ProfilePackDeliveryContract


class ProfilePackBinding(BaseModel):
    model_config = MODEL_CONFIG

    profile_id: str = Field(pattern=IDENTIFIER_PATTERN)
    version: int = Field(gt=0, strict=True)
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    qualification_profile_sha256: str = Field(pattern=SHA256_PATTERN)
    search_profile_sha256: str = Field(pattern=SHA256_PATTERN)
    fingerprint_sha256: str = Field(pattern=SHA256_PATTERN)


@dataclass(frozen=True)
class LoadedProfilePack:
    manifest_path: Path
    manifest: ProfilePackManifest
    qualification_profile_path: Path
    qualification_profile: dict[str, Any]
    search_profile_path: Path
    search_profile: dict[str, Any]
    binding: ProfilePackBinding


class ProfilePackParityReport(BaseModel):
    model_config = MODEL_CONFIG

    schema_version: Literal[PROFILE_PACK_SCHEMA_VERSION] = PROFILE_PACK_SCHEMA_VERSION
    profile_id: str = Field(pattern=IDENTIFIER_PATTERN)
    version: int = Field(gt=0, strict=True)
    status: Literal["parity", "mismatch"]
    fingerprint_sha256: str = Field(pattern=SHA256_PATTERN)
    qualification_profile_equal: bool
    search_profile_equal: bool
    target_category_count: int = Field(ge=0)
    search_pack_count: int = Field(ge=0)
    search_query_count: int = Field(ge=0)


def _normalized_secret_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _is_secret_key(value: object) -> bool:
    raw = str(value).casefold()
    normalized = _normalized_secret_key(raw)
    parts = {part for part in re.split(r"[^a-z0-9]+", raw) if part}
    secret_suffixes = (
        "apikey",
        "accesstoken",
        "authtoken",
        "clientpassword",
        "clientsecret",
        "refreshtoken",
    )
    return (
        normalized in SENSITIVE_KEYS
        or normalized.endswith(secret_suffixes)
        or bool(parts & {"token", "password", "secret", "credential", "credentials"})
    )


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_secret_key(key):
                return True
            if _contains_secret_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _validate_data_tree(value: Any, *, label: str) -> None:
    active: set[int] = set()
    seen: set[int] = set()
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        if depth > MAX_DATA_DEPTH:
            raise ProfilePackError(f"{label} exceeds the nesting limit")
        nodes += 1
        if nodes > MAX_DATA_NODES:
            raise ProfilePackError(f"{label} exceeds the structure limit")
        if isinstance(item, str) and len(item) > MAX_SCALAR_CHARS:
            raise ProfilePackError(f"{label} contains an oversized scalar")
        if not isinstance(item, (dict, list)):
            return

        identity = id(item)
        if identity in active:
            raise ProfilePackError(f"{label} contains a recursive YAML alias")
        if identity in seen:
            raise ProfilePackError(f"{label} contains a YAML alias")
        seen.add(identity)
        active.add(identity)
        try:
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ProfilePackError(f"{label} contains a non-string key")
                    if len(key) > 300:
                        raise ProfilePackError(f"{label} contains an oversized key")
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
        finally:
            active.remove(identity)

    visit(value, 0)


def _read_yaml_mapping(path: Path, *, max_bytes: int, label: str) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise ProfilePackError(f"{label} file does not exist")
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise ProfilePackError(f"{label} must be a YAML file")
    if path.stat().st_size > max_bytes:
        raise ProfilePackError(f"{label} exceeds the size limit")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProfilePackError(f"{label} must be UTF-8 encoded") from exc
    if len(text.encode("utf-8")) > max_bytes:
        raise ProfilePackError(f"{label} exceeds the size limit")
    try:
        value = yaml.load(text, Loader=_StrictSafeLoader) or {}
    except (RecursionError, yaml.YAMLError) as exc:
        raise ProfilePackError(f"{label} contains invalid YAML") from exc
    if not isinstance(value, dict):
        raise ProfilePackError(f"{label} must be a mapping")
    _validate_data_tree(value, label=label)
    if _contains_secret_key(value):
        raise ProfilePackError(f"{label} contains a secret-like key")
    return value


def _resolve_artifact_path(
    raw_path: str,
    *,
    manifest_path: Path,
    allowed_root: Path,
    label: str,
) -> Path:
    candidate_path = Path(raw_path)
    if candidate_path.is_absolute():
        raise ProfilePackError(f"{label} path must be relative")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw_path):
        raise ProfilePackError(f"{label} path contains control characters")
    candidate = (manifest_path.parent / candidate_path).resolve()
    if not candidate.is_relative_to(allowed_root):
        raise ProfilePackError(f"{label} path is outside allowed_root")
    return candidate


def _manifest_validation_error(exc: ValidationError) -> ProfilePackError:
    locations = sorted(
        {
            ".".join(str(part) for part in error.get("loc") or ("manifest",))
            for error in exc.errors(include_input=False, include_url=False)
        }
    )
    return ProfilePackError(
        "profile pack manifest failed validation at: " + ", ".join(locations)
    )


def load_profile_pack(
    path: str | Path = DEFAULT_PROFILE_PACK_PATH,
    *,
    allowed_root: str | Path = DEFAULT_CONFIG_ROOT,
) -> LoadedProfilePack:
    root = Path(allowed_root).expanduser().resolve()
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_relative_to(root):
        raise ProfilePackError("profile pack manifest is outside allowed_root")

    manifest_payload = _read_yaml_mapping(
        manifest_path,
        max_bytes=MAX_MANIFEST_BYTES,
        label="profile pack manifest",
    )
    try:
        manifest = ProfilePackManifest.model_validate(manifest_payload)
    except ValidationError as exc:
        raise _manifest_validation_error(exc) from exc

    qualification_path = _resolve_artifact_path(
        manifest.artifacts.qualification_profile.path,
        manifest_path=manifest_path,
        allowed_root=root,
        label="qualification profile",
    )
    search_path = _resolve_artifact_path(
        manifest.artifacts.search_profile.path,
        manifest_path=manifest_path,
        allowed_root=root,
        label="search profile",
    )

    qualification_profile = _read_yaml_mapping(
        qualification_path,
        max_bytes=MAX_ARTIFACT_BYTES,
        label="qualification profile",
    )
    search_profile = _read_yaml_mapping(
        search_path,
        max_bytes=MAX_ARTIFACT_BYTES,
        label="search profile",
    )

    vertical = qualification_profile.get("vertical") or {}
    if not isinstance(vertical, dict) or vertical.get("name") != manifest.vertical_id:
        raise ProfilePackError("qualification profile vertical does not match manifest")
    categories = qualification_profile.get("target_categories")
    if not isinstance(categories, dict) or not categories:
        raise ProfilePackError("qualification profile requires target_categories")
    try:
        validate_profile(search_profile)
    except ValueError as exc:
        raise ProfilePackError("search profile failed validation") from exc

    manifest_sha256 = sha256_json(manifest.model_dump(mode="python"))
    qualification_sha256 = sha256_json(qualification_profile)
    search_sha256 = sha256_json(search_profile)
    if qualification_sha256 != manifest.artifacts.qualification_profile.content_sha256:
        raise ProfilePackError("qualification profile content hash does not match manifest")
    if search_sha256 != manifest.artifacts.search_profile.content_sha256:
        raise ProfilePackError("search profile content hash does not match manifest")
    fingerprint = sha256_json(
        {
            "schema_version": PROFILE_PACK_SCHEMA_VERSION,
            "manifest_sha256": manifest_sha256,
            "qualification_profile_sha256": qualification_sha256,
            "search_profile_sha256": search_sha256,
        }
    )
    binding = ProfilePackBinding(
        profile_id=manifest.profile_id,
        version=manifest.version,
        manifest_sha256=manifest_sha256,
        qualification_profile_sha256=qualification_sha256,
        search_profile_sha256=search_sha256,
        fingerprint_sha256=fingerprint,
    )
    return LoadedProfilePack(
        manifest_path=manifest_path,
        manifest=manifest,
        qualification_profile_path=qualification_path,
        qualification_profile=qualification_profile,
        search_profile_path=search_path,
        search_profile=search_profile,
        binding=binding,
    )


def compare_profile_pack_to_legacy(
    pack: LoadedProfilePack,
    *,
    legacy_qualification_profile: str | Path,
    legacy_search_profile: str | Path,
    allowed_root: str | Path = DEFAULT_CONFIG_ROOT,
) -> ProfilePackParityReport:
    root = Path(allowed_root).expanduser().resolve()
    qualification_path = Path(legacy_qualification_profile).expanduser().resolve()
    search_path = Path(legacy_search_profile).expanduser().resolve()
    if not qualification_path.is_relative_to(root) or not search_path.is_relative_to(root):
        raise ProfilePackError("legacy parity path is outside allowed_root")
    current_qualification = load_business_profile(qualification_path)
    current_search = load_search_profile(search_path)
    qualification_equal = (
        sha256_json(current_qualification)
        == pack.binding.qualification_profile_sha256
    )
    search_equal = sha256_json(current_search) == pack.binding.search_profile_sha256
    packs = pack.search_profile.get("packs") or {}
    query_count = sum(
        len(pack_config.get("queries") or [])
        for pack_config in packs.values()
        if isinstance(pack_config, dict)
    )
    categories = pack.qualification_profile.get("target_categories") or {}
    return ProfilePackParityReport(
        profile_id=pack.manifest.profile_id,
        version=pack.manifest.version,
        status="parity" if qualification_equal and search_equal else "mismatch",
        fingerprint_sha256=pack.binding.fingerprint_sha256,
        qualification_profile_equal=qualification_equal,
        search_profile_equal=search_equal,
        target_category_count=len(categories),
        search_pack_count=len(packs),
        search_query_count=query_count,
    )
