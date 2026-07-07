"""SQLite-backed local cache of MissionChief state, via SQLAlchemy."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

Base = declarative_base()


class Building(Base):
    __tablename__ = "buildings"

    id = Column(Integer, primary_key=True)
    building_type = Column(Integer, nullable=False)
    caption = Column(String, nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    level = Column(Integer, nullable=False, default=0)
    personal_count = Column(Integer, nullable=False, default=0)
    personal_count_target = Column(Integer, nullable=False, default=0)
    small_building = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=True)
    hiring_phase = Column(Integer, nullable=False, default=0)
    hiring_automatic = Column(Boolean, nullable=False, default=False)
    leitstelle_building_id = Column(Integer, nullable=True)
    updated_iso = Column(String, nullable=True)
    raw_json = Column(Text, nullable=False)
    synced_at = Column(DateTime, nullable=False)


class CompletedAction(Base):
    """Records every completed write action (builds, and later expansions/purchases/etc.), so
    an interrupted or re-run command doesn't repeat something already done."""

    __tablename__ = "completed_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    action_type = Column(String, nullable=False)  # e.g. "build"
    poi_id = Column(Integer, nullable=True)
    building_id = Column(Integer, nullable=True)  # the resulting MissionChief building id
    building_type = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    cost = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False)


class StationPreset(Base):
    """One row per building_type: what to do to every station of that type — expand to a target
    level, a desired service state, a free hiring phase, and a shopping list of vehicles (each
    optionally with crew to assign). Applying a preset (presets.py) is idempotent, so this table
    only needs to describe the *target* state, not a one-shot script."""

    __tablename__ = "station_presets"

    building_type = Column(Integer, primary_key=True)
    target_level = Column(Integer, nullable=True)  # 1-39; None = don't manage expand
    manage_service = Column(Boolean, nullable=False, default=False)
    target_enabled = Column(Boolean, nullable=False, default=True)
    hire_days = Column(Integer, nullable=True)  # 1, 2, or 3 - confirmed live options (no "auto" yet)
    # JSON list of {"vehicle_type_id": int, "count": int, "personnel_per_vehicle": int} - presets.py.
    vehicles_json = Column(Text, nullable=False, default="[]")
    updated_at = Column(DateTime, nullable=False)


class PresetActionLog(Base):
    """Append-only record of every action a preset application has taken for a specific
    building. Two jobs: (1) shows progress on the building's page while/after a preset runs
    (which can take minutes - expand-to-max alone can be dozens of sequential, rate-limited
    requests), and (2) is what makes vehicle-purchase counts idempotent - MissionChief's own API
    has no confirmed way to reliably tell "how many of catalog vehicle_type_id X are already at
    this station" (see docs/missionchief-api.md), so we track our own purchases instead of
    guessing at an unconfirmed field mapping."""

    __tablename__ = "preset_action_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    building_id = Column(Integer, nullable=False)
    action_type = Column(String, nullable=False)  # "expand" | "toggle_service" | "hire" | "buy_vehicle"
    detail = Column(Integer, nullable=True)  # level / vehicle_type_id / hire days, depending on action_type
    success = Column(Boolean, nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False)


def get_engine(db_path: str) -> Engine:
    return create_engine(f"sqlite:///{db_path}")


def init_db(db_path: str) -> Engine:
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


def get_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine)


def upsert_buildings(db: Session, buildings: list[dict]) -> None:
    """Insert or update each building by id. Safe to call repeatedly (idempotent)."""
    now = datetime.now(timezone.utc)
    for b in buildings:
        obj = db.get(Building, b["id"])
        if obj is None:
            obj = Building(id=b["id"])
            db.add(obj)
        obj.building_type = b["building_type"]
        obj.caption = b["caption"]
        obj.latitude = b["latitude"]
        obj.longitude = b["longitude"]
        obj.level = b.get("level", 0)
        obj.personal_count = b.get("personal_count", 0)
        obj.personal_count_target = b.get("personal_count_target", 0)
        obj.small_building = b.get("small_building", False)
        obj.enabled = b.get("enabled", True)
        obj.hiring_phase = b.get("hiring_phase", 0)
        obj.hiring_automatic = b.get("hiring_automatic", False)
        obj.leitstelle_building_id = b.get("leitstelle_building_id")
        obj.updated_iso = b.get("updated_iso")
        obj.raw_json = json.dumps(b)
        obj.synced_at = now
    db.commit()


def has_completed_action(db: Session, action_type: str, poi_id: int) -> CompletedAction | None:
    return (
        db.query(CompletedAction)
        .filter_by(action_type=action_type, poi_id=poi_id)
        .one_or_none()
    )


def record_completed_action(
    db: Session,
    *,
    action_type: str,
    poi_id: int | None,
    building_id: int | None,
    building_type: int,
    name: str,
    cost: int | None,
) -> None:
    db.add(
        CompletedAction(
            action_type=action_type,
            poi_id=poi_id,
            building_id=building_id,
            building_type=building_type,
            name=name,
            cost=cost,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


def get_preset(db: Session, building_type: int) -> StationPreset | None:
    return db.get(StationPreset, building_type)


def list_presets(db: Session) -> list[StationPreset]:
    return db.query(StationPreset).order_by(StationPreset.building_type).all()


def save_preset(
    db: Session,
    building_type: int,
    *,
    target_level: int | None,
    manage_service: bool,
    target_enabled: bool,
    hire_days: int | None,
    vehicles: list[dict],
) -> None:
    obj = db.get(StationPreset, building_type)
    if obj is None:
        obj = StationPreset(building_type=building_type)
        db.add(obj)
    obj.target_level = target_level
    obj.manage_service = manage_service
    obj.target_enabled = target_enabled
    obj.hire_days = hire_days
    obj.vehicles_json = json.dumps(vehicles)
    obj.updated_at = datetime.now(timezone.utc)
    db.commit()


def log_preset_action(
    db: Session, building_id: int, action_type: str, detail: int | None, success: bool, message: str
) -> None:
    db.add(
        PresetActionLog(
            building_id=building_id,
            action_type=action_type,
            detail=detail,
            success=success,
            message=message,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


def count_preset_vehicle_purchases(db: Session, building_id: int, vehicle_type_id: int) -> int:
    return (
        db.query(PresetActionLog)
        .filter_by(building_id=building_id, action_type="buy_vehicle", detail=vehicle_type_id, success=True)
        .count()
    )


def get_preset_log(db: Session, building_id: int, limit: int = 20) -> list[PresetActionLog]:
    return (
        db.query(PresetActionLog)
        .filter_by(building_id=building_id)
        .order_by(PresetActionLog.created_at.desc())
        .limit(limit)
        .all()
    )
