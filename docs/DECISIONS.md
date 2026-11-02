# Decisions

Choices the brief left open. Newest at the bottom of each section.

## Tooling
- **Postgres port 55432** on 127.0.0.1 (5432/5433 were taken on the dev machine).
- **Driver: psycopg 3** (`psycopg[binary]`). A DB driver is required by SQLAlchemy; not a framework.
- **ULIDs generated in-process** (`nta/ids.py`, stdlib) instead of adding a ULID library. **Monotonic within a process**: IDs created in the same millisecond increment the random part, so id order = insertion order (allowlist replay depends on it; found via a flaky test).
- **Tests use a separate `nta_test` database**, dropped and rebuilt with `alembic upgrade head` once per pytest session, so the migration is tested too. Each test runs in a rolled-back transaction (rollback is not DELETE, so append-only still holds).

## Schema
- **Append-only trigger also blocks TRUNCATE** (statement-level), not just UPDATE/DELETE. Schema changes (DROP via Alembic downgrade) are still possible; that is deliberate, for dev resets.
- **runs has no `stopped_at` column; stops go to `run_stops(run_id, stopped_at, reason)`.** An append-only `runs` table cannot fill in `stopped_at` later. A crash leaves a run with no stop row, which is itself the crash signal. Added `runs.process` (`poll_edgar` | `quotes`) because both long-running processes record runs.
- **Allowlist versioning: rows are replayed in insertion order per FIGI.** A row without `removed_on` opens a membership interval; removing a ticker inserts a row with the same FIGI and `added_on` plus `removed_on`, which closes the newest open interval with that `added_on`. Active on day D iff `added_on <= D < removed_on` (or still open). Replaying in order, rather than grouping by `added_on`, makes a same-day remove + re-add work (bug found and fixed while correcting XOM).
- **CIKs stored zero-padded to 10 digits** (DB check constraint), matching EDGAR URLs.
- **`source_events` extra columns:** `items` (JSONB list of 8-K item numbers from the filing index, needed by the labeling tool), `primary_doc_url`, `raw_truncated`, `run_id`. `raw` holds the primary document as fetched (HTML, untrusted, never rendered); `raw_hash` is sha256 of the stored (capped) text. `raw` is nullable so a filing whose document fetch fails is still stored with its `first_observed_at` (rows cannot be filled in later).
- **`quotes` gets `first_observed_at` and `source_event_id`.** Quotes come from outside, so they carry both clocks (`ts` = Alpaca's timestamp, `first_observed_at` = ours). `source_event_id` links observe/mark quotes to their filing so moves are unambiguous when two filings hit one ticker; a check constraint requires it for every kind except `periodic`.
- **New table `quote_schedule(source_event_id, kind, due_at)`** persists pending observe/mark quotes. A schedule row is done when a quote with the same `(source_event_id, kind)` exists, so no update is ever needed.
- **New table `openfigi_responses`** caches OpenFIGI mapping responses in the DB (with `first_observed_at`), instead of a file cache, so provenance is kept.
- **Relabeling appends a new `labels` row; the latest per filing wins.** `labels.sample_seed` records the seed of the sampling session.
- **Enums are VARCHAR + CHECK** (not native Postgres enums), so adding a value later is a plain constraint swap.

## Allowlist loader
- **`.env` values with spaces must be quoted** (`SEC_USER_AGENT="Jane Doe jane@example.com"`) because the runbook loads it with `set -a; source .env`. The app reads only real environment variables; it does not parse `.env` itself.
- **Seed CSV `data/allowlist_dev.csv` is development data**: 20 US large caps picked by Claude, CIKs written from memory. Not the research allowlist; replace it with your CSV.
- **FIGI choice: the US composite FIGI of the common stock.** Query is `{"idType":"TICKER","idValue":<ticker>,"exchCode":"US"}`; keep results with marketSector=Equity and securityType2=Common Stock; exactly one distinct composite FIGI is required. Rejection reasons: `figi_not_found`, `figi_not_common_stock` (ETFs, ADRs, etc.), `figi_ambiguous`, `openfigi_error`, plus CSV problems (`invalid_ticker`, `invalid_cik`, `missing_name`, `duplicate_in_csv`) and `duplicate_figi`.
- **Share-class tickers**: `BRK.B` / `BRK-B` are sent to OpenFIGI as `BRK/B`; the ticker is stored as written in the CSV.
- **OpenFIGI rate limit**: no key, 25 requests per 60 s (confirmed from the `ratelimit-policy` header), 10 jobs per request. Token bucket 5 + 20/min. No API-key support (not needed at this volume).
- **OpenFIGI cache never expires.** FIGIs are permanent; a ticker moving to a new security (see XOM below) needs a manual remove + re-load anyway. Definitive answers ("found" and "not found") are cached; errors are not.
- **CIK cross-check against SEC `company_tickers.json` runs only when `SEC_USER_AGENT` is set** (it is a sec.gov request). Mismatches are rejected as `cik_mismatch` / `ticker_not_in_sec_list`. Without the env var the loader logs `allowlist_cik_check_skipped` and trusts the CSV. A wrong CIK would silently drop that ticker's filings for the whole window, so run the load with the variable set.
- **Loading only adds.** Tickers in the DB but not in the CSV are left alone; use `nta allowlist remove TICKER [--as-of DATE]`. Re-loading an already-active ticker is a no-op (`already_present`). Default membership date is today (`--as-of` to override).
- **Rate limiter is a token bucket with worst case `capacity + rate * window` per window.** SEC: capacity 2, rate 8/s, so any 1-second window holds at most 10. Every HTTP attempt, including retries, takes a token, so backoff cannot exceed the budget. Backoff is exponential with full jitter (base 1 s, cap 60 s, 6 attempts), and honors `Retry-After` up to 300 s; retried statuses are 429, 500, 502, 503, 504 and transport errors.

### Data issue found and fixed (2026-11-01)
- OpenFIGI maps ticker XOM to **"EXXONMOBIL HOLDINGS CORP" (BBG023CY9MM1)**; the old Exxon Mobil Corp FIGI (BBG000GZQ728) now carries a placeholder ticker. The SEC CIK check confirmed the filer changed: **CIK 0002115436**, not 0000034088. Fixed in the seed CSV; the dev DB row was removed and re-added (both rows kept, append-only). The other 19 seed CIKs matched SEC.

## EDGAR poller
- **One feed, both forms.** `type=8-K` on `getcurrent` returns 8-K and 8-K/A (and variants like 8-K12B). Stored forms: exactly `8-K` and `8-K/A`; others are ignored and counted as `other_form`.
- **Co-registrants.** One accession can appear once per filer (e.g. a REIT and its operating partnership). Entries are grouped by accession; the filing is stored if any of its CIKs is allowlisted, under that CIK.
- **`first_observed_at` = our clock right after the feed response arrives**, one stamp per feed page. The row is inserted after the index and document are fetched (append-only rules out filling `raw` in later). If the process dies in that window (normally under a second), the filing is re-observed on restart with a later stamp. Anything already stored is never re-stamped: it is in the DB-loaded `seen` set, and inserts use `ON CONFLICT DO NOTHING` on (source_id, accession).
- **Fetch failures still store the observation**, with `raw`/`raw_hash` NULL and a `filing_fetch_failed` log line. Losing the timestamp is worse than missing the document text.
- **Primary document = first row of the index's "Document Format Files" table whose Type equals the form type** (fallback: any `8-K*` row, then the first .htm/.txt). iXBRL viewer links (`/ix?doc=...`) are unwrapped to the raw document. **Exhibits (e.g. EX-99.1 press releases) are not fetched**, as the brief specifies the primary document; the labeling tool may want them later.
- **`raw` = the document as fetched (usually HTML), NULs stripped, capped at 2 MB on a UTF-8 boundary.** `raw_hash` = sha256 of the stored text.
- **Items** = union of the Atom summary's items and the index page's Items block, in that order.
- **`duplicate_seen`** counts, per poll and cumulatively, feed accessions already processed (stored, skipped as non-allowlisted, or lost an insert race). Logged in every `poll_ok` line and reported by `/healthz`.
- **Gap backfill.** If page 1 shares no accession with what we have seen, its oldest entry is newer than the newest stored filing, and the page is full, the poller reads further pages (`&start=100`, ...), up to 5 extra. In steady state page 1 always overlaps the previous poll, so this only fires after downtime or bursts of more than 100 8-Ks in 5 s. On an empty DB there is no anchor, so no backfill.
- **Allowlist is point-in-time by New York date**, refreshed every 60 s.
- **Poll interval is start-to-start** (5 s): a slow poll shortens the following sleep instead of adding to it.
- **HTTP timeouts: 10 s read, 5 s connect.** A live feed request once hung for a full read timeout; shorter timeouts keep a stall from delaying polls long.
- **`/healthz` on 127.0.0.1:8765** (stdlib `http.server`, own thread): JSON with `seconds_since_last_success`, poll counters, `duplicate_seen_total` and `run_id`. 200 if the last successful poll is at most 60 s old, else 503 (also before the first poll).
- **Runs.** `runs.code_sha` = git HEAD, suffixed `-dirty` if the tree has uncommitted changes. `config_hash` = sha256 of the effective non-secret config, which is also logged in `run_started`. A clean stop or a caught exception writes `run_stops`; a hard kill leaves none.
- **Signals.** SIGTERM/SIGINT set a lock-free flag; the current poll finishes, then the process exits and writes `run_stops`. Repeated SIGTERMs are ignored (`uv run` forwards SIGTERM, so `pkill -f` delivers two); a second Ctrl-C more than 1 s after the first forces exit. An earlier version called `threading.Event.set()` from the handler and deadlocked in a live test; signal handlers must not take locks.
- **XML parsing uses stdlib ElementTree** (no extra dependency). Python 3.12's bundled expat (>= 2.4) limits entity expansion, and ElementTree never resolves external entities.
- **Token bucket tolerance.** A refill can land at 0.9999999999999 tokens, whose remaining wait is too small to move a clock. The bucket treats >= 1 - 1e-9 as a full token and never sleeps less than 1 µs. Found as an intermittent test hang; covered by a regression test.

## Quote recorder
- **Snapshots endpoint only:** `GET https://data.alpaca.markets/v2/stocks/snapshots?feed=iex` gives the latest quote (bid/ask) and the latest trade (last) in one call, for up to 100 symbols. The client refuses any base URL other than `https://data.alpaca.markets`. Alpaca has no separate "paper data" host (paper and live share the data API); paper/live only differ on trading hosts, which are never configured.
- **Rate:** free plan is 200 requests/min; bucket 10 + 170/min. Requests time out after 5 s, with 4 attempts.
- **Quote fields:** `bid`/`ask` from the latest quote (0 = no quote on that side, stored as NULL; a crossed quote is dropped), `last` = latest trade price, `ts` = the newer of the quote and trade timestamps, `first_observed_at` = our clock right after the response.
- **Observe quote:** during market hours it is due at `first_observed_at`; the recorder checks the DB every 0.5 s, so it normally lands well within 2 s. **Outside market hours the observe quote is taken at the next regular-session open** (the brief only defines the in-hours case).
- **Marks are anchored on the observe time, not the raw `first_observed_at`.** `mark_1h` = observe + 1 h, rolled to the next open if that is outside a session. `mark_1d` / `mark_5d` = the same New York time of day, 1 / 5 *trading sessions* later, clamped to 1 minute before an early close. Reason: most 8-Ks are filed after 16:00; with a literal "first_observed_at + 1 h, rolled forward" the observe and the 1-h mark would both land on the next open and the 1-h move would always be zero. "1d / 5d" read as trading days, which is the usual market meaning; a calendar reading would make "5d" mean 3 trading days for a Friday filing.
- **Consequence:** for after-hours filings, moves measured from the observe quote exclude the overnight gap (close -> next open). The verdict CSV includes the last periodic quote before the filing so the gap can be analysed later.
- **Persisted schedule:** `quote_schedule` rows (observe + 3 marks) are written when the recorder first sees a filing; a row is done when a quote with the same `(source_event_id, kind)` exists. Due rows are only executed while the market is open; anything that came due while the recorder was down is executed late at the next open, and the verdict ignores marks recorded more than 10 minutes after their due time.
- **Missing symbols** (no usable quote or trade in the snapshot) are retried every 5 s, so one bad symbol cannot burn the request budget.
- **Periodic quotes:** once per 5-minute wall-clock slot (09:30, 09:35, ...) during the regular session, for every instrument in the allowlist that day. After a restart the recorder resumes from the last stored slot, so a slot is not recorded twice.
- **Market calendar:** `pandas_market_calendars` XNYS (holidays and early closes). Scheduling is pure (`nta/marketcal.py`) and tested across a weekend, Thanksgiving, the following early close and the Nov 1 DST change.

## Labeling tool
- **Sample = the `target` filings with the smallest `sha256("<seed>:<accession>")`**, labeled in that order. A filing's rank does not depend on which other filings exist, so a session resumes in the same order and the queue is stable. Default seed 7, target 100; `--start/--end` restrict to a window (New York date of first observation). Sample after the window closes: while collection is running, new filings can still enter the top 100.
- **Population = every stored EDGAR filing in the window** (8-K and 8-K/A).
- **Preview starts at the first "Item X.XX" heading, not the top of the document.** The first ~60 lines of an 8-K are cover-page boilerplate (seen on a real P&G filing), so a literal "first 60 lines" would rarely show the substance. The tool prints how many cover lines were skipped and falls back to the top if there is no Item heading. HTML is converted to text (scripts, styles and hidden iXBRL headers dropped).
- **Terminal safety:** filing text has C0/C1 control characters (including ESC), zero-width characters and bidi overrides removed before printing.
- **Answers:** material y/n; direction only asked when material (non-material is stored as `unclear`); event_type always asked (number 1-7 or name); note optional. `s` skips (asked again next session), `q` or Ctrl-D quits. Item numbers are shown with their official titles.
- **Labels are rows, never edits.** Relabeling (not exposed in the CLI) would append a row; the verdict uses the latest label per filing.

## Verdict
- **Window** = New York dates of `first_observed_at`, inclusive; default first to last stored filing. `--start/--end` override.
- **Price = mid only when the quote is tight (spread <= 20 bps of the mid); otherwise the last trade** (decided with the user on 2026-11-02, replacing the brief's "mid, or last if no bid/ask"). Reason: the free plan's IEX quotes show only IEX's own book. A live snapshot on 2026-11-02 had JPM, MA and PG with 500-600 bps spreads and mids 2.5-2.9% away from the last trade, which would swamp the 30 bps threshold. A wide quote with no trade has no price (the move counts as missing). The rule runs at verdict time on the stored bid/ask/last, so it can be changed later without re-collecting. The verdict prints how many prices came from each source, and the CSV has a `*_source` column per price.
- **Move** = mark price / observe price - 1, in bps; stats use the absolute value, the CSV keeps the sign. A quote recorded more than 10 minutes after its due time is not used (counted as "excluded"); a mark with no quote is "missing". Both counts are printed.
- **Percentiles** use linear interpolation between closest ranks (numpy's default); IQR = p25-p75.
- **Material filings per month** = filings observed in the window x material share of the labeled sample x 30.4375 / window calendar days. Interval = 95% Wilson interval of the share, scaled the same way (no finite-population correction, so it is slightly conservative when the sample is a large part of the window).
- **The disproof condition uses point estimates**, verbatim: STOP if the median |1d move| of labeled-material filings < 30 bps, or material filings per month < 60. If either quantity cannot be computed (no labels, no usable 1-day moves), the verdict is STOP with the reason printed: the idea has not shown it clears the bar.
- **"Observe quote > 5 s after first_observed_at" is reported over filings observed during market hours.** Filings first seen outside hours have their observe quote at the next open by design, so they would all count as late; they are reported as a separate count.
- **Splits** by event_type and direction are over material filings; "all filings" and "labeled" rows are added for context.
- **CSV** goes to `exports/verdict_<start>_<end>.csv` by default (git-ignored); prices with 4 decimals; includes `pre_filing_price` (last periodic quote before the filing) so the overnight gap can be studied later.
- **Fixture first:** `tests/fixtures/verdict/fixture.json` (10 filings) with the expected answers worked out by hand in `tests/test_verdict.py`'s docstring. The code matched all of them on its first run.

## Dashboard
- **Added at the user's request (2026-11-01), overriding the brief's "no web UI" rule.** Kept inside the brief's other constraints: no new dependencies (stdlib `http.server`, server-rendered HTML), read-only (GET only, no forms, no writes), bound to 127.0.0.1.
- **No JavaScript.** Auto-refresh every 15 s via `<meta http-equiv="refresh">`. CSP `default-src 'none'; style-src 'unsafe-inline'` blocks any script even if escaping were to fail.
- **Untrusted content:** every DB value is HTML-escaped; filing text is shown only as escaped plain text in `<pre>`; only `https://www.sec.gov/...` URLs become links.
- **Process status** comes from probing the poller's and recorder's `/healthz` (0.4 s timeout); runs with no `run_stops` row that are not the live process are flagged "no clean stop".
- **Verdict panel** reuses `nta.verdict.compute` over the default window, marked *provisional* until there are 30 trading days and 100 labels.
- **Demo mode (`--demo`)** drops and rebuilds a separate database, `nta_demo`, with a synthetic 30-trading-day dataset (deterministic seed) and shows a "Demo data" banner. It refuses to run if `DATABASE_URL` itself points at `nta_demo`. Synthetic filings link nowhere (`demo.invalid` URLs).
- **Bug found while checking against the real dev DB:** the per-day chart dropped weekends, so a database whose only filing was seen on a Sunday crashed the page. Weekend days with filings are now kept (regression test added).

## Throughput (2026-11-02)
- **Alpaca snapshot chunks (100 symbols each) are fetched concurrently**, up to 5 at a time, so a 500-ticker periodic quote is one round trip instead of five sequential ones and cannot delay observe quotes. The shared token bucket still caps the rate; a failed chunk fails the whole call, which is retried on the next tick.
- **OpenFIGI is not parallelized:** its limit is on requests per minute, so concurrent requests would only produce 429s. Instead, an **optional `OPENFIGI_API_KEY`** (free) raises the limits from 25 req/min of 10 tickers to 250 req/min (confirmed from the `ratelimit-policy: 250;w=60` header on 2026-11-02) of 100 tickers. Bucket 5 + 20 per 6 s, so at most 205 requests in any 60 s window. Without the key nothing changes. Answers are cached, so this only matters for the first load of a large allowlist.

# Phase 1 (decision loop on a paper account)

Spec: [phase1-brief.md](phase1-brief.md). Same rules as above: newest at the bottom of each section.

## Phase 1 schema (migration 0002)
- **Tables beyond the brief's list:** `risk_reviews` (exactly one per trade decision: nta risk's approve/reject with its own quote, so a risk rejection leaves a row and no order), `grades` (rubric scores, `grader` = llm | human, latest per grader wins), `estimates` (the estimate ledger, kept apart from real P&L).
- **Quote kinds `decision` and `manage`** added to `quotes.kind`. Every quote the agent acts on (entry decision, position-manager check) is stored like any outside data, so replay can use exactly what live saw. The verdict reads only observe/mark kinds of EDGAR events, so it is unaffected.
- **`decisions` holds no free text** (reasons are codes; quote, exposure and plan are numbers). nta risk may therefore read any decisions column. Free text (headline, thesis, contrary evidence) lives in `signals.extraction` / `signals.headline` and `llm_calls`.
- **One entry decision per signal** is a partial unique index (`signal_id WHERE kind = 'entry'`). Close decisions point at their entry through `parent_decision_id`; a failed close may be followed by another close decision.
- **A trade decision must be complete** (side, qty > 0, plan) by check constraint, so "the row is complete before anything reads it" holds at the DB level.
- **Incidents have no resolved flag.** An incident is open until the next human `resume` (a `RUN` row in `kill_switch_events` after it). Append-only rules out flipping a flag.
- **`fills` are one row per increase of an order's cumulative filled quantity**, unique on `(order_id, cum_qty)`, so re-polling an order can never double-count a fill.
- **Nullable JSONB columns store SQL NULL for Python `None`** (`JSONB(none_as_null=True)`); plain JSONB writes JSON `null`, which silently passed an `IS NOT NULL` check in the first test run.
