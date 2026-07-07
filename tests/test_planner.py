import pytest

from mc_autobuilder.planner import (
    build_plan,
    find_duplicate,
    haversine_distance_m,
    render_name,
)

# Fire station in downtown-ish Sacramento, and a nearby duplicate ~50m away vs. a distinct one
# a few km away.
SACRAMENTO_LAT, SACRAMENTO_LNG = 38.5816, -121.4944

EXISTING_BUILDINGS = [
    {"id": 101, "building_type": 0, "latitude": SACRAMENTO_LAT, "longitude": SACRAMENTO_LNG, "caption": "Existing Fire Station"},
    {"id": 102, "building_type": 5, "latitude": 38.6, "longitude": -121.5, "caption": "Existing Police Station"},
]


def test_haversine_zero_for_identical_points():
    assert haversine_distance_m(10.0, 20.0, 10.0, 20.0) == 0.0


def test_haversine_known_distance_roughly_111km_per_degree_latitude():
    # One degree of latitude is ~111.32km everywhere on Earth.
    dist = haversine_distance_m(0.0, 0.0, 1.0, 0.0)
    assert 110_500 < dist < 112_000


def test_find_duplicate_within_radius():
    # ~30m north of the existing fire station - well within a 150m dedupe radius.
    dup = find_duplicate(SACRAMENTO_LAT + 0.0003, SACRAMENTO_LNG, 0, EXISTING_BUILDINGS, 150.0)
    assert dup is not None
    assert dup["id"] == 101
    assert dup["_distance_m"] < 150


def test_find_duplicate_outside_radius_returns_none():
    # Same coordinates but ~5km away - outside a 150m dedupe radius.
    dup = find_duplicate(SACRAMENTO_LAT + 0.05, SACRAMENTO_LNG, 0, EXISTING_BUILDINGS, 150.0)
    assert dup is None


def test_find_duplicate_ignores_different_building_type():
    # Exact same coordinates as the existing fire station, but querying for a police station.
    dup = find_duplicate(SACRAMENTO_LAT, SACRAMENTO_LNG, 5, EXISTING_BUILDINGS, 150.0)
    assert dup is None


def test_render_name_template():
    poi = {"name": "Downtown Fire Station"}
    assert render_name("{poi_name}", poi) == "Downtown Fire Station"
    assert render_name("{city} - {poi_name}", poi, city="Sacramento") == "Sacramento - Downtown Fire Station"


def test_render_name_falls_back_when_poi_name_missing():
    assert render_name("{poi_name}", {}) == "Unnamed Station"


def test_render_name_truncates_over_missionchief_40_char_limit():
    # Real case: this exact name (41 chars) was rejected live by MissionChief with "is too long
    # (maximum is 40 characters)" - see docs/missionchief-api.md - while shorter names at the same
    # price built fine in the same run.
    poi = {"name": "Union City Police Department"}
    name = render_name("{poi_name}- {city}", poi, city="Fremont, CA")

    assert len(name) <= 40
    # The city suffix (organizationally meaningful) is preserved; the POI name is shortened instead.
    assert name.endswith("- Fremont, CA")


def test_render_name_hard_truncates_if_still_too_long_after_shortening_poi_name():
    poi = {"name": "X"}
    name = render_name("{poi_name}", poi, city="A" * 60)
    assert len(name) <= 40


def _candidate(poi_id, name, lat, lng, building_type=0, building_type_name="Fire station"):
    return {
        "id": poi_id, "name": name, "lat": lat, "lng": lng,
        "building_type": building_type, "building_type_name": building_type_name,
    }


def test_build_plan_skips_duplicates_and_plans_new_ones():
    candidates = [
        _candidate(1, "Duplicate Station", SACRAMENTO_LAT + 0.0001, SACRAMENTO_LNG),  # ~11m away, dup
        _candidate(2, "New Station", SACRAMENTO_LAT + 0.05, SACRAMENTO_LNG),  # ~5.5km away, new
    ]
    plan = build_plan(candidates, EXISTING_BUILDINGS, dedupe_radius_m=150.0)

    assert len(plan.to_build) == 1
    assert plan.to_build[0].poi_id == 2
    assert len(plan.skipped_duplicates) == 1
    assert plan.skipped_duplicates[0].poi_id == 1
    assert plan.skipped_duplicates[0].existing_building_id == 101


def test_build_plan_respects_per_type_cap():
    candidates = [
        _candidate(i, f"Station {i}", SACRAMENTO_LAT + 0.05 + i * 0.01, SACRAMENTO_LNG)
        for i in range(3)
    ]
    plan = build_plan(candidates, EXISTING_BUILDINGS, per_type_caps={0: 2})

    assert len(plan.to_build) == 2
    assert len(plan.skipped_capped) == 1
    assert plan.skipped_capped[0].reason == "per-run cap of 2 reached for this building type"


def test_build_plan_respects_budget_and_computes_total_cost():
    candidates = [
        _candidate(1, "Station A", SACRAMENTO_LAT + 0.05, SACRAMENTO_LNG),
        _candidate(2, "Station B", SACRAMENTO_LAT + 0.06, SACRAMENTO_LNG),
    ]
    plan = build_plan(
        candidates, EXISTING_BUILDINGS,
        prices={0: 1_000_000},
        max_total_cost=1_500_000,
    )

    assert len(plan.to_build) == 1
    assert plan.total_estimated_cost == 1_000_000
    assert len(plan.skipped_budget) == 1
    # The skipped entry records its cost so the dashboard can tell the user what to raise the cap to.
    assert plan.skipped_budget[0].estimated_cost == 1_000_000


def test_build_plan_applies_naming_template():
    candidates = [_candidate(1, "Downtown Station", SACRAMENTO_LAT + 0.05, SACRAMENTO_LNG)]
    plan = build_plan(candidates, EXISTING_BUILDINGS, naming_template="{city} - {poi_name}", city="Sacramento")

    assert plan.to_build[0].name == "Sacramento - Downtown Station"


def test_build_plan_with_no_existing_buildings_builds_everything_within_caps():
    candidates = [_candidate(1, "Station A", 10.0, 20.0), _candidate(2, "Station B", 30.0, 40.0)]
    plan = build_plan(candidates, [])
    assert len(plan.to_build) == 2
    assert plan.skipped_duplicates == []
