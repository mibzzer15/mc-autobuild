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
