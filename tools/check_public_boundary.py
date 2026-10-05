from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PARTS = {
    ".codex",
    ".hermes",
    "graphify-out",
    "agent_memory",
    "docs/api",
    "docs/knowledge",
    "deploy",
    "data",
    "logs",
    "output",
}
FORBIDDEN_NAMES = {
    "AGENTS.md",
    "docker-compose.yml",
    "sing-box.json",
    "prompt_ru.md",
    "PRODUCTION_STATUS.md",
    "DEPLOYMENT_GATES.md",
    "customer_contact_hints.json",
    "customer_contact_hints_extra.json",
    "customer_contact_hints_research.json",
}
FORBIDDEN_NAME_MARKERS = {".bak", ".fix", ".inline_", ".menu_", ".repair"}
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".css",
    ".example",
    ".html",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "openai-like-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "telegram-bot-token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "provider-host": re.compile(r"\bbidzaar\.com\b", re.IGNORECASE),
    "production-agent-runtime": re.compile(r"atr-hermes|hermes_gateway|compose\.hermes", re.IGNORECASE),
}


def load_private_patterns() -> dict[str, re.Pattern[str]]:
    """Read an operator-owned UTF-8 regex file outside the public tree."""
    configured = os.environ.get("PUBLIC_BOUNDARY_PRIVATE_PATTERNS")
    if not configured:
        return {}
    try:
        path = Path(configured).resolve()
        if path.is_relative_to(ROOT.resolve()):
            print("private-patterns: configured file must be outside the public tree", file=sys.stderr)
            raise SystemExit(2)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError, ValueError):
        print("private-patterns: cannot read configured UTF-8 file", file=sys.stderr)
        raise SystemExit(2)

    patterns = {}
    for line_number, line in enumerate(lines, 1):
        expression = line.strip()
        if not expression or expression.startswith("#"):
            continue
        try:
            patterns[f"private-pattern-{line_number}"] = re.compile(expression)
        except re.error:
            print(f"private-patterns: invalid regex at line {line_number}", file=sys.stderr)
            raise SystemExit(2)
    return patterns


PRIVATE_PATTERNS = load_private_patterns()
findings: list[tuple[str, str]] = []
text_files = 0
for path in ROOT.rglob("*"):
    if not path.is_file() or ".git" in path.parts:
        continue
    is_checker = path.resolve() == Path(__file__).resolve()
    relative = path.relative_to(ROOT).as_posix()
    lowered_parts = {part.lower() for part in path.relative_to(ROOT).parts}
    if path.name in FORBIDDEN_NAMES:
        findings.append(("forbidden-file", relative))
    if any(marker in path.name.lower() for marker in FORBIDDEN_NAME_MARKERS):
        findings.append(("backup-like-file", relative))
    relative_lower = relative.lower()
    if any(relative_lower == part or relative_lower.startswith(f"{part}/") for part in FORBIDDEN_PARTS) or "agent_memory" in lowered_parts:
        findings.append(("forbidden-path", relative))
    if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 5_000_000:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    text_files += 1
    # Built-in deny-list literals would match themselves. Private patterns
    # contain no public literals and apply to every text file, including this one.
    patterns = PRIVATE_PATTERNS if is_checker else PATTERNS | PRIVATE_PATTERNS
    for label, pattern in patterns.items():
        if pattern.search(text):
            findings.append((label, relative))

print(f"text_files={text_files} findings={len(findings)}")
for label, relative in findings:
    print(f"{label}\t{relative}")
raise SystemExit(1 if findings else 0)
