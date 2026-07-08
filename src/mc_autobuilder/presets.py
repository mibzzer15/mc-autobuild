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

from .mc_client import MissionChiefClient, summarize_html_for_log
from .models import StationPreset, count_preset_vehicle_purchases, log_preset_action

logger = logging.getLogger("mc_autobuilder.presets")


@dataclass
class VehiclePresetItem:
    vehicle_type_id: int
    count: int
    personnel_per_vehicle: int = 0


@dataclass
class PresetOutcome:
    """Result of applying a preset. `complete` is True only when every configured target was
    actually reached (expanded to the target level, service state set, hiring started, every
    vehicle bought, dispatch assigned) — the signal used to decide a station is fully "done",
    distinct from the build itself succeeding. A crew shortfall (not enough hireable personnel)
    is not treated as incomplete, since re-running can't conjure people who don't exist yet."""

    complete: bool
    messages: list[str]


def parse_vehicles_json(raw: str) -> list[VehiclePresetItem]:
    return [VehiclePresetItem(**item) for item in json.loads(raw or "[]")]


def dump_vehicles_json(items: list[dict]) -> str:
    return json.dumps(items)


def apply_preset(mc_client: MissionChiefClient, db, building_id: int, preset: StationPreset) -> PresetOutcome:
    """Applies every configured part of the preset and reports what happened. Every individual
    action is also logged to preset_action_log as it completes (not just at the end), so progress
    is visible on the building's page even while this is still running. `complete` in the returned
    outcome is True only if every configured target was reached."""
    messages: list[str] = []
    complete = True

    if preset.target_level:
        msgs, ok = _expand_to_level(mc_client, db, building_id, preset.target_level)
        messages.extend(msgs)
        complete = complete and ok

    if preset.manage_service:
        msgs, ok = _apply_service_state(mc_client, db, building_id, preset.target_enabled)
        messages.extend(msgs)
        complete = complete and ok

    if preset.personnel_count_target is not None:
        msgs, ok = _apply_personnel_target(mc_client, db, building_id, preset.personnel_count_target)
        messages.extend(msgs)
        complete = complete and ok

    if preset.hire_automatic:
        msgs, ok = _apply_auto_hiring(mc_client, db, building_id)
        messages.extend(msgs)
        complete = complete and ok
    elif preset.hire_days:
        msgs, ok = _apply_hiring(mc_client, db, building_id, preset.hire_days)
        messages.extend(msgs)
        complete = complete and ok

    # Shared across every vehicle item in this run so the same person never gets claimed for
    # two different vehicles bought in the same application (see _assign_crew).
    claimed_personal_ids: set[int] = set()
    for item in parse_vehicles_json(preset.vehicles_json):
        msgs, ok = _apply_vehicle_target(mc_client, db, building_id, item, claimed_personal_ids)
        messages.extend(msgs)
        complete = complete and ok

    if preset.dispatch_center_id is not None:
        msgs, ok = _apply_dispatch_center(mc_client, db, building_id, preset.dispatch_center_id)
        messages.extend(msgs)
        complete = complete and ok

    if not messages:
        messages.append("Nothing to do — this preset has no actions configured.")
    return PresetOutcome(complete=complete, messages=messages)


def _apply_dispatch_center(
    mc_client: MissionChiefClient, db, building_id: int, leitstelle_id: int
) -> tuple[list[str], bool]:
    try:
        result = mc_client.set_dispatch_center(building_id, leitstelle_id)
    except Exception as exc:
        log_preset_action(db, building_id, "set_dispatch_center", leitstelle_id, False, str(exc))
        logger.exception("Preset dispatch: error assigning building %s to leitstelle %s", building_id, leitstelle_id)
        return [f"Could not assign dispatch center {leitstelle_id}: {exc}"], False

    log_preset_action(
        db, building_id, "set_dispatch_center", leitstelle_id, result.success,
        f"leitstelle {leitstelle_id}" if result.success else "not confirmed",
    )
    if not result.success:
        return [f"Could not confirm dispatch-center assignment to {leitstelle_id}."], False
    return [f"Assigned to dispatch center {leitstelle_id}."], True


