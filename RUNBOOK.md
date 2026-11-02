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

## Phase 1: decision loop on paper accounts

### Credentials (once)
Two **separate Alpaca paper accounts**: D (data; the phase 0 `ALPACA_KEY_ID` / `ALPACA_SECRET`) and T (trading; `ALPACA_TRADING_KEY_ID` / `ALPACA_TRADING_SECRET`). Account T must be flat and used by nothing but NTA, or the reconciler pauses trading. Add to `.env`:
```sh
ANTHROPIC_API_KEY=...            # agent and evaluate only
ALPACA_TRADING_KEY_ID=...        # risk only
ALPACA_TRADING_SECRET=...
RISK_SIGNING_KEY=$(openssl rand -hex 32)   # paste the value; risk only
```
Never run `nta agent` from a shell that has the trading keys or the signing key, nor `nta risk` from a shell with any `ANTHROPIC_*` variable: each refuses to start (exit 2). With docker compose this split is automatic.

### Start / stop (docker compose)
```sh
NTA_CODE_SHA=$(git rev-parse HEAD) docker compose --profile app up -d --build   # db, migrate, poller, quotes, agent, risk
docker compose --profile app ps                       # health column
docker compose --profile app logs -f agent risk       # JSON lines
docker compose --profile app stop agent               # graceful (SIGTERM, up to 75 s)
docker compose --profile app down                     # stop everything (data stays in the volume)
```
`docker compose up -d` without the profile starts only Postgres (what the tests need). Health endpoints are published on the host's 127.0.0.1: poller 8765, quotes 8766, agent 8767, risk 8768. A process missing its credentials exits 2 and compose restarts it in a loop; `docker compose logs <service>` shows the `error:` line.

### Start / stop (host, for development)
```sh
# agent shell: only data keys + LLM key
env -u ALPACA_TRADING_KEY_ID -u ALPACA_TRADING_SECRET -u RISK_SIGNING_KEY uv run nta agent | tee -a logs/agent.jsonl
# risk shell: trading keys, no LLM key
env -u ANTHROPIC_API_KEY uv run nta risk | tee -a logs/risk.jsonl
```
Stop with Ctrl-C / SIGTERM like the phase 0 processes. Restarts are safe: the agent decides any signal left undecided; risk reconciles before sending anything and looks every pending order up by `client_order_id` before (re)sending.

Startup order does not matter. Until risk has written its first account T snapshot, the agent's decisions `wait` with `no_equity`. Both processes read `policy/policy.yaml` once at startup; after editing it, restart **both** (risk rejects decisions made under another policy version with `policy_mismatch`).

### What to watch
- Agent: `signal_written`, `decision_written` (`outcome`, `reasons`, `decide_latency_s`), `llm_call`, `llm_budget_hit`. `curl -s localhost:8767/healthz`.
- Risk: `reconciled` every 30 s, `risk_review`, `order_event` (`step`), `fill`, `incident`. `curl -s localhost:8768/healthz` (503 while any incident is open).
- Dashboard: `uv run nta dashboard` → `/loop`, `/decisions`, `/orders`, `/positions`, `/incidents`, `/spend`.

### Kill switch (humans only)
```sh
uv run nta kill status
uv run nta kill pause   -r "why"   # no new entries; stops, take-profits and expiries still close positions
uv run nta kill flatten -r "why"   # no entries; risk writes a close decision for every open position
uv run nta kill halt    -r "why"   # nothing is sent, not even closes; open orders are cancelled every tick
uv run nta kill resume  -r "why"   # lists open incidents, asks you to type RESUME; closes all open incidents
```
Processes only ever set PAUSE. After FLATTEN has closed everything, stay in FLATTEN or PAUSE until you resume. HALT leaves positions unmanaged: use it when orders themselves are the problem, and switch to FLATTEN or PAUSE once that is understood.

### Incidents
Each sets PAUSE (except where noted) and stays open, shown on risk's `/healthz` and `/incidents`, until `nta kill resume`.

| Kind (severity) | Meaning | Action |
|---|---|---|
| `reconcile_mismatch` (2) | Account T positions or open orders differ from the log (`detail` lists symbols with broker vs log qty, and unknown order ids). | Look at account T in Alpaca's paper dashboard. If someone traded T by hand, close that position by hand. If a fill was missed, it will be recorded on the next sync; check `/positions` shows "match", then resume. |
| `account_d_activity` (1) | Account D (data keys) shows an order or position. The data keys were used to trade. | Treat the D keys as compromised: rotate them (below), cancel/close in account D by hand, find out who used them. Resume only after D is empty. |
| `hmac_invalid` (1) | An order row with a bad signature was found; it was not sent. | Someone or something wrote to `orders` without the signing key, or the key changed under a running process. Inspect the row (`select * from orders where id = ...`), check DB access, then resume. |
| `order_without_trade_decision` (1) | A signed order points at a decision that is not a trade. Not sent. | A bug or tampering; inspect before resuming. |
| `drawdown_pause` (2) | Equity is 5 % or more below its peak snapshot. | Review recent trades (`/decisions`, `nta evaluate`) before resuming; consider a policy change. |
| `order_missing_at_broker` (2) | A sent order is not found by its `client_order_id`. | Check Alpaca's order list; if it is really gone, the order's last event stays as is and the position manager will retry closes; resume after `/positions` matches. |

