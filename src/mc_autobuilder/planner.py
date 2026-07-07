"""Builds a dry-run plan: which RLM POIs should become new MissionChief stations.

Deliberately pure/side-effect-free — everything here takes already-fetched data (existing
buildings, RLM candidates, live prices) as plain dicts/dataclasses, so it's fully unit-testable
without mocking HTTP. Fetching happens in rlm_client.py/mc_client.py; this module only decides
what to do with the results.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

EARTH_RADIUS_M = 6_371_000


def haversine_distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters between two lat/lng points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def find_duplicate(
    candidate_lat: float,
    candidate_lng: float,
    candidate_building_type: int,
    existing_buildings: list[dict],
    dedupe_radius_m: float,
) -> dict | None:
    """Return the existing building (with a `_distance_m` key added) if one of the same
    `building_type` is within `dedupe_radius_m` of the candidate, else None."""
    best: dict | None = None
    for b in existing_buildings:
        if b["building_type"] != candidate_building_type:
            continue
        dist = haversine_distance_m(candidate_lat, candidate_lng, b["latitude"], b["longitude"])
        if dist <= dedupe_radius_m and (best is None or dist < best["_distance_m"]):
            best = {**b, "_distance_m": dist}
    return best


# Confirmed live (docs/missionchief-api.md): building[name] rejects anything over 40 characters
# ("is too long (maximum is 40 characters)"), re-rendering the form with HTTP 200 instead of the
# usual redirect - this is what silently sank "Union City Police Department- Fremont, CA" (41
# chars) while shorter names built fine at the same price.
MAX_BUILDING_NAME_LENGTH = 40


def render_name(template: str, poi: dict, city: str = "") -> str:
    poi_name = poi.get("name") or "Unnamed Station"
    name = template.format(poi_name=poi_name, city=city)
    overflow = len(name) - MAX_BUILDING_NAME_LENGTH
    if overflow > 0:
        # Shorten the POI name first - it's the variable-length part - rather than the city
        # suffix, which matters more for keeping stations organized by region.
        poi_name = poi_name[: max(0, len(poi_name) - overflow)].rstrip()
        name = template.format(poi_name=poi_name, city=city)
    return name[:MAX_BUILDING_NAME_LENGTH]


@dataclass
class PlannedBuild:
    poi_id: int | None
    poi_name: str
    building_type: int
    building_type_name: str
    name: str
    latitude: float
    longitude: float
    estimated_cost: int | None = None


@dataclass
class SkippedDuplicate:
    poi_id: int | None
    poi_name: str
    building_type: int
    existing_building_id: int
    distance_m: float


@dataclass
class SkippedEntry:
    poi_id: int | None
    poi_name: str
    building_type: int
    reason: str
    estimated_cost: int | None = None


@dataclass
class Plan:
    to_build: list[PlannedBuild] = field(default_factory=list)
    skipped_duplicates: list[SkippedDuplicate] = field(default_factory=list)
    skipped_capped: list[SkippedEntry] = field(default_factory=list)
    skipped_budget: list[SkippedEntry] = field(default_factory=list)
    total_estimated_cost: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def build_plan(
    candidates: list[dict],
    existing_buildings: list[dict],
    *,
    dedupe_radius_m: float = 150.0,
    naming_template: str = "{poi_name}",
    city: str = "",
    per_type_caps: dict[int, int] | None = None,
    prices: dict[int, int] | None = None,
    max_total_cost: int | None = None,
) -> Plan:
    """`candidates` is a list of normalized RLM POI dicts, each already tagged with the
    `building_type` (int) and `building_type_name` (str) it should become. `existing_buildings`
    is the locally cached `Building` rows (as dicts) from `sync`."""
    plan = Plan()
    per_type_caps = per_type_caps or {}
    prices = prices or {}
    built_count_by_type: dict[int, int] = {}

    for poi in candidates:
        building_type = poi["building_type"]
        building_type_name = poi.get("building_type_name", "")
        lat, lng = poi["lat"], poi["lng"]
        poi_id = poi.get("id")
        poi_name = poi.get("name", "")

        dup = find_duplicate(lat, lng, building_type, existing_buildings, dedupe_radius_m)
        if dup:
            plan.skipped_duplicates.append(
                SkippedDuplicate(
                    poi_id=poi_id,
                    poi_name=poi_name,
                    building_type=building_type,
                    existing_building_id=dup["id"],
                    distance_m=round(dup["_distance_m"], 1),
                )
            )
            continue

        cap = per_type_caps.get(building_type)
        current_count = built_count_by_type.get(building_type, 0)
        if cap is not None and current_count >= cap:
            plan.skipped_capped.append(
                SkippedEntry(
                    poi_id=poi_id,
                    poi_name=poi_name,
                    building_type=building_type,
                    reason=f"per-run cap of {cap} reached for this building type",
                )
            )
            continue

        cost = prices.get(building_type)
        if max_total_cost is not None and cost is not None and plan.total_estimated_cost + cost > max_total_cost:
            plan.skipped_budget.append(
                SkippedEntry(
                    poi_id=poi_id,
                    poi_name=poi_name,
                    building_type=building_type,
                    reason="would exceed max_credits_per_run",
                    estimated_cost=cost,
                )
            )
            continue

        plan.to_build.append(
            PlannedBuild(
                poi_id=poi_id,
                poi_name=poi_name,
                building_type=building_type,
                building_type_name=building_type_name,
                name=render_name(naming_template, poi, city=city),
                latitude=lat,
                longitude=lng,
                estimated_cost=cost,
            )
        )
        built_count_by_type[building_type] = current_count + 1
        if cost is not None:
            plan.total_estimated_cost += cost

    return plan
