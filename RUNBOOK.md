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
Set `SEC_USER_AGENT` before loading so CIKs are checked against SEC's ticker list. For allowlists of hundreds of tickers, set the optional `OPENFIGI_API_KEY` (free at openfigi.com) so the first load takes seconds instead of minutes. Re-running a load is safe (no duplicates; OpenFIGI answers come from the DB cache). Rejected rows are printed with a reason and not stored.

## EDGAR poller
Start (foreground; logs are JSON lines on stdout):
```sh
set -a; source .env; set +a
mkdir -p logs && uv run nta poll edgar | tee -a logs/poll_edgar.jsonl
```
Options: `--interval 5`, `--health-port 8765`, `--stale-after 60`, `--max-backfill-pages 5`, `--once` (single poll, then exit).

Stop: Ctrl-C or `kill -TERM <pid>` (or `pkill -TERM -f "nta poll edgar"`). The current poll finishes (worst case: a request timeout of 10 s plus retries), then a `run_stopped` line is logged and a `run_stops` row is written. A second Ctrl-C after 1 s forces exit.

Restart: just start it again. Already-stored filings are never re-stamped. If the poller was down, it reads up to 5 extra feed pages (500 filings) to catch up; filings older than that are missed, and the `feed_backfill` line shows `gap_closed: false`.

Health:
```sh
curl -s localhost:8765/healthz      # 200 = polled within 60 s; 503 = stale or never polled
```

What to watch in the logs:
- `poll_ok` every ~5 s: `stored`, `duplicate_seen`, `allowlist_size`.
- `filing_stored`: one per stored filing, with `poll_latency_s` (first_observed_at - published_at).
- Crash check: `select r.id, r.started_at from runs r left join run_stops s on s.run_id = r.id where s.id is null;` lists runs that ended without a clean stop.

## Quote recorder
```sh
set -a; source .env; set +a     # needs ALPACA_KEY_ID / ALPACA_SECRET (free data plan)
mkdir -p logs && uv run nta quotes | tee -a logs/quotes.jsonl
curl -s localhost:8766/healthz  # 200 = ticked within 30 s
```
Run it alongside `nta poll edgar`. It schedules observe/mark quotes for every stored filing, records them when due (market hours only), and records a periodic quote for every allowlisted ticker every 5 minutes. Stop with Ctrl-C / SIGTERM; restarts lose nothing (the schedule is in `quote_schedule`).

## Labeling
After the 30-day window closes:
```sh
uv run nta label --start 2026-11-02 --end 2026-12-14     # your window dates; seed 7, 100 filings by default
```
Quit any time with `q` (or Ctrl-D); run the same command again to resume where you stopped. Keep the seed fixed.

## Verdict
```sh
uv run nta verdict --start 2026-11-02 --end 2026-12-14   # prints the report; last line is VERDICT: CONTINUE or STOP
```
Writes the per-filing table to `exports/verdict_<start>_<end>.csv` (`--csv PATH` to change, `--no-csv` to skip). Run it after labeling; before that it reports "no labeled filings" and STOP.

## Dashboard
```sh
uv run nta dashboard            # real database -> http://127.0.0.1:8780/
uv run nta dashboard --demo     # rebuilds the separate nta_demo database with synthetic data and shows it
```
Read-only. Shows process status (from the two `/healthz` endpoints), market status, the provisional verdict with its two thresholds, filings per day, poll latency, every filing with its observe/1h/1d/5d status, the quote schedule, process runs (crashes are flagged "no clean stop") and the allowlist. Click a ticker for the filing detail page (quotes, label, text preview). `/verdict` shows the full report; `/verdict.csv` downloads the per-filing table. Stop with Ctrl-C. Options: `--port`, `--poller-health`, `--quotes-health`.

## Tests
```sh
docker compose up -d
uv run pytest -q              # rebuilds the nta_test database from migrations each run
```

