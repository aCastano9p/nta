# Status

As of 2026-11-02. Update this file when a component lands, something is validated live, or an open item closes. Git history has the per-commit detail.

## Built

Every brief component plus the dashboard, each with tests (150 passing):

| Component | Command | Brief item |
|---|---|---|
| Schema + append-only trigger | `nta db upgrade` | 1 |
| Allowlist loader (OpenFIGI, SEC CIK check) | `nta allowlist load\|remove\|show` | 2 |
| EDGAR poller | `nta poll edgar` | 3 |
| Quote recorder | `nta quotes` | 4 |
| Labeling tool | `nta label` | 5 |
| Verdict + CSV | `nta verdict` | 6 |
| Dashboard (added at owner's request) | `nta dashboard [--demo]` | — |

All six tests the brief lists exist and pass (trigger, token bucket burst, re-ingest, restart re-stamp, weekend/holiday marks, verdict fixture).

## Validated live

- **SEC:** feed + index + document fetch; stored a real P&G 8-K; re-polls counted duplicates; `/healthz` 200; SIGTERM stop recorded.
- **OpenFIGI:** 20 dev tickers resolved; keyed limit confirmed (250 req / 60 s).
- **SEC CIK check:** caught XOM's new filer CIK (0002115436).
- **Alpaca (paper-account keys, IEX):** 20/20 snapshots; quote recorder observe latency 0.59 s on a TEST filing; periodic quotes at two 5-minute slots; clean SIGTERM stop.
- **Dashboard:** real dev DB and demo DB.

Not yet exercised live: both processes together over days, `mark_1d`/`mark_5d` firing on schedule, labeling a real sample, a verdict on real data.

## Open before the 30-day window

1. **Allowlist size:** the 20-ticker dev allowlist cannot reach 60 material 8-Ks/month (rough estimate: ~15–20 8-Ks/month in total), so the verdict would be STOP by construction. Waiting on the owner's large CSV (`ticker,name,cik`).
2. **Reset the dev database** (`uv run alembic downgrade base && uv run nta db upgrade`, or `docker compose down -v`). It holds smoke-test rows: the P&G filing, `TEST-LIVE-OBSERVE-1`, its schedule and quotes, and a crashed run.
3. **Keep the processes alive for 30 days** (always-on machine or supervisor such as launchd). Deferred by the owner until validation is done.
4. **Decide:** fetch exhibit 99.1 for labeling (helps only filings collected after the change); a stale-last-trade cutoff at verdict time (can be added later; timestamps are stored).

## Credentials in `.env` (all checked live)

`SEC_USER_AGENT`, `ALPACA_KEY_ID`, `ALPACA_SECRET`, `OPENFIGI_API_KEY`.

## After phase 0

The brief names later phases only by what it defers: a replay harness over this data, a message queue, an LLM client, Grafana, a trading adapter. All are gated on `VERDICT: CONTINUE`. No later-phase brief exists yet.
