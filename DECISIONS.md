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
