# Development

How to change this code without breaking its guarantees. Commands and layout are in `pyproject.toml`, `RUNBOOK.md` and `nta --help`; this file holds what those don't say.

## Tests

- DB tests need Postgres: `docker compose up -d`. The session fixture drops and rebuilds `nta_test` with `alembic upgrade head`, so every run tests the migrations too.
- Each test runs in one connection inside a transaction that is rolled back; sessions use `join_transaction_mode="create_savepoint"`, so application `commit()`s work and vanish afterwards. Rollback is not DELETE, so append-only holds.
- Fakes: `tests/fakes.FakeClock` (time moves only on `sleep`/`advance`), `tests/edgar_fakes.FakeSec`, `FakeAlpaca` in `test_quotes.py`, `FakeOpenFigi` in `test_allowlist.py`. All HTTP in tests goes through `httpx.MockTransport`.
- The verdict fixture (`tests/fixtures/verdict/fixture.json`) has expected answers worked out by hand in `tests/test_verdict.py`'s docstring. Expected numbers come from hand calculation, never from running the code.
- `pyproject.toml` sets `faulthandler_timeout = 60`: a hung test dumps every thread's stack instead of hanging silently.
- Commit only after `uv run pytest -q` exits 0 on its own. Piping it into `tail` in a `&&` chain once let a failing suite reach a commit.

## Patterns to follow

- **Append-only:** model a change as a new row and a "latest/replayed row wins" read. A new table needs the trigger created in its migration; `test_every_table_is_covered` fails if one is missing.
- **Two clocks:** any outside data gets the source's timestamp plus `first_observed_at = clock.now()` taken right after the response.
- **Outside HTTP:** build the request, then `send_with_retry(client, req, bucket=..., clock=...)` with the process-wide bucket from `nta.ratelimit`. One bucket per service per process.
- **Long-running process:** `start_run` / `stop_run`, a `HealthServer`, a `StopFlag` with `install_signal_handlers`, a `run(stop, interval)` loop that logs and continues on errors.
- **Pure core:** parsing, scheduling and math stay functions of data; inject `Clock` and `httpx.Client` so tests use fakes.
- **Untrusted text:** terminal output goes through `label.sanitize`; HTML output through `dashboard.esc`; links only to `https://www.sec.gov`.
- **Every open choice** gets a DECISIONS.md entry; every new logged error gets a RUNBOOK row.

## Gotchas (each one bit once)

- **Signal handlers take no locks.** `threading.Event.set()` in a handler deadlocked the poller when the signal hit inside `Event.wait()`. Use `nta.shutdown.StopFlag`.
- **Float time with a fake clock:** a token bucket at 0.9999999999999 tokens asked for a sleep too small to move the clock and spun forever. The bucket now has a 1e-9 tolerance and a 1 µs minimum sleep.
- **ULID order = insertion order** only because `new_ulid` is monotonic within a process; `allowlist.active_on` replays rows by id and depends on it.
- **Rate-limited calls move a fake clock:** capture expected timestamps before the call under test.
- **Shared test connection:** threads (dashboard server, Alpaca chunk pool) share one DB connection or one `FakeClock` in tests. The dashboard serializes requests with a lock; `FakeClock` is not thread-safe.
- **`.env` is shell-sourced** (`set -a; source .env`): quote values containing spaces.
- **Dev data is permanent:** smoke tests against the dev DB leave rows that cannot be deleted; reset by migration downgrade/upgrade.
- **IEX quotes are often wide** (500+ bps on large caps): price with `verdict.price_and_source`, never a raw mid.
