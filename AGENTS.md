# NTA

Phase 0 of a personal news-trading research system: measure whether material SEC 8-K filings by US large caps move prices enough to pay a retail round trip, then print `VERDICT: CONTINUE` or `STOP`. Python 3.12 + uv, Postgres 16 (`docker compose`), SQLAlchemy/Alembic, typer CLI `nta`.

## Hard rules

- **Append-only:** application code only inserts. A trigger raises on UPDATE/DELETE/TRUNCATE; express every change as a new row that readers pick as the winner.
- **Point-in-time:** outside data keeps its source timestamp and `first_observed_at` (our clock at first sight), which nothing re-stamps.
- **Phase 0 scope:** data collection and measurement only. Alpaca is used through `data.alpaca.markets`; trading endpoints, broker orders and LLM calls belong to later phases.
- **Rate limits:** outside HTTP goes through `nta.http.send_with_retry` with the process's shared token bucket (SEC ≤ 10 req/s, with `SEC_USER_AGENT`).
- **Untrusted content:** fetched text is stored as data and shown only sanitized or escaped.
- **Secrets** come from env (`.env`, git-ignored; placeholders in `.env.example`).
- **Green before commit:** `docker compose up -d && uv run pytest -q` must exit 0.
- **Decisions:** any choice the brief leaves open gets an entry in `docs/DECISIONS.md`.

## Docs

- `docs/phase0-brief.md`: the spec. Read before changing behaviour it defines.
- `docs/DECISIONS.md`: why things are as they are. Read before changing a behaviour or resolving an ambiguity; append when you decide.
- `docs/architecture.md`: processes, data flow, tables, module map. Read before adding a component or touching the schema.
- `docs/development.md`: test setup, patterns, gotchas. Read before writing tests or touching concurrency, signals, clocks or rate limits.
- `docs/status.md`: what is built, validated live and still open. Read when planning next work; update when that changes.
- `RUNBOOK.md`: operating the processes and handling each logged error.
