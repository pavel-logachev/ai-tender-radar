from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import SplitResult, urljoin, urlsplit

import httpx


HostResolver = Callable[[str, int], Iterable[str]]


@dataclass(frozen=True)
class BufferedDownloadResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    url: str


class UnsafeDownloadUrlError(RuntimeError):
    """Raised when a document URL crosses the allowed network boundary."""


class DownloadRedirectLimitError(RuntimeError):
    """Raised when a document response exceeds the bounded redirect budget."""


class DownloadSizeLimitError(RuntimeError):
    """Raised when a document response exceeds the bounded byte budget."""


def _normalized_hostname(parsed: SplitResult) -> str | None:
    raw_host = parsed.hostname
    if not raw_host:
        return None

    try:
        return raw_host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None


def _normalized_host_suffixes(values: Iterable[str]) -> tuple[str, ...]:
    suffixes: list[str] = []
    for value in values:
        normalized = str(value or "").strip().strip(".").lower()
        if normalized:
            suffixes.append(normalized)
    return tuple(suffixes)


def is_allowed_https_url(
    value: str | None,
    *,
    allowed_host_suffixes: Iterable[str],
) -> bool:
    if not value or value != value.strip():
        return False
    if "\\" in value or any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        return False

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False

    host = _normalized_hostname(parsed)
    suffixes = _normalized_host_suffixes(allowed_host_suffixes)
    if not host or not suffixes:
        return False

    return (
        parsed.scheme.lower() == "https"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)
    )


def resolve_hostname_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        address_info = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeDownloadUrlError("document host resolution failed") from exc

    addresses = {
        str(item[4][0]).split("%", 1)[0]
        for item in address_info
        if item[4] and item[4][0]
    }
    return tuple(sorted(addresses))


def ensure_public_download_target(
    value: str,
    *,
    allowed_host_suffixes: Iterable[str],
    resolver: HostResolver = resolve_hostname_addresses,
) -> None:
    if not is_allowed_https_url(value, allowed_host_suffixes=allowed_host_suffixes):
        raise UnsafeDownloadUrlError("document URL is outside the trusted HTTPS boundary")

    parsed = urlsplit(value)
    host = _normalized_hostname(parsed)
    if not host:
        raise UnsafeDownloadUrlError("document URL has no valid hostname")

    try:
        addresses = tuple(resolver(host, parsed.port or 443))
    except UnsafeDownloadUrlError:
        raise
    except Exception as exc:
        raise UnsafeDownloadUrlError("document host resolution failed") from exc

    if not addresses:
        raise UnsafeDownloadUrlError("document hostname resolved to no addresses")

    for value_address in addresses:
        try:
            address = ipaddress.ip_address(str(value_address).split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeDownloadUrlError("document hostname resolved to an invalid address") from exc
        if not address.is_global:
            raise UnsafeDownloadUrlError("document hostname resolved to a non-public address")


def _declared_content_length(response: httpx.Response) -> int | None:
    raw_value = response.headers.get("content-length")
    if raw_value is None:
        return None

    normalized = raw_value.strip()
    if not normalized.isdigit():
        raise DownloadSizeLimitError("document response has invalid Content-Length")
    return int(normalized)


def download_bounded_https_document(
    client: httpx.Client,
    url: str,
    *,
    allowed_host_suffixes: Iterable[str],
    max_redirects: int,
    max_bytes: int,
    resolver: HostResolver = resolve_hostname_addresses,
    chunk_size: int = 64 * 1024,
) -> BufferedDownloadResponse:
    if max_redirects < 0:
        raise ValueError("max_redirects must be non-negative")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    current_url = url
    redirects_followed = 0

    while True:
        ensure_public_download_target(
            current_url,
            allowed_host_suffixes=allowed_host_suffixes,
            resolver=resolver,
        )

        with client.stream("GET", current_url, follow_redirects=False) as response:
            if 300 <= response.status_code < 400:
                location = response.headers.get("location")
                if not location:
                    raise UnsafeDownloadUrlError("document redirect has no Location header")
                if redirects_followed >= max_redirects:
                    raise DownloadRedirectLimitError("document redirect limit exceeded")

                current_url = urljoin(str(response.url), location)
                redirects_followed += 1
                continue

            response.raise_for_status()

            declared_length = _declared_content_length(response)
            if declared_length is not None and declared_length > max_bytes:
                raise DownloadSizeLimitError("document response exceeds the byte limit")

            content = bytearray()
            for chunk in response.iter_bytes(chunk_size=chunk_size):
                if len(content) + len(chunk) > max_bytes:
                    raise DownloadSizeLimitError("document response exceeds the byte limit")
                content.extend(chunk)

            return BufferedDownloadResponse(
                status_code=response.status_code,
                headers=response.headers,
                content=bytes(content),
                url=str(response.url),
            )
