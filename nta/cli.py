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
kill_app = typer.Typer(no_args_is_help=True, help="Kill switch. For humans only: no process calls these.")
app.add_typer(kill_app, name="kill")

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
    from nta.config import openfigi_api_key, optional_sec_user_agent
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
        figi_key = openfigi_api_key()
        figi = allowlist.OpenFigiClient(http, openfigi_bucket(clock, keyed=bool(figi_key)), clock, sf,
                                        api_key=figi_key)
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


@app.command("agent")
def agent_cmd(
    tick: float = typer.Option(1.0, help="Seconds between ticks."),
    health_port: int = typer.Option(8767, help="Port for GET /healthz on 127.0.0.1."),
    stale_after: float = typer.Option(60.0, help="/healthz returns 503 after this many seconds without a successful tick."),
    lookback_h: float = typer.Option(24.0, help="Only source events first observed within this many hours are processed."),
    policy_path: Path = typer.Option(None, "--policy", help="Policy file (default policy/policy.yaml)."),
    once: bool = typer.Option(False, help="Run one tick and exit (for checks)."),
) -> None:
    """Extract, cluster and decide on new filings; manage open positions. Never trades."""
    from datetime import timedelta

    import httpx

    from nta.agent import AgentProcess, LiveQuotes, Pipeline, PositionManager
    from nta.alpaca import AlpacaData
    from nta.clock import SystemClock
    from nta.config import ConfigError, alpaca_credentials, anthropic_api_key, assert_agent_env
    from nta.db import make_engine, make_session_factory
    from nta.extract import AnthropicBackend, LlmGateway, daily_usd_from_env, model_config_from_env
    from nta.health import HealthServer
    from nta.logs import log
    from nta.policy import DEFAULT_POLICY_PATH, load_policy, record_policy_version
    from nta.ratelimit import alpaca_bucket
    from nta.runs import code_sha, start_run, stop_run
    from nta.shutdown import StopFlag, install_signal_handlers

    try:
        assert_agent_env()  # first: refuse before reading any other credential
        key, secret = alpaca_credentials()
        llm_key = anthropic_api_key()
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    clock = SystemClock()
    sf = make_session_factory(make_engine())
    loaded = load_policy(policy_path or DEFAULT_POLICY_PATH)
    models = model_config_from_env()
    config = {"process": "agent", "tick_s": tick, "lookback_h": lookback_h, "policy_version": loaded.hash,
              "models": models.model_id, "prompts": models.prompt_version, "feed": loaded.policy.quote_source}
    run_id = start_run(sf, "agent", config, clock)
    record_policy_version(sf, loaded, process="agent", run_id=run_id)
    stop = StopFlag()
    install_signal_handlers(stop)
    health = None
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0)) as http:
            alpaca = AlpacaData(http, alpaca_bucket(clock), clock, key, secret, feed=loaded.policy.quote_source)
            gateway = LlmGateway(sf, clock, backend=AnthropicBackend(llm_key), daily_usd=daily_usd_from_env())
            quotes = LiveQuotes(sf, alpaca, clock)
            pipeline = Pipeline(sf=sf, clock=clock, gateway=gateway, models=models, policy=loaded, quotes=quotes,
                                code_sha=code_sha())
            manager = PositionManager(pipeline)
            proc = AgentProcess(pipeline, clock, run_id=run_id, lookback=timedelta(hours=lookback_h),
                                manager=manager.step)
            if once:
                proc.tick()
            else:
                health = HealthServer(proc.health, port=health_port, stale_after_s=stale_after).start()
                log("healthz_listening", url=f"http://127.0.0.1:{health.port}/healthz")
                proc.run(stop, tick_s=tick)
    except BaseException as exc:
        stop.set(f"error {type(exc).__name__}: {exc}")
        raise
    finally:
        if health:
            health.stop()
        stop_run(sf, run_id, stop.reason, clock)