## Errors
| Symptom | Meaning | Action |
|---|---|---|
| `append-only table X: UPDATE is not allowed` | Code tried to change or delete a stored row. | This is a bug in the caller; rows are never changed. Insert a new row instead (see docs/DECISIONS.md, "latest row wins"). |
| `Cannot reach test Postgres` (pytest) | DB container not running. | `docker compose up -d`, wait for healthy (`docker compose ps`). |
| `rejected ... figi_not_found` / `figi_not_common_stock` / `figi_ambiguous` | OpenFIGI could not map the ticker to exactly one US common stock. | Check the ticker spelling (share classes: `BRK.B`). ETFs and ADRs are rejected on purpose. |
| `rejected ... cik_mismatch` / `ticker_not_in_sec_list` | CSV CIK differs from SEC's ticker list. | Fix the CIK in the CSV (SEC's value is in the detail) and reload. |
| `rejected ... openfigi_error`, or `http_retry` lines for api.openfigi.com | OpenFIGI error or rate limit. | Retried automatically (6 attempts). If it still fails, wait a minute and reload; cached rows are not re-requested. |
| `allowlist_cik_check_skipped` | `SEC_USER_AGENT` not set. | Set it and reload to verify CIKs. |
| `error: SEC_USER_AGENT is not set` / `must look like "<name> <email>"` (exit 2) | Poller refuses to start without a valid SEC User-Agent. | Put `SEC_USER_AGENT="Your Name you@example.com"` in `.env` (quoted) and `source` it. |
| `http_retry` for www.sec.gov with status 429 | SEC throttling. Should not happen at 8 req/s. | Automatic backoff. If persistent, check that only one poller is running (`pgrep -fl "nta poll"`): the rate budget is per process. |
| `http_retry` with 5xx or `ReadTimeout` / `ConnectError` | SEC or network trouble. | Automatic (6 attempts, jittered backoff). If it keeps failing, `poll_failed` lines follow and `/healthz` turns 503 after 60 s. |
| `poll_failed` | A whole poll failed after retries (network, DB down, unexpected feed). | Polling continues every 5 s. If it repeats: check network, `docker compose ps`, and the error text. |
| `filing_fetch_failed` | Index or primary document could not be fetched; the filing was stored without `raw`. | Nothing to do for the measurement (timestamps are kept). Document text is missing for labeling; open the `url` in a browser. |
| `feed_backfill` with `gap_closed: false` | Downtime longer than ~500 filings; some filings were never observed. | Note the gap window; those filings are absent from the dataset (point-in-time: we did not see them). |
| `/healthz` 503 | No successful poll in 60 s (or not polled yet). | Check the logs for `poll_failed` / `http_retry`; restart if the process is stuck. |
| Process won't stop after SIGTERM | Waiting on a slow request or backoff. | Wait up to ~1 min, or press Ctrl-C again (after 1 s) to force; a forced stop is recorded as `error KeyboardInterrupt`. |
| `error: ALPACA_KEY_ID and ALPACA_SECRET must be set` (exit 2) | Quote recorder needs Alpaca data keys. | Put both in `.env`. |
| `quote_missing` | Alpaca returned no usable quote or trade for a symbol. | Retried every 5 s automatically. If it persists for one ticker, check the ticker exists on IEX (`alpaca_symbol` turns BRK/B into BRK.B). |
| `quotes_tick_failed` | A tick failed (Alpaca down, bad keys -> 401/403, DB down). | Ticks continue every 0.5 s; `/healthz` on 8766 turns 503 after 30 s. A 401/403 means wrong keys. |
| Dashboard shows an error page (500) | A rendering bug; the server keeps running. | The page shows the exception text; report it. Other pages still work. |
| `nta db check` exits 1 | Migrations not applied. | `uv run nta db upgrade`. |

## Dev reset (destroys all data)
Append-only means rows cannot be deleted. To wipe a dev database: `uv run alembic downgrade base && uv run nta db upgrade`, or `docker compose down -v`.
