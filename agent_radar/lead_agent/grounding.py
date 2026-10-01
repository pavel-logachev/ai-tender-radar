"""Mechanical grounding of an agent result against what the agent actually read.

A phone, e-mail or person name that never occurs in any tool output cannot be trusted by a caller, so it is
removed and flagged. Grades are then recomputed from the verified contacts, using exactly the definitions
stated in the prompt (A: named person with a verified phone; B: verified phone without a name; C: no
verified phone, which is never a ready lead: a caller needs a number).
"""
from __future__ import annotations

import re

_DIGITS = re.compile(r"\D+")
_SPLIT = re.compile(r"[;\n]|\bили\b")


def _digits(value: str) -> str:
    return _DIGITS.sub("", value or "")


def _core(number_digits: str) -> str:
    """First 10 digits of a Russian number without the country prefix (extension excluded)."""
    if len(number_digits) >= 11 and number_digits[0] in "78":
        number_digits = number_digits[1:]
    return number_digits[:10]


def _name_found(name: str, text_lower: str) -> bool:
    tokens = [token for token in re.findall(r"[A-Za-zА-Яа-яЁё-]{3,}", name)]
    if not tokens:
        return False
    # Case endings vary between sources ("Иванова"/"Ивановой"): compare a stem, but every token must match.
    return all(token.lower()[:max(4, len(token) - 2)] in text_lower for token in tokens)


def verify_phones(phone: str | None, source_digits: str) -> list[str]:
    kept = []
    for part in _SPLIT.split(phone or ""):
        core = _core(_digits(part))
        if len(core) >= 10 and core in source_digits:
            kept.append(part.strip(" ,"))
    return kept


def ground_result(result: dict, tool_text: str) -> dict:
    source_digits = _digits(tool_text)
    source_lower = re.sub(r"\s+", " ", tool_text or "").lower()
    person_with_phone = phone_only = person_email = 0
    for contact in result.get("contacts") or []:
        if not isinstance(contact, dict):
            continue
        original = {key: contact.get(key) for key in ("name", "phone", "email")}
        phones = verify_phones(contact.get("phone"), source_digits)
        contact["phone"] = "; ".join(phones) or None
        email = (contact.get("email") or "").strip().lower()
        contact["email"] = contact["email"] if email and email in source_lower else None
        if contact.get("name") and not _name_found(contact["name"], source_lower):
            contact["name"] = None
        contact["verified"] = {"phone": bool(phones), "email": bool(contact["email"]), "name": bool(contact.get("name"))}
        contact["removed_unverified"] = [key for key, value in original.items() if value and not contact.get(key)]
        # A third-party aggregator hit (confidence low) names someone, but not a vetted caller: it never earns grade A.
        if contact.get("name") and phones and str(contact.get("confidence")).lower() != "low":
            person_with_phone += 1
        elif phones:
            phone_only += 1
        elif contact.get("name") and contact["email"]:
            person_email += 1
    grade = "A" if person_with_phone else "B" if phone_only else "C"
    if result.get("verdict") in ("lead", "candidate"):
        if result.get("grade") != grade:
            result["grade_adjusted"] = {"from": result.get("grade"), "to": grade}
        result["grade"] = grade
        if grade == "C" and result["verdict"] == "lead":
            result["verdict"] = "candidate"
    result["grounding"] = {"person_with_phone": person_with_phone, "phone_only": phone_only, "person_email_only": person_email}
    return result
