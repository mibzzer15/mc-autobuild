import json

from typer.testing import CliRunner

from mc_autobuilder.auth import SessionExpiredError
from mc_autobuilder.cli import app
from mc_autobuilder.mc_client import (
    AssignPersonnelResult,
    BuildResult,
    ExpandResult,
    HireResult,
    ServiceToggleResult,
    VehicleOption,
    VehiclePurchaseResult,
)

runner = CliRunner()

SAMPLE_PLAN = {
    "to_build": [
        {
            "poi_id": 5,
            "poi_name": "Test Police Station",
            "building_type": 5,
            "building_type_name": "Police station",
            "name": "Test Police Station",
            "latitude": 37.7,
            "longitude": -122.3,
            "estimated_cost": 100_000,
        }
    ]
}

MINIMAL_CONFIG = """
mission_chief:
  game_world: "US"
regions:
  - name: "Bay Area"
    bbox: {north: 38.0, south: 37.2, east: -121.7, west: -122.6}
building_types:
  poi_fire_station:
    building_type: 0
"""

MULTI_PLAN = {
    "to_build": [
        {
            "poi_id": 1, "poi_name": "Station A", "building_type": 5,
            "building_type_name": "Police station", "name": "Station A",
            "latitude": 1.0, "longitude": 2.0, "estimated_cost": 100_000,
        },
        {
            "poi_id": 2, "poi_name": "Station B", "building_type": 5,
            "building_type_name": "Police station", "name": "Station B",
            "latitude": 3.0, "longitude": 4.0, "estimated_cost": 100_000,
        },
        {
            "poi_id": 3, "poi_name": "Station C", "building_type": 5,
            "building_type_name": "Police station", "name": "Station C",
            "latitude": 5.0, "longitude": 6.0, "estimated_cost": 100_000,
        },
    ]
}


def _config_with_budget(credit_reserve=0, max_credits_per_run=None):
    budget_lines = f"budget:\n  credit_reserve: {credit_reserve}\n"
    if max_credits_per_run is not None:
        budget_lines += f"  max_credits_per_run: {max_credits_per_run}\n"
    return MINIMAL_CONFIG + "\n" + budget_lines


def test_plan_reports_session_expiry_cleanly_instead_of_crashing(tmp_path, monkeypatch):
    # Regression test: `plan` used to only catch SessionExpiredError around build_session(),
    # so a session that expired later (e.g. during the live price fetch) produced a raw Python
    # traceback instead of the same clean "Authentication failed: ..." message `sync` gives.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MINIMAL_CONFIG)
    (tmp_path / ".env").write_text("MC_AUTH_MODE=cookie\nMC_SESSION_COOKIE=session_id=dummy\n")

    monkeypatch.setattr("mc_autobuilder.cli.build_session", lambda config: object())

    def fake_get_building_prices(self):
        raise SessionExpiredError("MissionChief returned HTTP 401 — the session has likely expired.")

    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_building_prices", fake_get_building_prices)

    result = runner.invoke(app, ["plan"])

    assert result.exit_code == 1
    assert "Authentication failed" in result.output
    assert "Traceback" not in result.output


def test_build_without_execute_is_a_pure_dry_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(SAMPLE_PLAN))

    result = runner.invoke(app, ["build", "--poi-id", "5"])

    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "Test Police Station" in result.output
    assert "100,000" in result.output


def test_build_missing_plan_file_errors_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["build", "--poi-id", "5"])
    assert result.exit_code == 1
    assert "plan.json" in result.output
    assert "Traceback" not in result.output


def test_build_unknown_poi_id_errors_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(SAMPLE_PLAN))

    result = runner.invoke(app, ["build", "--poi-id", "999"])

    assert result.exit_code == 1
    assert "No to_build entry" in result.output


def test_build_execute_declined_at_confirmation_does_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(SAMPLE_PLAN))

    result = runner.invoke(app, ["build", "--poi-id", "5", "--execute"], input="n\n")

    assert result.exit_code == 0
    assert "Cancelled" in result.output


