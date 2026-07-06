from typer.testing import CliRunner

from mc_autobuilder.auth import SessionExpiredError
from mc_autobuilder.cli import app

runner = CliRunner()

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
