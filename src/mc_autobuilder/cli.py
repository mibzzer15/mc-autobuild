"""mc-autobuilder command-line interface."""
from __future__ import annotations

import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import typer

from .auth import AuthConfig, SessionExpiredError, build_session, interactive_playwright_login
from .constants import BUILDING_TYPES
from .mc_client import MissionChiefClient
from .models import Building, get_session_factory, init_db, upsert_buildings

app = typer.Typer(add_completion=False, help="Automate MissionChief station management.")


def _setup_logging(run_name: str) -> Path:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{run_name}_{datetime.now().strftime('%Y%m%dT%H%M%S')}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(logging.FileHandler(log_path))
    root.handlers[-1].setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)
    return log_path


@app.command()
def login(env_file: str = ".env") -> None:
    """Interactively log into MissionChief with a real browser (Playwright mode) and save the session."""
    config = AuthConfig.from_env(env_file)
    interactive_playwright_login(config)


@app.command()
def sync(env_file: str = ".env", db_path: str = "mc_autobuilder.db") -> None:
    """Pull your current buildings from MissionChief and cache them locally."""
    log_path = _setup_logging("sync")
    logger = logging.getLogger("mc_autobuilder.sync")

    config = AuthConfig.from_env(env_file)
    try:
        session = build_session(config)
        client = MissionChiefClient(session, config.base_url)
        logger.info("Fetching buildings from %s ...", config.base_url)
        buildings = client.get_buildings()
    except SessionExpiredError as exc:
        typer.secho(f"Authentication failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    engine = init_db(db_path)
    session_factory = get_session_factory(engine)
    with session_factory() as db:
        upsert_buildings(db, buildings)
        total_cached = db.query(Building).count()

    logger.info("Synced %d buildings into %s", len(buildings), db_path)

    counts = Counter(b["building_type"] for b in buildings)
    enabled_count = sum(1 for b in buildings if b.get("enabled", True))
    dispatch_ids = {b["leitstelle_building_id"] for b in buildings if b.get("leitstelle_building_id")}

    typer.echo("")
    typer.secho(f"Synced {len(buildings)} buildings ({total_cached} total cached in {db_path})", bold=True)
    typer.echo(f"In service: {enabled_count} / {len(buildings)}")
    typer.echo("")
    typer.echo("By building type:")
    for type_id, count in counts.most_common():
        name = BUILDING_TYPES.get(type_id, f"Unknown type {type_id}")
        disabled = sum(
            1 for b in buildings if b["building_type"] == type_id and not b.get("enabled", True)
        )
        suffix = f" ({disabled} out of service)" if disabled else ""
        typer.echo(f"  {name:35s} {count:4d}{suffix}")

    typer.echo("")
    typer.echo(f"Dispatch centers referenced: {len(dispatch_ids)}")
    typer.echo(f"Full log: {log_path}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
