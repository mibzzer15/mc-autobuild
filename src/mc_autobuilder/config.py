"""Loads and validates config.yaml. See config.example.yaml for the fully-commented schema."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# Ordered so a freshly-written config.yaml (from the web dashboard's Config editor) reads in the
# same section order as config.example.yaml, rather than whatever order a plain dict happened to
# accumulate keys in.
_SECTION_ORDER = [
    "mission_chief", "regions", "building_types", "dedupe", "naming", "budget",
    "rlm_cache", "rate_limiting",
]


def load_raw_config(path: str | Path) -> dict:
    """Loads config.yaml as a plain dict, with no validation - used by the web dashboard's
    Config editor, which needs to work even with a partially-filled or missing file (unlike
    Config.from_yaml, which is strict since the CLI needs a fully valid config to run `plan`)."""
    raw_path = Path(path)
    if not raw_path.exists():
        return {}
    return yaml.safe_load(raw_path.read_text()) or {}


def save_raw_config(path: str | Path, data: dict) -> None:
    ordered = {k: data[k] for k in _SECTION_ORDER if k in data}
    ordered.update({k: v for k, v in data.items() if k not in ordered})
    Path(path).write_text(yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True))


class ConfigError(ValueError):
    """Raised when config.yaml is missing required fields or self-contradictory."""


@dataclass
class RegionConfig:
    name: str
    bbox: dict | None = None
    city: str | None = None
    center: dict | None = None
    radius_km: float | None = None

    def __post_init__(self) -> None:
        modes = [self.bbox is not None, self.city is not None, self.center is not None]
        if sum(modes) != 1:
            raise ConfigError(
                f"Region {self.name!r} must specify exactly one of: bbox, city, or center "
                "(with radius_km)."
            )
        if (self.city is not None or self.center is not None) and self.radius_km is None:
            raise ConfigError(f"Region {self.name!r} uses city/center and must also set radius_km.")
        if self.bbox is not None:
            missing = {"north", "south", "east", "west"} - self.bbox.keys()
            if missing:
                raise ConfigError(f"Region {self.name!r} bbox is missing keys: {sorted(missing)}")
        if self.center is not None:
            missing = {"lat", "lng"} - self.center.keys()
            if missing:
                raise ConfigError(f"Region {self.name!r} center is missing keys: {sorted(missing)}")


@dataclass
class BuildingTypeConfig:
    poi_type: str
    building_type: int
    max_per_run: int | None = None


@dataclass
class Config:
    game_world: str
    mc_base_url: str
    regions: list[RegionConfig]
    building_types: list[BuildingTypeConfig]
    dedupe_radius_m: float
    naming_template: str
    max_credits_per_run: int | None
    credit_reserve: int
    rlm_cache_ttl_hours: float
    rlm_cache_dir: str
    rate_limit_min_delay: float
    rate_limit_max_delay: float

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Config":
        raw_path = Path(path)
        if not raw_path.exists():
            raise ConfigError(
                f"{raw_path} not found. Copy config.example.yaml to {raw_path} and edit it."
            )
        data = yaml.safe_load(raw_path.read_text()) or {}

        mission_chief = data.get("mission_chief", {})
        if "game_world" not in mission_chief:
            raise ConfigError("mission_chief.game_world is required (e.g. 'US') — see docs/rlm-api.md")

        regions_raw = data.get("regions") or []
        if not regions_raw:
            raise ConfigError("At least one entry in `regions` is required.")
        regions = [RegionConfig(**r) for r in regions_raw]

        building_types_raw = data.get("building_types") or {}
        if not building_types_raw:
            raise ConfigError("At least one entry in `building_types` is required.")
        building_types = [
            BuildingTypeConfig(poi_type=poi_type, **cfg) for poi_type, cfg in building_types_raw.items()
        ]

        dedupe = data.get("dedupe", {})
        naming = data.get("naming", {})
        budget = data.get("budget", {})
        rlm_cache = data.get("rlm_cache", {})
        rate_limiting = data.get("rate_limiting", {})

        naming_template = naming.get("template", "{poi_name}")
        try:
            naming_template.format(poi_name="test", city="test")
        except (KeyError, IndexError) as exc:
            raise ConfigError(
                f"naming.template {naming_template!r} uses an unsupported placeholder "
                "(only {poi_name} and {city} are available)."
            ) from exc

        return cls(
            game_world=mission_chief["game_world"],
            mc_base_url=mission_chief.get("base_url", "https://www.missionchief.com"),
            regions=regions,
            building_types=building_types,
            dedupe_radius_m=float(dedupe.get("radius_m", 150.0)),
            naming_template=naming_template,
            max_credits_per_run=budget.get("max_credits_per_run"),
            credit_reserve=int(budget.get("credit_reserve", 0)),
            rlm_cache_ttl_hours=float(rlm_cache.get("ttl_hours", 24.0)),
            rlm_cache_dir=rlm_cache.get("cache_dir", ".rlm_cache"),
            rate_limit_min_delay=float(rate_limiting.get("min_delay_seconds", 2.0)),
            rate_limit_max_delay=float(rate_limiting.get("max_delay_seconds", 5.0)),
        )
