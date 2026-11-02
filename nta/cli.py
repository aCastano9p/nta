"""`nta` command-line entry point. Subcommands are added per component."""

from datetime import date
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)
db_app = typer.Typer(no_args_is_help=True, help="Database maintenance.")
app.add_typer(db_app, name="db")
allowlist_app = typer.Typer(no_args_is_help=True, help="Manage the instrument allowlist.")
app.add_typer(allowlist_app, name="allowlist")
poll_app = typer.Typer(no_args_is_help=True, help="Long-running pollers.")
app.add_typer(poll_app, name="poll")

ROOT = Path(__file__).resolve().parent.parent


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply all migrations (uses DATABASE_URL)."""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


@db_app.command("check")
def db_check() -> None:
    """Exit non-zero if the database is unreachable or not at the latest migration."""
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from nta.db import make_engine

    head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    with make_engine().connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    typer.echo(f"db revision={current} head={head}")
    if current != head:
        raise typer.Exit(1)


def _as_of(value: str | None) -> date:
    return date.fromisoformat(value) if value else date.today()


@allowlist_app.command("load")
def allowlist_load(
    csv_path: Path = typer.Argument(..., exists=True, dir_okay=False, help="CSV: ticker,name,cik"),
    as_of: str | None = typer.Option(None, help="Membership start date (YYYY-MM-DD); default today."),
) -> None:
    """Add tickers from a CSV. Rows whose FIGI cannot be resolved are rejected, not stored."""
    import httpx

    from nta import allowlist
    from nta.clock import SystemClock
    from nta.config import optional_sec_user_agent
    from nta.db import make_engine, make_session_factory
    from nta.logs import log
    from nta.ratelimit import openfigi_bucket, sec_bucket

    clock = SystemClock()
    sf = make_session_factory(make_engine())
    ua = optional_sec_user_agent()
    with httpx.Client(timeout=30.0) as http:
        sec_ciks = None
        if ua:
            sec_ciks = allowlist.fetch_sec_tickers(http, sec_bucket(clock), clock, ua)
        else:
            log("allowlist_cik_check_skipped", reason="SEC_USER_AGENT not set")
        figi = allowlist.OpenFigiClient(http, openfigi_bucket(clock), clock, sf)
        report = allowlist.load(sf, csv_path.read_text(), figi, _as_of(as_of), sec_ciks)
    typer.echo(f"added={len(report.added)} already_present={len(report.already_present)} "
               f"rejected={len(report.rejected)} openfigi_requests={figi.http_requests}")
    for r in report.rejected:
        typer.echo(f"  rejected line {r.line} {r.ticker}: {r.reason} {r.detail}".rstrip())


@allowlist_app.command("remove")
def allowlist_remove(
    ticker: str,
    as_of: str | None = typer.Option(None, help="Removal date (YYYY-MM-DD); default today."),
) -> None:
    """Remove a ticker from the allowlist from the given date on (appends a row)."""
    from nta import allowlist
    from nta.db import make_engine, make_session_factory

    if not allowlist.remove(make_session_factory(make_engine()), ticker, _as_of(as_of)):
        typer.echo(f"{ticker} is not in the allowlist")
        raise typer.Exit(1)
    typer.echo(f"removed {ticker.upper()}")


@allowlist_app.command("show")
def allowlist_show(
    as_of: str | None = typer.Option(None, help="Date (YYYY-MM-DD); default today."),
) -> None:
    """Print the allowlist as of a date."""
    from nta import allowlist
    from nta.db import make_engine, make_session_factory

    with make_session_factory(make_engine())() as s:
        rows = allowlist.current(s, _as_of(as_of))
    for i in rows:
        typer.echo(f"{i.ticker:<8} {i.figi}  {i.cik}  {i.added_on}  {i.name}")
    typer.echo(f"{len(rows)} instruments")


@poll_app.command("edgar")
def poll_edgar(
    interval: float = typer.Option(5.0, help="Seconds between polls (start to start)."),
    health_port: int = typer.Option(8765, help="Port for GET /healthz on 127.0.0.1."),
    stale_after: float = typer.Option(60.0, help="/healthz returns 503 after this many seconds without a successful poll."),
    max_backfill_pages: int = typer.Option(5, help="Extra feed pages to read when a gap since the last poll is possible."),
    once: bool = typer.Option(False, help="Poll once and exit (for checks)."),
) -> None:
    """Poll the EDGAR 8-K Atom feed and store allowlisted filings."""
    import httpx

    from nta.clock import SystemClock
    from nta.config import ConfigError, sec_user_agent
    from nta.db import make_engine, make_session_factory
    from nta.edgar import FEED_URL, EdgarPoller
    from nta.health import HealthServer
    from nta.logs import log
    from nta.ratelimit import sec_bucket
    from nta.runs import start_run, stop_run
    from nta.shutdown import StopFlag, install_signal_handlers

    try:
        ua = sec_user_agent()  # fail at startup, before touching anything else
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    clock = SystemClock()
    sf = make_session_factory(make_engine())
    config = {"process": "poll_edgar", "feed_url": FEED_URL, "interval_s": interval,
              "max_backfill_pages": max_backfill_pages, "rate": "8/s burst 2",
              "forms": ["8-K", "8-K/A"]}
    run_id = start_run(sf, "poll_edgar", config, clock)
    stop = StopFlag()
    install_signal_handlers(stop)

    health = None
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as http:
            poller = EdgarPoller(http=http, bucket=sec_bucket(clock), clock=clock,
                                 session_factory=sf, user_agent=ua, run_id=run_id,
                                 max_backfill_pages=max_backfill_pages)
            if once:
                poller.poll_once()
            else:
                health = HealthServer(poller.health, port=health_port,
                                      stale_after_s=stale_after).start()
                log("healthz_listening", url=f"http://127.0.0.1:{health.port}/healthz")
                poller.run(stop, interval_s=interval)
    except BaseException as exc:
        stop.set(f"error {type(exc).__name__}: {exc}")
        raise
    finally:
        if health:
            health.stop()
        stop_run(sf, run_id, stop.reason, clock)


@app.command("quotes")
def quotes_cmd(
    tick: float = typer.Option(0.5, help="Seconds between scheduler ticks."),
    health_port: int = typer.Option(8766, help="Port for GET /healthz on 127.0.0.1."),
    stale_after: float = typer.Option(30.0, help="/healthz returns 503 after this many seconds without a successful tick."),
    once: bool = typer.Option(False, help="Run one tick and exit (for checks)."),
) -> None:
    """Record observe/mark quotes for stored filings and periodic quotes for the allowlist."""
    import httpx

    from nta.alpaca import ALPACA_DATA_URL, AlpacaData
    from nta.clock import SystemClock
    from nta.config import ConfigError, alpaca_credentials
    from nta.db import make_engine, make_session_factory
    from nta.health import HealthServer
    from nta.logs import log
    from nta.quotes import QuoteRecorder
    from nta.ratelimit import alpaca_bucket
    from nta.runs import start_run, stop_run
    from nta.shutdown import StopFlag, install_signal_handlers

    try:
        key, secret = alpaca_credentials()
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    clock = SystemClock()
    sf = make_session_factory(make_engine())
    config = {"process": "quotes", "data_url": ALPACA_DATA_URL, "feed": "iex", "tick_s": tick,
              "periodic_every_s": 300, "rate": "170/min burst 10"}
    run_id = start_run(sf, "quotes", config, clock)
    stop = StopFlag()
    install_signal_handlers(stop)
    health = None
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0)) as http:
            alpaca = AlpacaData(http, alpaca_bucket(clock), clock, key, secret)
            recorder = QuoteRecorder(session_factory=sf, alpaca=alpaca, clock=clock, run_id=run_id)
            if once:
                recorder.tick()
            else:
                health = HealthServer(recorder.health, port=health_port,
                                      stale_after_s=stale_after).start()
                log("healthz_listening", url=f"http://127.0.0.1:{health.port}/healthz")
                recorder.run(stop, tick_s=tick)
    except BaseException as exc:
        stop.set(f"error {type(exc).__name__}: {exc}")
        raise
    finally:
        if health:
            health.stop()
        stop_run(sf, run_id, stop.reason, clock)


@app.command("label")
def label_cmd(
    seed: int = typer.Option(7, help="Sampling seed. Keep it fixed so sessions resume in the same order."),
    target: int = typer.Option(100, help="Sample size."),
    start: str | None = typer.Option(None, help="Window start (YYYY-MM-DD, New York date of first observation)."),
    end: str | None = typer.Option(None, help="Window end (inclusive)."),
) -> None:
    """Label a seeded random sample of stored filings in the terminal."""
    from nta.clock import SystemClock
    from nta.db import make_engine, make_session_factory
    from nta.label import run_session

    run_session(make_session_factory(make_engine()), SystemClock(), seed=seed, target=target,
                start=date.fromisoformat(start) if start else None,
                end=date.fromisoformat(end) if end else None)
