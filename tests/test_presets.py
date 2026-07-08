import pytest

from mc_autobuilder.mc_client import (
    AssignPersonnelResult,
    DispatchAssignResult,
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
from mc_autobuilder.presets import apply_preset, dump_vehicles_json, _best_expand_param


def test_best_expand_param_picks_highest_rung_without_overshooting():
    prices = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}
    # From level 0, to reach level 3 we click ?level=2 (target - 1).
    assert _best_expand_param(prices, current_level=0, target_level=3) == 2
    # From level 2, same target still means ?level=2.
    assert _best_expand_param(prices, current_level=2, target_level=3) == 2
    # Target beyond what's offered -> take the highest offered rung and loop from there.
    assert _best_expand_param(prices, current_level=0, target_level=99) == 4
    # Nothing at or above the current level -> no option.
    assert _best_expand_param({0: 1, 1: 2}, current_level=5, target_level=10) is None
    assert _best_expand_param({}, current_level=0, target_level=5) is None


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
    messages = apply_preset(client, db, 5558174, _preset()).messages
    assert messages == ["Nothing to do — this preset has no actions configured."]


def test_expand_jumps_straight_to_target_in_one_request(client, db, monkeypatch):
    # Confirmed semantics (docs/missionchief-api.md): the expand page lists every reachable target
    # level and clicking a far one jumps straight there. To land on level L you click `?level=L-1`
    # (a level-0 building expands via `?level=0` to reach level 1). So reaching level 2 from level 0
    # is a single `?level=1` request, not one rung at a time.
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"level": 0})
    monkeypatch.setattr(
        MissionChiefClient, "get_expand_prices", lambda self, bid: {0: 10_000, 1: 60_000}
    )
    calls = []

    def fake_expand(self, bid, level):
        calls.append(level)
        # Direct jump: clicking `?level=L-1` lands on level L.
        return ExpandResult(success=True, level=level, price=60_000, new_level=level + 1, response_status=302)

    monkeypatch.setattr(MissionChiefClient, "expand_building", fake_expand)

    messages = apply_preset(client, db, 5558174, _preset(target_level=2)).messages

    assert calls == [1]  # one request straight to the target (param = target - 1)
    assert "Expanded to level 2" in messages[-1]


def test_expand_falls_back_to_looping_if_a_jump_lands_short(client, db, monkeypatch):
    # If a single expand only advances one level (defensive fallback), the loop keeps going from
    # where it actually landed rather than assuming the jump reached the target. _expand_to_level
    # reads the starting level once and then trusts each result's verified new_level.
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"level": 0})
    monkeypatch.setattr(
        MissionChiefClient, "get_expand_prices", lambda self, bid: {0: 10_000, 1: 60_000, 2: 160_000}
    )
    reached = {"level": 0}
    calls = []

    def only_one_rung(self, bid, level):
        calls.append(level)
        reached["level"] += 1  # cap: advance a single level no matter which param was requested
        return ExpandResult(
            success=True, level=level, price=10_000, new_level=reached["level"], response_status=302
        )

    monkeypatch.setattr(MissionChiefClient, "expand_building", only_one_rung)

    messages = apply_preset(client, db, 5558174, _preset(target_level=3)).messages

    assert reached["level"] == 3  # eventually reached the target
    assert len(calls) == 3  # took three jumps because each only advanced one level
    assert "Expanded to level 3" in messages[-1]


def test_expand_to_level_stops_once_target_reached(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"level": 5})
    monkeypatch.setattr(MissionChiefClient, "get_expand_prices", lambda self, bid: {5: 10_000})
    monkeypatch.setattr(
        MissionChiefClient, "expand_building",
        lambda self, bid, level: pytest.fail("should not be called"),
    )

    messages = apply_preset(client, db, 5558174, _preset(target_level=5)).messages
    assert "Already at level 5 (target 5)." in messages[0]


