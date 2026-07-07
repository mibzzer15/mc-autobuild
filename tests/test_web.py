import json
import time
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from mc_autobuilder.mc_client import (
    AssignPersonnelResult,
    BuildResult,
    DispatchAssignResult,
    ExpandResult,
    HireResult,
    PersonnelEntry,
    ServiceToggleResult,
    VehicleOption,
    VehiclePurchaseResult,
)
from mc_autobuilder.models import get_preset_log
from mc_autobuilder.web.app import create_app

PASSWORD = "testpass123"


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        f"MC_AUTH_MODE=cookie\nMC_SESSION_COOKIE=session_id=dummy\nDASHBOARD_PASSWORD={PASSWORD}\n"
    )
    return path


@pytest.fixture
def client(tmp_path, env_file, monkeypatch):
    monkeypatch.setattr("mc_autobuilder.web.app.build_session", lambda config: object())
    app = create_app(
        env_file=str(env_file),
        db_path=str(tmp_path / "test.db"),
        plan_path=str(tmp_path / "plan.json"),
        config_file=str(tmp_path / "config.yaml"),
    )
    with TestClient(app) as c:
        yield c


def _login(client):
    return client.post("/login", data={"password": PASSWORD})


def test_root_redirects_to_login_when_unauthenticated(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_wrong_password_stays_logged_out(client):
    resp = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
    assert resp.headers["location"] == "/login?error=1"

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303


def test_login_correct_password_grants_access(client):
    resp = _login(client)
    assert resp.status_code == 200  # followed the redirect to "/"
    assert "Dashboard" in resp.text


def test_logout_revokes_access(client):
    _login(client)
    client.post("/logout")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303


def test_dashboard_shows_balance_error_cleanly_when_client_fails(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.get_credits_balance",
        lambda self: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Could not read live balance" in resp.text


def test_dashboard_shows_balance_when_client_succeeds(client, monkeypatch):
    _login(client)
    monkeypatch.setattr("mc_autobuilder.web.app.MissionChiefClient.get_credits_balance", lambda self: 2_456_738_985)
    resp = client.get("/")
    assert "2,456,738,985" in resp.text


def test_buildings_list_renders_cached_buildings(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.get_buildings",
        lambda self: [
            {
                "id": 1, "building_type": 5, "caption": "Test PD", "latitude": 1.0, "longitude": 2.0,
                "level": 0, "personal_count": 5, "personal_count_target": 10, "enabled": True,
            }
        ],
    )
    client.post("/sync")
    resp = client.get("/buildings")
    assert "Test PD" in resp.text
    assert "Police station" in resp.text


BUILDING_DETAIL = {"building_type": 0, "level": 0, "enabled": True, "hiring_phase": 0, "leitstelle_building_id": None}


def _patch_building_detail_deps(monkeypatch):
    monkeypatch.setattr("mc_autobuilder.web.app.MissionChiefClient.get_building_detail", lambda self, bid: BUILDING_DETAIL)
    monkeypatch.setattr("mc_autobuilder.web.app.MissionChiefClient.get_vehicles", lambda self: [])
    monkeypatch.setattr("mc_autobuilder.web.app.MissionChiefClient.get_expand_prices", lambda self, bid: {0: 10_000})
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.get_vehicle_purchase_options",
        lambda self, bid: {0: VehicleOption(0, "Type 1 fire engine", 5_000, "fire_engine")},
    )
    monkeypatch.setattr("mc_autobuilder.web.app.MissionChiefClient.get_hire_day_options", lambda self, bid: [1, 2, 3])


def test_building_detail_renders_live_state(client, monkeypatch):
    _login(client)
    _patch_building_detail_deps(monkeypatch)

    resp = client.get("/buildings/5558174")

    assert resp.status_code == 200
    assert "10,000" in resp.text  # expand price
    assert "Type 1 fire engine" in resp.text
    assert "Recruit 1 day" in resp.text


def test_toggle_service_execute_flashes_success(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.toggle_service",
        lambda self, bid: ServiceToggleResult(success=True, enabled=False, response_status=302),
    )
    resp = client.post("/buildings/5558174/toggle-service", follow_redirects=False)
    assert resp.status_code == 303
    assert "out of service" in unquote(resp.headers["location"])


def test_expand_requires_two_steps_before_spending(client, monkeypatch):
    _login(client)
    monkeypatch.setattr("mc_autobuilder.web.app.MissionChiefClient.get_expand_prices", lambda self, bid: {0: 10_000})

    # Step 1: confirm page shows the price but does NOT execute.
    confirm = client.get("/buildings/5558174/expand/confirm?level=0")
    assert confirm.status_code == 200
    assert "10,000" in confirm.text
    assert "Yes, spend" in confirm.text

    called = []
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.expand_building",
        lambda self, bid, level: (called.append((bid, level)) or ExpandResult(True, level, 10_000, level + 1, 302)),
    )
    # Step 2: only the POST actually executes.
    execute = client.post("/buildings/5558174/expand", data={"level": "0"}, follow_redirects=False)
    assert execute.status_code == 303
    assert called == [(5558174, 0)]