def test_build_execute_confirmed_records_completed_action(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(SAMPLE_PLAN))
    (tmp_path / ".env").write_text("MC_AUTH_MODE=cookie\nMC_SESSION_COOKIE=session_id=dummy\n")

    monkeypatch.setattr("mc_autobuilder.cli.build_session", lambda config: object())

    def fake_create_building(self, **kwargs):
        return BuildResult(success=True, building={"id": 12345}, price=100_000, response_status=200)

    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.create_building", fake_create_building)

    result = runner.invoke(app, ["build", "--poi-id", "5", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "Built" in result.output
    assert "12345" in result.output

    # Re-running should now be a no-op skip, not a second build.
    result2 = runner.invoke(app, ["build", "--poi-id", "5", "--execute"], input="y\n")
    assert result2.exit_code == 0
    assert "Already built" in result2.output


def test_build_execute_reports_unexpected_error_cleanly_not_as_traceback(tmp_path, monkeypatch):
    # Regression test: found via a real smoke test where a malformed /buildings/new page made
    # create_building raise a plain ValueError, which crashed with a raw traceback instead of a
    # clean message - unacceptable for the command that actually spends real credits.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(SAMPLE_PLAN))
    (tmp_path / ".env").write_text("MC_AUTH_MODE=cookie\nMC_SESSION_COOKIE=session_id=dummy\n")

    monkeypatch.setattr("mc_autobuilder.cli.build_session", lambda config: object())

    def fake_create_building(self, **kwargs):
        raise ValueError("Could not find authenticity_token on /buildings/new")

    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.create_building", fake_create_building)

    result = runner.invoke(app, ["build", "--poi-id", "5", "--execute"], input="y\n")

    assert result.exit_code == 1
    assert "Unexpected error while building" in result.output
    assert "Traceback" not in result.output


def test_build_execute_reports_unconfirmed_failure_clearly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(SAMPLE_PLAN))
    (tmp_path / ".env").write_text("MC_AUTH_MODE=cookie\nMC_SESSION_COOKIE=session_id=dummy\n")

    monkeypatch.setattr("mc_autobuilder.cli.build_session", lambda config: object())

    def fake_create_building(self, **kwargs):
        return BuildResult(success=False, building=None, price=100_000, response_status=200)

    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.create_building", fake_create_building)

    result = runner.invoke(app, ["build", "--poi-id", "5", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "Could not confirm" in result.output


def test_run_without_execute_is_a_pure_dry_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(MULTI_PLAN))
    (tmp_path / "config.yaml").write_text(MINIMAL_CONFIG)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "3 station(s) to build" in result.output
    assert "300,000" in result.output


def test_run_missing_plan_file_errors_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MINIMAL_CONFIG)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "plan.json" in result.output


def test_run_empty_to_build_list_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps({"to_build": []}))
    (tmp_path / "config.yaml").write_text(MINIMAL_CONFIG)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "Nothing to build" in result.output


def test_run_execute_declined_does_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(MULTI_PLAN))
    (tmp_path / "config.yaml").write_text(MINIMAL_CONFIG)

    result = runner.invoke(app, ["run", "--execute"], input="n\n")

    assert result.exit_code == 0
    assert "Cancelled" in result.output


def _patch_successful_run_dependencies(monkeypatch, balance=10_000_000, price=100_000):
    monkeypatch.setattr("mc_autobuilder.cli.build_session", lambda config: object())
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_credits_balance", lambda self: balance)
    monkeypatch.setattr(
        "mc_autobuilder.cli.MissionChiefClient.get_building_prices", lambda self: {5: price}
    )
    next_id = [900]

    def fake_create_building(self, **kwargs):
        next_id[0] += 1
        return BuildResult(success=True, building={"id": next_id[0]}, price=price, response_status=200)

    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.create_building", fake_create_building)


