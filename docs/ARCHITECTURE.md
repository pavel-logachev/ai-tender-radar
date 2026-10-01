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

## Agent-first contour (`agent_radar`)

The batch pipeline above is complemented by a contour in which an agent never reaches live sources or databases:

1. **Source worker (not published).** A provider-specific worker with its own credentials exports a bounded, schema-checked source export. The export format is public (`agent_radar/source_export.py`); the connectors are not.
2. **Snapshot and bundle.** `snapshot.py` and `bundle.py` materialize versioned, bounded snapshots and publish snapshot, provenance manifest and commit together as one visible offline bundle. Consumers verify checksums before reading.
3. **Offline evidence.** `document_extract.py` and `document_package.py` turn document bytes into bounded text without extracting archives to paths, OCR or external fetches. A failed extraction is never treated as evidence that a document is absent. Untrusted documents belong in a network-disabled worker with memory, CPU and process limits.
4. **Read-only broker.** `mcp_server.py` exposes `list_candidates`, `get_tender` and `read_document_chunk` over MCP. It has no source API or database credentials and cannot download or publish.
5. **Reviewed suggestions.** `lead.py` and `review_queue.py` apply strict offline evidence checks before an agent suggestion may be reviewed, and keep manual decisions and durable delivery claims in a local store.
6. **Lead research agent.** `lead_agent/` runs an OpenAI-compatible agent loop. Code enforces only hard budgets (turns and money); behaviour is steered by the prompt. Web tools refuse non-public addresses, mark fetched content as untrusted and cap output. `grounding.py` removes any phone, e-mail or name that never occurs in a tool output and recomputes the lead grade from what was verified.
7. **Delivery.** `lead_agent/card.py` renders a bounded Telegram card; `excel_report.py` and `lead_agent/export.py` write passive workbooks with no formulas, macros or embedded objects; `digest_store.py` and `digest_delivery.py` keep a pure digest journal and send single-response reports through the host bot.

Design rules: judgement lives in the prompt, plumbing and safety live in code; every claim a person may call must be traceable to something the agent read; nothing a model emits is trusted to be a contact, a link or markup.

## Failure model

- External source failures are isolated behind adapter and budget boundaries.
- `429` responses stop or defer additional downloads instead of exhausting quota.
- Empty or malformed LLM output is rejected by schema validation.
- Missing primary evidence prevents unsupported deep analysis.
- Durable jobs use explicit status and retry state.
- Delivery retries do not silently duplicate analytical reports.
- Agent output is grounded mechanically: unverified contacts are removed, grades are recomputed and results with internal model markers are rejected before rendering.
- The agent loop stops on turn or cost budgets and forces a final answer instead of looping.
- Offline bundles are staged in a hidden directory and published with a same-parent rename on a local filesystem, then verified before use. This is not a cross-process lock or a power-loss durability guarantee.

## Data boundary

Raw source records, downloaded documents, extracted text, LLM prompts/outputs and feedback may contain sensitive business information. Production deployments must treat all five as protected data and apply retention, access and logging controls.

## Public-versus-production boundary

The public tree contains the application architecture, the agent-first core and sanitized profiles. Production credentials, datasets, customer research, reports, provider connectors, the production prompt, host and container configuration, incident history and vendor documentation are maintained separately.