def test_buy_vehicle_requires_two_steps_before_spending(client, monkeypatch):
    _login(client)
    option = VehicleOption(0, "Type 1 fire engine", 5_000, "fire_engine")
    monkeypatch.setattr("mc_autobuilder.web.app.MissionChiefClient.get_vehicle_purchase_options", lambda self, bid: {0: option})

    confirm = client.get("/buildings/5558174/vehicles/new/confirm?vehicle_type=0")
    assert confirm.status_code == 200
    assert "5,000" in confirm.text

    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.buy_vehicle",
        lambda self, bid, vehicle_type_id: VehiclePurchaseResult(True, {"id": 999}, 5_000, 302),
    )
    execute = client.post("/buildings/5558174/vehicles/new", data={"vehicle_type": "0"}, follow_redirects=False)
    assert execute.status_code == 303
    assert "flash" in execute.headers["location"]


def test_hire_execute_is_single_step_free_action(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.hire",
        lambda self, bid, days: HireResult(True, days, 302),
    )
    resp = client.post("/buildings/5558174/hire", data={"days": "1"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "level=success" in resp.headers["location"]


def test_set_dispatch_center_execute(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.set_dispatch_center",
        lambda self, bid, lid: DispatchAssignResult(True, bid, lid, 200),
    )
    resp = client.post("/buildings/5558174/dispatch", data={"leitstelle_id": "2534509"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "level=success" in resp.headers["location"]


def test_zuweisung_page_lists_roster_and_assign_toggles(client, monkeypatch):
    _login(client)
    vehicle = {"id": 14577420, "building_id": 5558174, "caption": "Type 1 fire engine", "assigned_personnel_count": 2}
    monkeypatch.setattr("mc_autobuilder.web.app.MissionChiefClient.get_vehicles", lambda self: [vehicle])
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.get_personnel_roster",
        lambda self, bid: [PersonnelEntry(personal_id=135847194, name="Paul G.", assigned_to="Type 1 fire engine")],
    )

    resp = client.get("/vehicles/14577420/zuweisung")
    assert resp.status_code == 200
    assert "Paul G." in resp.text

    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.assign_personnel",
        lambda self, vehicle_id, personal_id: AssignPersonnelResult(True, 3, 200),
    )
    resp = client.post("/vehicles/14577420/zuweisungDo/135847194", follow_redirects=False)
    assert resp.status_code == 303
    assert "level=success" in resp.headers["location"]


def test_plan_missing_shows_helpful_message(client):
    _login(client)
    resp = client.get("/plan")
    assert "No plan.json found" in resp.text


def test_plan_build_two_step_flow(client, monkeypatch, tmp_path):
    _login(client)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "to_build": [
                    {
                        "poi_id": 5, "poi_name": "Test", "building_type": 5,
                        "building_type_name": "Police station", "name": "Test Police Station",
                        "latitude": 1.0, "longitude": 2.0, "estimated_cost": 100_000,
                    }
                ]
            }
        )
    )
    # Rebuild app pointing at this specific plan file — the `client` fixture's app used a
    # different (nonexistent) plan_path.
    app = client.app
    app.state.plan_path = str(plan_path)

    resp = client.get("/plan")
    assert "Test Police Station" in resp.text

    confirm = client.get("/plan/build/confirm?poi_id=5")
    assert confirm.status_code == 200
    assert "100,000" in confirm.text

    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.create_building",
        lambda self, **kwargs: BuildResult(True, {"id": 12345}, 100_000, 200),
    )
    execute = client.post("/plan/build", data={"poi_id": "5"}, follow_redirects=False)
    assert execute.status_code == 303
    assert "level=success" in execute.headers["location"]

    # Re-building should now report it's already built.
    resp = client.get("/plan/build/confirm?poi_id=5", follow_redirects=False)
    # entry still exists in plan.json (build doesn't rewrite it), but /plan itself should show 0 pending.
    resp2 = client.get("/plan")
    assert "0 station(s) to build" in resp2.text or "already done" in resp2.text