def _best_expand_param(prices: dict[int, int], current_level: int, target_level: int) -> int | None:
    """Pick the `?level=` query param that jumps the building as close to `target_level` as the
    expand page allows without overshooting. To land on level L you click `?level=L-1` (confirmed:
    a level-0 building expands via `?level=0` to reach level 1). MissionChief lists every reachable
    target level as its own link and clicking a far one jumps straight there, so we take the
    highest offered rung that is at or above the current level and at or below `target_level - 1`."""
    candidates = [p for p in prices if current_level <= p <= target_level - 1]
    return max(candidates) if candidates else None


def _expand_to_level(
    mc_client: MissionChiefClient, db, building_id: int, target_level: int
) -> tuple[list[str], bool]:
    messages = []
    try:
        current_level = mc_client.get_building_detail(building_id)["level"]
    except Exception as exc:
        messages.append(f"Stopped expanding: could not read live state ({exc}).")
        logger.exception("Preset expand: could not read state for building %s", building_id)
        return messages, False

    if current_level >= target_level:
        messages.append(f"Already at level {current_level} (target {target_level}).")
        return messages, True

    # Jump straight to the target in a single request rather than buying one rung at a time (up to
    # ~38 extra round-trips for a full level-up). The loop only re-runs as a fallback if a single
    # jump lands short of the target - e.g. if an account ever caps how far one expand can go.
    while current_level < target_level:
        try:
            prices = mc_client.get_expand_prices(building_id)
        except Exception as exc:
            messages.append(f"Stopped expanding at level {current_level}: could not read live prices ({exc}).")
            logger.exception("Preset expand: could not read prices for building %s", building_id)
            break

        param = _best_expand_param(prices, current_level, target_level)
        if param is None:
            messages.append(
                f"No expand option available to reach level {target_level} from level {current_level} "
                f"(offered levels: {sorted(prices)})."
            )
            break

        try:
            result = mc_client.expand_building(building_id, param)
        except Exception as exc:
            messages.append(f"Stopped expanding at level {current_level}: {exc}")
            log_preset_action(db, building_id, "expand", param, False, str(exc))
            logger.exception("Preset expand: error expanding building %s", building_id)
            break

        log_preset_action(
            db, building_id, "expand", param, result.success,
            f"{result.price} credits" if result.success else "not confirmed",
        )
        if not result.success:
            messages.append(f"Stopped expanding at level {current_level}: could not confirm the expansion.")
            break
        messages.append(f"Expanded to level {result.new_level} ({result.price:,} credits).")

        # Trust the independently-verified level from the result. Guard against a no-op response
        # (level didn't move) so a bad jump can't spin this loop forever.
        if result.new_level is None or result.new_level <= current_level:
            messages.append("Expansion didn't advance the level — stopping.")
            break
        current_level = result.new_level

    return messages, current_level >= target_level


def _apply_service_state(
    mc_client: MissionChiefClient, db, building_id: int, target_enabled: bool
) -> tuple[list[str], bool]:
    try:
        current_enabled = mc_client.get_building_detail(building_id)["enabled"]
    except Exception as exc:
        logger.exception("Preset service state: could not read state for building %s", building_id)
        return [f"Could not check service state: {exc}"], False

    if current_enabled == target_enabled:
        return [], True

    try:
        result = mc_client.toggle_service(building_id)
    except Exception as exc:
        log_preset_action(db, building_id, "toggle_service", None, False, str(exc))
        logger.exception("Preset service state: error toggling building %s", building_id)
        return [f"Could not set service state: {exc}"], False

    log_preset_action(db, building_id, "toggle_service", None, result.success, str(result.enabled))
    if not result.success:
        return ["Could not confirm the service-state change."], False
    return [f"Service state set to {'in service' if result.enabled else 'out of service'}."], True


