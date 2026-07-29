# Third-party notices

Runtime packages are pinned in `requirements.lock`. Their upstream licenses remain in force.

Notable license boundaries at publication time:

| Package group | License family | Note |
|---|---|---|
| PyMuPDF | AGPL-3.0 or commercial | The public application is licensed under AGPL-3.0. |
| psycopg / psycopg-binary | LGPL-3.0 | PostgreSQL client. |
| python-telegram-bot | LGPL-3.0 | Telegram workflow adapter. |
| py7zr and related archive packages | LGPL-2.1 or later | Archive processing. |
| certifi | MPL-2.0 | CA certificate bundle. |
| requests | Apache-2.0 | HTTP client. |
| pydantic, httpx, openpyxl and most remaining packages | MIT/BSD/PSF-compatible | See each distribution for exact terms. |

This file is a practical inventory, not a replacement for upstream license texts. Before redistributing a packaged application, regenerate the dependency inventory from the exact lockfile and retain all notices required by the package authors.
