# Verification record

Date: 2026-07-29

## Public tree

Executed in a clean Python 3.12.10 virtual environment with global `PYTHONPATH` cleared:

- `pip install -r requirements.lock`: PASS;
- `pip check`: PASS;
- `python -m compileall -q app scripts tests`: PASS;
- `python scripts/apply_migrations.py --dry-run`: PASS;
- `python -m unittest discover`: PASS;
- tests executed: `579`.

Warnings emitted by negative-path tests are expected: mocked network failures, missing optional legacy-DOC tools and simulated Telegram errors are asserted failure behavior.

## Production evidence

A read-only check of the independent production contour confirmed on 2026-07-29:

- scheduled timer active;
- latest scheduled service result `success` with exit status `0`;
- PostgreSQL, network boundary and Telegram workflow containers running;
- restart count `0` for all three checked services.

No production records, credentials, hostnames or customer metrics are included in this repository.

## Security

The private source history scanner reported three credential-URL pattern matches. All three were verified as synthetic negative-test fixtures. In the clean-room public tree those fixture URLs are assembled at runtime so generic scanners do not misclassify them as leaked credentials. The new public history was then scanned independently with zero findings.
