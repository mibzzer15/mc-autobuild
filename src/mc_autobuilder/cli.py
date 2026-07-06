"""mc-autobuilder command-line interface."""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
import typer

from .auth import AuthConfig, SessionExpiredError, build_session, interactive_playwright_login
from .config import Config, ConfigError
from .constants import BUILDING_TYPES
from .mc_client import MissionChiefClient
from .models import (
    Building,
    get_session_factory,
    has_completed_action,
    init_db,
    record_completed_action,
    upsert_buildings,
)
from .planner import Plan, build_plan
from .rlm_client import RLM_BASE_URL, BoundingBox, RLMClient, RLMClientConfig, geocode_city

app = typer.Typer(add_completion=False, help="Automate MissionChief station management.")


class _NoTracebackFilter(logging.Filter):
    """Keeps exception tracebacks out of the console — they still go to the full log file, but
    the console is meant for our own clean, explicit `typer.secho` messages, not raw Python
    tracebacks caught and logged via `logger.exception(...)`."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.exc_info is None


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
    console.addFilter(_NoTracebackFilter())
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


def _resolve_region_bbox(region, geocode_session: requests.Session) -> tuple[BoundingBox, str]:
    """Returns (bbox, city_label_for_naming_template)."""
    if region.bbox is not None:
        return BoundingBox(**region.bbox), region.name
    if region.city is not None:
        center = geocode_city(region.city, session=geocode_session)
        center_lat = (center.north + center.south) / 2
        center_lng = (center.east + center.west) / 2
        return BoundingBox.from_center_radius(center_lat, center_lng, region.radius_km), region.city
    return BoundingBox.from_center_radius(region.center["lat"], region.center["lng"], region.radius_km), region.name


@app.command()
def plan(
    env_file: str = ".env",
    config_file: str = "config.yaml",
    db_path: str = "mc_autobuilder.db",
    plan_path: str = "plan.json",
    rlm_base_url: str = RLM_BASE_URL,
) -> None:
    """Fetch RLM candidate stations for the configured regions, dedupe against your existing
    buildings, and write a dry-run plan. Never builds or spends anything."""
    log_path = _setup_logging("plan")
    logger = logging.getLogger("mc_autobuilder.plan")

    try:
        config = Config.from_yaml(config_file)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    auth_config = AuthConfig.from_env(env_file)
    try:
        session = build_session(auth_config)
    except SessionExpiredError as exc:
        typer.secho(f"Authentication failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    mc_client = MissionChiefClient(session, auth_config.base_url)

    engine = init_db(db_path)
    session_factory = get_session_factory(engine)
    with session_factory() as db:
        existing_buildings = [
            {
                "id": b.id,
                "building_type": b.building_type,
                "latitude": b.latitude,
                "longitude": b.longitude,
            }
            for b in db.query(Building).all()
        ]
    if not existing_buildings:
        typer.secho(
            "Warning: no cached buildings found. Run `mc-autobuilder sync` first so duplicates "
            "against your existing stations can actually be detected.",
            fg=typer.colors.YELLOW,
        )

    rlm_client = RLMClient(
        RLMClientConfig(
            base_url=rlm_base_url,
            cache_dir=Path(config.rlm_cache_dir),
            cache_ttl_hours=config.rlm_cache_ttl_hours,
            min_delay_seconds=config.rate_limit_min_delay,
            max_delay_seconds=config.rate_limit_max_delay,
        )
    )
    geocode_session = requests.Session()

    logger.info("Fetching current building prices from %s ...", auth_config.base_url)
    try:
        prices = mc_client.get_building_prices()
    except SessionExpiredError as exc:
        typer.secho(f"Authentication failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    per_type_caps = {bt.building_type: bt.max_per_run for bt in config.building_types if bt.max_per_run}

    all_candidates = []
    for region in config.regions:
        bbox, city_label = _resolve_region_bbox(region, geocode_session)
        logger.info("Region %s resolved to bbox %s", region.name, bbox)
        for bt_config in config.building_types:
            pois = rlm_client.get_pois(bt_config.poi_type, bbox)
            logger.info(
                "Region %s / %s: %d candidate POIs", region.name, bt_config.poi_type, len(pois)
            )
            for poi in pois:
                all_candidates.append(
                    {
                        **poi,
                        "building_type": bt_config.building_type,
                        "building_type_name": BUILDING_TYPES.get(bt_config.building_type, ""),
                        "_region": region.name,
                        "_city_label": city_label,
                    }
                )

    # Plan region-by-region so the naming template's {city} reflects each candidate's own region,
    # then merge into one overall plan for reporting/budget purposes.
    merged = Plan()
    built_so_far_cost = 0
    for region in config.regions:
        region_candidates = [c for c in all_candidates if c["_region"] == region.name]
        if not region_candidates:
            continue
        city_label = region_candidates[0]["_city_label"]
        region_plan = build_plan(
            region_candidates,
            existing_buildings,
            dedupe_radius_m=config.dedupe_radius_m,
            naming_template=config.naming_template,
            city=city_label,
            per_type_caps=per_type_caps,
            prices=prices,
            max_total_cost=(
                config.max_credits_per_run - built_so_far_cost
                if config.max_credits_per_run is not None
                else None
            ),
        )
        merged.to_build.extend(region_plan.to_build)
        merged.skipped_duplicates.extend(region_plan.skipped_duplicates)
        merged.skipped_capped.extend(region_plan.skipped_capped)
        merged.skipped_budget.extend(region_plan.skipped_budget)
        merged.total_estimated_cost += region_plan.total_estimated_cost
        built_so_far_cost += region_plan.total_estimated_cost

    plan_output = {
        "generated_at": datetime.now().isoformat(),
        "regions": [r.name for r in config.regions],
        **merged.to_dict(),
        "budget": {
            "max_credits_per_run": config.max_credits_per_run,
            "credit_reserve": config.credit_reserve,
        },
    }
    Path(plan_path).write_text(json.dumps(plan_output, indent=2))

    logger.info(
        "Plan complete: %d to build, %d duplicates skipped, %d capped, %d over budget",
        len(merged.to_build),
        len(merged.skipped_duplicates),
        len(merged.skipped_capped),
        len(merged.skipped_budget),
    )

    typer.echo("")
    typer.secho(f"Plan: {len(merged.to_build)} station(s) to build", bold=True)
    for b in merged.to_build:
        cost_str = f"{b.estimated_cost:,} credits" if b.estimated_cost is not None else "price unknown"
        typer.echo(f"  + {b.name!r} ({b.building_type_name}) @ {b.latitude:.5f},{b.longitude:.5f} — {cost_str}")

    typer.echo("")
    typer.echo(f"Skipped as duplicates: {len(merged.skipped_duplicates)}")
    typer.echo(f"Skipped (per-type cap reached): {len(merged.skipped_capped)}")
    typer.echo(f"Skipped (over budget): {len(merged.skipped_budget)}")
    typer.echo("")
    typer.secho(f"Total estimated cost: {merged.total_estimated_cost:,} credits", bold=True)
    if config.max_credits_per_run is not None:
        typer.echo(f"Run budget: {config.max_credits_per_run:,} credits")
    typer.echo(
        f"Credit reserve ({config.credit_reserve:,}) is not yet enforced against your live "
        "balance — there's no confirmed endpoint for reading it yet (see docs/missionchief-api.md)."
    )
    typer.echo("")
    typer.echo(f"Plan written to: {plan_path}")
    typer.echo(f"Full log: {log_path}")
    typer.echo("")
    typer.secho("This is a dry run. Nothing was built or spent.", fg=typer.colors.CYAN)


@app.command()
def build(
    poi_id: int = typer.Option(..., help="poi_id of the plan.json `to_build` entry to build"),
    plan_path: str = "plan.json",
    env_file: str = ".env",
    db_path: str = "mc_autobuilder.db",
    execute: bool = typer.Option(
        False, "--execute", help="Actually submit the build. Without this, only previews it."
    ),
) -> None:
    """Build exactly ONE station from a previously generated plan.json.

    Dry-run by default (shows what would be submitted and its cost, spends nothing). Pass
    --execute to actually submit it — you'll still get one interactive confirmation naming the
    exact station and price before anything is spent. Safe to re-run: a station already recorded
    as built for this poi_id is skipped rather than built again.
    """
    log_path = _setup_logging("build")
    logger = logging.getLogger("mc_autobuilder.build")

    plan_file = Path(plan_path)
    if not plan_file.exists():
        typer.secho(f"{plan_path} not found. Run `mc-autobuilder plan` first.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    plan_data = json.loads(plan_file.read_text())
    entry = next((b for b in plan_data.get("to_build", []) if b["poi_id"] == poi_id), None)
    if entry is None:
        typer.secho(f"No to_build entry with poi_id={poi_id} in {plan_path}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    engine = init_db(db_path)
    session_factory = get_session_factory(engine)
    with session_factory() as db:
        already_done = has_completed_action(db, "build", poi_id)
        if already_done:
            typer.secho(
                f"Already built (recorded {already_done.created_at}, MissionChief building id "
                f"{already_done.building_id}) — skipping to avoid a duplicate.",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(code=0)

    typer.echo("")
    typer.secho(f"Station: {entry['name']!r} ({entry['building_type_name']})", bold=True)
    typer.echo(f"Location: {entry['latitude']:.5f}, {entry['longitude']:.5f}")
    typer.echo(f"Estimated cost (from plan): {entry['estimated_cost']:,} credits" if entry.get("estimated_cost") is not None else "Cost: unknown")

    if not execute:
        typer.echo("")
        typer.secho("Dry run — pass --execute to actually build this. Nothing was submitted.", fg=typer.colors.CYAN)
        raise typer.Exit(code=0)

    typer.echo("")
    if not typer.confirm(
        f"This will spend real credits to build {entry['name']!r}. The exact current price will "
        "be re-checked live immediately before submitting. Continue?"
    ):
        typer.echo("Cancelled — nothing was built.")
        raise typer.Exit(code=0)

    auth_config = AuthConfig.from_env(env_file)
    try:
        session = build_session(auth_config)
        mc_client = MissionChiefClient(session, auth_config.base_url)
        logger.info("Submitting build for poi_id=%s: %s", poi_id, entry["name"])
        result = mc_client.create_building(
            building_type=entry["building_type"],
            name=entry["name"],
            latitude=entry["latitude"],
            longitude=entry["longitude"],
        )
    except SessionExpiredError as exc:
        typer.secho(f"Authentication failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        logger.exception("Unexpected error while submitting the build")
        typer.echo("")
        typer.secho(
            f"Unexpected error while building: {exc}\n"
            "Check your MissionChief account manually to see whether it actually built anyway "
            f"before retrying, and see {log_path} for the full error.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    if result.success:
        with session_factory() as db:
            record_completed_action(
                db,
                action_type="build",
                poi_id=poi_id,
                building_id=result.building["id"],
                building_type=entry["building_type"],
                name=entry["name"],
                cost=result.price,
            )
        logger.info("Build succeeded: new building id %s, cost %s", result.building["id"], result.price)
        typer.echo("")
        typer.secho(
            f"Built {entry['name']!r} — MissionChief building id {result.building['id']}, "
            f"cost {result.price:,} credits.",
            fg=typer.colors.GREEN,
            bold=True,
        )
    else:
        logger.error(
            "Build could not be confirmed. POST /buildings returned HTTP %s; no new building of "
            "type %s appeared in /api/buildings afterward.",
            result.response_status,
            entry["building_type"],
        )
        typer.echo("")
        typer.secho(
            "Could not confirm the build succeeded — no new matching building appeared in "
            "/api/buildings afterward. Check your account manually before retrying, and see the "
            "log for details (this could mean insufficient funds, a full building slot, or an "
            "unexpected response).",
            fg=typer.colors.RED,
        )

    typer.echo(f"Full log: {log_path}")


@app.command()
def run(
    plan_path: str = "plan.json",
    env_file: str = ".env",
    config_file: str = "config.yaml",
    db_path: str = "mc_autobuilder.db",
    execute: bool = typer.Option(
        False, "--execute", help="Actually build everything. Without this, only previews it."
    ),
) -> None:
    """Build every not-yet-built station in plan.json's `to_build` list, one at a time.

    Dry-run by default (shows the full list and total cost, spends nothing). Pass --execute for
    ONE confirmation covering the whole batch, then it builds through all of them — with the
    usual rate-limit delays between each — stopping immediately (not just skipping) if: your live
    credit balance would drop below budget.credit_reserve, a build fails to confirm success, the
    session expires, or this run's budget.max_credits_per_run is used up. Already-built entries
    (recorded locally, e.g. from a previous interrupted run) are skipped rather than repeated.
    """
    log_path = _setup_logging("run")
    logger = logging.getLogger("mc_autobuilder.run")

    try:
        config = Config.from_yaml(config_file)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    plan_file = Path(plan_path)
    if not plan_file.exists():
        typer.secho(f"{plan_path} not found. Run `mc-autobuilder plan` first.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    entries = json.loads(plan_file.read_text()).get("to_build", [])
    if not entries:
        typer.echo("Nothing to build — plan.json's to_build list is empty.")
        raise typer.Exit(code=0)

    engine = init_db(db_path)
    session_factory = get_session_factory(engine)
    with session_factory() as db:
        pending = [e for e in entries if has_completed_action(db, "build", e["poi_id"]) is None]
    already_done = len(entries) - len(pending)

    if not pending:
        typer.secho(f"All {len(entries)} station(s) in {plan_path} are already built.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)

    total_estimated_cost = sum(e.get("estimated_cost") or 0 for e in pending)
    typer.echo("")
    header = f"{len(pending)} station(s) to build"
    if already_done:
        header += f" ({already_done} already done, skipping)"
    typer.secho(header, bold=True)
    for e in pending:
        cost_str = f"{e['estimated_cost']:,} credits" if e.get("estimated_cost") is not None else "price unknown"
        typer.echo(f"  + {e['name']!r} ({e['building_type_name']}) — {cost_str}")
    typer.echo("")
    typer.secho(f"Total estimated cost: {total_estimated_cost:,} credits", bold=True)
    typer.echo(
        f"Credit reserve to protect: {config.credit_reserve:,} credits "
        "(checked against your live balance before each station)"
    )

    if not execute:
        typer.echo("")
        typer.secho("Dry run — pass --execute to build all of these. Nothing was submitted.", fg=typer.colors.CYAN)
        raise typer.Exit(code=0)

    typer.echo("")
    if not typer.confirm(
        f"This will spend up to {total_estimated_cost:,} credits building {len(pending)} "
        "station(s), one at a time, stopping early if your balance would drop below the reserve "
        "or anything looks wrong. Continue?"
    ):
        typer.echo("Cancelled — nothing was built.")
        raise typer.Exit(code=0)

    auth_config = AuthConfig.from_env(env_file)
    try:
        session = build_session(auth_config)
    except SessionExpiredError as exc:
        typer.secho(f"Authentication failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    mc_client = MissionChiefClient(session, auth_config.base_url)

    built = 0
    spent = 0
    typer.echo("")
    for entry in pending:
        try:
            balance = mc_client.get_credits_balance()
            live_prices = mc_client.get_building_prices()
        except SessionExpiredError as exc:
            typer.secho(f"Stopping — authentication failed: {exc}", fg=typer.colors.RED, err=True)
            break
        except Exception as exc:
            logger.exception("Could not read live credit balance / prices")
            typer.secho(f"Stopping — could not read your live balance/prices: {exc}", fg=typer.colors.RED)
            break

        price = live_prices.get(entry["building_type"])
        if price is None:
            typer.secho(
                f"Stopping — no live price found for building_type {entry['building_type']}.",
                fg=typer.colors.RED,
            )
            break
        if balance - price < config.credit_reserve:
            typer.secho(
                f"Stopping — building {entry['name']!r} (~{price:,} credits) would drop your "
                f"balance ({balance:,}) below the {config.credit_reserve:,} reserve.",
                fg=typer.colors.YELLOW,
            )
            break
        if config.max_credits_per_run is not None and spent + price > config.max_credits_per_run:
            typer.secho(
                f"Stopping — this run's {config.max_credits_per_run:,}-credit budget is used up.",
                fg=typer.colors.YELLOW,
            )
            break

        typer.echo(f"Building {entry['name']!r} ({price:,} credits)...")
        try:
            result = mc_client.create_building(
                building_type=entry["building_type"],
                name=entry["name"],
                latitude=entry["latitude"],
                longitude=entry["longitude"],
            )
        except SessionExpiredError as exc:
            typer.secho(f"Stopping — authentication failed: {exc}", fg=typer.colors.RED, err=True)
            break
        except Exception as exc:
            logger.exception("Unexpected error while building %s", entry["name"])
            typer.secho(f"Stopping — unexpected error building {entry['name']!r}: {exc}", fg=typer.colors.RED)
            break

        if not result.success:
            typer.secho(
                f"Stopping — could not confirm {entry['name']!r} was built. Check your account "
                "manually before retrying.",
                fg=typer.colors.RED,
            )
            break

        with session_factory() as db:
            record_completed_action(
                db,
                action_type="build",
                poi_id=entry["poi_id"],
                building_id=result.building["id"],
                building_type=entry["building_type"],
                name=entry["name"],
                cost=result.price,
            )
        built += 1
        spent += result.price or 0
        logger.info(
            "Built %s (poi_id=%s) -> building id %s, cost %s",
            entry["name"],
            entry["poi_id"],
            result.building["id"],
            result.price,
        )
        typer.secho(f"  Built — building id {result.building['id']}, cost {result.price:,} credits.", fg=typer.colors.GREEN)

    typer.echo("")
    typer.secho(f"Run complete: built {built}/{len(pending)}, spent {spent:,} credits.", bold=True)
    typer.echo(f"Full log: {log_path}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