def test_expand_stops_if_no_forward_option_is_offered(client, db, monkeypatch):
    # Nothing at or above the current level is offered (e.g. the station is maxed, so the expand
    # page has no forward links) — don't buy anything, just report it can't reach the target.
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"level": 3})
    monkeypatch.setattr(MissionChiefClient, "get_expand_prices", lambda self, bid: {})
    monkeypatch.setattr(
        MissionChiefClient, "expand_building",
        lambda self, bid, level: pytest.fail("should not be called"),
    )

    messages = apply_preset(client, db, 5558174, _preset(target_level=10)).messages
    assert "No expand option available to reach level 10" in messages[0]


def test_expand_stops_on_unconfirmed_failure_rather_than_looping_forever(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"level": 0})
    monkeypatch.setattr(MissionChiefClient, "get_expand_prices", lambda self, bid: {0: 10_000, 1: 20_000})
    monkeypatch.setattr(
        MissionChiefClient, "expand_building",
        lambda self, bid, level: ExpandResult(False, level, 10_000, None, 200, "insufficient funds"),
    )

    messages = apply_preset(client, db, 5558174, _preset(target_level=5)).messages
    assert "could not confirm the expansion" in messages[-1]
    assert len(get_preset_log(db, 5558174)) == 1  # only tried once, not in an infinite loop


def test_service_state_skips_when_already_matching(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"enabled": True})
    monkeypatch.setattr(
        MissionChiefClient, "toggle_service", lambda self, bid: pytest.fail("should not be called")
    )

    messages = apply_preset(client, db, 5558174, _preset(manage_service=True, target_enabled=True)).messages
    assert messages == ["Nothing to do — this preset has no actions configured."]


def test_service_state_toggles_when_it_does_not_match(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"enabled": True})
    monkeypatch.setattr(
        MissionChiefClient, "toggle_service",
        lambda self, bid: ServiceToggleResult(True, False, 302),
    )

    messages = apply_preset(client, db, 5558174, _preset(manage_service=True, target_enabled=False)).messages
    assert "out of service" in messages[0]


def test_hiring_skips_when_phase_already_active(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"hiring_phase": 2})
    monkeypatch.setattr(MissionChiefClient, "hire", lambda self, bid, days: pytest.fail("should not be called"))

    messages = apply_preset(client, db, 5558174, _preset(hire_days=3)).messages
    assert "already active" in messages[0]


def test_hiring_starts_when_no_phase_active(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "get_building_detail", lambda self, bid: {"hiring_phase": 0})
    monkeypatch.setattr(MissionChiefClient, "hire", lambda self, bid, days: HireResult(True, days, 302))

    messages = apply_preset(client, db, 5558174, _preset(hire_days=3)).messages
    assert "Started a 3-day recruiting phase." in messages[0]


def test_auto_hire_applied_and_marks_complete(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "hire_automatic", lambda self, bid: HireResult(True, None, 302))
    # Day-based hire must NOT be called when auto is set.
    monkeypatch.setattr(MissionChiefClient, "hire", lambda self, bid, days: pytest.fail("day-hire called"))

    outcome = apply_preset(client, db, 5558174, _preset(hire_automatic=True))
    assert outcome.complete is True
    assert "Auto-hire enabled" in outcome.messages[0]


def test_auto_hire_failure_on_non_premium_marks_incomplete(client, db, monkeypatch):
    monkeypatch.setattr(MissionChiefClient, "hire_automatic", lambda self, bid: HireResult(False, None, 302, "no"))

    outcome = apply_preset(client, db, 5558174, _preset(hire_automatic=True))
    assert outcome.complete is False
    assert "premium" in outcome.messages[0]


def test_personnel_count_target_applied(client, db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        MissionChiefClient, "set_personnel_count_target",
        lambda self, bid, target: (calls.append(target) or True),
    )
    outcome = apply_preset(client, db, 5558174, _preset(personnel_count_target=500))
    assert calls == [500]
    assert "Set personnel (desired) target to 500." in outcome.messages[0]


