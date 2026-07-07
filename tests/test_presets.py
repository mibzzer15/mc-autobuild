import pytest

from mc_autobuilder.mc_client import (
    AssignPersonnelResult,
    ExpandResult,
    HireResult,
    MissionChiefClient,
    PersonnelEntry,
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
        building_type=0, target_level=None, manage_service=False, target_enabled=True,
        hire_days=None, vehicles_json="[]",
    )
    defaults.update(overrides)
    return StationPreset(**defaults)


def test_apply_preset_with_nothing_configured_is_a_noop_message(client, db):
    messages = apply_preset(client, db, 5558174, _preset())
    assert messages == ["Nothing to do — this preset has no actions configured."]


def test_expand_to_level_uses_current_level_as_the_query_param_not_current_plus_one(client, db, monkeypatch):
    # Real confirmed semantics (docs/missionchief-api.md): a building at level 0 expands via
    # `?level=0` to reach level 1 - the query param equals the CURRENT level, not current+1.
    # This regression-guards a bug where the preset loop passed current+1 instead, which would
    # buy the wrong (more expensive) rung and skip the one actually available next.
    levels = iter([0, 1, 2])
    monkeypatch.setattr(
        MissionChiefClient, "get_building_detail", lambda self, bid: {"level": next(levels)}
    )
    monkeypatch.setattr(MissionChiefClient, "get_expand_prices", lambda self, bid: {0: 10_000, 1: 60_000})
    calls = []

    def fake_expand(self, bid, level):
        calls.append(level)
        return ExpandResult(success=True, level=level, price=10_000, new_level=level + 1, response_status=302)

    monkeypatch.setattr(MissionChiefClient, "expand_building", fake_expand)

    messages = apply_preset(client, db, 5558174, _preset(target_level=2))

    assert calls == [0, 1]  # query param = current level, both times
    assert "Already at level 2 (target 2)." in messages[-1]


def test_expand_to_level_stops_once_target_reached(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"level": 5})
    monkeypatch.setattr(MissionChiefClient, "get_expand_prices", lambda self, bid: {5: 10_000})
    monkeypatch.setattr(
        MissionChiefClient, "expand_building",
        lambda self, bid, level: pytest.fail("should not be called"),
    )

    messages = apply_preset(client, db, 5558174, _preset(target_level=5))
    assert "Already at level 5 (target 5)." in messages[0]


def test_expand_stops_if_target_unreachable_from_current_prices(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"level": 3})
    monkeypatch.setattr(MissionChiefClient, "get_expand_prices", lambda self, bid: {5: 10_000})  # no key "3"
    monkeypatch.setattr(
        MissionChiefClient, "expand_building",
        lambda self, bid, level: pytest.fail("should not be called"),
    )

    messages = apply_preset(client, db, 5558174, _preset(target_level=10))
    assert "can't reach level 10" in messages[0]


def test_expand_stops_on_unconfirmed_failure_rather_than_looping_forever(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"level": 0})
    monkeypatch.setattr(MissionChiefClient, "get_expand_prices", lambda self, bid: {0: 10_000, 1: 20_000})
    monkeypatch.setattr(
        MissionChiefClient, "expand_building",
        lambda self, bid, level: ExpandResult(False, level, 10_000, None, 200, "insufficient funds"),
    )

    messages = apply_preset(client, db, 5558174, _preset(target_level=5))
    assert "could not confirm the expansion" in messages[-1]
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

    vehicles = dump_vehicles_json([{"vehicle_type_id": 0, "count": 3, "personnel_per_vehicle": 0}])
    messages = apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles))

    assert calls == [0, 0]  # only 2 more needed to reach target of 3
    assert len([m for m in messages if "Bought" in m]) == 2