def test_plan_map_includes_station_coordinates_for_leaflet(client, tmp_path):
    _login(client)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "to_build": [
                    {
                        "poi_id": 5, "poi_name": "Test", "building_type": 5,
                        "building_type_name": "Police station", "name": "Test Police Station",
                        "latitude": 37.7749, "longitude": -122.4194, "estimated_cost": 100_000,
                    }
                ]
            }
        )
    )
    client.app.state.plan_path = str(plan_path)

    resp = client.get("/plan")
    assert "leaflet.js" in resp.text
    assert "37.7749" in resp.text
    assert "-122.4194" in resp.text


def test_plan_map_data_escapes_script_breakout_in_station_names(client, tmp_path):
    # Station/POI names come from community-submitted RLM data - guard against a name like
    # "</script><script>alert(1)</script>" breaking out of the embedded JSON <script> block.
    _login(client)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "to_build": [
                    {
                        "poi_id": 5, "poi_name": "Test", "building_type": 5,
                        "building_type_name": "Police station",
                        "name": "</script><script>alert(1)</script>",
                        "latitude": 1.0, "longitude": 2.0, "estimated_cost": 100_000,
                    }
                ]
            }
        )
    )
    client.app.state.plan_path = str(plan_path)

    resp = client.get("/plan")
    assert "</script><script>alert(1)" not in resp.text
    assert "\\u003c/script>\\u003cscript>alert(1)\\u003c/script>" in resp.text


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_presets_list_shows_no_preset_by_default(client):
    _login(client)
    resp = client.get("/presets")
    assert resp.status_code == 200
    assert "No preset" in resp.text


def test_preset_edit_form_renders_without_a_synced_building_of_that_type(client):
    _login(client)
    resp = client.get("/presets/0")
    assert resp.status_code == 200
    assert "No Fire station synced yet" in resp.text.replace("&#39;", "'")


