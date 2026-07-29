# Security policy

## Supported version

Security fixes are applied to the current `main` branch.

## Reporting a vulnerability

Do not open a public issue for credentials, access-control failures, SSRF, unsafe archive extraction, document parser issues, prompt-injection paths or data exposure.

Report privately through GitHub Security Advisories for this repository. Include:

- affected component;
- minimal reproduction using synthetic data;
- expected and observed behavior;
- impact and suggested mitigation, if known.

Never include production tokens, customer records, private tender documents or live service credentials in a report.

## Security boundary

The repository does not contain production credentials or data. Operators are responsible for:

- using credentials only for sources they are authorized to access;
- restricting Telegram recipients;
- isolating document processing;
- keeping PostgreSQL and provider endpoints private;
- applying network egress controls and rate limits;
- reviewing LLM output before commercial action.
