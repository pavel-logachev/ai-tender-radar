from __future__ import annotations

import re
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
    "internal-windows-path": re.compile(r"C:\\Users\\pavel", re.IGNORECASE),
    "internal-linux-path": re.compile(r"/home/deploy/ai-tender-radar", re.IGNORECASE),
    "internal-host-alias": re.compile(r"pavel-production-vps", re.IGNORECASE),
    "production-ip": re.compile(r"\b194\.87\.101\.107\b"),
    "private-domain-email": re.compile(r"\b[^\s@]+@logachev\.net\b", re.IGNORECASE),
    "provider-host": re.compile(r"\bbidzaar\.com\b", re.IGNORECASE),
    "production-agent-runtime": re.compile(r"atr-hermes|hermes_gateway|compose\.hermes", re.IGNORECASE),
}

findings: list[tuple[str, str]] = []
text_files = 0
for path in ROOT.rglob("*"):
    if not path.is_file() or ".git" in path.parts:
        continue
    if path.resolve() == Path(__file__).resolve():
        continue
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
    for label, pattern in PATTERNS.items():
        if pattern.search(text):
            findings.append((label, relative))

print(f"text_files={text_files} findings={len(findings)}")
for label, relative in findings:
    print(f"{label}\t{relative}")
raise SystemExit(1 if findings else 0)
