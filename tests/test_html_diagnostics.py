from __future__ import annotations

import unittest

from app.collector.html_diagnostics import (
    EXTERNAL_MARKETPLACE_AUTH_REQUIRED_HINT,
    classify_html_response,
    extract_html_diagnostics,
    is_marketplace_auth_required_html,
)


class HtmlDiagnosticsTest(unittest.TestCase):
    def test_extracts_external_marketplace_auth_required_page(self) -> None:
        diagnostics = extract_html_diagnostics(
            """
            <html>
              <head>
                <title>Корпоративная торговая секция ПАО Ростелеком на ЭТП Росэлторг</title>
              </head>
              <body>
                Просмотр данного извещения доступен только авторизованным пользователям.
                Необходимо авторизоваться или зарегистрироваться на площадке.
              </body>
            </html>
            """.encode("utf-8"),
            "text/html; charset=utf-8",
        )

        self.assertEqual(diagnostics.hint, EXTERNAL_MARKETPLACE_AUTH_REQUIRED_HINT)
        self.assertTrue(
            is_marketplace_auth_required_html(
                title=diagnostics.title,
                preview=diagnostics.preview,
            )
        )

    def test_extracts_login_like_page(self) -> None:
        diagnostics = extract_html_diagnostics(
            b"""
            <html>
              <head><title>Login required</title></head>
              <body><form action="/account/login">Please sign in</form></body>
            </html>
            """
        )

        self.assertEqual(diagnostics.hint, "login_page")
        self.assertEqual(diagnostics.title, "Login required")
        self.assertEqual(diagnostics.form_action, "/account/login")
        self.assertIn("Please sign in", diagnostics.preview)

    def test_extracts_error_page(self) -> None:
        diagnostics = extract_html_diagnostics(
            """
            <html>
              <head><title>Ошибка сервера</title></head>
              <body><h1>500</h1><p>Сервис временно недоступен</p></body>
            </html>
            """.encode("cp1251"),
            "text/html; charset=windows-1251",
        )

        self.assertEqual(diagnostics.hint, "error_page")
        self.assertIn("Ошибка", diagnostics.title)
        self.assertIn("недоступен", diagnostics.preview)

    def test_extracts_meta_refresh_redirect(self) -> None:
        diagnostics = extract_html_diagnostics(
            b"""
            <html>
              <head>
                <title>Redirect</title>
                <meta http-equiv="refresh" content="0; url=/download/123">
              </head>
              <body>Redirecting...</body>
            </html>
            """
        )

        self.assertEqual(diagnostics.hint, "redirect_wrapper")
        self.assertEqual(diagnostics.meta_refresh, "0; url=/download/123")
        self.assertEqual(diagnostics.redirect_hint, "0; url=/download/123")

    def test_unknown_html_is_compact_and_stripped(self) -> None:
        diagnostics = extract_html_diagnostics(
            b"""
            <html>
              <head><title>Document</title><script>alert('x')</script></head>
              <body><p>Hello <b>world</b></p></body>
            </html>
            """
        )

        self.assertEqual(diagnostics.hint, "unknown_html")
        self.assertEqual(diagnostics.preview, "Document Hello world")
        self.assertNotIn("<b>", diagnostics.preview)

    def test_classify_access_denied(self) -> None:
        self.assertEqual(
            classify_html_response(
                title="Access denied",
                preview="Forbidden 403",
            ),
            "access_denied",
        )


if __name__ == "__main__":
    unittest.main()
