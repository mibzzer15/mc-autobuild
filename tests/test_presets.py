import pytest

from mc_autobuilder.mc_client import (
    ExpandResult,
    HireResult,
    MissionChiefClient,
    ServiceToggleResult,
    VehiclePurchaseResult,
)
from mc_autobuilder.models import (
    StationPreset,
    get_preset_log,
    get_session_factory,
    init_db,
    log_preset_action,
    save_preset,
)
from mc_autobuilder.presets import apply_preset, dump_vehicles_json


@pytest.fixture
def db(tmp_path):
    engine = init_db(str(tmp_path / "test.db"))
    factory = get_session_factory(engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def client():
    return MissionChiefClient(session=None, base_url="https://www.missionchief.com")


def _preset(**overrides):
    defaults = dict(
        building_type=0, max_level=False, manage_service=False, target_enabled=True,
        hire_days=None, vehicles_json="[]",
    )
    defaults.update(overrides)
    return StationPreset(**defaults)


def test_apply_preset_with_nothing_configured_is_a_noop_message(client, db):
    messages = apply_preset(client, db, 5558174, _preset())
    assert messages == ["Nothing to do — this preset has no actions configured."]


def test_expand_to_max_stops_when_no_more_levels_available(client, db, monkeypatch):
    levels = iter([0, 1, 2])
    monkeypatch.setattr(
        MissionChiefClient, "get_building_detail", lambda self, bid: {"level": next(levels)}
    )
    monkeypatch.setattr(MissionChiefClient, "get_expand_prices", lambda self, bid: {1: 10_000, 2: 20_000})
    calls = []

    def fake_expand(self, bid, level):
        calls.append(level)
        return ExpandResult(success=True, level=level, price=10_000 * level, new_level=level, response_status=302)

    monkeypatch.setattr(MissionChiefClient, "expand_building", fake_expand)

    messages = apply_preset(client, db, 5558174, _preset(max_level=True))

    assert calls == [1, 2]
    assert "Already at max level (2)." in messages[-1]
    log = get_preset_log(db, 5558174)
    assert len([e for e in log if e.action_type == "expand"]) == 2


def test_expand_to_max_stops_immediately_if_already_at_max(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"level": 5})
    monkeypatch.setattr(MissionChiefClient, "get_expand_prices", lambda self, bid: {1: 10_000})
    monkeypatch.setattr(
        MissionChiefClient, "expand_building",
        lambda self, bid, level: pytest.fail("should not be called"),
    )

    messages = apply_preset(client, db, 5558174, _preset(max_level=True))
    assert "Already at max level (5)." in messages[0]


def test_expand_stops_on_unconfirmed_failure_rather_than_looping_forever(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"level": 0})
    monkeypatch.setattr(MissionChiefClient, "get_expand_prices", lambda self, bid: {1: 10_000, 2: 20_000})
    monkeypatch.setattr(
        MissionChiefClient, "expand_building",
        lambda self, bid, level: ExpandResult(False, level, 10_000, None, 200, "insufficient funds"),
    )

    messages = apply_preset(client, db, 5558174, _preset(max_level=True))
    assert "could not be confirmed" in messages[-1]
    assert len(get_preset_log(db, 5558174)) == 1  # only tried once, not in an infinite loop


def test_service_state_skips_when_already_matching(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"enabled": True})
    monkeypatch.setattr(
        MissionChiefClient, "toggle_service", lambda self, bid: pytest.fail("should not be called")
    )

    messages = apply_preset(client, db, 5558174, _preset(manage_service=True, target_enabled=True))
    assert messages == ["Nothing to do — this preset has no actions configured."]


def test_service_state_toggles_when_it_does_not_match(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"enabled": True})
    monkeypatch.setattr(
        MissionChiefClient, "toggle_service",
        lambda self, bid: ServiceToggleResult(True, False, 302),
    )

    messages = apply_preset(client, db, 5558174, _preset(manage_service=True, target_enabled=False))
    assert "out of service" in messages[0]


def test_hiring_skips_when_phase_already_active(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"hiring_phase": 2})
    monkeypatch.setattr(MissionChiefClient, "hire", lambda self, bid, days: pytest.fail("should not be called"))

    messages = apply_preset(client, db, 5558174, _preset(hire_days=3))
    assert "already active" in messages[0]


def test_hiring_starts_when_no_phase_active(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"hiring_phase": 0})
    monkeypatch.setattr(MissionChiefClient, "hire", lambda self, bid, days: HireResult(True, days, 302))

    messages = apply_preset(client, db, 5558174, _preset(hire_days=3))
    assert "Started a 3-day recruiting phase." in messages[0]


def test_vehicle_purchases_are_capped_by_our_own_log_not_live_state(client, db, monkeypatch):
    # Simulate 1 of this vehicle_type already bought via a previous preset application.
    log_preset_action(db, 5558174, "buy_vehicle", 0, True, "5000 credits")

    calls = []
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id: (calls.append(vehicle_type_id) or VehiclePurchaseResult(True, {"id": 1}, 5_000, 302)),
    )

    vehicles = dump_vehicles_json([{"vehicle_type_id": 0, "count": 3}])
    messages = apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles))

    assert calls == [0, 0]  # only 2 more needed to reach target of 3
    assert len([m for m in messages if "Bought" in m]) == 2


def test_vehicle_purchases_stop_on_first_unconfirmed_failure(client, db, monkeypatch):
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id: VehiclePurchaseResult(False, None, 5_000, 200, "insufficient funds"),
    )

    vehicles = dump_vehicles_json([{"vehicle_type_id": 0, "count": 3}])
    messages = apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles))

    assert len(get_preset_log(db, 5558174)) == 1  # stopped after the first failed attempt
    assert "stopping this type" in messages[-1]


def test_save_preset_round_trips_through_the_database(db):
    save_preset(
        db, building_type=0, max_level=True, manage_service=True, target_enabled=False,
        hire_days=3, vehicles=[{"vehicle_type_id": 0, "count": 2}],
    )
    from mc_autobuilder.models import get_preset

    preset = get_preset(db, 0)
    assert preset.max_level is True
    assert preset.hire_days == 3
    assert preset.vehicles_json == '[{"vehicle_type_id": 0, "count": 2}]'
