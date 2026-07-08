"""Phase 6 web dashboard: a full control panel over the same MissionChiefClient/planner code
the CLI uses, gated behind its own password (separate from the MissionChief session itself —
see web_config.py) since it can spend real credits.

Every write action follows the same safety rule as the CLI: money-spending actions (expand, buy
vehicle) are a genuine two-step preview -> confirm flow (never a single click), while free actions
(toggle service, hire, assign personnel, set dispatch center) are a single deliberate form submit
whose button states the exact consequence. Every action verifies success the same way its
MissionChiefClient method does (see mc_client.py / docs/missionchief-api.md) rather than trusting
an assumed response shape.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..auth import AuthConfig, SessionExpiredError, build_session, load_config, update_env_file
from ..config import Config, ConfigError, load_raw_config, save_raw_config
from ..constants import BUILDING_TYPES
from ..mc_client import MissionChiefClient, RateLimitConfig, summarize_html_for_log
from ..models import (
    Building,
    get_preset,
    get_preset_log,
    get_session_factory,
    has_completed_action,
    init_db,
    list_presets,
    record_completed_action,
    save_preset,
    upsert_buildings,
)
from ..plan_generation import generate_plan
from ..presets import apply_preset, parse_vehicles_json
from ..rlm_client import RLM_BASE_URL, RLMClient, RLMClientConfig
from ..web_config import WebConfig

logger = logging.getLogger("mc_autobuilder.web")


def _safe_json_for_script(data) -> str:
    """JSON for embedding directly in a <script> block via `{{ ... | safe }}`. Station/POI names
    come from community-submitted RLM data, not something we control, so escape `<` as \\u003c -
    still valid JSON, but makes a `</script>` breakout impossible regardless of what's in there."""
    return json.dumps(data).replace("<", "\\u003c")


def _region_to_row(region: dict) -> dict:
    """Normalizes a config.yaml region entry (exactly one of bbox/city/center) into a flat dict
    for the Config editor form, which shows one row per region with a single "mode" selector."""
    row = {
        "name": region.get("name", ""),
        "north": "", "south": "", "east": "", "west": "",
        "city": "", "center_lat": "", "center_lng": "", "radius_km": "",
    }
    if region.get("bbox"):
        row["mode"] = "bbox"
        row.update(region["bbox"])
    elif region.get("city"):
        row["mode"] = "city"
        row["city"] = region["city"]
        row["radius_km"] = region.get("radius_km", "")
    elif region.get("center"):
        row["mode"] = "center"
        row["center_lat"] = region["center"].get("lat", "")
        row["center_lng"] = region["center"].get("lng", "")
        row["radius_km"] = region.get("radius_km", "")
    else:
        row["mode"] = "bbox"
    return row


def _rate_limit_from_config(config_file: str) -> RateLimitConfig:
    """Reads the `rate_limiting` delays out of config.yaml so the dashboard's game requests honour
    the same min/max the Config page edits. Falls back to RateLimitConfig's defaults when the file
    or section is missing (e.g. a brand-new install), rather than erroring."""
    rl = RateLimitConfig()
    try:
        section = (load_raw_config(config_file) or {}).get("rate_limiting") or {}
    except Exception:
        return rl
    if "min_delay_seconds" in section:
        rl.min_delay = float(section["min_delay_seconds"])
    if "max_delay_seconds" in section:
        rl.max_delay = float(section["max_delay_seconds"])
    return rl


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


class NotAuthenticated(Exception):
    pass


class ConfigFormError(Exception):
    """Raised for a bad /config submission that should show a flash error, not a 500."""


