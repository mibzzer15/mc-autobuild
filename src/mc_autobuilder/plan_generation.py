"""Orchestrates a full plan-generation run: RLM candidate fetch + live prices + dedupe/build
planning (planner.py) + the plan.json shape. Shared between the CLI's `plan` command and the web
dashboard's Plan page "Generate plan" button, so both stay identical rather than drifting apart.
"""
from __future__ import annotations

import logging
from datetime import datetime

import requests

from .config import Config
from .constants import BUILDING_TYPES
from .mc_client import MissionChiefClient
from .planner import Plan, build_plan
from .rlm_client import BoundingBox, RLMClient, geocode_city

logger = logging.getLogger("mc_autobuilder.plan_generation")


def resolve_region_bbox(region, geocode_session: requests.Session) -> tuple[BoundingBox, str]:
    """Returns (bbox, city_label_for_naming_template)."""
    if region.bbox is not None:
        return BoundingBox(**region.bbox), region.name
    if region.city is not None:
        center = geocode_city(region.city, session=geocode_session)
        center_lat = (center.north + center.south) / 2
        center_lng = (center.east + center.west) / 2
        return BoundingBox.from_center_radius(center_lat, center_lng, region.radius_km), region.city
    return BoundingBox.from_center_radius(region.center["lat"], region.center["lng"], region.radius_km), region.name


def generate_plan(
    config: Config,
    mc_client: MissionChiefClient,
    rlm_client: RLMClient,
    existing_buildings: list[dict],
) -> dict:
    """Fetches live build prices + RLM candidates for every configured region, dedupes/plans each
    region (so the naming template's {city} reflects each candidate's own region), merges them,
    and returns the exact dict shape written to plan.json. Raises SessionExpiredError from the
    live price fetch if the session has expired - callers should let that propagate and handle it
    the same way every other live call is handled."""
    geocode_session = requests.Session()
    prices = mc_client.get_building_prices()
    per_type_caps = {bt.building_type: bt.max_per_run for bt in config.building_types if bt.max_per_run}

    all_candidates = []
    # Per (region, poi_type) candidate counts, so the dashboard can explain an empty plan: a
    # region that returned 0 RLM POIs points at a bad bbox / poi_type mapping, whereas lots of
    # candidates but 0 to build points at dedupe/cap/budget instead.
    candidate_breakdown: list[dict] = []
    for region in config.regions:
        bbox, city_label = resolve_region_bbox(region, geocode_session)
        logger.info("Region %s resolved to bbox %s", region.name, bbox)
        for bt_config in config.building_types:
            pois = rlm_client.get_pois(bt_config.poi_type, bbox)
            logger.info("Region %s / %s: %d candidate POIs", region.name, bt_config.poi_type, len(pois))
            candidate_breakdown.append(
                {"region": region.name, "poi_type": bt_config.poi_type, "candidates": len(pois)}
            )
            for poi in pois:
                all_candidates.append(
                    {
                        **poi,
                        "building_type": bt_config.building_type,
                        "building_type_name": BUILDING_TYPES.get(bt_config.building_type, ""),
                        "_region": region.name,
                        "_city_label": city_label,
                    }
                )

    merged = Plan()
    built_so_far_cost = 0
    for region in config.regions:
        region_candidates = [c for c in all_candidates if c["_region"] == region.name]
        if not region_candidates:
            continue
        city_label = region_candidates[0]["_city_label"]
        region_plan = build_plan(
            region_candidates,
            existing_buildings,
            dedupe_radius_m=config.dedupe_radius_m,
            naming_template=config.naming_template,
            city=city_label,
            per_type_caps=per_type_caps,
            prices=prices,
            max_total_cost=(
                config.max_credits_per_run - built_so_far_cost
                if config.max_credits_per_run is not None
                else None
            ),
        )
        merged.to_build.extend(region_plan.to_build)
        merged.skipped_duplicates.extend(region_plan.skipped_duplicates)
        merged.skipped_capped.extend(region_plan.skipped_capped)
        merged.skipped_budget.extend(region_plan.skipped_budget)
        merged.total_estimated_cost += region_plan.total_estimated_cost
        built_so_far_cost += region_plan.total_estimated_cost

    return {
        "generated_at": datetime.now().isoformat(),
        "regions": [r.name for r in config.regions],
        "candidate_breakdown": candidate_breakdown,
        "total_candidates": len(all_candidates),
        **merged.to_dict(),
        "budget": {
            "max_credits_per_run": config.max_credits_per_run,
            "credit_reserve": config.credit_reserve,
        },
    }
