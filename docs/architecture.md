# Architecture

How the pieces connect. The *why* behind each choice lives in [DECISIONS.md](DECISIONS.md); the requirements in [phase0-brief.md](phase0-brief.md) and [phase1-brief.md](phase1-brief.md).

## Processes and data flow

```
 SEC Atom feed ─► nta poll edgar ─┐        X API ─► nta poll x (optional, X_ENABLED)
                                  ▼                         │
 instruments (allowlist) ──► source_events ◄────────────────┘
                                  │
            ┌─────────────────────┼──────────────────────────────┐
            ▼                     ▼                              ▼
   nta quotes (account D)   nta agent (account D data + LLM key)   nta label / verdict (phase 0 gate)
   quote_schedule, quotes   llm_calls ─► signals ─► decisions
   (observe/marks/periodic)  (triage, extract,  (entry; close from
                              cluster)           the position manager)
                                                    │  outcome = trade
                                                    ▼
                                  nta risk (account T trading keys + RISK_SIGNING_KEY, no LLM)
                                  risk_reviews ─► orders (HMAC) ─► Alpaca paper ─► order_events, fills
                                  reconcile every 30 s ─► position_snapshots, incidents, kill_switch_events
                                  watchdog: account D must stay empty
                                                    │
                     nta evaluate (after the horizon): grades, postmortems, estimates, guardrail_proposals
                     nta replay: same pipeline, recorded data, cache-only LLM, schema replay_<run_id>
                     nta dashboard: read-only HTML over all of it
```

- **Long-running:** `nta poll edgar` (:8765), `nta quotes` (:8766), `nta agent` (:8767), `nta risk` (:8768), `nta poll x` (:8769, optional). Each writes `runs` / `run_stops`; they share nothing in memory and talk only through the database. `docker compose --profile app up -d --build` runs the first four (see RUNBOOK).
- **One-shot:** `nta allowlist`, `nta label [--review]`, `nta verdict`, `nta evaluate`, `nta replay`, `nta kill`, `nta db`.
- **Viewer:** `nta dashboard` (:8780).

### Credential boundary

| Process | Account D (`ALPACA_KEY_ID`) | Account T (`ALPACA_TRADING_*`) | `RISK_SIGNING_KEY` | LLM key |
|---|---|---|---|---|
| poll edgar | – | – | – | – |
| quotes | data | – | – | – |
| agent, evaluate | data | refuses to start | refuses to start | yes |
| risk | watchdog reads only | trading + its own quotes | yes | refuses to start |

## Life of a filing (phase 1)

1. Poller stores the filing (`first_observed_at` stamped on the feed response).
2. Agent (1 s tick): triage (Haiku) → extraction (Sonnet) if material, every call cached in `llm_calls` by request hash → cluster (MinHash, citations, 72 h) → `signals` row → fresh quote from account D stored as quote kind `decision` → pure checks as of that quote → exactly one `decisions` row (trade / wait / abstain, reasons, plan, version tuple, content hash).
3. Risk (0.5 s tick): re-checks the trade with its own account T quote and real positions → `risk_reviews` row → signed `orders` row → executor: verify HMAC, look up by `client_order_id`, submit once → sync: `order_events`, `fills`.
4. Agent position manager (5 s): stop / take-profit / expiry on a stored `manage` quote → close decision → risk → sell.
5. Risk reconcile (30 s): broker vs log; mismatch → incident + PAUSE.
6. After the horizon, `nta evaluate`: frozen packet → blind LLM grade → metrics → quadrant; estimate ledger for some abstentions.

## Data model

Every table is **append-only** (trigger raises on UPDATE, DELETE, TRUNCATE), has a ULID `id` and `created_at`. State changes are new rows; readers pick the row that wins.

| Table | Written by | Meaning / invariant |
|---|---|---|
| `instruments` | allowlist | One row per membership interval; membership replayed per FIGI in id order. |
| `openfigi_responses` | allowlist | Cache of OpenFIGI answers. |
| `source_events` | pollers | One row per accession (EDGAR) or post id (X). Two clocks. |
| `quote_schedule`, `quotes` | recorder, agent | Phase 0 observe/marks/periodic; phase 1 adds kinds `decision` and `manage` (agent). |
| `labels` | label tool | Human labels; latest wins. |
| `runs`, `run_stops` | every long-running process | No stop row = crash. |
| `llm_calls` | agent, evaluate | Request, response, validity, latency, cost; unique request hash = the cache. |
| `signals` | agent | One per source event: status, extraction, cluster (`cluster_id` = first member's source event), flags. |
| `decisions` | agent (risk for flatten) | One entry per signal, closes by parent; no free text; `content_hash`. |
| `risk_reviews` | risk | One per trade decision: approve / reject, risk's quote and checks. |
| `orders`, `order_events`, `fills` | risk | Signed orders; every executor step; one fill per increase of cumulative filled qty. |
| `position_snapshots` | risk | Each reconcile (account T) and watchdog (account D) cycle. |
| `kill_switch_events`, `incidents` | human (`nta kill`), risk | Latest kill row is in force; incidents open until a human resume. |
| `grades`, `postmortems`, `estimates`, `guardrail_proposals` | evaluate, label --review | Grades by grader (latest wins); metrics and quadrant; hypothetical trades; proposals only. |
| `policy_versions` | agent, risk | Text + hash of the policy seen at startup; risk's rows are signed. |
| `x_polls` | poll x | Spend and since_id per request. |

Schema lives in `nta/schema.py`; triggers are installed in each migration (`migrations/versions/0001..0003`).

## Modules

Pure functions take data and return data; I/O sits at the edge of each module.

| Module | Pure core | I/O edge |
|---|---|---|
| `allowlist`, `edgar`, `marketcal`, `alpaca`, `quotes`, `label`, `verdict`, `demo` | (phase 0, unchanged) | |
| `policy` | `parse_policy`, `sign`, `verify` | `load_policy`, `record_policy_version` |
| `cluster` | `normalize_headline`, `minhash`, `assign`, `cited_accessions`, `injection_suspected` | `load_members` |
| `extract` | `build_request`, `request_hash`, `validate`, `cost_usd`, `user_turn` | `LlmGateway` (cache, budget, replay), `AnthropicBackend` |
| `decide` | checks, `size_and_plan`, `decide_entry`, `close_trigger`, `DecisionContent.content_hash`, `row_payload` | point-in-time loaders (`load_exposure`, `load_kill_state`, ...) |
| `agent` | – | `Pipeline` (signal + entry decision), `PositionManager`, `AgentProcess`, `LiveQuotes` |
| `broker` | `client_order_id`, `sign_order`, `verify_order`, `parse_order` | `AlpacaPaperBroker` |
| `risk` | – | `RiskService` (review, execute, sync, reconcile, watchdog, flatten/halt) |
| `simulate` | `simulate_long`, `net_bps` | – |
| `evaluate` | `quality`, `cohen_kappa`, `grade_request`, `packet_hash` | `Evaluator`, `build_packet`, `propose_guardrails`, `AlpacaBenchmark` |
| `replay` | `compare`, `format_paths` | `run_replay`, `RecordedQuotes`, `SimBook`, schema helpers |
| `xsource`, `xpoll` | registry, `cashtags`, `map_instrument`, `quarantined` | `XPoller` |
| `dashboard`, `dashboard_loop` | `render_*` | `Dashboard` server |

Shared infrastructure: `http.send_with_retry`, `ratelimit`, `clock` (the only wall-clock reader), `shutdown.StopFlag`, `health.HealthServer` (`unhealthy` flag), `runs`, `logs.log`, `config` (env only, credential guards), `ids.new_ulid`.