def test_run_execute_builds_everything_when_healthy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(MULTI_PLAN))
    (tmp_path / "config.yaml").write_text(_config_with_budget(credit_reserve=0))
    (tmp_path / ".env").write_text("MC_AUTH_MODE=cookie\nMC_SESSION_COOKIE=session_id=dummy\n")
    _patch_successful_run_dependencies(monkeypatch)

    result = runner.invoke(app, ["run", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "Run complete: built 3/3, spent 300,000 credits." in result.output


def test_run_skips_already_completed_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(MULTI_PLAN))
    (tmp_path / "config.yaml").write_text(_config_with_budget(credit_reserve=0))
    (tmp_path / ".env").write_text("MC_AUTH_MODE=cookie\nMC_SESSION_COOKIE=session_id=dummy\n")
    _patch_successful_run_dependencies(monkeypatch)

    # Build poi_id=1 first on its own, then run the rest.
    runner.invoke(app, ["build", "--poi-id", "1", "--execute"], input="y\n")

    result = runner.invoke(app, ["run", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "2 station(s) to build (1 already done, skipping)" in result.output
    assert "Run complete: built 2/2" in result.output


def test_run_stops_when_reserve_would_be_breached(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(MULTI_PLAN))
    # Reserve is 1,000,000 but balance is only 1,050,000 - the first 100,000 build would leave
    # 950,000, below the reserve, so nothing should be built at all.
    (tmp_path / "config.yaml").write_text(_config_with_budget(credit_reserve=1_000_000))
    (tmp_path / ".env").write_text("MC_AUTH_MODE=cookie\nMC_SESSION_COOKIE=session_id=dummy\n")
    _patch_successful_run_dependencies(monkeypatch, balance=1_050_000, price=100_000)

    result = runner.invoke(app, ["run", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "Stopping — building" in result.output
    assert "reserve" in result.output
    assert "Run complete: built 0/3" in result.output


def test_run_stops_on_unconfirmed_build_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(MULTI_PLAN))
    (tmp_path / "config.yaml").write_text(_config_with_budget(credit_reserve=0))
    (tmp_path / ".env").write_text("MC_AUTH_MODE=cookie\nMC_SESSION_COOKIE=session_id=dummy\n")

    monkeypatch.setattr("mc_autobuilder.cli.build_session", lambda config: object())
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_credits_balance", lambda self: 10_000_000)
    monkeypatch.setattr(
        "mc_autobuilder.cli.MissionChiefClient.get_building_prices", lambda self: {5: 100_000}
    )

    call_count = [0]

    def fake_create_building(self, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            return BuildResult(success=False, building=None, price=100_000, response_status=200)
        return BuildResult(success=True, building={"id": 900 + call_count[0]}, price=100_000, response_status=200)

    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.create_building", fake_create_building)

    result = runner.invoke(app, ["run", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "Stopping — could not confirm" in result.output
    assert "Run complete: built 1/3" in result.output


def test_run_respects_max_credits_per_run_budget(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps(MULTI_PLAN))
    # Enough for exactly one 100,000-credit build, not two.
    (tmp_path / "config.yaml").write_text(_config_with_budget(credit_reserve=0, max_credits_per_run=150_000))
    (tmp_path / ".env").write_text("MC_AUTH_MODE=cookie\nMC_SESSION_COOKIE=session_id=dummy\n")
    _patch_successful_run_dependencies(monkeypatch)

    result = runner.invoke(app, ["run", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "Stopping — this run's 150,000-credit budget is used up." in result.output
    assert "Run complete: built 1/3" in result.output


def _with_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MC_AUTH_MODE=cookie\nMC_SESSION_COOKIE=session_id=dummy\n")
    monkeypatch.setattr("mc_autobuilder.cli.build_session", lambda config: object())


def test_expand_without_execute_is_a_pure_dry_run(tmp_path, monkeypatch):
    _with_env(tmp_path, monkeypatch)
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_expand_prices", lambda self, bid: {0: 10_000})
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_building_detail", lambda self, bid: {"level": 0})

    result = runner.invoke(app, ["expand", "--building-id", "5558174", "--level", "0"])

    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "10,000" in result.output


def test_expand_execute_confirmed_reports_success(tmp_path, monkeypatch):
    _with_env(tmp_path, monkeypatch)
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_expand_prices", lambda self, bid: {0: 10_000})
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_building_detail", lambda self, bid: {"level": 0})
    monkeypatch.setattr(
        "mc_autobuilder.cli.MissionChiefClient.expand_building",
        lambda self, bid, level: ExpandResult(success=True, level=0, price=10_000, new_level=1, response_status=302),
    )

    result = runner.invoke(app, ["expand", "--building-id", "5558174", "--level", "0", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "Expanded building 5558174 to level 1" in result.output


def test_toggle_service_without_execute_is_a_pure_dry_run(tmp_path, monkeypatch):
    _with_env(tmp_path, monkeypatch)
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_building_detail", lambda self, bid: {"enabled": True})

    result = runner.invoke(app, ["toggle-service", "--building-id", "5558174"])

    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "in service" in result.output


def test_toggle_service_execute_confirmed_reports_success(tmp_path, monkeypatch):
    _with_env(tmp_path, monkeypatch)
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_building_detail", lambda self, bid: {"enabled": True})
    monkeypatch.setattr(
        "mc_autobuilder.cli.MissionChiefClient.toggle_service",
        lambda self, bid: ServiceToggleResult(success=True, enabled=False, response_status=302),
    )

    result = runner.invoke(app, ["toggle-service", "--building-id", "5558174", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "now out of service" in result.output


def test_buy_vehicle_without_execute_is_a_pure_dry_run(tmp_path, monkeypatch):
    _with_env(tmp_path, monkeypatch)
    option = VehicleOption(vehicle_type_id=0, name="Type 1 fire engine", price_credits=5_000, return_tab="fire_engine")
    monkeypatch.setattr(
        "mc_autobuilder.cli.MissionChiefClient.get_vehicle_purchase_options", lambda self, bid: {0: option}
    )

    result = runner.invoke(app, ["buy-vehicle", "--building-id", "5558174", "--vehicle-type", "0"])

    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "Type 1 fire engine" in result.output
    assert "5,000" in result.output


def test_buy_vehicle_execute_confirmed_reports_success(tmp_path, monkeypatch):
    _with_env(tmp_path, monkeypatch)
    option = VehicleOption(vehicle_type_id=0, name="Type 1 fire engine", price_credits=5_000, return_tab="fire_engine")
    monkeypatch.setattr(
        "mc_autobuilder.cli.MissionChiefClient.get_vehicle_purchase_options", lambda self, bid: {0: option}
    )
    monkeypatch.setattr(
        "mc_autobuilder.cli.MissionChiefClient.buy_vehicle",
        lambda self, bid, vehicle_type_id: VehiclePurchaseResult(
            success=True, vehicle={"id": 9999}, price=5_000, response_status=302
        ),
    )

    result = runner.invoke(app, ["buy-vehicle", "--building-id", "5558174", "--vehicle-type", "0", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "Bought" in result.output
    assert "9999" in result.output


def test_hire_rejects_day_option_not_offered(tmp_path, monkeypatch):
    _with_env(tmp_path, monkeypatch)
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_hire_day_options", lambda self, bid: [1, 2, 3])

    result = runner.invoke(app, ["hire", "--building-id", "5558174", "--days", "7"])

    assert result.exit_code == 1
    assert "No 7-day hiring option" in result.output


def test_hire_execute_confirmed_reports_success(tmp_path, monkeypatch):
    _with_env(tmp_path, monkeypatch)
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_hire_day_options", lambda self, bid: [1, 2, 3])
    monkeypatch.setattr(
        "mc_autobuilder.cli.MissionChiefClient.hire",
        lambda self, bid, days: HireResult(success=True, hiring_phase=1, response_status=302),
    )

    result = runner.invoke(app, ["hire", "--building-id", "5558174", "--days", "1", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "Started a 1-day recruiting phase" in result.output
    assert "not immediately" in result.output


def test_assign_personnel_without_execute_is_a_pure_dry_run(tmp_path, monkeypatch):
    _with_env(tmp_path, monkeypatch)
    vehicle = {"id": 14577420, "caption": "Type 1 fire engine", "assigned_personnel_count": 2}
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_vehicles", lambda self: [vehicle])

    result = runner.invoke(app, ["assign-personnel", "--vehicle-id", "14577420", "--personal-id", "135847194"])

    assert result.exit_code == 0
    assert "Dry run" in result.output


def test_assign_personnel_execute_confirmed_reports_success(tmp_path, monkeypatch):
    _with_env(tmp_path, monkeypatch)
    vehicle = {"id": 14577420, "caption": "Type 1 fire engine", "assigned_personnel_count": 2}
    monkeypatch.setattr("mc_autobuilder.cli.MissionChiefClient.get_vehicles", lambda self: [vehicle])
    monkeypatch.setattr(
        "mc_autobuilder.cli.MissionChiefClient.assign_personnel",
        lambda self, vehicle_id, personal_id: AssignPersonnelResult(
            success=True, assigned_personnel_count=3, response_status=200
        ),
    )

    result = runner.invoke(
        app, ["assign-personnel", "--vehicle-id", "14577420", "--personal-id", "135847194", "--execute"], input="y\n"
    )

    assert result.exit_code == 0
    assert "now has 3 assigned crew" in result.output
