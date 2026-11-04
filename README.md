# NTA: news-trading research

NTA is a personal research system that asks one question: **do material SEC 8-K filings by large US companies move their stock enough to pay for a retail round trip, and can an automated loop trade them sensibly?**

It is built in two phases:

- **Phase 0, measurement.** Collect every 8-K filed by an allowlist of large caps, record prices around each one, hand-label a sample, and print a verdict: `VERDICT: CONTINUE` or `VERDICT: STOP`. This is the go/no-go gate for everything else.
- **Phase 1, decision loop on paper.** Read each filing with an LLM, decide whether to trade under a written policy, have a separate risk process re-check and place the order on an **Alpaca paper account**, manage the position, then grade every decision after the fact. Every step is logged so any past decision can be replayed exactly.

> **Research only, paper only.** NTA has no live-trading configuration: the broker client accepts only Alpaca's paper endpoint. Nothing here is investment advice.

## Status

| | |
|---|---|
| Phase 0 | Built and validated against the live SEC, OpenFIGI and Alpaca data APIs. Waiting on a 30-day collection window and a full-size allowlist. |
| Phase 1 | Built and tested (351 tests), not yet run end to end against the live Anthropic and Alpaca trading APIs. |

Details and open items: [`docs/status.md`](docs/status.md).

## How it works

```
 SEC EDGAR feed (every 5 s) ─► nta poll edgar ─┐      X API (optional) ─► nta poll x
                                               ▼                              │
                                     source_events (filings, posts) ◄─────────┘
                                               │
         ┌─────────────────────────────────────┼────────────────────────────────────┐
         ▼                                     ▼                                    ▼
  nta quotes                         nta agent                              nta label / verdict
  prices around each filing          LLM triage + extraction                (phase 0 gate)
  (Alpaca data, account D)           clustering, policy checks, sizing
                                     → one decision per filing
                                     position manager (stop / take-profit / expiry)
                                               │ trade decisions
                                               ▼
                                     nta risk  (separate process, no LLM access)
                                     re-checks with its own quote → signs the order
                                     → Alpaca paper account T → fills
                                     reconciles every 30 s, kill switch, incidents
                                               │ after the horizon
                                               ▼
                                     nta evaluate: blind LLM grade, P&L, quadrant
                                     nta replay:   re-run any window bit-for-bit
                                     nta dashboard: read-only view of everything
```

**Where the LLM is used.** Only to *read* news: a cheap model (Claude Haiku) triages every item, a stronger one (Claude Sonnet) extracts direction, probability, thesis and counter-arguments from the material ones, and after the horizon Claude Sonnet grades each decision without seeing its outcome. The symbol, quantity, prices, risk checks and orders are all plain code driven by [`policy/policy.yaml`](policy/policy.yaml).

## Design principles

- **Append-only data.** A Postgres trigger forbids UPDATE, DELETE and TRUNCATE on every table. A change of state is a new row; current state (positions, kill switch, policy in force) is derived from the latest rows.
- **Point-in-time.** Everything from outside keeps its own timestamp plus the moment NTA first saw it. Decisions read their inputs "as of" a time, which is what makes replay exact.
- **Two processes, two sets of credentials.** `nta agent` holds market-data keys and the LLM key and refuses to start with trading keys. `nta risk` holds the trading keys and the order-signing key and refuses to start with any LLM key; it never reads free text. Two separate Alpaca paper accounts keep data and trading apart, and a watchdog raises an incident if the data account ever shows an order.
- **No order without a decision and a signature.** Every order traces to a decision row and carries an HMAC from `nta risk`; the executor verifies it before sending, never re-sends blindly, and looks orders up by a deterministic `client_order_id` after a timeout or restart.
- **Untrusted text stays data.** Filing and post text goes to the model only inside a delimited data block; output that fails schema validation becomes an abstention, and instruction-like text is flagged and never traded.
- **Replayable.** Every model call is cached by request hash; `nta replay` re-runs a window from the log without calling the model and checks every decision's content hash against live.
- **Humans own the policy and the kill switch.** Code may propose policy changes (as rows), never edit the file. `nta kill pause | flatten | halt | resume` is for people; resuming requires typing `RESUME`.

