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
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..auth import AuthConfig, SessionExpiredError, build_session
from ..constants import BUILDING_TYPES
from ..mc_client import MissionChiefClient, summarize_html_for_log
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
from ..presets import apply_preset, parse_vehicles_json
from ..web_config import WebConfig

logger = logging.getLogger("mc_autobuilder.web")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


class NotAuthenticated(Exception):
    pass


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
    app.state.session_factory = get_session_factory(engine)
    app.state.plan_path = plan_path
    app.state.config_file = config_file
    app.state.mc_client = None
    # building_ids with a preset application currently running in a background thread - guards
    # against double-triggering (e.g. double-clicking "Apply preset") while one's already going.
    app.state.presets_in_progress = set()

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
            request.app.state.mc_client = MissionChiefClient(session, request.app.state.auth_config.base_url)
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
                **flash_context(request),
            },
        )

    @app.post("/presets/{building_type}")
    async def preset_save(request: Request, building_type: int, _: None = Depends(require_login)):
        form = await request.form()
        hire_days_raw = (form.get("hire_days") or "").strip()

        vehicles = []
        for vt_raw, count_raw in zip(form.getlist("vehicle_type_id"), form.getlist("vehicle_count")):
            vt_raw, count_raw = vt_raw.strip(), count_raw.strip()
            if vt_raw and count_raw and int(count_raw) > 0:
                vehicles.append({"vehicle_type_id": int(vt_raw), "count": int(count_raw)})

        with db_session(request) as db:
            save_preset(
                db,
                building_type,
                max_level=form.get("max_level") == "on",
                manage_service=form.get("manage_service") == "on",
                target_enabled=form.get("target_enabled") == "on",
                hire_days=int(hire_days_raw) if hire_days_raw else None,
                vehicles=vehicles,
            )
        type_name = BUILDING_TYPES.get(building_type, f"Type {building_type}")
        return flash_redirect("/presets", f"Saved preset for {type_name}.")

    # ---------------------------------------------------------------- plan

    @app.get("/plan", response_class=HTMLResponse)
    def plan_view(request: Request, _: None = Depends(require_login)):
        plan_file = Path(request.app.state.plan_path)
        if not plan_file.exists():
            return templates.TemplateResponse(
                request, "plan.html", {"entries": [], "plan_missing": True, **flash_context(request)}
            )
        data = json.loads(plan_file.read_text())
        entries = data.get("to_build", [])
        with db_session(request) as db:
            pending = [e for e in entries if has_completed_action(db, "build", e["poi_id"]) is None]
        return templates.TemplateResponse(
            request,
            "plan.html",
            {
                "entries": pending,
                "already_done": len(entries) - len(pending),
                "total_estimated_cost": sum(e.get("estimated_cost") or 0 for e in pending),
                "plan_missing": False,
                **flash_context(request),
            },
        )

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
                preset_note = " Preset application started in the background."
            elif status == "error":
                preset_note = f" (Could not start its preset: {message})"
            # "no_preset"/"already_running" need no note - nothing configured, or nothing to add.

        return report_action_result(
            request, "/plan", "build", result,
            f"Built {entry['name']!r} — building id {result.building['id']}, cost {result.price:,} credits."
            f"{preset_note}",
        )

    return app