def _kill(state: str, reason: str) -> None:
    from nta.clock import SystemClock
    from nta.db import make_engine, make_session_factory
    from nta.risk import write_kill
    from nta.schema import KillState

    sf = make_session_factory(make_engine())
    with sf() as s, s.begin():
        write_kill(s, KillState(state), "human", reason, SystemClock().now())
    typer.echo(f"kill switch -> {state} ({reason})")


@kill_app.command("pause")
def kill_pause(reason: str = typer.Option(..., "--reason", "-r", help="Why (stored with the event).")) -> None:
    """Stop new entries; open positions are still managed and closed."""
    _kill("PAUSE", reason)


@kill_app.command("flatten")
def kill_flatten(reason: str = typer.Option(..., "--reason", "-r")) -> None:
    """Stop entries and close every open position."""
    _kill("FLATTEN", reason)


@kill_app.command("halt")
def kill_halt(reason: str = typer.Option(..., "--reason", "-r")) -> None:
    """Send nothing at all and cancel open orders; positions are left as they are."""
    _kill("HALT", reason)


@kill_app.command("resume")
def kill_resume(reason: str = typer.Option(..., "--reason", "-r")) -> None:
    """Back to RUN. Closes every open incident. Requires typing RESUME."""
    from nta.db import make_engine, make_session_factory
    from nta.risk import open_incidents

    with make_session_factory(make_engine())() as s:
        incidents = open_incidents(s)
    for i in incidents:
        typer.echo(f"open incident: sev{i.severity} {i.kind} at {i.at.isoformat()} {i.detail}")
    answer = typer.prompt("Type RESUME to resume trading")
    if answer.strip() != "RESUME":
        typer.echo("not resumed")
        raise typer.Exit(1)
    _kill("RUN", reason)


@kill_app.command("status")
def kill_status() -> None:
    """Print the kill switch state and open incidents."""
    from nta.clock import SystemClock
    from nta.db import make_engine, make_session_factory
    from nta.decide import load_kill_state
    from nta.risk import open_incidents

    with make_session_factory(make_engine())() as s:
        typer.echo(f"state: {load_kill_state(s, SystemClock().now()).value}")
        for i in open_incidents(s):
            typer.echo(f"open incident: sev{i.severity} {i.kind} at {i.at.isoformat()} {i.detail}")


@app.command("risk")
def risk_cmd(
    tick: float = typer.Option(0.5, help="Seconds between ticks."),
    health_port: int = typer.Option(8768, help="Port for GET /healthz on 127.0.0.1."),
    stale_after: float = typer.Option(30.0, help="/healthz returns 503 after this many seconds without a successful tick."),
    policy_path: Path = typer.Option(None, "--policy", help="Policy file (default policy/policy.yaml)."),
    once: bool = typer.Option(False, help="Run one tick and exit (for checks)."),
) -> None:
    """Re-check, sign and execute trade decisions on paper account T; reconcile; watchdog account D."""
    import httpx

    from nta.alpaca import AlpacaData
    from nta.broker import AlpacaPaperBroker
    from nta.clock import SystemClock
    from nta.config import ConfigError, alpaca_credentials, assert_risk_env, risk_signing_key, trading_credentials
    from nta.db import make_engine, make_session_factory
    from nta.health import HealthServer
    from nta.logs import log
    from nta.policy import DEFAULT_POLICY_PATH, load_policy, record_policy_version
    from nta.ratelimit import alpaca_bucket
    from nta.risk import RiskService, alpaca_quote_fn
    from nta.runs import code_sha, start_run, stop_run
    from nta.shutdown import StopFlag, install_signal_handlers

    try:
        assert_risk_env()  # first: refuse before reading any other credential
        t_key, t_secret = trading_credentials()
        signing_key = risk_signing_key()
        d_key, d_secret = alpaca_credentials()  # account D, read-only use by the watchdog
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    clock = SystemClock()
    sf = make_session_factory(make_engine())
    loaded = load_policy(policy_path or DEFAULT_POLICY_PATH)
    config = {"process": "risk", "tick_s": tick, "policy_version": loaded.hash, "feed": loaded.policy.quote_source,
              "trading_url": "https://paper-api.alpaca.markets"}
    run_id = start_run(sf, "risk", config, clock)
    record_policy_version(sf, loaded, process="risk", run_id=run_id, signing_key=signing_key)
    stop = StopFlag()
    install_signal_handlers(stop)
    health = None
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0)) as http:
            bucket = alpaca_bucket(clock)
            data_t = AlpacaData(http, alpaca_bucket(clock), clock, t_key, t_secret, feed=loaded.policy.quote_source)
            risk = RiskService(sf=sf, clock=clock, broker=AlpacaPaperBroker(http, bucket, clock, t_key, t_secret),
                               quotes=alpaca_quote_fn(data_t, clock), policy=loaded, signing_key=signing_key,
                               run_id=run_id, watchdog=AlpacaPaperBroker(http, alpaca_bucket(clock), clock, d_key, d_secret),
                               code_sha=code_sha())
            if once:
                risk.tick()
            else:
                health = HealthServer(risk.health, port=health_port, stale_after_s=stale_after).start()
                log("healthz_listening", url=f"http://127.0.0.1:{health.port}/healthz")
                risk.run(stop, tick_s=tick)
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


