"""Static MissionChief reference data, confirmed in docs/missionchief-api.md."""

DEFAULT_BASE_URL = "https://www.missionchief.com"

# From the `/buildings/new` form's `building[building_type]` select, captured 2026-07-05.
BUILDING_TYPES = {
    1: "Dispatch Center",
    0: "Fire station",
    13: "Fire station (Small station)",
    4: "Fire academy",
    3: "Ambulance station",
    16: "Ambulance station (Small station)",
    2: "Hospital",
    14: "Clinic",
    19: "Rescue (EMS) academy",
    7: "Police academy",
    5: "Police station",
    15: "Police station (Small station)",
    8: "Police Aviation",
    6: "Medical helicopter station",
    12: "Rescue boat dock",
    11: "Fire boat dock",
    23: "Coastal Rescue Station",
    26: "Lifeguard Post",
    24: "Coastal Rescue School",
    25: "Coastal Air Station",
    9: "Staging area",
    17: "Firefighting plane station",
    18: "Federal Police Station",
    22: "Fire Marshal's Office",
    10: "Prison",
    27: "Tow Truck Station",
    28: "Mountain Rescue Station",
}
