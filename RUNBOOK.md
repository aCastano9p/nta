# Runbook

## Setup (once)
```sh
cp .env.example .env          # fill in SEC_USER_AGENT and Alpaca keys
docker compose up -d          # Postgres 16 on 127.0.0.1:55432
uv sync
set -a; source .env; set +a
uv run nta db upgrade         # apply migrations
uv run nta db check           # prints revision; exits 1 if not at head
```

## Allowlist
```sh
uv run nta allowlist load data/allowlist_dev.csv   # or your CSV: ticker,name,cik
uv run nta allowlist show [--as-of 2026-11-02]
uv run nta allowlist remove XOM [--as-of 2026-11-02]
```
Set `SEC_USER_AGENT` before loading so CIKs are checked against SEC's ticker list. Re-running a load is safe (no duplicates; OpenFIGI answers come from the DB cache). Rejected rows are printed with a reason and not stored.

## Tests
```sh
docker compose up -d
uv run pytest -q              # rebuilds the nta_test database from migrations each run
```

## Errors
| Symptom | Meaning | Action |
|---|---|---|
| `append-only table X: UPDATE is not allowed` | Code tried to change or delete a stored row. | This is a bug in the caller; rows are never changed. Insert a new row instead (see DECISIONS.md "latest row wins"). |
| `Cannot reach test Postgres` (pytest) | DB container not running. | `docker compose up -d`, wait for healthy (`docker compose ps`). |
| `rejected ... figi_not_found` / `figi_not_common_stock` / `figi_ambiguous` | OpenFIGI could not map the ticker to exactly one US common stock. | Check the ticker spelling (share classes: `BRK.B`). ETFs and ADRs are rejected on purpose. |
| `rejected ... cik_mismatch` / `ticker_not_in_sec_list` | CSV CIK differs from SEC's ticker list. | Fix the CIK in the CSV (SEC's value is in the detail) and reload. |
| `rejected ... openfigi_error`, or `http_retry` lines for api.openfigi.com | OpenFIGI error or rate limit. | Retried automatically (6 attempts). If it still fails, wait a minute and reload; cached rows are not re-requested. |
| `allowlist_cik_check_skipped` | `SEC_USER_AGENT` not set. | Set it and reload to verify CIKs. |
| `nta db check` exits 1 | Migrations not applied. | `uv run nta db upgrade`. |

## Dev reset (destroys all data)
Append-only means rows cannot be deleted. To wipe a dev database: `uv run alembic downgrade base && uv run nta db upgrade`, or `docker compose down -v`.
