from __future__ import annotations

import unittest
from collections.abc import Callable, Iterable
from types import SimpleNamespace

import app.collector.safe_http_download as safe_http_download
from app.collector.safe_http_download import (
    DownloadRedirectLimitError,
    DownloadSizeLimitError,
    UnsafeDownloadUrlError,
    download_bounded_https_document,
    ensure_public_download_target,
    is_allowed_https_url,
)


ALLOWED_HOSTS = ("zakupki.gov.ru",)
PUBLIC_ADDRESS = ("93.184.216.34",)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        chunks: Iterable[bytes] = (),
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}
        self._chunks = tuple(chunks)
        self.request = SimpleNamespace(method="GET", url=url)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise safe_http_download.httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=self,
            )

    def iter_bytes(self, *, chunk_size: int) -> Iterable[bytes]:
        del chunk_size
        yield from self._chunks


class FakeClient:
    def __init__(self, handler: Callable[[str], FakeResponse]) -> None:
        self.handler = handler
        self.requested_urls: list[str] = []

    def stream(self, method: str, url: str, *, follow_redirects: bool) -> FakeResponse:
        if method != "GET" or follow_redirects:
            raise AssertionError("safe downloader must use a manual non-following GET")
        self.requested_urls.append(url)
        return self.handler(url)


