# Architecture

## System objective

AI Tender Radar converts a broad procurement feed into a small, auditable queue of customer-development signals. It is intentionally split between semantic reasoning and deterministic execution.

## Pipeline

1. **Collection**
   - query packs and source budgets;
   - normalized short and full records;
   - additive source adapters.
2. **Cheap gate**
   - hard-noise and safety filtering;
   - deduplication and minimum evidence checks;
   - bounded shortlist size.
3. **Lead triage**
   - strict input/output contracts;
   - provider-independent LLM boundary;
   - shadow evaluation and reason capture.
4. **Document planning**
   - metadata-first selection;
   - bounded number and size of downloads;
   - deterministic fallback when planning fails.
5. **Safe acquisition**
   - URL validation;
   - rate-limit and retry accounting;
   - archive/content-type verification;
   - extraction diagnostics.
6. **Evidence selection**
   - technical-spec detection;
   - primary-document and embedded-section selection;
   - no-text and missing-evidence guards.
7. **Report and delivery**
   - customer-lead report contract;
   - PostgreSQL persistence;
   - Telegram queue and Excel export;
   - human feedback and operational audit.

## Failure model

- External source failures are isolated behind adapter and budget boundaries.
- `429` responses stop or defer additional downloads instead of exhausting quota.
- Empty or malformed LLM output is rejected by schema validation.
- Missing primary evidence prevents unsupported deep analysis.
- Durable jobs use explicit status and retry state.
- Delivery retries do not silently duplicate analytical reports.

## Data boundary

Raw source records, downloaded documents, extracted text, LLM prompts/outputs and feedback may contain sensitive business information. Production deployments must treat all five as protected data and apply retention, access and logging controls.

## Public-versus-production boundary

The public tree contains the application architecture and sanitized profiles. Production credentials, datasets, reports, host configuration, incident history and vendor documentation are maintained separately.