def _apply_hiring(mc_client: MissionChiefClient, db, building_id: int, hire_days: int) -> tuple[list[str], bool]:
    try:
        hiring_phase = mc_client.get_building_detail(building_id)["hiring_phase"]
    except Exception as exc:
        logger.exception("Preset hiring: could not read state for building %s", building_id)
        return [f"Could not check hiring phase: {exc}"], False

    if hiring_phase:
        return ["A recruiting phase is already active — skipped."], True

    try:
        result = mc_client.hire(building_id, hire_days)
    except Exception as exc:
        log_preset_action(db, building_id, "hire", hire_days, False, str(exc))
        logger.exception("Preset hiring: error hiring for building %s", building_id)
        return [f"Could not start hiring: {exc}"], False

    log_preset_action(db, building_id, "hire", hire_days, result.success, "started" if result.success else "not confirmed")
    if not result.success:
        return ["Could not confirm the recruiting phase started."], False
    return [f"Started a {hire_days}-day recruiting phase."], True


def _apply_auto_hiring(mc_client: MissionChiefClient, db, building_id: int) -> tuple[list[str], bool]:
    try:
        result = mc_client.hire_automatic(building_id)
    except Exception as exc:
        log_preset_action(db, building_id, "hire_automatic", None, False, str(exc))
        logger.exception("Preset auto-hire: error for building %s", building_id)
        return [f"Could not enable auto-hire: {exc}"], False

    log_preset_action(
        db, building_id, "hire_automatic", None, result.success,
        "enabled" if result.success else "not confirmed (premium only?)",
    )
    if not result.success:
        return [
            "Could not confirm auto-hire turned on — it's a premium feature, so this account may "
            "not have it. Use a 1/2/3-day recruiting phase instead."
        ], False
    return ["Auto-hire enabled (recruits toward the staffing target)."], True


def _apply_personnel_target(mc_client: MissionChiefClient, db, building_id: int, target: int) -> tuple[list[str], bool]:
    try:
        ok = mc_client.set_personnel_count_target(building_id, target)
    except Exception as exc:
        log_preset_action(db, building_id, "personnel_target", target, False, str(exc))
        logger.exception("Preset personnel target: error for building %s", building_id)
        return [f"Could not set the personnel target: {exc}"], False

    log_preset_action(db, building_id, "personnel_target", target, ok, "set" if ok else "not confirmed")
    if not ok:
        return [f"Could not confirm the personnel target was set to {target}."], False
    return [f"Set personnel (desired) target to {target}."], True


def _apply_vehicle_target(
    mc_client: MissionChiefClient, db, building_id: int, item: VehiclePresetItem, claimed_personal_ids: set[int]
) -> tuple[list[str], bool]:
    already = count_preset_vehicle_purchases(db, building_id, item.vehicle_type_id)
    to_buy = item.count - already
    if to_buy <= 0:
        return [], True

    messages = []
    bought = 0
    # Chain the vehicle-id snapshot across this shopping list so we don't re-download the whole
    # /api/vehicles list before every single purchase (see buy_vehicle).
    before_ids: set[int] | None = None
    for _ in range(to_buy):
        try:
            result = mc_client.buy_vehicle(building_id, item.vehicle_type_id, before_ids=before_ids)
        except Exception as exc:
            log_preset_action(db, building_id, "buy_vehicle", item.vehicle_type_id, False, str(exc))
            logger.exception("Preset vehicles: error buying for building %s", building_id)
            messages.append(f"Could not buy vehicle_type {item.vehicle_type_id}: {exc}")
            break

        detail = summarize_html_for_log(result.response_text, max_chars=180)
        log_preset_action(
            db, building_id, "buy_vehicle", item.vehicle_type_id, result.success,
            f"{result.price} credits" if result.success else f"not confirmed: {detail}",
        )
        if not result.success:
            messages.append(
                f"Could not confirm a vehicle_type {item.vehicle_type_id} purchase — stopping this "
                f"type. Server said: {detail!r}"
            )
            break
        before_ids = result.known_vehicle_ids
        bought += 1
        messages.append(f"Bought vehicle_type {item.vehicle_type_id} ({result.price:,} credits).")

        if item.personnel_per_vehicle > 0 and result.vehicle:
            # Crew assignment is best-effort — a shortage of hireable personnel doesn't make the
            # preset "incomplete" (re-running can't create people), so it doesn't affect the flag.
            messages.extend(
                _assign_crew(
                    mc_client, db, building_id, result.vehicle["id"], item.personnel_per_vehicle, claimed_personal_ids
                )
            )
    return messages, bought == to_buy


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