### Evaluation
```sh
uv run nta evaluate            # after positions close / horizons pass; idempotent
uv run nta label --review      # grade a 20 % sample + every decision with a zero; q to stop, rerun to resume
```
Needs agent-side credentials (account D for SPY bars, the LLM key for grading; `--no-grade` skips the LLM). Proposals appear in `guardrail_proposals`; they never change the policy.

### Replay
```sh
uv run nta replay --from 2026-11-04 --to 2026-11-04                        # exit 1 if any decision differs from live
uv run nta replay --from 2026-11-02 --to 2026-11-06 --policy my_policy.yaml  # two simulated portfolio paths
```
Writes only to new schemas `replay_<run_id>`; drop old ones with `DROP SCHEMA replay_... CASCADE`. Never calls the LLM: `CacheMiss` means an event in the window was never processed live (e.g. the agent was down or the event was older than its 24 h lookback).

### X ingestion (optional)
Add accounts to `sources.yaml` (numeric author id, approved handle and display name), set `X_ENABLED=true`, `X_BEARER_TOKEN`, `X_DAILY_USD` (default 3), then `uv run nta poll x` (health :8769). Restart `nta agent` after editing `sources.yaml`. A quarantined account (changed handle or name) shows `source_quarantined` on its signals: verify the account, then update `sources.yaml`.

### Rotating keys
- **Anthropic key:** create a new key, update `.env`, `docker compose --profile app up -d agent` (or restart `nta agent` / `nta evaluate`), revoke the old key. Cached answers stay valid.
- **Alpaca account D:** regenerate in the paper dashboard of account D, update `ALPACA_KEY_ID` / `ALPACA_SECRET`, restart `quotes`, `agent` and `risk` (the watchdog uses them).
- **Alpaca account T:** `nta kill pause -r "rotating T keys"`, wait for working orders to finish, regenerate, update `ALPACA_TRADING_*`, restart `risk`, check `reconciled` with no mismatch, `nta kill resume`.
- **`RISK_SIGNING_KEY`:** `nta kill pause`, wait until `/orders` shows no unsent orders (an unsent order signed with the old key would raise `hmac_invalid` after the change), set the new key, restart `risk` (it writes a newly signed `policy_versions` row), `nta kill resume`.
- **X bearer token:** update `X_BEARER_TOKEN`, restart `nta poll x`.

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
| `error: nta agent refuses to start: trading credentials in its environment` / `nta risk refuses to start: LLM credentials` (exit 2) | Credential split violated. | Remove the named variables from that process's environment (see "Start / stop (host)"). |
| `error: account T and account D must be different paper accounts` (exit 2) | `ALPACA_TRADING_KEY_ID` equals `ALPACA_KEY_ID`. | Use a second paper account for T. |
| `error: RISK_SIGNING_KEY must be set` / `ANTHROPIC_API_KEY must be set` (exit 2) | Missing secret. | Set it in `.env`. |
| `llm_budget_hit` | `LLM_DAILY_USD` spent since New York midnight; new items are abstained with `llm_budget`. | Expected on a busy day; raise the cap if the spend is worth it. Check `/spend`. |
| `llm_call` with `status: error` | Anthropic API failed after the SDK's retries; stored, signal abstained `llm_error`. | Transient: nothing. Repeated: check the key, network, status page. |
| `llm_call` with `status: invalid` | Output failed schema validation (or refusal / max_tokens); signal abstained `llm_invalid`. | Occasional is fine. Many in a row: look at `llm_calls.response` for the pattern. |
| `agent_tick_failed` / `risk_tick_failed` | A tick raised (DB down, Alpaca error, bug). | Ticks continue; `/healthz` turns 503 when stale. Read the error. |
| `quote_missing` (agent) | No snapshot for the symbol; the decision waits with `quote_missing`. | Check the symbol on IEX. |
| `reconcile_failed` | Account T unreachable or keys wrong. Before the first success nothing is sent. | Check keys / Alpaca status. |
| `watchdog_failed` | Account D unreachable from risk. | Check D keys. |
| `order_event` `submit_timeout` | Broker did not answer the order POST and the lookup did not find it. | Automatic: next tick looks up again before resending. |
| `order_event` `broker_rejected` | Alpaca refused the order (buying power, halted symbol, bad price). Final. | Read `detail.body`; a rejected close is retried by the position manager after 60 s. |
| `order_event` `blocked` | Kill switch blocked an unsent order. Final. | Expected under PAUSE/FLATTEN (entries) and HALT (everything). |
| `risk_review` with `policy_mismatch` | Agent and risk run different policy files. | Restart both after a policy edit. |
| `risk_review` with `decision_stale` | Risk saw an entry more than 30 s after it was decided (risk was down or slow). | Check risk's uptime; nothing to do for that decision. |
| `x_budget_hit` / `x_poll_failed` | X daily cap reached / X API error. | Cap: polling resumes after New York midnight. Error 401: token; 429: wait. |
| `CacheMiss` from `nta replay` | An event in the window has no recorded model answer. | Narrow the window, or accept that those events were never processed live. |

## Dev reset (destroys all data)
Append-only means rows cannot be deleted. To wipe a dev database: `uv run alembic downgrade base && uv run nta db upgrade`, or `docker compose down -v`.