@app.command("verdict")
def verdict_cmd(
    start: str | None = typer.Option(None, help="Window start (YYYY-MM-DD, New York date). Default: first filing."),
    end: str | None = typer.Option(None, help="Window end, inclusive. Default: last filing."),
    csv_path: Path | None = typer.Option(None, "--csv", help="Per-filing CSV path. Default: exports/verdict_<start>_<end>.csv"),
    no_csv: bool = typer.Option(False, help="Do not write the CSV."),
) -> None:
    """Print the phase 0 verdict for a window and export the per-filing table."""
    from nta import verdict
    from nta.db import make_engine, make_session_factory

    s_date = date.fromisoformat(start) if start else None
    e_date = date.fromisoformat(end) if end else None
    with make_session_factory(make_engine())() as s:
        if not no_csv and csv_path is None:
            window = (s_date, e_date)
            if None in window:
                dw = verdict.default_window(s) or (date.today(), date.today())
                window = (s_date or dw[0], e_date or dw[1])
            csv_path = Path("exports") / f"verdict_{window[0]}_{window[1]}.csv"
        text, _ = verdict.run(s, s_date, e_date, None if no_csv else csv_path)
    typer.echo(text)


@app.command("dashboard")
def dashboard_cmd(
    port: int = typer.Option(8780, help="Port on 127.0.0.1."),
    demo: bool = typer.Option(False, help="Rebuild the separate nta_demo database with synthetic data and show that."),
    poller_health: str = typer.Option("http://127.0.0.1:8765/healthz", help="EDGAR poller /healthz URL."),
    quotes_health: str = typer.Option("http://127.0.0.1:8766/healthz", help="Quote recorder /healthz URL."),
) -> None:
    """Serve a read-only dashboard at http://127.0.0.1:<port>/."""
    from sqlalchemy.engine import make_url

    from nta.dashboard import Dashboard
    from nta.db import database_url, make_engine, make_session_factory
    from nta.demo import recreate_demo_database

    url = database_url()
    if demo:
        typer.echo("building demo database (synthetic data)...")
        url = recreate_demo_database(url)
    db_name = make_url(url).database or "?"
    health = {} if demo else {"EDGAR poller": poller_health, "Quote recorder": quotes_health}
    dash = Dashboard(make_session_factory(make_engine(url)), db_name=db_name, demo=demo,
                     port=port, health_urls=health)
    typer.echo(f"dashboard: http://127.0.0.1:{dash.port}/  (database {db_name}; Ctrl-C to stop)")
    try:
        dash.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dash.stop()
