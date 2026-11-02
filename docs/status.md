# Status

As of 2026-11-02. Update this file when a component lands, something is validated live, or an open item closes. Git history has the per-commit detail.

## Built

Phase 0 (unchanged; `nta verdict` output is tested to be identical with phase 1 rows present) and every phase 1 component, each with tests (351 passing, 1 opt-in regeneration test skipped):

| Component | Command / module | Phase 1 brief item |
|---|---|---|
| Schema (migrations 0002, 0003) | `nta db upgrade` | 1 |
| Policy file, hashing, risk-only signing, `quote_source` | `policy/policy.yaml`, `nta/policy.py` | 2 |
| Clustering | `nta/cluster.py` | 3 |
| LLM triage + extraction, cache, budget | `nta/extract.py`, `prompts/` | 4 |
| Decision engine + agent process | `nta agent` | 5 |
| Risk service, executor, kill switch | `nta risk`, `nta kill` | 6 |
| Reconciler + account D watchdog | inside `nta risk` | 7 |
| Position manager | inside `nta agent` | 8 |
| Evaluation, review mode, kappa | `nta evaluate`, `nta label --review` | 9 |
| Replay (golden log, `--policy`) | `nta replay` | 10 |
| X ingestion (off by default) | `nta poll x`, `sources.yaml` | 11 |
| Dashboard pages | `nta dashboard` → `/loop` and others | 12 |
| Containers | `Dockerfile`, `docker compose --profile app` | DoD |

An independent review of the money path found six issues, all fixed with regression tests (DECISIONS, "Fixes after an independent review"). Every test the phase 1 brief lists exists: credential refusal (both processes, CLI and container), injected HMAC, timeout after accept, restart at both crash points, reconcile mismatch → PAUSE with the stop-loss still firing, prompt injection (obeying model and schema-valid output), golden replay + cache miss, verdict unchanged, LLM and X budget caps.

## Validated live

- Phase 0 items as before (SEC, OpenFIGI, Alpaca IEX snapshots, dashboard).
- 2026-11-02: image builds; `migrate` service upgraded the dev database to 0003; agent and risk containers refuse missing or wrong credentials as designed.

**Not yet exercised live** (needs credentials this machine does not have): a real Anthropic call, a second Alpaca paper account (T), and therefore the definition of done itself: a real 8-K → decision within 60 s → paper order and fill on account T within 10 s → graded after its horizon → replay of that day identical. Also untested live: the Alpaca trading endpoints' exact responses (the client follows Alpaca's documented API; only the fake broker has run), the X API.

## Open

1. **Credentials for the live run:** `ANTHROPIC_API_KEY`, a second paper account's `ALPACA_TRADING_KEY_ID` / `ALPACA_TRADING_SECRET` (account T must be flat and used by nothing else), `RISK_SIGNING_KEY` (`openssl rand -hex 32`). Then: `docker compose --profile app up -d --build`, watch `/healthz` on 8767/8768 and the dashboard's `/loop`, and run `nta replay` for the day.
2. **Phase 0 items still open** (allowlist size, dev DB reset, 30-day uptime, exhibit 99.1). Fetching EX-99.1 matters more now: most 8-K primary documents point to the press release, so extraction often sees little.
3. **`quote_source`:** decide whether to pay for SIP (USD 99/month); with IEX every decision is `spread_unverified` and the 20 bps check is skipped.
4. **Replay limits:** only the first close per position is replayed; replay uses the current `sources.yaml`.
5. **Reconsider guesses with data:** post latency budget (10 min) and horizon (1 day), `min_p_up` 0.6, the already-priced 5-minute floor, cost model numbers.