def create_app(
    env_file: str = ".env",
    db_path: str = "mc_autobuilder.db",
    plan_path: str = "plan.json",
    config_file: str = "config.yaml",
) -> FastAPI:
    web_config = WebConfig.from_env(env_file)
    auth_config = AuthConfig.from_env(env_file)
    engine = init_db(db_path)

    app = FastAPI(title="mc-autobuilder dashboard")
    app.add_middleware(SessionMiddleware, secret_key=web_config.secret_key)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app.state.web_config = web_config
    app.state.auth_config = auth_config
    app.state.env_file = env_file
    app.state.session_factory = get_session_factory(engine)
    app.state.plan_path = plan_path
    app.state.config_file = config_file
    app.state.mc_client = None
    # building_ids with a preset application currently running in a background thread - guards
    # against double-triggering (e.g. double-clicking "Apply preset") while one's already going.
    app.state.presets_in_progress = set()
    app.state.plan_generation_in_progress = False
    # Outcome of the most recent background plan-generation run, so the Plan page can report
    # success/failure - otherwise a run that raised (session expired, RLM error, ...) just logs
    # to the server and looks to the user like "nothing happened". None until the first run.
    app.state.plan_generation_result = None
    # Same pattern for "Run entire plan" (batch build + per-station preset application), which
    # spends credits and can run for many minutes across dozens of rate-limited requests.
    app.state.plan_run_in_progress = False
    app.state.plan_run_result = None

    @app.exception_handler(NotAuthenticated)
    async def _redirect_to_login(request: Request, exc: NotAuthenticated) -> RedirectResponse:
        return RedirectResponse(url="/login", status_code=303)

    def require_login(request: Request) -> None:
        if not request.session.get("authenticated"):
            raise NotAuthenticated()

    def get_client(request: Request) -> MissionChiefClient:
        """Builds the MissionChiefClient once and caches it on app state — rebuilding a fresh
        session on every request would be wasteful and harder on the rate limiter. If the
        session expires later, the SessionExpiredError from whatever call triggered it is what
        callers should catch and show, not this constructor."""
        if request.app.state.mc_client is None:
            session = build_session(request.app.state.auth_config)
            request.app.state.mc_client = MissionChiefClient(
                session,
                request.app.state.auth_config.base_url,
                rate_limit=_rate_limit_from_config(request.app.state.config_file),
            )
        return request.app.state.mc_client

    @contextmanager
    def db_session(request: Request):
        db = request.app.state.session_factory()
        try:
            yield db
        finally:
            db.close()

    def flash_redirect(url: str, message: str, level: str = "success") -> RedirectResponse:
        return RedirectResponse(url=f"{url}?flash={quote(message)}&level={level}", status_code=303)

    def flash_context(request: Request) -> dict:
        return {
            "flash": request.query_params.get("flash"),
            "flash_level": request.query_params.get("level", "success"),
        }

    def error_page(request: Request, message: str, back_url: str = "/") -> HTMLResponse:
        return templates.TemplateResponse(
            request, "error.html", {"message": message, "back_url": back_url}, status_code=200
        )

    def report_action_result(request: Request, back_url: str, action_label: str, result, success_message: str):
        """Shared success/failure flash handling for every write action — mirrors
        cli.py's _report_write_result. Every result exposes `.success` and `.response_text`
        (failure-response shapes are largely unconfirmed — docs/missionchief-api.md — so the raw
        body is surfaced via a short summary rather than guessed at)."""
        if result.success:
            logger.info("%s succeeded: %s", action_label, success_message)
            return flash_redirect(back_url, success_message, "success")
        logger.error("%s could not be confirmed. Response body:\n%s", action_label, result.response_text)
        summary = summarize_html_for_log(result.response_text, max_chars=200)
        return flash_redirect(
            back_url, f"Could not confirm {action_label} succeeded. Page said: {summary!r}", "error"
        )

    def run_action(request: Request, back_url: str, action_label: str, fn):
        """Runs fn(), converting SessionExpiredError/any other exception into a flash message
        instead of a 500 page — same intent as cli.py's _run_write_action."""
        try:
            return fn(), None
        except SessionExpiredError as exc:
            return None, flash_redirect(back_url, f"Authentication failed: {exc}", "error")
        except Exception as exc:
            logger.exception("Unexpected error during %s", action_label)
            return None, flash_redirect(back_url, f"Unexpected error during {action_label}: {exc}", "error")

    def _run_preset_in_background(mc_client: MissionChiefClient, building_id: int, preset) -> None:
        db = request_app_session_factory()
        try:
            apply_preset(mc_client, db, building_id, preset)
        except Exception:
            logger.exception("Preset application crashed for building %s", building_id)
        finally:
            db.close()
            app.state.presets_in_progress.discard(building_id)

    # Bound once here (not per-request) since the background thread has no Request of its own.
    def request_app_session_factory():
        return app.state.session_factory()

    def start_preset_application(request: Request, building_id: int, building_type: int) -> tuple[str, str]:
        """Kicks off apply_preset in a background thread (it can take minutes - expand-to-max
        alone can be dozens of sequential rate-limited requests). Returns (status, detail):
        status is "started", "no_preset", "already_running", or "error" (detail has the message)."""
        if building_id in request.app.state.presets_in_progress:
            return "already_running", "A preset application is already in progress for this station."
        with db_session(request) as db:
            preset = get_preset(db, building_type)
        if preset is None:
            return "no_preset", "No preset configured for this building type."
        try:
            client = get_client(request)
        except SessionExpiredError as exc:
            return "error", f"Authentication failed: {exc}"

        request.app.state.presets_in_progress.add(building_id)
        threading.Thread(
            target=_run_preset_in_background, args=(client, building_id, preset), daemon=True
        ).start()
        return "started", "Preset application started in the background — refresh this page to see progress."

    def _run_plan_generation_in_background(
        mc_client: MissionChiefClient, config: Config, existing_buildings: list[dict]
    ) -> None:
        try:
            rlm_client = RLMClient(
                RLMClientConfig(
                    base_url=RLM_BASE_URL,
                    cache_dir=Path(config.rlm_cache_dir),
                    cache_ttl_hours=config.rlm_cache_ttl_hours,
                    min_delay_seconds=config.rate_limit_min_delay,
                    max_delay_seconds=config.rate_limit_max_delay,
                )
            )
            plan_output = generate_plan(config, mc_client, rlm_client, existing_buildings)
            Path(app.state.plan_path).write_text(json.dumps(plan_output, indent=2))
            entries = plan_output["to_build"]
            # Count only what's actually left to build, matching the Plan page's own summary line -
            # some plan entries may already have a completed build recorded (e.g. built earlier but
            # not yet re-synced, so dedupe didn't drop them), and reporting the raw total here made
            # the header ("N to build") disagree with the summary ("M to build, K already done").
            db = app.state.session_factory()
            try:
                already_built = sum(1 for e in entries if has_completed_action(db, "build", e["poi_id"]))
            finally:
                db.close()
            pending = len(entries) - already_built
            logger.info("Plan generation complete: %d to build, %d already built", pending, already_built)
            done_note = f" ({already_built} already built, skipping)" if already_built else ""
            app.state.plan_generation_result = {
                "status": "success",
                "message": f"Plan generated: {pending} station(s) to build{done_note}.",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }
        except Exception as exc:
            logger.exception("Plan generation failed")
            app.state.plan_generation_result = {
                "status": "error",
                "message": f"Plan generation failed: {type(exc).__name__}: {exc}",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }
        finally:
            app.state.plan_generation_in_progress = False

    def start_plan_generation(request: Request) -> tuple[str, str]:
        """Kicks off generate_plan in a background thread - RLM fetches across several regions
        plus the live price fetch can take a while, especially on a cold cache, so this can't
        just block the request the way a single quick action would. Returns (status, detail)."""
        if request.app.state.plan_generation_in_progress:
            return "already_running", "Plan generation is already in progress."
        try:
            config = Config.from_yaml(request.app.state.config_file)
        except ConfigError as exc:
            return "error", f"Config error: {exc}"
        try:
            client = get_client(request)
        except SessionExpiredError as exc:
            return "error", f"Authentication failed: {exc}"

        with db_session(request) as db:
            existing_buildings = [
                {"id": b.id, "building_type": b.building_type, "latitude": b.latitude, "longitude": b.longitude}
                for b in db.query(Building).all()
            ]

        request.app.state.plan_generation_in_progress = True
        request.app.state.plan_generation_result = None  # clear the previous run's outcome
        threading.Thread(
            target=_run_plan_generation_in_background, args=(client, config, existing_buildings), daemon=True
        ).start()
        return "started", "Plan generation started in the background — refresh this page in a bit to see the result."

    def _run_plan_execution_in_background(
        mc_client: MissionChiefClient, entries: list[dict], max_credits_per_run: int | None
    ) -> None:
        """Builds every pending plan entry in order, applying each station's preset inline right
        after it's built. Deliberately aborts (rather than skipping ahead) on the first hard
        problem - an unconfirmed build, an expired session, or the budget cap - so a partial run
        stops cleanly instead of compounding errors, matching the project's safety rules."""
        db = app.state.session_factory()
        log: list[dict] = []
        spent = 0
        built = 0
        aborted: str | None = None
        try:
            for entry in entries:
                poi_id = entry["poi_id"]
                name = entry["name"]
                building_type = entry["building_type"]

                # Another run (or a single-station build) may have built this in the meantime.
                if has_completed_action(db, "build", poi_id):
                    log.append({"name": name, "status": "skipped", "detail": "already built"})
                    continue

                # Budget gate against the LIVE price - MissionChief escalates each type's price as
                # you build more, so the plan's snapshot estimate understates later stations.
                if max_credits_per_run is not None:
                    try:
                        price = mc_client.get_building_prices().get(building_type)
                    except SessionExpiredError as exc:
                        aborted = f"Session expired: {exc}"
                        break
                    if price is not None and spent + price > max_credits_per_run:
                        log.append({
                            "name": name, "status": "budget_stopped",
                            "detail": f"next station costs {price:,} credits, over the remaining budget",
                        })
                        aborted = f"Budget cap ({max_credits_per_run:,}) reached — stopping."
                        break

                try:
                    result = mc_client.create_building(
                        building_type=building_type, name=name,
                        latitude=entry["latitude"], longitude=entry["longitude"],
                    )
                except SessionExpiredError as exc:
                    aborted = f"Session expired while building {name!r}: {exc}"
                    break
                except Exception as exc:
                    log.append({"name": name, "status": "failed", "detail": str(exc)})
                    aborted = f"Error building {name!r}: {exc}"
                    break

                if not result.success:
                    summary = summarize_html_for_log(result.response_text, max_chars=200)
                    log.append({"name": name, "status": "failed", "detail": f"could not confirm build: {summary}"})
                    aborted = f"Could not confirm {name!r} built — stopping to avoid compounding errors."
                    break

                spent += result.price
                built += 1
                record_completed_action(
                    db, action_type="build", poi_id=poi_id, building_id=result.building["id"],
                    building_type=building_type, name=name, cost=result.price,
                )

                # Apply the preset inline (this run is already sequential; no need for a nested
                # thread). presets_in_progress is still flagged so the building page won't let a
                # manual apply race this one.
                preset = get_preset(db, building_type)
                if preset is None:
                    preset_summary = "no preset configured"
                else:
                    app.state.presets_in_progress.add(result.building["id"])
                    try:
                        msgs = apply_preset(mc_client, db, result.building["id"], preset)
                        preset_summary = "; ".join(msgs)
                    except SessionExpiredError as exc:
                        preset_summary = f"session expired during preset: {exc}"
                        log.append({"name": name, "status": "built", "detail": f"{result.price:,} credits · {preset_summary}"})
                        aborted = f"Session expired applying {name!r}'s preset: {exc}"
                        break
                    except Exception as exc:
                        logger.exception("Preset failed during plan run for building %s", result.building["id"])
                        preset_summary = f"preset error: {exc}"
                    finally:
                        app.state.presets_in_progress.discard(result.building["id"])

                log.append({"name": name, "status": "built", "detail": f"{result.price:,} credits · {preset_summary}"})

            if aborted:
                message = (
                    f"Built {built} station(s) (spent {spent:,} credits), then stopped: {aborted}"
                    if built else f"Stopped without building anything: {aborted}"
                )
            else:
                message = f"Done — built {built} station(s), spent {spent:,} credits."
            app.state.plan_run_result = {
                "status": "error" if aborted else "success",
                "message": message,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "log": log,
            }
        except Exception as exc:
            logger.exception("Plan run crashed")
            app.state.plan_run_result = {
                "status": "error",
                "message": f"Plan run crashed after {built} build(s): {type(exc).__name__}: {exc}",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "log": log,
            }
        finally:
            db.close()
            app.state.plan_run_in_progress = False

    def start_plan_execution(request: Request) -> tuple[str, str]:
        """Kicks off _run_plan_execution_in_background for every not-yet-built plan entry."""
        if request.app.state.plan_run_in_progress:
            return "already_running", "A plan run is already in progress."
        plan_file = Path(request.app.state.plan_path)
        if not plan_file.exists():
            return "error", "No plan.json yet — generate a plan first."
        data = json.loads(plan_file.read_text())
        entries = data.get("to_build", [])
        max_credits = (data.get("budget") or {}).get("max_credits_per_run")
        try:
            client = get_client(request)
        except SessionExpiredError as exc:
            return "error", f"Authentication failed: {exc}"
        with db_session(request) as db:
            pending = [e for e in entries if has_completed_action(db, "build", e["poi_id"]) is None]
        if not pending:
            return "error", "Nothing to build — every station in the plan is already built."

        request.app.state.plan_run_in_progress = True
        request.app.state.plan_run_result = None
        threading.Thread(
            target=_run_plan_execution_in_background, args=(client, pending, max_credits), daemon=True
        ).start()
        return "started", (
            f"Building {len(pending)} station(s) in the background and applying presets — this can "
            "take a while; refresh to see progress."
        )

    # ---------------------------------------------------------------- auth

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, error: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/login")
    def login_submit(request: Request, password: str = Form(...)) -> RedirectResponse:
        if secrets.compare_digest(password, request.app.state.web_config.password):
            request.session["authenticated"] = True
            return RedirectResponse(url="/", status_code=303)
        return RedirectResponse(url="/login?error=1", status_code=303)

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    # ----------------------------------------------------------- dashboard

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, _: None = Depends(require_login)) -> HTMLResponse:
        with db_session(request) as db:
            buildings = db.query(Building).all()
        counts = Counter(b.building_type for b in buildings)

        balance, balance_error = None, None
        try:
            balance = get_client(request).get_credits_balance()
        except Exception as exc:  # noqa: BLE001 - shown inline, not fatal to the whole page
            balance_error = str(exc)

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "balance": balance,
                "balance_error": balance_error,
                "total_buildings": len(buildings),
                "counts": [
                    (BUILDING_TYPES.get(t, f"Type {t}"), c) for t, c in counts.most_common()
                ],
                **flash_context(request),
            },
        )

    @app.post("/sync")
    def sync_now(request: Request, _: None = Depends(require_login)):
        buildings, redirect = run_action(request, "/", "sync", lambda: get_client(request).get_buildings())
        if redirect:
            return redirect
        with db_session(request) as db:
            upsert_buildings(db, buildings)
        return flash_redirect("/", f"Synced {len(buildings)} buildings.")

    # ----------------------------------------------------------- buildings

    @app.get("/buildings", response_class=HTMLResponse)
    def buildings_list(request: Request, _: None = Depends(require_login)) -> HTMLResponse:
        with db_session(request) as db:
            buildings = db.query(Building).order_by(Building.caption).all()
        return templates.TemplateResponse(
            request,
            "buildings.html",
            {"buildings": buildings, "BUILDING_TYPES": BUILDING_TYPES, **flash_context(request)},
        )

    @app.get("/buildings/{building_id}", response_class=HTMLResponse)
    def building_detail(request: Request, building_id: int, _: None = Depends(require_login)):
        client = get_client(request)
        try:
            detail = client.get_building_detail(building_id)
            vehicles = [v for v in client.get_vehicles() if v.get("building_id") == building_id]
            expand_prices = client.get_expand_prices(building_id)
            vehicle_options = client.get_vehicle_purchase_options(building_id)
            hire_options = client.get_hire_day_options(building_id)
        except SessionExpiredError as exc:
            return error_page(request, f"Authentication failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error loading building %s", building_id)
            return error_page(request, f"Unexpected error: {exc}")

        with db_session(request) as db:
            cached = db.get(Building, building_id)
            preset = get_preset(db, detail["building_type"])
            preset_log = get_preset_log(db, building_id, limit=15)

        return templates.TemplateResponse(
            request,
            "building_detail.html",
            {
                "building_id": building_id,
                "detail": detail,
                "cached": cached,
                "vehicles": vehicles,
                "expand_prices": sorted(expand_prices.items()),
                "vehicle_options": sorted(vehicle_options.values(), key=lambda o: o.vehicle_type_id),
                "hire_options": hire_options,
                "BUILDING_TYPES": BUILDING_TYPES,
                "preset": preset,
                "preset_log": preset_log,
                "preset_in_progress": building_id in request.app.state.presets_in_progress,
                **flash_context(request),
            },
        )

    @app.post("/buildings/{building_id}/apply-preset")
    def apply_preset_route(request: Request, building_id: int, _: None = Depends(require_login)):
        back_url = f"/buildings/{building_id}"
        if building_id in request.app.state.presets_in_progress:
            return flash_redirect(back_url, "A preset application is already in progress for this station.", "error")
        try:
            detail = get_client(request).get_building_detail(building_id)
        except SessionExpiredError as exc:
            return flash_redirect(back_url, f"Authentication failed: {exc}", "error")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not read building %s before applying preset", building_id)
            return flash_redirect(back_url, f"Unexpected error: {exc}", "error")

        status, message = start_preset_application(request, building_id, detail["building_type"])
        return flash_redirect(back_url, message, "success" if status == "started" else "error")

    @app.post("/buildings/{building_id}/toggle-service")
    def toggle_service_route(request: Request, building_id: int, _: None = Depends(require_login)):
        back_url = f"/buildings/{building_id}"
        result, redirect = run_action(
            request, back_url, "toggle-service", lambda: get_client(request).toggle_service(building_id)
        )
        if redirect:
            return redirect
        return report_action_result(
            request, back_url, "toggle-service", result,
            f"Building {building_id} is now {'in service' if result.enabled else 'out of service'}.",
        )

    @app.post("/buildings/{building_id}/hire")
    def hire_route(request: Request, building_id: int, days: int = Form(...), _: None = Depends(require_login)):
        back_url = f"/buildings/{building_id}"
        result, redirect = run_action(
            request, back_url, "hire", lambda: get_client(request).hire(building_id, days)
        )
        if redirect:
            return redirect
        return report_action_result(
            request, back_url, "hire", result,
            f"Started a {days}-day recruiting phase for building {building_id}. "
            "New personnel arrive later, not immediately.",
        )

    @app.post("/buildings/{building_id}/dispatch")
    def set_dispatch_center_route(
        request: Request, building_id: int, leitstelle_id: int = Form(...), _: None = Depends(require_login)
    ):
        back_url = f"/buildings/{building_id}"
        result, redirect = run_action(
            request, back_url, "set-dispatch-center",
            lambda: get_client(request).set_dispatch_center(building_id, leitstelle_id),
        )
        if redirect:
            return redirect
        return report_action_result(
            request, back_url, "set-dispatch-center", result,
            f"Building {building_id} is now assigned to dispatch center {leitstelle_id}.",
        )

    # --------------------------------------------------------------- expand

    @app.get("/buildings/{building_id}/expand/confirm", response_class=HTMLResponse)
    def expand_confirm(request: Request, building_id: int, level: int, _: None = Depends(require_login)):
        back_url = f"/buildings/{building_id}"
        prices, redirect = run_action(
            request, back_url, "expand", lambda: get_client(request).get_expand_prices(building_id)
        )
        if redirect:
            return redirect
        price = prices.get(level)
        if price is None:
            return flash_redirect(back_url, f"No expand option for level {level} anymore — refresh and retry.", "error")
        return templates.TemplateResponse(
            request, "expand_confirm.html", {"building_id": building_id, "level": level, "price": price}
        )

    @app.post("/buildings/{building_id}/expand")
    def expand_execute(request: Request, building_id: int, level: int = Form(...), _: None = Depends(require_login)):
        back_url = f"/buildings/{building_id}"
        result, redirect = run_action(
            request, back_url, "expand", lambda: get_client(request).expand_building(building_id, level)
        )
        if redirect:
            return redirect
        return report_action_result(
            request, back_url, "expand", result,
            f"Expanded building {building_id} to level {result.new_level}, cost {result.price:,} credits.",
        )

    # ----------------------------------------------------------- vehicles

    @app.get("/buildings/{building_id}/vehicles/new/confirm", response_class=HTMLResponse)
    def buy_vehicle_confirm(request: Request, building_id: int, vehicle_type: int, _: None = Depends(require_login)):
        back_url = f"/buildings/{building_id}"
        options, redirect = run_action(
            request, back_url, "buy-vehicle", lambda: get_client(request).get_vehicle_purchase_options(building_id)
        )
        if redirect:
            return redirect
        option = options.get(vehicle_type)
        if option is None:
            return flash_redirect(back_url, f"No purchase option for vehicle_type {vehicle_type} anymore — refresh and retry.", "error")
        return templates.TemplateResponse(
            request, "buy_vehicle_confirm.html", {"building_id": building_id, "option": option}
        )

    @app.post("/buildings/{building_id}/vehicles/new")
    def buy_vehicle_execute(
        request: Request, building_id: int, vehicle_type: int = Form(...), _: None = Depends(require_login)
    ):
        back_url = f"/buildings/{building_id}"
        result, redirect = run_action(
            request, back_url, "buy-vehicle", lambda: get_client(request).buy_vehicle(building_id, vehicle_type)
        )
        if redirect:
            return redirect
        vehicle_id = result.vehicle["id"] if result.vehicle else "?"
        return report_action_result(
            request, back_url, "buy-vehicle", result,
            f"Bought a vehicle for building {building_id} — vehicle id {vehicle_id}, cost {result.price:,} credits.",
        )

    # --------------------------------------------------------- personnel

    @app.get("/vehicles/{vehicle_id}/zuweisung", response_class=HTMLResponse)
    def zuweisung(request: Request, vehicle_id: int, _: None = Depends(require_login)):
        client = get_client(request)
        try:
            vehicles = client.get_vehicles()
            vehicle = next((v for v in vehicles if v["id"] == vehicle_id), None)
            if vehicle is None:
                return error_page(request, f"No vehicle with id {vehicle_id} found in /api/vehicles.")
            roster = client.get_personnel_roster(vehicle["building_id"])
        except SessionExpiredError as exc:
            return error_page(request, f"Authentication failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error loading roster for vehicle %s", vehicle_id)
            return error_page(request, f"Unexpected error: {exc}")

        return templates.TemplateResponse(
            request,
            "zuweisung.html",
            {
                "vehicle": vehicle,
                "roster": roster,
                **flash_context(request),
            },
        )

    @app.post("/vehicles/{vehicle_id}/zuweisungDo/{personal_id}")
    def assign_personnel_route(
        request: Request, vehicle_id: int, personal_id: int, _: None = Depends(require_login)
    ):
        back_url = f"/vehicles/{vehicle_id}/zuweisung"
        result, redirect = run_action(
            request, back_url, "assign-personnel",
            lambda: get_client(request).assign_personnel(vehicle_id, personal_id),
        )
        if redirect:
            return redirect
        return report_action_result(
            request, back_url, "assign-personnel", result,
            f"Vehicle {vehicle_id} now has {result.assigned_personnel_count} assigned crew.",
        )

    # ------------------------------------------------------------- presets

    @app.get("/presets", response_class=HTMLResponse)
    def presets_list(request: Request, _: None = Depends(require_login)):
        with db_session(request) as db:
            presets = {p.building_type: p for p in list_presets(db)}
        return templates.TemplateResponse(
            request,
            "presets.html",
            {"presets": presets, "BUILDING_TYPES": BUILDING_TYPES, **flash_context(request)},
        )

    @app.get("/presets/{building_type}", response_class=HTMLResponse)
    def preset_edit(request: Request, building_type: int, _: None = Depends(require_login)):
        with db_session(request) as db:
            preset = get_preset(db, building_type)
            # A representative building of this type (if one's been synced) so the vehicle
            # catalog can be shown with real names/prices instead of asking for raw ids blind.
            example_building = db.query(Building).filter_by(building_type=building_type).first()
            # The account's synced dispatch centers (building_type 1), so the preset can offer a
            # dropdown to auto-assign new stations rather than asking for a raw leitstelle id.
            dispatch_centers = [
                {"id": b.id, "caption": b.caption}
                for b in db.query(Building).filter_by(building_type=1).order_by(Building.caption).all()
            ]
        vehicle_catalog = None
        if example_building is not None:
            try:
                vehicle_catalog = sorted(
                    get_client(request).get_vehicle_purchase_options(example_building.id).values(),
                    key=lambda o: o.vehicle_type_id,
                )
            except Exception:  # noqa: BLE001 - catalog is a nice-to-have, not required to edit
                logger.exception("Could not load vehicle catalog for building_type %s", building_type)

        return templates.TemplateResponse(
            request,
            "preset_edit.html",
            {
                "building_type": building_type,
                "building_type_name": BUILDING_TYPES.get(building_type, f"Type {building_type}"),
                "preset": preset,
                "vehicles": parse_vehicles_json(preset.vehicles_json) if preset else [],
                "vehicle_catalog": vehicle_catalog,
                "example_building": example_building,
                "dispatch_centers": dispatch_centers,
                **flash_context(request),
            },
        )

    @app.post("/presets/{building_type}")
    async def preset_save(request: Request, building_type: int, _: None = Depends(require_login)):
        form = await request.form()
        target_level_raw = (form.get("target_level") or "").strip()
        service_state = form.get("service_state") or ""  # "" | "on" | "off"
        hire_days_raw = form.get("hire_days") or ""  # "1" | "2" | "3" - "auto" isn't implemented yet
        dispatch_raw = (form.get("dispatch_center_id") or "").strip()  # "" = don't manage dispatch

        vehicles = []
        for vt_raw, count_raw, crew_raw in zip(
            form.getlist("vehicle_type_id"), form.getlist("vehicle_count"), form.getlist("vehicle_personnel")
        ):
            vt_raw, count_raw, crew_raw = vt_raw.strip(), count_raw.strip(), crew_raw.strip()
            if vt_raw and count_raw and int(count_raw) > 0:
                vehicles.append({
                    "vehicle_type_id": int(vt_raw),
                    "count": int(count_raw),
                    "personnel_per_vehicle": int(crew_raw) if crew_raw else 0,
                })

        with db_session(request) as db:
            save_preset(
                db,
                building_type,
                target_level=int(target_level_raw) if target_level_raw else None,
                manage_service=service_state != "",
                target_enabled=service_state != "off",
                hire_days=int(hire_days_raw) if hire_days_raw in ("1", "2", "3") else None,
                vehicles=vehicles,
                dispatch_center_id=int(dispatch_raw) if dispatch_raw else None,
            )
        type_name = BUILDING_TYPES.get(building_type, f"Type {building_type}")
        return flash_redirect("/presets", f"Saved preset for {type_name}.")

    # ---------------------------------------------------------------- config

    @app.get("/config", response_class=HTMLResponse)
    def config_edit(request: Request, _: None = Depends(require_login)):
        raw = load_raw_config(request.app.state.config_file)
        mission_chief = raw.get("mission_chief", {})
        regions = [_region_to_row(r) for r in raw.get("regions", [])]
        building_types = [
            {"poi_type": poi_type, **cfg} for poi_type, cfg in (raw.get("building_types") or {}).items()
        ]
        dedupe = raw.get("dedupe", {})
        naming = raw.get("naming", {})
        budget = raw.get("budget", {})
        rlm_cache = raw.get("rlm_cache", {})
        rate_limiting = raw.get("rate_limiting", {})

        env_values = load_config(request.app.state.env_file)

        return templates.TemplateResponse(
            request,
            "config_edit.html",
            {
                "config_exists": bool(raw),
                "game_world": mission_chief.get("game_world", ""),
                "base_url": mission_chief.get("base_url", ""),
                "regions": regions,
                "building_types": building_types,
                "dedupe_radius_m": dedupe.get("radius_m", 150),
                "naming_template": naming.get("template", "{poi_name}"),
                "max_credits_per_run": budget.get("max_credits_per_run"),
                "credit_reserve": budget.get("credit_reserve", 0),
                "rlm_cache_ttl_hours": rlm_cache.get("ttl_hours", 24),
                "rlm_cache_dir": rlm_cache.get("cache_dir", ".rlm_cache"),
                "rate_limit_min_delay": rate_limiting.get("min_delay_seconds", 0.3),
                "rate_limit_max_delay": rate_limiting.get("max_delay_seconds", 0.8),
                "mc_auth_mode": env_values.get("MC_AUTH_MODE", "cookie"),
                "mc_username": env_values.get("MC_USERNAME", ""),
                "mc_password_set": bool(env_values.get("MC_PASSWORD")),
                "mc_session_cookie_set": bool(env_values.get("MC_SESSION_COOKIE")),
                "mc_base_url": env_values.get("MC_BASE_URL", "https://www.missionchief.com"),
                **flash_context(request),
            },
        )

    def _form_value(form, key: str, i: int) -> str:
        values = form.getlist(key)
        return values[i].strip() if i < len(values) else ""

    def _required_float(form, key: str, i: int, region_name: str, field_label: str) -> float:
        raw = _form_value(form, key, i)
        if not raw:
            raise ConfigFormError(f"Region '{region_name}': {field_label} is required for this mode.")
        try:
            return float(raw)
        except ValueError:
            raise ConfigFormError(f"Region '{region_name}': {field_label} must be a number, got {raw!r}.")

    @app.post("/config")
    async def config_save(request: Request, _: None = Depends(require_login)):
        form = await request.form()

        try:
            regions = []
            for i in range(len(form.getlist("region_name"))):
                name = _form_value(form, "region_name", i)
                if not name:
                    continue
                mode = _form_value(form, "region_mode", i)
                region: dict = {"name": name}
                if mode == "bbox":
                    region["bbox"] = {
                        "north": _required_float(form, "region_north", i, name, "North"),
                        "south": _required_float(form, "region_south", i, name, "South"),
                        "east": _required_float(form, "region_east", i, name, "East"),
                        "west": _required_float(form, "region_west", i, name, "West"),
                    }
                elif mode == "city":
                    city = _form_value(form, "region_city", i)
                    if not city:
                        raise ConfigFormError(f"Region '{name}': City is required for city mode.")
                    region["city"] = city
                    region["radius_km"] = _required_float(form, "region_radius_km", i, name, "Radius km")
                elif mode == "center":
                    region["center"] = {
                        "lat": _required_float(form, "region_center_lat", i, name, "Center lat"),
                        "lng": _required_float(form, "region_center_lng", i, name, "Center lng"),
                    }
                    region["radius_km"] = _required_float(form, "region_radius_km", i, name, "Radius km")
                else:
                    raise ConfigFormError(f"Region '{name}': unknown mode {mode!r}.")
                regions.append(region)

            building_types = {}
            for poi_type, bt_raw, max_raw in zip(
                form.getlist("bt_poi_type"), form.getlist("bt_building_type"), form.getlist("bt_max_per_run")
            ):
                poi_type = poi_type.strip()
                bt_raw = bt_raw.strip()
                max_raw = max_raw.strip()
                if not poi_type or not bt_raw:
                    continue
                try:
                    entry = {"building_type": int(bt_raw)}
                    if max_raw:
                        entry["max_per_run"] = int(max_raw)
                except ValueError:
                    raise ConfigFormError(f"Building type '{poi_type}': building_type/max_per_run must be whole numbers.")
                building_types[poi_type] = entry

            max_credits_raw = (form.get("max_credits_per_run") or "").strip()
            try:
                data = {
                    "mission_chief": {
                        "game_world": (form.get("game_world") or "").strip(),
                        "base_url": (form.get("base_url") or "").strip() or "https://www.missionchief.com",
                    },
                    "regions": regions,
                    "building_types": building_types,
                    "dedupe": {"radius_m": float(form.get("dedupe_radius_m") or 150)},
                    "naming": {"template": form.get("naming_template") or "{poi_name}"},
                    "budget": {
                        "max_credits_per_run": int(max_credits_raw) if max_credits_raw else None,
                        "credit_reserve": int(form.get("credit_reserve") or 0),
                    },
                    "rlm_cache": {
                        "ttl_hours": float(form.get("rlm_cache_ttl_hours") or 24),
                        "cache_dir": form.get("rlm_cache_dir") or ".rlm_cache",
                    },
                    "rate_limiting": {
                        "min_delay_seconds": float(form.get("rate_limit_min_delay") or 0.3),
                        "max_delay_seconds": float(form.get("rate_limit_max_delay") or 0.8),
                    },
                }
            except ValueError as exc:
                raise ConfigFormError(f"Invalid number in one of the settings fields: {exc}")
        except ConfigFormError as exc:
            return flash_redirect("/config", str(exc), "error")

        save_raw_config(request.app.state.config_file, data)
        # Drop the cached client so edited rate-limiting delays take effect on the next request
        # instead of sticking with whatever was read when the client was first built.
        request.app.state.mc_client = None

        # MissionChief account settings live in .env, not config.yaml (see auth.py) - blank
        # fields mean "keep the current value", so they're simply omitted from the update rather
        # than written as empty (which would wipe out a saved password/session cookie).
        env_updates = {}
        if form.get("mc_auth_mode"):
            env_updates["MC_AUTH_MODE"] = form["mc_auth_mode"]
        if (form.get("mc_username") or "").strip():
            env_updates["MC_USERNAME"] = form["mc_username"].strip()
        if (form.get("mc_password") or "").strip():
            env_updates["MC_PASSWORD"] = form["mc_password"].strip()
        if (form.get("mc_session_cookie") or "").strip():
            env_updates["MC_SESSION_COOKIE"] = form["mc_session_cookie"].strip()
        if (form.get("mc_base_url") or "").strip():
            env_updates["MC_BASE_URL"] = form["mc_base_url"].strip()
        if env_updates:
            update_env_file(request.app.state.env_file, env_updates)
            # Force the next request to rebuild the session/client with the new credentials,
            # instead of continuing to use whatever was cached at server startup.
            request.app.state.auth_config = AuthConfig.from_env(request.app.state.env_file)
            request.app.state.mc_client = None

        return flash_redirect("/config", "Saved config.yaml.")

    # ---------------------------------------------------------------- plan

    @app.get("/plan", response_class=HTMLResponse)
    def plan_view(request: Request, _: None = Depends(require_login)):
        plan_file = Path(request.app.state.plan_path)
        if not plan_file.exists():
            return templates.TemplateResponse(
                request,
                "plan.html",
                {
                    "entries": [], "plan_missing": True,
                    "plan_generation_in_progress": request.app.state.plan_generation_in_progress,
                    "plan_generation_result": request.app.state.plan_generation_result,
                    "plan_run_in_progress": request.app.state.plan_run_in_progress,
                    "plan_run_result": request.app.state.plan_run_result,
                    **flash_context(request),
                },
            )
        data = json.loads(plan_file.read_text())
        entries = data.get("to_build", [])
        with db_session(request) as db:
            pending = [e for e in entries if has_completed_action(db, "build", e["poi_id"]) is None]
            existing = db.query(Building).all()

        map_data = {
            "toBuild": [
                {
                    "lat": e["latitude"], "lng": e["longitude"], "name": e["name"],
                    "type": e["building_type_name"],
                    "cost": e.get("estimated_cost"), "poiId": e["poi_id"],
                }
                for e in pending
            ],
            "existing": [
                {
                    "lat": b.latitude, "lng": b.longitude, "name": b.caption,
                    "type": BUILDING_TYPES.get(b.building_type, f"Type {b.building_type}"),
                }
                for b in existing
            ],
        }
        # Breakdown of where candidates went, so an empty/short plan is self-explaining rather
        # than a mystery: how many RLM candidates each region/poi_type returned, and how many
        # were dropped as duplicates / over a per-type cap / over budget.
        budget_skips = data.get("skipped_budget", [])
        budget_costs = [s["estimated_cost"] for s in budget_skips if s.get("estimated_cost") is not None]
        diagnostics = {
            "total_candidates": data.get("total_candidates"),
            "candidate_breakdown": data.get("candidate_breakdown", []),
            "skipped_duplicates": len(data.get("skipped_duplicates", [])),
            "skipped_capped": len(data.get("skipped_capped", [])),
            "skipped_budget": len(budget_skips),
            # For the "everything's over budget" hint: the configured cap and the cheapest
            # candidate that got rejected, so the user knows what to raise the budget to.
            "max_credits_per_run": (data.get("budget") or {}).get("max_credits_per_run"),
            "cheapest_over_budget": min(budget_costs) if budget_costs else None,
        }
        return templates.TemplateResponse(
            request,
            "plan.html",
            {
                "entries": pending,
                "already_done": len(entries) - len(pending),
                "total_estimated_cost": sum(e.get("estimated_cost") or 0 for e in pending),
                "plan_missing": False,
                "map_data_json": _safe_json_for_script(map_data),
                "plan_generation_in_progress": request.app.state.plan_generation_in_progress,
                "plan_generation_result": request.app.state.plan_generation_result,
                "plan_run_in_progress": request.app.state.plan_run_in_progress,
                "plan_run_result": request.app.state.plan_run_result,
                "diagnostics": diagnostics,
                **flash_context(request),
            },
        )

    @app.post("/plan/generate")
    def plan_generate_route(request: Request, _: None = Depends(require_login)):
        status, message = start_plan_generation(request)
        return flash_redirect("/plan", message, "success" if status == "started" else "error")

    @app.get("/plan/run/confirm", response_class=HTMLResponse)
    def plan_run_confirm(request: Request, _: None = Depends(require_login)):
        plan_file = Path(request.app.state.plan_path)
        if not plan_file.exists():
            return flash_redirect("/plan", "No plan.json yet — generate a plan first.", "error")
        data = json.loads(plan_file.read_text())
        entries = data.get("to_build", [])
        with db_session(request) as db:
            pending = [e for e in entries if has_completed_action(db, "build", e["poi_id"]) is None]
            preset_types = {p.building_type for p in list_presets(db)}
        # Count how many pending stations have a preset, so the confirm page can warn if a lot of
        # them will build "bare" (level 1, no vehicles) because no preset covers their type.
        with_preset = sum(1 for e in pending if e["building_type"] in preset_types)
        return templates.TemplateResponse(
            request,
            "plan_run_confirm.html",
            {
                "pending": pending,
                "count": len(pending),
                "total_estimated_cost": sum(e.get("estimated_cost") or 0 for e in pending),
                "max_credits_per_run": (data.get("budget") or {}).get("max_credits_per_run"),
                "with_preset": with_preset,
                "without_preset": len(pending) - with_preset,
            },
        )

    @app.post("/plan/run")
    def plan_run_execute(request: Request, _: None = Depends(require_login)):
        status, message = start_plan_execution(request)
        return flash_redirect("/plan", message, "success" if status == "started" else "error")

    @app.get("/plan/build/confirm", response_class=HTMLResponse)
    def plan_build_confirm(request: Request, poi_id: int, _: None = Depends(require_login)):
        entries = json.loads(Path(request.app.state.plan_path).read_text()).get("to_build", [])
        entry = next((e for e in entries if e["poi_id"] == poi_id), None)
        if entry is None:
            return flash_redirect("/plan", f"No to_build entry with poi_id={poi_id} in the current plan.", "error")
        return templates.TemplateResponse(request, "plan_build_confirm.html", {"entry": entry})

    @app.post("/plan/build")
    def plan_build_execute(request: Request, poi_id: int = Form(...), _: None = Depends(require_login)):
        entries = json.loads(Path(request.app.state.plan_path).read_text()).get("to_build", [])
        entry = next((e for e in entries if e["poi_id"] == poi_id), None)
        if entry is None:
            return flash_redirect("/plan", f"No to_build entry with poi_id={poi_id} in the current plan.", "error")

        with db_session(request) as db:
            if has_completed_action(db, "build", poi_id):
                return flash_redirect("/plan", f"{entry['name']!r} is already built — skipping.", "error")

        result, redirect = run_action(
            request, "/plan", "build",
            lambda: get_client(request).create_building(
                building_type=entry["building_type"],
                name=entry["name"],
                latitude=entry["latitude"],
                longitude=entry["longitude"],
            ),
        )
        if redirect:
            return redirect

        preset_note = ""
        if result.success:
            with db_session(request) as db:
                record_completed_action(
                    db, action_type="build", poi_id=poi_id, building_id=result.building["id"],
                    building_type=entry["building_type"], name=entry["name"], cost=result.price,
                )
            status, message = start_preset_application(request, result.building["id"], entry["building_type"])
            if status == "started":
                preset_note = " Preset application started in the background — see this station's page for progress."
            elif status == "error":
                preset_note = f" (Could not start its preset: {message})"
            elif status == "no_preset":
                # Surface this explicitly - a silent no-op here is exactly why a freshly-built
                # station looks like the preset "did nothing" (no expand, no vehicles).
                preset_note = (
                    f" No preset is configured for this building type ({entry['building_type']}), so "
                    "nothing else was applied — set one on the Presets page to auto-expand/buy vehicles."
                )

        return report_action_result(
            request, "/plan", "build", result,
            f"Built {entry['name']!r} — building id {result.building['id']}, cost {result.price:,} credits."
            f"{preset_note}",
        )

    return app
