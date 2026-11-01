"""`nta` command-line entry point. Subcommands are added per component."""

from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)
db_app = typer.Typer(no_args_is_help=True, help="Database maintenance.")
app.add_typer(db_app, name="db")

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
