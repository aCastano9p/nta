# Phase 0 brief (spec)

The owner's brief, verbatim. It is the requirement; where the build departs from it, the owner agreed and the departure is recorded in [DECISIONS.md](DECISIONS.md) (notably: the localhost dashboard, mark timing, and the spread-aware price rule).

---

You are building phase 0 of a personal news-trading research system called NTA. Read this whole brief before writing code. Work in small commits. When something in the brief is ambiguous, pick the simplest option, write the choice in DECISIONS.md, and continue.

Goal of phase 0

Measure, over 30 trading days, whether material SEC 8-K filings by liquid US large caps are followed by price moves large enough to pay a retail round trip. Everything is logged point-in-time so the same data can later drive a replay harness. Output: a dataset plus one script that prints the verdict.

Disproof condition (fixed now, before any data): if the median absolute 1-day move after a human-labeled material 8-K is below 30 bps, or fewer than 60 labelable material filings occur per month in the allowlist, the news-trading idea stops here.

Constraints
Python 3.12, uv for env, pydantic v2, httpx, sqlalchemy + alembic, typer CLI, pytest. No frameworks beyond these without asking.
Storage: Postgres 16 via docker-compose.yml. Every table is append-only: no UPDATE, no DELETE in application code; enforce with a DB trigger that raises on both.
Every row that comes from outside carries two timestamps: published_at (the source's own) and first_observed_at (our clock at the moment we first saw it). Never overwrite first_observed_at.
SEC rules: max 10 requests/second to any sec.gov host, and a User-Agent header of the form "<name> <email>" read from env (SEC_USER_AGENT). Fail at startup if it is missing.
No trading, no broker order endpoints, no LLM calls in this phase. If you find yourself adding any of these, stop.
Secrets only from env; .env.example with placeholders; .env in .gitignore.
Treat all fetched content as untrusted data: store it, never execute or template it.
What to build
1. Schema (nta/schema.py, Alembic migration)

Tables, all with a ULID id and created_at:

instruments: ticker, figi, cik, name, aliases (JSONB), added_on, removed_on (nullable). The allowlist is versioned by date through these two columns.
source_events: source_id (edgar for now), external_id (accession number), form_type, cik, instrument_id (nullable), published_at, first_observed_at, ingested_at, raw (text of the filing index page or primary document, capped at 2 MB), raw_hash, url.
quotes: instrument_id, ts, bid, ask, last, source (alpaca_iex), kind (observe, mark_1h, mark_1d, mark_5d, periodic).
labels: source_event_id, labeler (human), material (bool), direction (up/down/unclear), event_type (free text from a fixed enum: leadership, guidance, m_and_a, legal, financing, product, other), labeled_at, note.
runs: one row per poller process start, with code_sha, config_hash, started_at, stopped_at.

Add a DB trigger on every table that raises on UPDATE and DELETE. Add a test that proves the trigger fires.

2. Allowlist loader (nta allowlist load <csv>)

Loads tickers from a CSV I will provide (columns: ticker, name, cik). Resolve FIGI via the OpenFIGI public mapping endpoint (free, needs no key for low volume; respect its rate limit, cache responses). Rows without a resolved FIGI are rejected with a reason, not stored. Seed CSV for development: 20 large caps of your choice, labeled as development data in DECISIONS.md.

3. EDGAR poller (nta poll edgar)
Poll https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&owner=include&count=100&output=atom every 5 seconds. Also accept 8-K/A.
For each new accession number whose CIK is in the allowlist: stamp first_observed_at immediately, then fetch the filing index and the primary document (respecting the 10 req/s budget via a token bucket shared across the process), store the text, hash it.
Idempotent: re-seeing an accession number is a no-op; log a duplicate_seen counter.
Backoff on HTTP 429/5xx with jitter; never exceed 10 req/s even while backing off.
Resumable: on start, record a runs row; on crash, the next start does not re-stamp first_observed_at for anything already stored.
Emit structured JSON logs (one line per event) and a /healthz HTTP endpoint that reports seconds since last successful poll.
Measure and log the gap between published_at (the Atom <updated> value) and first_observed_at for every filing; this is the poll latency metric.
4. Quote recorder (nta quotes)
Alpaca free data plan (IEX feed, 15-minute history restriction is acceptable here). Env: ALPACA_KEY_ID, ALPACA_SECRET. Paper-data base URL only; never configure a trading endpoint.
On every new source_events row during market hours, record a kind=observe quote for its instrument within 2 seconds.
Schedule mark_1h, mark_1d, mark_5d quotes relative to first_observed_at, using the next available regular-session time when the mark falls outside hours. Persist the schedule so a restart does not lose it.
Record a periodic quote for every allowlisted instrument every 5 minutes during market hours (this becomes the replay fallback later).
Market hours and holidays: use pandas_market_calendars or an equivalent; do not hardcode.
5. Labeling tool (nta label)

A minimal terminal loop: shows the filing's items and first 60 lines of the primary document, asks material? direction? event_type? note?, writes a labels row. Random order, seeded so a session can resume. Target: 100 filings sampled at random from the 30-day window, labeled by me.

6. Verdict script (nta verdict)

Prints, for the window: count of 8-K filings observed; count per month of human-labeled material filings (extrapolated from the sample with a stated interval); median and interquartile range of absolute moves at 1 h, 1 d, 5 d measured from the observe quote (mid, or last if no bid/ask); the same split by event_type and direction; poll latency p50/p95; share of filings whose observe quote arrived more than 5 s after first_observed_at. Ends with one line: VERDICT: CONTINUE or VERDICT: STOP, applying the disproof condition verbatim. No charts; a CSV export of the per-filing table.

Tests I expect
Append-only trigger fires on UPDATE and DELETE.
Token bucket never exceeds 10 requests in any 1-second window under a burst (simulate with a fake clock).
Re-ingesting the same Atom feed twice produces no new rows and increments duplicate_seen.
first_observed_at is never changed by a restart (fixture: store, restart, re-poll).
Mark scheduling rolls correctly across a weekend and a holiday.
Verdict script reproduces a known answer from a fixture dataset.
Repo layout
nta/            package (schema, edgar, quotes, label, verdict, cli)
migrations/     alembic
tests/
docker-compose.yml
DECISIONS.md    every choice you made that the brief left open
RUNBOOK.md      how to start, stop, check health, and what to do on each error you handle
Working rules
Before writing the poller, write the schema and its tests. Before the verdict script, write its fixture.
Keep functions pure where you can: parsing, scheduling and the verdict math take data in and return data out; I/O lives at the edges.
After each component, run the full test suite and show me the output. Then stop and summarize what you built and what you decided, in under ten lines.
Do not add a web UI, a message queue, an LLM client, Grafana, or a trading adapter. Those are later phases and depend on this one's verdict.
