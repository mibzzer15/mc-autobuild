"""Applies a per-building-type StationPreset (models.py) to a specific building: expand to a
target level, a desired service state, a free hiring phase, a shopping list of vehicles, and
(optionally) crew for each vehicle bought.

Idempotent by design, so it's always safe to re-run (e.g. after an interrupted application, or
after editing a preset): expand/service-state check live /api/buildings/<id> fields before acting,
hiring skips if a phase is already running, and vehicle purchases are capped against our own
preset_action_log rather than trusting an unconfirmed way to count existing vehicles by catalog
type from the game's own data (see models.py's PresetActionLog docstring).

Only covers actions confirmed against a real account (see docs/missionchief-api.md): station
extensions, equipment purchase, and "auto" (premium) hiring are NOT included here - they were
never captured live, so building preset support for them now would mean guessing at endpoints,
which this project deliberately never does.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .mc_client import MissionChiefClient
from .models import StationPreset, count_preset_vehicle_purchases, log_preset_action

logger = logging.getLogger("mc_autobuilder.presets")


@dataclass
class VehiclePresetItem:
    vehicle_type_id: int
    count: int
    personnel_per_vehicle: int = 0


def parse_vehicles_json(raw: str) -> list[VehiclePresetItem]:
    return [VehiclePresetItem(**item) for item in json.loads(raw or "[]")]


def dump_vehicles_json(items: list[dict]) -> str:
    return json.dumps(items)


def apply_preset(mc_client: MissionChiefClient, db, building_id: int, preset: StationPreset) -> list[str]:
    """Returns human-readable messages describing what happened, in the order taken. Every
    individual action is logged to preset_action_log as it completes (not just at the end), so
    progress is visible on the building's page even while this is still running."""
    messages: list[str] = []

    if preset.target_level:
        messages.extend(_expand_to_level(mc_client, db, building_id, preset.target_level))

    if preset.manage_service:
        messages.extend(_apply_service_state(mc_client, db, building_id, preset.target_enabled))

    if preset.hire_days:
        messages.extend(_apply_hiring(mc_client, db, building_id, preset.hire_days))

    # Shared across every vehicle item in this run so the same person never gets claimed for
    # two different vehicles bought in the same application (see _assign_crew).
    claimed_personal_ids: set[int] = set()
    for item in parse_vehicles_json(preset.vehicles_json):
        messages.extend(_apply_vehicle_target(mc_client, db, building_id, item, claimed_personal_ids))

    if not messages:
        messages.append("Nothing to do — this preset has no actions configured.")
    return messages


def _expand_to_level(mc_client: MissionChiefClient, db, building_id: int, target_level: int) -> list[str]:
    messages = []
    while True:
        try:
            current_level = mc_client.get_building_detail(building_id)["level"]
        except Exception as exc:
            messages.append(f"Stopped expanding: could not read live state ({exc}).")
            logger.exception("Preset expand: could not read state for building %s", building_id)
            break

        if current_level >= target_level:
            messages.append(f"Already at level {current_level} (target {target_level}).")
            break

        try:
            prices = mc_client.get_expand_prices(building_id)
        except Exception as exc:
            messages.append(f"Stopped expanding at level {current_level}: could not read live prices ({exc}).")
            logger.exception("Preset expand: could not read prices for building %s", building_id)
            break

        # Confirmed live (docs/missionchief-api.md): the query param for "expand to the next
        # level" equals the CURRENT level, not current+1 - e.g. a building at level 0 expands via
        # `?level=0` to reach level 1. Passing current+1 here would buy the wrong, more expensive
        # rung while skipping the one actually available next.
        if current_level not in prices:
            messages.append(f"No expand option available from level {current_level} — can't reach level {target_level}.")
            break

        try:
            result = mc_client.expand_building(building_id, current_level)
        except Exception as exc:
            messages.append(f"Stopped expanding at level {current_level}: {exc}")
            log_preset_action(db, building_id, "expand", current_level, False, str(exc))
            logger.exception("Preset expand: error expanding building %s", building_id)
            break

        log_preset_action(
            db, building_id, "expand", current_level, result.success,
            f"{result.price} credits" if result.success else "not confirmed",
        )
        if not result.success:
            messages.append(f"Stopped expanding at level {current_level}: could not confirm the expansion.")
            break
        messages.append(f"Expanded to level {result.new_level} ({result.price:,} credits).")
    return messages


