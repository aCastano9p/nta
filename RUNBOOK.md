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
| `nta db check` exits 1 | Migrations not applied. | `uv run nta db upgrade`. |

## Dev reset (destroys all data)
Append-only means rows cannot be deleted. To wipe a dev database: `uv run alembic downgrade base && uv run nta db upgrade`, or `docker compose down -v`.
