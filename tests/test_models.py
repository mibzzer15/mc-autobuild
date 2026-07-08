import sqlite3

from mc_autobuilder.models import (
    Building,
    get_session_factory,
    has_completed_action,
    init_db,
    list_presets,
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


def test_init_db_migrates_old_station_presets_schema_without_a_column_error(tmp_path):
    # Regression test: a db created before target_level replaced the old max_level column used
    # to raise "no such column: station_presets.target_level" on every preset query, since
    # create_all() only creates missing tables - it never alters an existing one.
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE station_presets (
          building_type INTEGER PRIMARY KEY,
          max_level BOOLEAN NOT NULL,
          manage_service BOOLEAN NOT NULL,
          target_enabled BOOLEAN NOT NULL,
          hire_days INTEGER,
          vehicles_json TEXT NOT NULL,
          updated_at DATETIME NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO station_presets VALUES (0, 1, 0, 1, NULL, '[]', '2026-01-01')")
    conn.commit()
    conn.close()

    engine = init_db(str(db_path))
    db = get_session_factory(engine)()
    try:
        assert list_presets(db) == []  # old row is gone, but querying no longer errors
    finally:
        db.close()


def test_init_db_adds_dispatch_center_id_column_to_existing_presets_without_dropping_them(tmp_path):
    # Unlike the max_level change, dispatch_center_id has a clean default (NULL), so the migration
    # adds the column in place and keeps existing presets rather than recreating the table.
    from mc_autobuilder.models import get_preset, save_preset

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE station_presets (
          building_type INTEGER PRIMARY KEY,
          target_level INTEGER,
          manage_service BOOLEAN NOT NULL,
          target_enabled BOOLEAN NOT NULL,
          hire_days INTEGER,
          vehicles_json TEXT NOT NULL,
          updated_at DATETIME NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO station_presets VALUES (5, 10, 0, 1, NULL, '[]', '2026-01-01')")
    conn.commit()
    conn.close()

    engine = init_db(str(db_path))
    db = get_session_factory(engine)()
    try:
        preset = get_preset(db, 5)
        assert preset is not None  # existing preset preserved
        assert preset.target_level == 10
        assert preset.dispatch_center_id is None  # new column defaults to NULL
        # And the new column is writable.
        save_preset(
            db, 5, target_level=10, manage_service=False, target_enabled=True,
            hire_days=None, vehicles=[], dispatch_center_id=2534509,
        )
        assert get_preset(db, 5).dispatch_center_id == 2534509
    finally:
        db.close()