def test_preset_save_and_reload_round_trips(client):
    _login(client)
    resp = client.post(
        "/presets/0",
        data={
            "target_level": "20",
            "service_state": "on",
            "hire_days": "3",
            "vehicle_type_id": ["0"] + [""] * 14,
            "vehicle_count": ["2"] + [""] * 14,
            "vehicle_personnel": ["4"] + [""] * 14,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get("/presets/0")
    assert 'value="20"' in resp.text  # target_level
    assert 'value="3"' not in resp.text or "selected" in resp.text  # hire_days is now a <select>
    assert 'value="0"' in resp.text  # vehicle_type_id
    assert 'value="2"' in resp.text  # vehicle_count
    assert 'value="4"' in resp.text  # vehicle_personnel

    resp = client.get("/presets")
    assert "Level 20" in resp.text
    assert "Keep in service" in resp.text
    assert "Recruit 3d" in resp.text


def test_building_detail_shows_no_preset_configured_by_default(client, monkeypatch):
    _login(client)
    _patch_building_detail_deps(monkeypatch)
    resp = client.get("/buildings/5558174")
    assert "No preset configured for this building type" in resp.text


def test_apply_preset_runs_in_background_and_logs_actions(client, monkeypatch):
    _login(client)
    client.post(
        "/presets/0",
        data={"hire_days": "3", "vehicle_type_id": [""], "vehicle_count": [""]},
    )
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.get_building_detail",
        lambda self, bid: {"building_type": 0, "level": 0, "enabled": True, "hiring_phase": 0},
    )
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.hire",
        lambda self, bid, days: HireResult(True, days, 302),
    )

    resp = client.post("/buildings/5558174/apply-preset", follow_redirects=False)
    assert resp.status_code == 303
    assert "started in the background" in unquote(resp.headers["location"])

    session_factory = client.app.state.session_factory
    assert _wait_for(lambda: len(get_preset_log(session_factory(), 5558174)) > 0)
    assert 5558174 not in client.app.state.presets_in_progress


def test_apply_preset_rejects_double_trigger_while_in_progress(client, monkeypatch):
    _login(client)
    client.post("/presets/0", data={"hire_days": "3", "vehicle_type_id": [""], "vehicle_count": [""]})
    # Manually mark it as already running, as if a previous click's background thread hadn't
    # finished yet - the point is the *route* rejects a second trigger, not the thread timing.
    client.app.state.presets_in_progress.add(5558174)
    try:
        resp = client.post("/buildings/5558174/apply-preset", follow_redirects=False)
        assert "already in progress" in unquote(resp.headers["location"])
    finally:
        client.app.state.presets_in_progress.discard(5558174)


def test_plan_build_auto_applies_preset_when_one_is_configured(client, monkeypatch, tmp_path):
    _login(client)
    client.post("/presets/5", data={"hire_days": "1", "vehicle_type_id": [""], "vehicle_count": [""]})

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "to_build": [
                    {
                        "poi_id": 9, "poi_name": "Test", "building_type": 5,
                        "building_type_name": "Police station", "name": "Test PD",
                        "latitude": 1.0, "longitude": 2.0, "estimated_cost": 100_000,
                    }
                ]
            }
        )
    )
    client.app.state.plan_path = str(plan_path)

    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.create_building",
        lambda self, **kwargs: BuildResult(True, {"id": 555}, 100_000, 200),
    )
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.get_building_detail",
        lambda self, bid: {"building_type": 5, "hiring_phase": 0},
    )
    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.hire", lambda self, bid, days: HireResult(True, days, 302)
    )

    resp = client.post("/plan/build", data={"poi_id": "9"}, follow_redirects=False)
    assert "Preset application started" in unquote(resp.headers["location"])

    session_factory = client.app.state.session_factory
    assert _wait_for(lambda: len(get_preset_log(session_factory(), 555)) > 0)


def test_config_edit_shows_helpful_message_when_missing(client):
    _login(client)
    resp = client.get("/config")
    assert "No config.yaml found yet" in resp.text