## Quickstart

Requires Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker.

```sh
cp .env.example .env            # fill in SEC_USER_AGENT and Alpaca data keys at minimum
docker compose up -d            # Postgres 16 on 127.0.0.1:55432
uv sync
set -a; source .env; set +a
uv run nta db upgrade
uv run nta allowlist load data/allowlist_dev.csv
uv run pytest -q                # the full suite; needs the Postgres container
```

Phase 0 (measurement):

```sh
uv run nta poll edgar           # store allowlisted 8-K filings as they appear
uv run nta quotes               # record prices around each filing
uv run nta label                # hand-label a seeded sample (after the window)
uv run nta verdict              # VERDICT: CONTINUE or STOP
uv run nta dashboard            # http://127.0.0.1:8780/  (--demo for synthetic data)
```

Phase 1 (paper trading) needs `ANTHROPIC_API_KEY`, a second Alpaca paper account and a signing key in `.env` (see [`.env.example`](.env.example)). The simplest way to run all long-running processes with the right credentials each:

```sh
docker compose --profile app up -d --build    # poller, quotes, agent, risk
uv run nta kill status
uv run nta evaluate                           # after positions close
uv run nta replay --from 2026-11-04 --to 2026-11-04
```

Plain `docker compose up -d` starts only Postgres. Full operating instructions, every incident type and key rotation are in [`RUNBOOK.md`](RUNBOOK.md).

## Commands

| Command | What it does |
|---|---|
| `nta poll edgar` | Polls the SEC 8-K feed and stores allowlisted filings. |
| `nta quotes` | Records prices at observation, +1 h, +1 day, +5 days, and every 5 minutes. |
| `nta label [--review]` | Labels filings in the terminal, or (`--review`) grades decisions blind. |
| `nta verdict` | Prints the phase 0 verdict and exports a per-filing CSV. |
| `nta agent` | Reads, clusters and decides on new filings; manages open positions. Never places orders. |
| `nta risk` | Re-checks, signs and sends paper orders; reconciles; watches the data account. |
| `nta kill ...` | Kill switch, for humans only. |
| `nta evaluate` | Grades and measures decisions whose horizon has passed. |
| `nta replay` | Re-runs a window from the log; `--policy` compares two policies side by side. |
| `nta poll x` | Optional X ingestion from approved accounts (off unless `X_ENABLED=true`). |
| `nta dashboard` | Read-only HTML dashboard on localhost (no JavaScript). |
| `nta allowlist`, `nta db` | Manage the allowlist and migrations. |

## Repository layout

```
nta/          the package (one module per component; see docs/architecture.md)
migrations/   Alembic migrations, including the append-only triggers
policy/       policy.yaml: every trading limit, edited by humans only
prompts/      versioned LLM prompts (triage, extraction, grading)
tests/        pytest suite, fakes for SEC, Alpaca, the broker and the LLM; golden replay log
data/         development allowlist
docs/         specs, decisions, architecture, development notes, status
sources.yaml  approved X accounts (empty by default)
```

## Documentation

| Read | When |
|---|---|
| [`docs/phase0-brief.md`](docs/phase0-brief.md), [`docs/phase1-brief.md`](docs/phase1-brief.md) | The specifications. |
| [`docs/architecture.md`](docs/architecture.md) | Processes, data flow, tables, module map. |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Why things are the way they are: every choice the specs left open. |
| [`docs/development.md`](docs/development.md) | Test setup, patterns and gotchas before changing code. |
| [`docs/status.md`](docs/status.md) | What is built, what is validated live, what is open. |
| [`RUNBOOK.md`](RUNBOOK.md) | Operating the system, the kill switch, incidents, key rotation. |

## Tech

Python 3.12 · uv · PostgreSQL 16 · SQLAlchemy and Alembic · Pydantic v2 · httpx · Typer · pandas-market-calendars · Anthropic Python SDK · pytest. Market data and paper trading through Alpaca; filings from SEC EDGAR; FIGIs from OpenFIGI.