def test_vehicle_purchases_stop_on_first_unconfirmed_failure(client, db, monkeypatch):
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id: VehiclePurchaseResult(False, None, 5_000, 200, "insufficient funds"),
    )

    vehicles = dump_vehicles_json([{"vehicle_type_id": 0, "count": 3, "personnel_per_vehicle": 0}])
    messages = apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles))

    assert len(get_preset_log(db, 5558174)) == 1  # stopped after the first failed attempt
    assert "stopping this type" in messages[-1]


def test_vehicle_purchase_assigns_crew_from_unassigned_roster(client, db, monkeypatch):
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id: VehiclePurchaseResult(True, {"id": 999}, 5_000, 302),
    )
    monkeypatch.setattr(
        MissionChiefClient, "get_personnel_roster",
        lambda self, bid: [
            PersonnelEntry(personal_id=1, name="Paul G.", assigned_to=None),
            PersonnelEntry(personal_id=2, name="Colin X.", assigned_to="Type 1 fire engine"),  # already bound
            PersonnelEntry(personal_id=3, name="Maria B.", assigned_to=None),
        ],
    )
    assigned = []
    monkeypatch.setattr(
        MissionChiefClient, "assign_personnel",
        lambda self, vehicle_id, personal_id: (assigned.append((vehicle_id, personal_id)) or AssignPersonnelResult(True, 1, 200)),
    )

    vehicles = dump_vehicles_json([{"vehicle_type_id": 0, "count": 1, "personnel_per_vehicle": 2}])
    messages = apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles))

    assert assigned == [(999, 1), (999, 3)]  # only the two unassigned people, not Colin X.
    assert "Assigned Paul G. to vehicle 999." in messages
    assert "Assigned Maria B. to vehicle 999." in messages


def test_crew_assignment_reports_shortfall_when_not_enough_unassigned_personnel(client, db, monkeypatch):
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id: VehiclePurchaseResult(True, {"id": 999}, 5_000, 302),
    )
    monkeypatch.setattr(
        MissionChiefClient, "get_personnel_roster",
        lambda self, bid: [PersonnelEntry(personal_id=1, name="Paul G.", assigned_to=None)],
    )
    monkeypatch.setattr(
        MissionChiefClient, "assign_personnel",
        lambda self, vehicle_id, personal_id: AssignPersonnelResult(True, 1, 200),
    )

    vehicles = dump_vehicles_json([{"vehicle_type_id": 0, "count": 1, "personnel_per_vehicle": 3}])
    messages = apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles))

    assert "Only 1/3 available" in messages[-1]


def test_crew_assignment_does_not_double_claim_the_same_person_across_vehicles(client, db, monkeypatch):
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id: VehiclePurchaseResult(True, {"id": 100 + vehicle_type_id}, 5_000, 302),
    )
    monkeypatch.setattr(
        MissionChiefClient, "get_personnel_roster",
        lambda self, bid: [PersonnelEntry(personal_id=1, name="Paul G.", assigned_to=None)],
    )
    assigned = []
    monkeypatch.setattr(
        MissionChiefClient, "assign_personnel",
        lambda self, vehicle_id, personal_id: (assigned.append((vehicle_id, personal_id)) or AssignPersonnelResult(True, 1, 200)),
    )

    vehicles = dump_vehicles_json([
        {"vehicle_type_id": 0, "count": 1, "personnel_per_vehicle": 1},
        {"vehicle_type_id": 1, "count": 1, "personnel_per_vehicle": 1},
    ])
    apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles))

    # Paul G. can only crew one of the two new vehicles, not both.
    assert assigned == [(100, 1)]


def test_save_preset_round_trips_through_the_database(db):
    save_preset(
        db, building_type=0, target_level=20, manage_service=True, target_enabled=False,
        hire_days=3, vehicles=[{"vehicle_type_id": 0, "count": 2, "personnel_per_vehicle": 4}],
    )
    from mc_autobuilder.models import get_preset

    preset = get_preset(db, 0)
    assert preset.target_level == 20
    assert preset.hire_days == 3
    assert preset.vehicles_json == '[{"vehicle_type_id": 0, "count": 2, "personnel_per_vehicle": 4}]'
