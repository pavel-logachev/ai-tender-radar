from __future__ import annotations

from dataclasses import dataclass
import html
import re


MAX_HTML_PREVIEW_CHARS = 400
EXTERNAL_MARKETPLACE_AUTH_REQUIRED_HINT = "external_marketplace_auth_required"

AUTH_REQUIRED_MARKERS = (
    "login",
    "log in",
    "sign in",
    "auth",
    "authorization",
    "authenticate",
    "\u0430\u0432\u0442\u043e\u0440\u0438\u0437\u0430",
    "\u0432\u043e\u0439\u0434\u0438\u0442\u0435",
    "\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d \u0442\u043e\u043b\u044c\u043a\u043e \u0430\u0432\u0442\u043e\u0440\u0438\u0437\u043e\u0432\u0430\u043d\u043d\u044b\u043c",
    "\u0442\u043e\u043b\u044c\u043a\u043e \u0430\u0432\u0442\u043e\u0440\u0438\u0437\u043e\u0432\u0430\u043d\u043d\u044b\u043c \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f\u043c",
    "\u043d\u0435\u043e\u0431\u0445\u043e\u0434\u0438\u043c\u043e \u0430\u0432\u0442\u043e\u0440\u0438\u0437\u043e\u0432\u0430\u0442\u044c\u0441\u044f",
    "\u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u0442\u044c\u0441\u044f",
)

MARKETPLACE_MARKERS = (
    "\u0440\u043e\u0441\u044d\u043b\u0442\u043e\u0440\u0433",
    "\u0440\u043e\u0441\u0435\u043b\u0442\u043e\u0440\u0433",
    "\u0440\u043e\u0441\u0442\u0435\u043b\u0435\u043a\u043e\u043c",
    "\u044d\u0442\u043f",
    "\u0442\u043e\u0440\u0433\u043e\u0432\u0430\u044f \u0441\u0435\u043a\u0446\u0438\u044f",
    "\u043a\u043e\u0440\u043f\u043e\u0440\u0430\u0442\u0438\u0432\u043d\u0430\u044f \u0442\u043e\u0440\u0433\u043e\u0432\u0430\u044f \u0441\u0435\u043a\u0446\u0438\u044f",
    "etp",
    "marketplace",
)


@dataclass(frozen=True)
class HtmlDiagnostics:
    title: str
    preview: str
    hint: str
    form_action: str
    meta_refresh: str
    redirect_hint: str


def compact_text(value: str, max_chars: int = MAX_HTML_PREVIEW_CHARS) -> str:
    compacted = " ".join(html.unescape(value or "").split())
    if len(compacted) <= max_chars:
        return compacted

    return f"{compacted[:max_chars].rstrip()}..."


def decode_html_response(content: bytes, content_type: str | None = None) -> str:
    charset_match = re.search(
        r"charset=([A-Za-z0-9._-]+)",
        content_type or "",
        re.IGNORECASE,
    )
    encodings = []
    if charset_match:
        encodings.append(charset_match.group(1))

    encodings.extend(["utf-8", "cp1251"])

    for encoding in encodings:
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue

    return content.decode("utf-8", errors="replace")


def strip_html_tags(html_text: str) -> str:
    text = re.sub(
        r"(?is)<(script|style|noscript)\b.*?</\1>",
        " ",
        html_text,
    )
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return compact_text(text)


def extract_tag_text(html_text: str, tag_name: str) -> str:
    match = re.search(
        rf"(?is)<{tag_name}\b[^>]*>(.*?)</{tag_name}>",
        html_text,
    )
    if not match:
        return ""

    return strip_html_tags(match.group(1))


def extract_first_attr(html_text: str, tag_name: str, attr_name: str) -> str:
    tag_match = re.search(rf"(?is)<{tag_name}\b[^>]*>", html_text)
    if not tag_match:
        return ""

    attr_match = re.search(
        rf"""(?is)\b{attr_name}\s*=\s*["']?([^"'\s>]+)""",
        tag_match.group(0),
    )
    if not attr_match:
        return ""

    return compact_text(attr_match.group(1), max_chars=180)


def extract_meta_refresh(html_text: str) -> str:
    for meta_match in re.finditer(r"(?is)<meta\b[^>]*>", html_text):
        tag = meta_match.group(0)
        http_equiv = extract_attr_from_tag(tag, "http-equiv").lower()
        content = extract_attr_from_tag(tag, "content")
        if http_equiv == "refresh" or "url=" in content.lower():
            return compact_text(content, max_chars=180)

    return ""


def extract_attr_from_tag(tag: str, attr_name: str) -> str:
    attr_match = re.search(
        rf"""(?is)\b{attr_name}\s*=\s*["']?([^"'>]+)""",
        tag,
    )
    if not attr_match:
        return ""

    return compact_text(attr_match.group(1), max_chars=180)


def classify_html_response(
    *,
    title: str,
    preview: str,
    form_action: str = "",
    meta_refresh: str = "",
) -> str:
    haystack = " ".join([title, preview, form_action, meta_refresh]).lower()

    if is_marketplace_auth_required_html(
        title=title,
        preview=preview,
        form_action=form_action,
        meta_refresh=meta_refresh,
    ):
        return EXTERNAL_MARKETPLACE_AUTH_REQUIRED_HINT

    if meta_refresh or "window.location" in haystack or "redirect" in haystack:
        return "redirect_wrapper"

    if any(
        marker in haystack
        for marker in (
            "login",
            "log in",
            "sign in",
            "auth",
            "authorization",
            "authenticate",
            "парол",
            "логин",
            "авториза",
            "войдите",
        )
    ):
        return "login_page"

    if any(
        marker in haystack
        for marker in (
            "denied",
            "forbidden",
            "unauthorized",
            "access denied",
            "403",
            "доступ запрещ",
            "нет доступа",
        )
    ):
        return "access_denied"

    if any(
        marker in haystack
        for marker in (
            "error",
            "exception",
            "unavailable",
            "not found",
            "500",
            "503",
            "ошибка",
            "недоступ",
            "не найден",
        )
    ):
        return "error_page"

    return "unknown_html"


def is_marketplace_auth_required_html(
    *,
    title: str,
    preview: str,
    form_action: str = "",
    meta_refresh: str = "",
) -> bool:
    haystack = " ".join([title, preview, form_action, meta_refresh]).lower()
    has_auth_marker = any(marker in haystack for marker in AUTH_REQUIRED_MARKERS)
    has_marketplace_marker = any(marker in haystack for marker in MARKETPLACE_MARKERS)

    return has_auth_marker and has_marketplace_marker


def extract_html_diagnostics(
    content: bytes,
    content_type: str | None = None,
) -> HtmlDiagnostics:
    html_text = decode_html_response(content, content_type)
    title = compact_text(extract_tag_text(html_text, "title"), max_chars=180)
    preview = strip_html_tags(html_text)
    form_action = extract_first_attr(html_text, "form", "action")
    meta_refresh = extract_meta_refresh(html_text)
    redirect_hint = ""

    if meta_refresh:
        redirect_hint = meta_refresh
    elif re.search(r"(?is)window\.location|location\.href", html_text):
        redirect_hint = "javascript_location"

    hint = classify_html_response(
        title=title,
        preview=preview,
        form_action=form_action,
        meta_refresh=meta_refresh,
    )

    return HtmlDiagnostics(
        title=title,
        preview=preview,
        hint=hint,
        form_action=form_action,
        meta_refresh=meta_refresh,
        redirect_hint=redirect_hint,
    )