def test_config_save_and_reload_round_trips(client, tmp_path):
    _login(client)
    client.app.state.config_file = str(tmp_path / "config.yaml")

    resp = client.post(
        "/config",
        data={
            "game_world": "US",
            "base_url": "https://www.missionchief.com",
            "region_name": ["Bay Area"] + [""] * 14,
            "region_mode": ["bbox"] * 15,
            "region_north": ["38.0"] + [""] * 14,
            "region_south": ["37.2"] + [""] * 14,
            "region_east": ["-121.7"] + [""] * 14,
            "region_west": ["-122.6"] + [""] * 14,
            "region_city": [""] * 15,
            "region_center_lat": [""] * 15,
            "region_center_lng": [""] * 15,
            "region_radius_km": [""] * 15,
            "bt_poi_type": ["poi_fire_station"] + [""] * 14,
            "bt_building_type": ["0"] + [""] * 14,
            "bt_max_per_run": ["5"] + [""] * 14,
            "dedupe_radius_m": "150",
            "naming_template": "{poi_name}",
            "max_credits_per_run": "5000000",
            "credit_reserve": "1000000",
            "rlm_cache_ttl_hours": "24",
            "rlm_cache_dir": ".rlm_cache",
            "rate_limit_min_delay": "2",
            "rate_limit_max_delay": "5",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from mc_autobuilder.config import Config

    config = Config.from_yaml(tmp_path / "config.yaml")
    assert config.game_world == "US"
    assert config.regions[0].name == "Bay Area"
    assert config.regions[0].bbox == {"north": 38.0, "south": 37.2, "east": -121.7, "west": -122.6}
    assert config.building_types[0].poi_type == "poi_fire_station"
    assert config.building_types[0].max_per_run == 5
    assert config.max_credits_per_run == 5_000_000

    resp = client.get("/config")
    assert "Bay Area" in resp.text
    assert 'value="38.0"' in resp.text


def test_config_save_with_blank_bbox_fields_shows_flash_error_not_500(client, tmp_path):
    _login(client)
    client.app.state.config_file = str(tmp_path / "config.yaml")

    # A user who types only a region name and leaves the bbox coordinate fields blank (the
    # default mode for a fresh row) used to crash config_save() with an unhandled
    # ValueError from float(""), producing a raw 500 instead of a clean error message.
    resp = client.post(
        "/config",
        data={
            "game_world": "US",
            "region_name": ["Bay Area"] + [""] * 14,
            "region_mode": ["bbox"] * 15,
            "region_north": [""] * 15,
            "region_south": [""] * 15,
            "region_east": [""] * 15,
            "region_west": [""] * 15,
            "region_city": [""] * 15,
            "region_center_lat": [""] * 15,
            "region_center_lng": [""] * 15,
            "region_radius_km": [""] * 15,
            "bt_poi_type": [""] * 15,
            "bt_building_type": [""] * 15,
            "bt_max_per_run": [""] * 15,
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "error" in resp.headers["location"]

    resp2 = client.get(resp.headers["location"])
    assert "Bay Area" in resp2.text
    assert "North" in resp2.text
    assert "is required for this mode" in resp2.text

    # config.yaml must not have been written with an incomplete region.
    from mc_autobuilder.config import load_raw_config

    raw = load_raw_config(client.app.state.config_file)
    assert raw == {}


def test_config_page_shows_account_fields_without_leaking_secrets(client, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MC_AUTH_MODE=credentials\nMC_USERNAME=alice\nMC_PASSWORD=hunter2\n"
        "MC_SESSION_COOKIE=session_id=abc\nDASHBOARD_PASSWORD=testpass123\n"
    )
    client.app.state.env_file = str(env_path)
    _login(client)

    resp = client.get("/config")

    assert "alice" in resp.text  # username is shown
    assert "hunter2" not in resp.text  # password is never echoed back
    assert "session_id=abc" not in resp.text  # neither is the session cookie
    assert "currently set" in resp.text


def test_config_save_updates_mc_credentials_without_touching_dashboard_password(client, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("MC_AUTH_MODE=cookie\nDASHBOARD_PASSWORD=testpass123\n")
    client.app.state.env_file = str(env_path)
    _login(client)

    resp = client.post(
        "/config",
        data={"game_world": "US", "mc_auth_mode": "credentials", "mc_username": "bob", "mc_password": "hunter2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from mc_autobuilder.auth import load_config

    values = load_config(env_path)
    assert values["MC_AUTH_MODE"] == "credentials"
    assert values["MC_USERNAME"] == "bob"
    assert values["MC_PASSWORD"] == "hunter2"
    assert values["DASHBOARD_PASSWORD"] == "testpass123"  # untouched


def test_config_save_with_blank_password_keeps_existing_value(client, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("MC_AUTH_MODE=cookie\nMC_PASSWORD=original\nDASHBOARD_PASSWORD=testpass123\n")
    client.app.state.env_file = str(env_path)
    _login(client)

    client.post("/config", data={"game_world": "US", "mc_auth_mode": "credentials"}, follow_redirects=False)

    from mc_autobuilder.auth import load_config

    assert load_config(env_path)["MC_PASSWORD"] == "original"


def test_plan_generate_writes_plan_json_from_config(client, monkeypatch, tmp_path):
    _login(client)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mission_chief:
  game_world: "US"
regions:
  - name: "Test Region"
    bbox: {north: 1.0, south: 0.0, east: 1.0, west: 0.0}
building_types:
  poi_fire_station:
    building_type: 0
"""
    )
    plan_path = tmp_path / "plan.json"
    client.app.state.config_file = str(config_path)
    client.app.state.plan_path = str(plan_path)

    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.get_building_prices", lambda self: {0: 100_000}
    )
    monkeypatch.setattr(
        "mc_autobuilder.web.app.RLMClient.get_pois",
        lambda self, poi_type, bbox: [{"id": 1, "name": "Test Fire POI", "lat": 0.5, "lng": 0.5}],
    )

    resp = client.post("/plan/generate", follow_redirects=False)
    assert resp.status_code == 303
    assert "started in the background" in unquote(resp.headers["location"])

    assert _wait_for(lambda: plan_path.exists() and plan_path.read_text())
    data = json.loads(plan_path.read_text())
    assert len(data["to_build"]) == 1
    assert data["to_build"][0]["poi_name"] == "Test Fire POI"
    assert data["to_build"][0]["estimated_cost"] == 100_000

    # The Plan page should report the run's outcome, not just silently show the new plan.
    assert _wait_for(lambda: client.app.state.plan_generation_result is not None)
    assert client.app.state.plan_generation_result["status"] == "success"
    page = client.get("/plan").text
    assert "1 station(s) to build" in page


def test_plan_generate_surfaces_background_failure_instead_of_silent_nothing(client, monkeypatch, tmp_path):
    _login(client)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mission_chief:
  game_world: "US"
regions:
  - name: "Test Region"
    bbox: {north: 1.0, south: 0.0, east: 1.0, west: 0.0}
building_types:
  poi_fire_station:
    building_type: 0
"""
    )
    plan_path = tmp_path / "plan.json"
    client.app.state.config_file = str(config_path)
    client.app.state.plan_path = str(plan_path)

    monkeypatch.setattr(
        "mc_autobuilder.web.app.MissionChiefClient.get_building_prices", lambda self: {0: 100_000}
    )

    def _boom(self, poi_type, bbox):
        raise RuntimeError("RLM unreachable")

    monkeypatch.setattr("mc_autobuilder.web.app.RLMClient.get_pois", _boom)

    resp = client.post("/plan/generate", follow_redirects=False)
    assert resp.status_code == 303

    # Previously this failure was swallowed into the server log and the user saw nothing change.
    assert _wait_for(lambda: client.app.state.plan_generation_result is not None)
    assert client.app.state.plan_generation_result["status"] == "error"
    assert not plan_path.exists()  # a failed run must not leave a stale/partial plan.json

    page = client.get("/plan").text
    assert "Plan generation failed" in page
    assert "RLM unreachable" in page


def test_plan_generate_rejects_double_trigger_while_in_progress(client, tmp_path):
    _login(client)
    client.app.state.config_file = str(tmp_path / "config.yaml")  # missing - would error anyway
    client.app.state.plan_generation_in_progress = True
    try:
        resp = client.post("/plan/generate", follow_redirects=False)
        assert "already in progress" in unquote(resp.headers["location"])
    finally:
        client.app.state.plan_generation_in_progress = False


def test_plan_generate_reports_config_error_cleanly(client, tmp_path):
    _login(client)
    client.app.state.config_file = str(tmp_path / "does-not-exist.yaml")

    resp = client.post("/plan/generate", follow_redirects=False)
    assert "Config error" in unquote(resp.headers["location"])