def _apply_service_state(mc_client: MissionChiefClient, db, building_id: int, target_enabled: bool) -> list[str]:
    try:
        current_enabled = mc_client.get_building_detail(building_id)["enabled"]
    except Exception as exc:
        logger.exception("Preset service state: could not read state for building %s", building_id)
        return [f"Could not check service state: {exc}"]

    if current_enabled == target_enabled:
        return []

    try:
        result = mc_client.toggle_service(building_id)
    except Exception as exc:
        log_preset_action(db, building_id, "toggle_service", None, False, str(exc))
        logger.exception("Preset service state: error toggling building %s", building_id)
        return [f"Could not set service state: {exc}"]

    log_preset_action(db, building_id, "toggle_service", None, result.success, str(result.enabled))
    if not result.success:
        return ["Could not confirm the service-state change."]
    return [f"Service state set to {'in service' if result.enabled else 'out of service'}."]


def _apply_hiring(mc_client: MissionChiefClient, db, building_id: int, hire_days: int) -> list[str]:
    try:
        hiring_phase = mc_client.get_building_detail(building_id)["hiring_phase"]
    except Exception as exc:
        logger.exception("Preset hiring: could not read state for building %s", building_id)
        return [f"Could not check hiring phase: {exc}"]

    if hiring_phase:
        return ["A recruiting phase is already active — skipped."]

    try:
        result = mc_client.hire(building_id, hire_days)
    except Exception as exc:
        log_preset_action(db, building_id, "hire", hire_days, False, str(exc))
        logger.exception("Preset hiring: error hiring for building %s", building_id)
        return [f"Could not start hiring: {exc}"]

    log_preset_action(db, building_id, "hire", hire_days, result.success, "started" if result.success else "not confirmed")
    if not result.success:
        return ["Could not confirm the recruiting phase started."]
    return [f"Started a {hire_days}-day recruiting phase."]


def _apply_vehicle_target(
    mc_client: MissionChiefClient, db, building_id: int, item: VehiclePresetItem, claimed_personal_ids: set[int]
) -> list[str]:
    already = count_preset_vehicle_purchases(db, building_id, item.vehicle_type_id)
    to_buy = item.count - already
    if to_buy <= 0:
        return []

    messages = []
    for _ in range(to_buy):
        try:
            result = mc_client.buy_vehicle(building_id, item.vehicle_type_id)
        except Exception as exc:
            log_preset_action(db, building_id, "buy_vehicle", item.vehicle_type_id, False, str(exc))
            logger.exception("Preset vehicles: error buying for building %s", building_id)
            messages.append(f"Could not buy vehicle_type {item.vehicle_type_id}: {exc}")
            break

        log_preset_action(
            db, building_id, "buy_vehicle", item.vehicle_type_id, result.success,
            f"{result.price} credits" if result.success else "not confirmed",
        )
        if not result.success:
            messages.append(f"Could not confirm a vehicle_type {item.vehicle_type_id} purchase — stopping this type.")
            break
        messages.append(f"Bought vehicle_type {item.vehicle_type_id} ({result.price:,} credits).")

        if item.personnel_per_vehicle > 0 and result.vehicle:
            messages.extend(
                _assign_crew(
                    mc_client, db, building_id, result.vehicle["id"], item.personnel_per_vehicle, claimed_personal_ids
                )
            )
    return messages


def _assign_crew(
    mc_client: MissionChiefClient, db, building_id: int, vehicle_id: int, count: int, claimed_personal_ids: set[int]
) -> list[str]:
    try:
        roster = mc_client.get_personnel_roster(building_id)
    except Exception as exc:
        logger.exception("Preset crew assignment: could not read roster for building %s", building_id)
        return [f"Could not read personnel roster to staff vehicle {vehicle_id}: {exc}"]

    available = [p for p in roster if p.assigned_to is None and p.personal_id not in claimed_personal_ids]
    to_assign = available[:count]

    messages = []
    for person in to_assign:
        try:
            result = mc_client.assign_personnel(vehicle_id, person.personal_id)
        except Exception as exc:
            log_preset_action(db, building_id, "assign_personnel", person.personal_id, False, str(exc))
            logger.exception("Preset crew assignment: error assigning personnel for building %s", building_id)
            messages.append(f"Could not assign {person.name} to vehicle {vehicle_id}: {exc}")
            continue

        log_preset_action(
            db, building_id, "assign_personnel", person.personal_id, result.success,
            f"vehicle {vehicle_id}" if result.success else "not confirmed",
        )
        if result.success:
            claimed_personal_ids.add(person.personal_id)
            messages.append(f"Assigned {person.name} to vehicle {vehicle_id}.")
        else:
            messages.append(f"Could not confirm assigning {person.name} to vehicle {vehicle_id}.")

    if len(to_assign) < count:
        messages.append(
            f"Only {len(to_assign)}/{count} available (unassigned) personnel found to staff vehicle "
            f"{vehicle_id} — hire more, or lower personnel_per_vehicle."
        )
    return messages
