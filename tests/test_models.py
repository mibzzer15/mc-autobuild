from mc_autobuilder.models import Building, get_session_factory, init_db, upsert_buildings

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