class SafeHttpDownloadTest(unittest.TestCase):
    def test_url_policy_allows_only_trusted_https_default_port(self) -> None:
        allowed = (
            "https://zakupki.gov.ru/file.pdf",
            "https://sub.zakupki.gov.ru:443/file.pdf?download=1",
        )
        rejected = (
            "http://zakupki.gov.ru/file.pdf",
            "https://zakupki.gov.ru:8443/file.pdf",
            "https://zakupki.gov.ru.evil.test/file.pdf",
            "https://token:" + "secret@zakupki.gov.ru/file.pdf",
            "https://zakupki.gov.ru\\@evil.test/file.pdf",
            " https://zakupki.gov.ru/file.pdf",
            "https://zakupki.gov.ru/file.pdf\n",
        )

        for url in allowed:
            with self.subTest(url=url):
                self.assertTrue(is_allowed_https_url(url, allowed_host_suffixes=ALLOWED_HOSTS))
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(is_allowed_https_url(url, allowed_host_suffixes=ALLOWED_HOSTS))

    def test_public_target_rejects_private_and_mixed_dns_answers(self) -> None:
        ensure_public_download_target(
            "https://zakupki.gov.ru/file.pdf",
            allowed_host_suffixes=ALLOWED_HOSTS,
            resolver=lambda host, port: PUBLIC_ADDRESS,
        )

        rejected_answers = (
            ("127.0.0.1",),
            ("10.0.0.1",),
            ("169.254.169.254",),
            ("93.184.216.34", "192.168.1.2"),
        )
        for addresses in rejected_answers:
            with self.subTest(addresses=addresses):
                with self.assertRaisesRegex(UnsafeDownloadUrlError, "non-public"):
                    ensure_public_download_target(
                        "https://zakupki.gov.ru/file.pdf",
                        allowed_host_suffixes=ALLOWED_HOSTS,
                        resolver=lambda host, port, values=addresses: values,
                    )

    def test_downloads_allowed_document_with_streaming_limit(self) -> None:
        def handler(url: str) -> FakeResponse:
            return FakeResponse(
                200,
                url=url,
                headers={"content-type": "application/pdf"},
                chunks=(b"%PDF", b"-1.7", b"\nbody"),
            )

        response = download_bounded_https_document(
            FakeClient(handler),
            "https://zakupki.gov.ru/file.pdf",
            allowed_host_suffixes=ALLOWED_HOSTS,
            max_redirects=3,
            max_bytes=1024,
            resolver=lambda host, port: PUBLIC_ADDRESS,
            chunk_size=4,
        )

        self.assertEqual(response.content, b"%PDF-1.7\nbody")
        self.assertEqual(str(response.url), "https://zakupki.gov.ru/file.pdf")

    def test_validates_every_redirect_before_requesting_it(self) -> None:
        requested_hosts: list[str] = []
        resolved_hosts: list[str] = []

        def handler(url: str) -> FakeResponse:
            host = safe_http_download.urlsplit(url).hostname or ""
            requested_hosts.append(host)
            if host == "zakupki.gov.ru":
                return FakeResponse(
                    302,
                    url=url,
                    headers={"location": "https://files.zakupki.gov.ru/document.pdf"},
                )
            return FakeResponse(200, url=url, chunks=(b"document",))

        def resolver(host: str, port: int) -> tuple[str, ...]:
            resolved_hosts.append(host)
            return PUBLIC_ADDRESS

        response = download_bounded_https_document(
            FakeClient(handler),
            "https://zakupki.gov.ru/start",
            allowed_host_suffixes=ALLOWED_HOSTS,
            max_redirects=3,
            max_bytes=1024,
            resolver=resolver,
        )

        self.assertEqual(response.content, b"document")
        self.assertEqual(requested_hosts, ["zakupki.gov.ru", "files.zakupki.gov.ru"])
        self.assertEqual(resolved_hosts, ["zakupki.gov.ru", "files.zakupki.gov.ru"])

    def test_rejects_redirect_to_untrusted_host_before_second_request(self) -> None:
        def handler(url: str) -> FakeResponse:
            return FakeResponse(
                302,
                url=url,
                headers={"location": "https://127.0.0.1/internal"},
            )

        client = FakeClient(handler)
        with self.assertRaises(UnsafeDownloadUrlError):
            download_bounded_https_document(
                client,
                "https://zakupki.gov.ru/start",
                allowed_host_suffixes=ALLOWED_HOSTS,
                max_redirects=3,
                max_bytes=1024,
                resolver=lambda host, port: PUBLIC_ADDRESS,
            )

        self.assertEqual(len(client.requested_urls), 1)

    def test_rejects_redirect_when_trusted_host_resolves_to_private_address(self) -> None:
        def handler(url: str) -> FakeResponse:
            return FakeResponse(
                302,
                url=url,
                headers={"location": "https://files.zakupki.gov.ru/internal"},
            )

        def resolver(host: str, port: int) -> tuple[str, ...]:
            if host == "files.zakupki.gov.ru":
                return ("169.254.169.254",)
            return PUBLIC_ADDRESS

        client = FakeClient(handler)
        with self.assertRaisesRegex(UnsafeDownloadUrlError, "non-public"):
            download_bounded_https_document(
                client,
                "https://zakupki.gov.ru/start",
                allowed_host_suffixes=ALLOWED_HOSTS,
                max_redirects=3,
                max_bytes=1024,
                resolver=resolver,
            )

        self.assertEqual(len(client.requested_urls), 1)

    def test_stops_redirect_loop_at_configured_limit(self) -> None:
        def handler(url: str) -> FakeResponse:
            return FakeResponse(302, url=url, headers={"location": "/loop"})

        client = FakeClient(handler)
        with self.assertRaises(DownloadRedirectLimitError):
            download_bounded_https_document(
                client,
                "https://zakupki.gov.ru/loop",
                allowed_host_suffixes=ALLOWED_HOSTS,
                max_redirects=3,
                max_bytes=1024,
                resolver=lambda host, port: PUBLIC_ADDRESS,
            )

        self.assertEqual(len(client.requested_urls), 4)

    def test_rejects_oversized_content_length_before_reading_body(self) -> None:
        def handler(url: str) -> FakeResponse:
            return FakeResponse(
                200,
                url=url,
                headers={"content-length": "1025"},
                chunks=(b"small fixture",),
            )

        with self.assertRaises(DownloadSizeLimitError):
            download_bounded_https_document(
                FakeClient(handler),
                "https://zakupki.gov.ru/file.pdf",
                allowed_host_suffixes=ALLOWED_HOSTS,
                max_redirects=3,
                max_bytes=1024,
                resolver=lambda host, port: PUBLIC_ADDRESS,
            )

    def test_rejects_chunked_body_that_crosses_limit(self) -> None:
        def handler(url: str) -> FakeResponse:
            return FakeResponse(200, url=url, chunks=(b"x" * 513, b"x" * 512))

        with self.assertRaises(DownloadSizeLimitError):
            download_bounded_https_document(
                FakeClient(handler),
                "https://zakupki.gov.ru/file.pdf",
                allowed_host_suffixes=ALLOWED_HOSTS,
                max_redirects=3,
                max_bytes=1024,
                resolver=lambda host, port: PUBLIC_ADDRESS,
                chunk_size=128,
            )

    def test_preserves_http_status_error_for_rate_limit_handling(self) -> None:
        def handler(url: str) -> FakeResponse:
            return FakeResponse(429, url=url, headers={"retry-after": "10"})

        with self.assertRaises(safe_http_download.httpx.HTTPStatusError) as raised:
            download_bounded_https_document(
                FakeClient(handler),
                "https://zakupki.gov.ru/file.pdf",
                allowed_host_suffixes=ALLOWED_HOSTS,
                max_redirects=3,
                max_bytes=1024,
                resolver=lambda host, port: PUBLIC_ADDRESS,
            )

        self.assertEqual(raised.exception.response.status_code, 429)


if __name__ == "__main__":
    unittest.main()
