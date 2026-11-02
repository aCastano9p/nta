# Architecture

How the pieces connect. The *why* behind each choice lives in [DECISIONS.md](DECISIONS.md); the requirement in [phase0-brief.md](phase0-brief.md).

## Processes and data flow

```
            SEC getcurrent Atom feed (every 5 s)
                         │
   nta poll edgar ───────┤ index page + primary doc (shared SEC bucket, ≤10 req/s)
                         ▼
   instruments ──► source_events ──────────────┐
   (allowlist,           │                     │
    nta allowlist)       ▼                     ▼
               nta quotes: quote_schedule ──► quotes ◄── periodic, every 5 min
                         (observe, 1h, 1d, 5d)    (Alpaca IEX snapshots)
                                               │
   nta label ──► labels ───────────────────────┤
                                               ▼
                  nta verdict (report + CSV)   nta dashboard (read-only HTML)
```

- **Long-running:** `nta poll edgar` (healthz :8765) and `nta quotes` (healthz :8766). Each writes a `runs` row on start and a `run_stops` row on a clean stop; they share nothing in memory and talk only through the database.
- **One-shot:** `nta allowlist load|remove|show`, `nta label`, `nta verdict`, `nta db upgrade|check`.
- **Viewer:** `nta dashboard` (:8780), read-only; `--demo` serves the separate `nta_demo` database.

## Data model

Every table is **append-only** (trigger raises on UPDATE, DELETE, TRUNCATE), has a ULID `id` and `created_at`. State changes are new rows; readers pick the row that wins.

| Table | Written by | Meaning / invariant |
|---|---|---|
| `instruments` | allowlist | One row per membership interval. Membership on a date = replay rows per FIGI in id order (`allowlist.active_on`). Removal = new row with `removed_on`. |
| `openfigi_responses` | allowlist | Cache of definitive OpenFIGI answers; latest per request key wins. |
| `source_events` | poller | One row per accession (unique `source_id, external_id`). Two clocks: `published_at` (Atom `<updated>`), `first_observed_at` (ours, never re-stamped). `raw` may be NULL if the document fetch failed. |
| `quote_schedule` | recorder | Due times for observe + 3 marks per filing. A row is **done** when a `quotes` row with the same `(source_event_id, kind)` exists. |
| `quotes` | recorder | `ts` = Alpaca's time, `first_observed_at` = ours. `source_event_id` set for observe/marks, NULL for periodic (check constraint). bid/ask/last stored raw; pricing happens at read time. |
| `labels` | label tool | Human labels; latest per filing wins. |
| `runs`, `run_stops` | both processes | A run with no stop row ended by crash or kill. |

Schema lives in `nta/schema.py`; the trigger is installed per table inside each migration (`migrations/versions/`).

## Modules

Pure functions take data and return data; I/O sits in a thin class or function at the edge of each module.

| Module | Pure core | I/O edge |
|---|---|---|
| `allowlist` | `parse_csv`, `choose_figi`, `check_cik`, `active_on` | `OpenFigiClient`, `fetch_sec_tickers`, `load`, `remove` |
| `edgar` | `parse_feed`, `group_filings`, `classify`, `parse_index`, `cap_raw` | `EdgarPoller` (`poll_once`, `run`, `health`) |
| `marketcal` | `schedule_quotes`, `roll_forward`, `is_open`, `periodic_slot` | `nyse_sessions` (pandas_market_calendars, cached) |
| `alpaca` | `parse_snapshots` | `AlpacaData.snapshots` (100 symbols/request, chunks in parallel) |
| `quotes` | — | `QuoteRecorder` (`tick` = schedule → execute due → periodic) |
| `label` | `html_to_lines`, `preview_lines`, `sanitize`, `choose_sample` | `run_session` |
| `verdict` | `price_and_source`, `compute`, `format_report`, `csv_rows` | `load_records`, `run` |
| `dashboard` | `render_*` | `gather`, `Dashboard` server |
| `demo` | `synthetic_spec` | `load_spec`, `recreate_demo_database` |

Shared infrastructure: `http.send_with_retry` (every attempt takes a token; jittered backoff; honors `Retry-After`), `ratelimit` (thread-safe `TokenBucket` + per-service presets), `clock` (`Clock` protocol; tests use a fake), `shutdown.StopFlag` (lock-free, signal-safe), `health.HealthServer`, `runs`, `logs.log` (one JSON line per event), `config` (env only), `ids.new_ulid` (monotonic in-process).

## Life of a filing

1. Poller fetches the feed; `first_observed_at` = clock right after the response. Entries are grouped by accession (co-registrants appear once per filer).
2. New accession with an allowlisted CIK (allowlist as of the New York date) → fetch index → primary doc → one `INSERT ... ON CONFLICT DO NOTHING`. Seen accessions (from the DB at startup, then in memory) are counted as `duplicate_seen`.
3. Recorder (0.5 s tick) finds filings without schedule rows → writes observe/1h/1d/5d due times (`marketcal.schedule_quotes`).
4. While the market is open, due rows without a quote are fetched from Alpaca and stored.
5. After the window: `nta label` labels a seeded sample; `nta verdict` loads everything, prices each quote (mid if spread ≤ 20 bps, else last), drops quotes recorded > 10 min after due, and applies the disproof condition.
