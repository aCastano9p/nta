"""`nta` command-line entry point. Subcommands are added per component."""

from datetime import date
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)
db_app = typer.Typer(no_args_is_help=True, help="Database maintenance.")
app.add_typer(db_app, name="db")
allowlist_app = typer.Typer(no_args_is_help=True, help="Manage the instrument allowlist.")
app.add_typer(allowlist_app, name="allowlist")

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