def test_vehicle_purchases_are_capped_by_our_own_log_not_live_state(client, db, monkeypatch):
    # Simulate 1 of this vehicle_type already bought via a previous preset application.
    log_preset_action(db, 5558174, "buy_vehicle", 0, True, "5000 credits")

    calls = []
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id, before_ids=None: (calls.append(vehicle_type_id) or VehiclePurchaseResult(True, {"id": 1}, 5_000, 302)),
    )

    vehicles = dump_vehicles_json([{"vehicle_type_id": 0, "count": 3, "personnel_per_vehicle": 0}])
    messages = apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles)).messages

    assert calls == [0, 0]  # only 2 more needed to reach target of 3
    assert len([m for m in messages if "Bought" in m]) == 2


def test_vehicle_purchases_stop_on_first_unconfirmed_failure(client, db, monkeypatch):
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id, before_ids=None: VehiclePurchaseResult(False, None, 5_000, 200, "insufficient funds"),
    )

    vehicles = dump_vehicles_json([{"vehicle_type_id": 0, "count": 3, "personnel_per_vehicle": 0}])
    messages = apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles)).messages

    assert len(get_preset_log(db, 5558174)) == 1  # stopped after the first failed attempt
    assert "stopping this type" in messages[-1]


def test_vehicle_purchase_failure_surfaces_server_response_for_diagnosis(client, db, monkeypatch):
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id, before_ids=None: VehiclePurchaseResult(
            False, None, 5_000, 200, "POST /buildings/1/vehicle/1/0/credits -> 200\n<html>Not enough credits</html>"
        ),
    )

    vehicles = dump_vehicles_json([{"vehicle_type_id": 0, "count": 1}])
    messages = apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles)).messages

    # The failure message and the logged action both include the server's response so a broken
    # purchase can be diagnosed from the building's action log without a fresh HAR.
    assert "Not enough credits" in messages[-1]
    log = get_preset_log(db, 5558174)
    assert "Not enough credits" in log[-1].message


def test_preset_assigns_dispatch_center_when_configured(client, db, monkeypatch):
    calls = []

    def fake_set_dispatch(self, bid, leitstelle_id):
        calls.append((bid, leitstelle_id))
        return DispatchAssignResult(success=True, building_id=bid, leitstelle_id=leitstelle_id, response_status=200)

    monkeypatch.setattr(MissionChiefClient, "set_dispatch_center", fake_set_dispatch)

    messages = apply_preset(client, db, 5558174, _preset(dispatch_center_id=2534509)).messages

    assert calls == [(5558174, 2534509)]
    assert "Assigned to dispatch center 2534509." in messages[-1]
    assert get_preset_log(db, 5558174)[-1].action_type == "set_dispatch_center"


def test_preset_skips_dispatch_center_when_not_configured(client, db, monkeypatch):
    monkeypatch.setattr(
        MissionChiefClient, "set_dispatch_center",
        lambda self, bid, leitstelle_id: pytest.fail("should not be called when dispatch_center_id is None"),
    )
    messages = apply_preset(client, db, 5558174, _preset()).messages  # dispatch_center_id defaults to None
    assert "Nothing to do" in messages[-1]


def test_vehicle_purchase_assigns_crew_from_unassigned_roster(client, db, monkeypatch):
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id, before_ids=None: VehiclePurchaseResult(True, {"id": 999}, 5_000, 302),
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
    messages = apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles)).messages

    assert assigned == [(999, 1), (999, 3)]  # only the two unassigned people, not Colin X.
    assert "Assigned Paul G. to vehicle 999." in messages
    assert "Assigned Maria B. to vehicle 999." in messages


def test_crew_assignment_reports_shortfall_when_not_enough_unassigned_personnel(client, db, monkeypatch):
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id, before_ids=None: VehiclePurchaseResult(True, {"id": 999}, 5_000, 302),
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
    messages = apply_preset(client, db, 5558174, _preset(vehicles_json=vehicles)).messages

    assert "Only 1/3 available" in messages[-1]


def test_crew_assignment_does_not_double_claim_the_same_person_across_vehicles(client, db, monkeypatch):
    monkeypatch.setattr(
        MissionChiefClient, "buy_vehicle",
        lambda self, bid, vehicle_type_id, before_ids=None: VehiclePurchaseResult(True, {"id": 100 + vehicle_type_id}, 5_000, 302),
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
