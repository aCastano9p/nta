# Decisions

Choices the brief left open. Newest at the bottom of each section.

## Tooling
- **Postgres port 55432** on 127.0.0.1 (5432/5433 were taken on the dev machine).
- **Driver: psycopg 3** (`psycopg[binary]`). A DB driver is required by SQLAlchemy; not a framework.
- **ULIDs generated in-process** (`nta/ids.py`, ~15 lines, stdlib) instead of adding a ULID library.
- **Tests use a separate `nta_test` database**, dropped and rebuilt with `alembic upgrade head` once per pytest session, so the migration is tested too. Each test runs in a rolled-back transaction (rollback is not DELETE, so append-only still holds).

## Schema
- **Append-only trigger also blocks TRUNCATE** (statement-level), not just UPDATE/DELETE. Schema changes (DROP via Alembic downgrade) are still possible; that is deliberate, for dev resets.
- **runs has no `stopped_at` column; stops go to `run_stops(run_id, stopped_at, reason)`.** An append-only `runs` table cannot fill in `stopped_at` later. A crash leaves a run with no stop row, which is itself the crash signal. Added `runs.process` (`poll_edgar` | `quotes`) because both long-running processes record runs.
- **Allowlist versioning: one `instruments` row per membership interval; the latest row per FIGI is current.** Removing a ticker = inserting a new row with the same FIGI and `removed_on` set. Membership on day D = latest row per FIGI created by then, with `added_on <= D` and (`removed_on` null or `> D`).
- **CIKs stored zero-padded to 10 digits** (DB check constraint), matching EDGAR URLs.
- **`source_events` extra columns:** `items` (JSONB list of 8-K item numbers from the filing index, needed by the labeling tool), `primary_doc_url`, `raw_truncated`, `run_id`. `raw` holds the primary document as fetched (HTML, untrusted, never rendered); `raw_hash` is sha256 of the stored (capped) text. `raw` is nullable so a filing whose document fetch fails is still stored with its `first_observed_at` (rows cannot be filled in later).
- **`quotes` gets `first_observed_at` and `source_event_id`.** Quotes come from outside, so they carry both clocks (`ts` = Alpaca's timestamp, `first_observed_at` = ours). `source_event_id` links observe/mark quotes to their filing so moves are unambiguous when two filings hit one ticker; a check constraint requires it for every kind except `periodic`.
- **New table `quote_schedule(source_event_id, kind, due_at)`** persists pending observe/mark quotes. A schedule row is done when a quote with the same `(source_event_id, kind)` exists, so no update is ever needed.
- **New table `openfigi_responses`** caches OpenFIGI mapping responses in the DB (with `first_observed_at`), instead of a file cache, so provenance is kept.
- **Relabeling appends a new `labels` row; the latest per filing wins.** `labels.sample_seed` records the seed of the sampling session.
- **Enums are VARCHAR + CHECK** (not native Postgres enums), so adding a value later is a plain constraint swap.

## Allowlist loader
- **Seed CSV `data/allowlist_dev.csv` is development data**: 20 US large caps picked by Claude, CIKs written from memory. Not the research allowlist; replace it with your CSV.
- **FIGI choice: the US composite FIGI of the common stock.** Query is `{"idType":"TICKER","idValue":<ticker>,"exchCode":"US"}`; keep results with marketSector=Equity and securityType2=Common Stock; exactly one distinct composite FIGI is required. Rejection reasons: `figi_not_found`, `figi_not_common_stock` (ETFs, ADRs, etc.), `figi_ambiguous`, `openfigi_error`, plus CSV problems (`invalid_ticker`, `invalid_cik`, `missing_name`, `duplicate_in_csv`) and `duplicate_figi`.
- **Share-class tickers**: `BRK.B` / `BRK-B` are sent to OpenFIGI as `BRK/B`; the ticker is stored as written in the CSV.
- **OpenFIGI rate limit**: no key, 25 requests per 60 s (confirmed from the `ratelimit-policy` header), 10 jobs per request. Token bucket 5 + 20/min. No API-key support (not needed at this volume).
- **OpenFIGI cache never expires.** FIGIs are permanent; a ticker moving to a new security (see XOM below) needs a manual remove + re-load anyway. Definitive answers ("found" and "not found") are cached; errors are not.
- **CIK cross-check against SEC `company_tickers.json` runs only when `SEC_USER_AGENT` is set** (it is a sec.gov request). Mismatches are rejected as `cik_mismatch` / `ticker_not_in_sec_list`. Without the env var the loader logs `allowlist_cik_check_skipped` and trusts the CSV. A wrong CIK would silently drop that ticker's filings for the whole window, so run the load with the variable set.
- **Loading only adds.** Tickers in the DB but not in the CSV are left alone; use `nta allowlist remove TICKER [--as-of DATE]`. Re-loading an already-active ticker is a no-op (`already_present`). Default membership date is today (`--as-of` to override).
- **Rate limiter is a token bucket with worst case `capacity + rate * window` per window.** SEC: capacity 2, rate 8/s, so any 1-second window holds at most 10. Every HTTP attempt, including retries, takes a token, so backoff cannot exceed the budget. Backoff is exponential with full jitter (base 1 s, cap 60 s, 6 attempts), and honors `Retry-After` up to 300 s; retried statuses are 429, 500, 502, 503, 504 and transport errors.

### Known data issue (2026-11-01)
- OpenFIGI maps ticker XOM to **"EXXONMOBIL HOLDINGS CORP" (BBG023CY9MM1)**; the old Exxon Mobil Corp FIGI (BBG000GZQ728) now carries a placeholder ticker. Exxon's 8-K filer CIK has probably changed too, so the dev seed CIK 0000034088 may be stale. Set `SEC_USER_AGENT` and re-run the load to verify; if it reports `cik_mismatch`, run `nta allowlist remove XOM`, fix the CSV, and load again.
