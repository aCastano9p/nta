# NTA

A personal news-trading research system. Phase 0 measures whether material SEC 8-K filings by US large caps move prices enough to pay a retail round trip (`nta verdict` prints `VERDICT: CONTINUE` or `STOP`, the go/no-go gate). Phase 1 runs the full decision loop on Alpaca paper accounts: extract → decide → risk check → paper order → reconcile → manage → evaluate, plus replay. Python 3.12 + uv, Postgres 16 (`docker compose`), SQLAlchemy/Alembic, typer CLI `nta`.

## Hard rules

- **Append-only:** application code only inserts. A trigger raises on UPDATE/DELETE/TRUNCATE; express every change as a new row that readers pick as the winner.
- **Point-in-time:** outside data keeps its source timestamp and `first_observed_at` (our clock at first sight), which nothing re-stamps.
- **Two credential sets:** `nta agent` (and `nta evaluate`) hold account D data keys and the LLM key, never trading keys. `nta risk` holds account T trading keys and `RISK_SIGNING_KEY`, never imports the LLM client, never reads free text (signals, llm_calls, raw). Trading is paper only (`paper-api.alpaca.markets`); no order without a decision row and a valid HMAC.
- **Replayable:** decision code reads time only through a `Clock` (`nta/clock.py` is the only wall-clock reader); decision content must replay bit-identically (`tests/fixtures/replay/golden.json`). Replay never calls the LLM.
- **Policy:** `policy/policy.yaml` changes only by a human edit; code may insert `guardrail_proposals` rows.
- **Rate limits:** outside HTTP goes through `nta.http.send_with_retry` with the process's shared token bucket (SEC ≤ 10 req/s, with `SEC_USER_AGENT`). The one exception is the broker's order POST, sent exactly once.
- **Untrusted content:** fetched text is stored as data, shown only sanitized or escaped, and sent to a model only inside the user turn's `<untrusted_item>` block.
- **Secrets** come from env (`.env`, git-ignored; placeholders in `.env.example`).
- **Green before commit:** `docker compose up -d && uv run pytest -q` must exit 0.
- **Decisions:** any choice a brief leaves open gets an entry in `docs/DECISIONS.md`.

## Docs

- `docs/phase0-brief.md`, `docs/phase1-brief.md`: the specs. Read before changing behaviour they define.
- `docs/DECISIONS.md`: why things are as they are. Read before changing a behaviour or resolving an ambiguity; append when you decide.
- `docs/architecture.md`: processes, data flow, tables, module map. Read before adding a component or touching the schema.
- `docs/development.md`: test setup, patterns, gotchas. Read before writing tests or touching concurrency, signals, clocks, rate limits, replay or decision content.
- `docs/status.md`: what is built, validated live and still open. Read when planning next work; update when that changes.
- `RUNBOOK.md`: operating the processes, the kill switch, each incident and logged error, key rotation.
