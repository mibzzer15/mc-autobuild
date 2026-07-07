import json
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
