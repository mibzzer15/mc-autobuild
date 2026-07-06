from mc_autobuilder.models import (
    Building,
    get_session_factory,
    has_completed_action,
    init_db,
    record_completed_action,
    upsert_buildings,
)

SAMPLE_BUILDING = {
    "id": 778773,
    "personal_count": 400,
    "level": 12,
    "building_type": 0,
    "caption": "ACFD Station 10",
    "latitude": 37.70879027778312,
    "longitude": -122.18151211651276,
    "small_building": False,
    "enabled": True,
    "personal_count_target": 400,
    "hiring_phase": 3,
    "hiring_automatic": True,
    "leitstelle_building_id": 376606,
    "updated_iso": "2023-04-12T23:39:39-04:00",
}


def _memory_session_factory():
    engine = init_db(":memory:")
    return get_session_factory(engine)


def test_upsert_inserts_new_building():
    factory = _memory_session_factory()
    with factory() as db:
        upsert_buildings(db, [SAMPLE_BUILDING])
        obj = db.get(Building, 778773)
        assert obj is not None
        assert obj.caption == "ACFD Station 10"
        assert obj.enabled is True
        assert obj.leitstelle_building_id == 376606


def test_upsert_is_idempotent_and_updates_in_place():
    factory = _memory_session_factory()
    with factory() as db:
        upsert_buildings(db, [SAMPLE_BUILDING])

        updated = dict(SAMPLE_BUILDING, enabled=False, caption="ACFD Station 10 (renamed)")
        upsert_buildings(db, [updated])

        assert db.query(Building).count() == 1
        obj = db.get(Building, 778773)
        assert obj.enabled is False
        assert obj.caption == "ACFD Station 10 (renamed)"


def test_has_completed_action_returns_none_when_not_recorded():
    factory = _memory_session_factory()
    with factory() as db:
        assert has_completed_action(db, "build", poi_id=42) is None


def test_record_and_check_completed_action():
    factory = _memory_session_factory()
    with factory() as db:
        record_completed_action(
            db,
            action_type="build",
            poi_id=42,
            building_id=999,
            building_type=0,
            name="Test Fire Station",
            cost=500_000,
        )

        found = has_completed_action(db, "build", poi_id=42)
        assert found is not None
        assert found.building_id == 999
        assert found.cost == 500_000

        # A different poi_id, or a different action_type, isn't the same completed action.
        assert has_completed_action(db, "build", poi_id=43) is None
        assert has_completed_action(db, "expand", poi_id=42) is None
