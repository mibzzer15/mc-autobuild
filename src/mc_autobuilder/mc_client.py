"""Read/write client for MissionChief's authenticated JSON endpoints.

Endpoint behavior documented in docs/missionchief-api.md. Every request goes through a shared
rate limiter: a randomized delay between requests, and exponential backoff on 429/5xx, per the
project's non-negotiable rate-limiting requirement.
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from .auth import check_session_alive
from .planner import MAX_BUILDING_NAME_LENGTH

logger = logging.getLogger(__name__)

# Confirmed in docs/missionchief-api.md: each building type on /buildings/new has its own
# `detail_<building_type>` block with a `build_credits_<building_type>` submit button reading
# "Build <price> Credits". Prices are dynamic (scale with account/alliance progression), so
# they're parsed live every time rather than ever being cached or hardcoded.
BUILD_PRICE_RE = re.compile(r"Build\s+([\d,]+)\s+Credits")


def parse_building_prices(html: str) -> dict[int, int]:
    soup = BeautifulSoup(html, "html.parser")
    prices: dict[int, int] = {}
    for button in soup.find_all("input", id=re.compile(r"^build_credits_\d+$")):
        building_type = int(button["id"].removeprefix("build_credits_"))
        match = BUILD_PRICE_RE.search(button.get("value", ""))
        if match:
            prices[building_type] = int(match.group(1).replace(",", ""))
    return prices


CREDITS_UPDATE_RE = re.compile(r"creditsUpdate\((\d+)\)")


def parse_credits_balance(html: str) -> int:
    """Parse the live credit balance from the inline `creditsUpdate(<n>)` JS call near the
    bottom of every authenticated page (confirmed against a real account).

    The nav bar's `<span class="credits-value">` is confirmed **empty** in the raw HTML — it's
    populated client-side by this same call, so it can't be read directly from a plain HTTP GET.
    `creditsUpdate(...)` works instead because it's server-rendered with the real current value
    at request time, not delivered via a later AJAX/websocket update — a plain GET already
    contains it (docs/missionchief-api.md).
    """
    match = CREDITS_UPDATE_RE.search(html)
    if not match:
        raise ValueError("Could not find creditsUpdate(...) on the page — is the session logged in?")
    return int(match.group(1))


@dataclass
class NewBuildingForm:
    authenticity_token: str
    prices: dict[int, int]
    # building_type -> (form field name, first <option> value) for types that have a
    # required "start vehicle" select (confirmed in docs/missionchief-api.md: only some types,
    # e.g. Fire station, have this at all — most don't).
    start_vehicle_fields: dict[int, tuple[str, str]]


def parse_new_building_form(html: str) -> NewBuildingForm:
    soup = BeautifulSoup(html, "html.parser")
    token_input = soup.find("input", {"name": "authenticity_token"})
    if not token_input or not token_input.get("value"):
        raise ValueError("Could not find authenticity_token on /buildings/new")

    start_vehicle_fields: dict[int, tuple[str, str]] = {}
    for detail_div in soup.find_all("div", id=re.compile(r"^detail_\d+$")):
        building_type = int(detail_div["id"].removeprefix("detail_"))
        select = detail_div.find("select")
        if select and select.get("name", "").startswith("building[start_vehicle"):
            options = [o["value"] for o in select.find_all("option") if o.get("value")]
            if options:
                field_name = select["name"][len("building[") : -1]
                start_vehicle_fields[building_type] = (field_name, options[0])

    return NewBuildingForm(
        authenticity_token=token_input["value"],
        prices=parse_building_prices(html),
        start_vehicle_fields=start_vehicle_fields,
    )


# Confirmed in docs/missionchief-api.md: /buildings/<id>/expand lists every level as a plain
# link, "Expand (<price> Credits)" — prices scale with level, so always parsed live, never cached.
EXPAND_LEVEL_HREF_RE = re.compile(r"/expand_do/credits\?level=(\d+)")
CREDITS_PRICE_RE = re.compile(r"([\d,]+)\s+Credits")


def parse_expand_prices(html: str) -> dict[int, int]:
    soup = BeautifulSoup(html, "html.parser")
    prices: dict[int, int] = {}
    for link in soup.find_all("a", href=EXPAND_LEVEL_HREF_RE):
        level = int(EXPAND_LEVEL_HREF_RE.search(link["href"]).group(1))
        match = CREDITS_PRICE_RE.search(link.get_text())
        if match:
            prices[level] = int(match.group(1).replace(",", ""))
    return prices


@dataclass
class VehicleOption:
    vehicle_type_id: int
    name: str
    price_credits: int
    return_tab: str
    # The exact relative href of the Credits purchase link as it appears on the page, e.g.
    # "/buildings/123/vehicle/123/0/credits?building=123&return_tab=fire_engine". Used verbatim by
    # buy_vehicle rather than reconstructing the URL - the two path ids aren't necessarily both the
    # building_id, so rebuilding from building_id alone can hit the wrong URL.
    purchase_href: str


# Confirmed in docs/missionchief-api.md: /buildings/<id>/vehicles/new groups purchasable vehicles
# into `<div class="vehicle_type well">` blocks, each with an <h3> name and a Credits/Coins link
# pair. `vehicle_type_id` here is a purchase-catalog id local to this page, distinct from the
# building_type enum used on /buildings/new.
VEHICLE_CREDITS_HREF_RE = re.compile(r"/vehicle/\d+/(\d+)/credits\?building=\d+&return_tab=([a-z_]+)")


def parse_vehicle_purchase_options(html: str) -> dict[int, VehicleOption]:
    soup = BeautifulSoup(html, "html.parser")
    options: dict[int, VehicleOption] = {}
    for block in soup.find_all("div", class_="vehicle_type"):
        heading = block.find("h3")
        link = block.find("a", href=VEHICLE_CREDITS_HREF_RE)
        if not heading or not link:
            continue
        match = VEHICLE_CREDITS_HREF_RE.search(link["href"])
        price_match = CREDITS_PRICE_RE.search(link.get_text())
        if not price_match:
            continue
        vehicle_type_id = int(match.group(1))
        options[vehicle_type_id] = VehicleOption(
            vehicle_type_id=vehicle_type_id,
            name=heading.get_text(strip=True),
            price_credits=int(price_match.group(1).replace(",", "")),
            return_tab=match.group(2),
            purchase_href=link["href"],
        )
    return options


# Confirmed in docs/missionchief-api.md: /buildings/<id>/hire lists free day-based recruiting
# options as plain `/hire_do/<n>` links; `hire_do/0` is "Cancel recruitment phase", not an option.
HIRE_DAY_HREF_RE = re.compile(r"/hire_do/(\d+)$")


def parse_hire_day_options(html: str) -> list[int]:
    soup = BeautifulSoup(html, "html.parser")
    days = set()
    for link in soup.find_all("a", href=HIRE_DAY_HREF_RE):
        day = int(HIRE_DAY_HREF_RE.search(link["href"]).group(1))
        if day > 0:
            days.add(day)
    return sorted(days)


@dataclass
class PersonnelEntry:
    personal_id: int
    name: str
    assigned_to: str | None  # currently-assigned vehicle's caption, or None if unassigned


def parse_personnel_roster(html: str) -> list[PersonnelEntry]:
    """Parses /buildings/<id>/personals — the only place `personal_id`s (needed by
    `assign_personnel`) are discoverable, per docs/missionchief-api.md."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="personal_table")
    entries: list[PersonnelEntry] = []
    if not table:
        return entries
    for row in table.find_all("tr"):
        checkbox = row.find("input", class_="personal-delete-checkbox")
        if not checkbox or not checkbox.get("value"):
            continue
        cells = row.find_all("td")
        name = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        assigned_to = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        entries.append(
            PersonnelEntry(personal_id=int(checkbox["value"]), name=name, assigned_to=assigned_to or None)
        )
    return entries


# Confirmed in docs/missionchief-api.md: on /vehicles/<id>/zuweisung each roster row's action link
# is /vehicles/<id>/zuweisungDo/<personal_id>; a person already bound to THIS vehicle gets the
# "Remove binding" variant (class btn-assigned), an unbound one gets "Assign vehicle".
ZUWEISUNG_DO_HREF_RE = re.compile(r"/vehicles/\d+/zuweisungDo/(\d+)")


def parse_vehicle_bound_personnel_ids(html: str) -> set[int]:
    """From /vehicles/<id>/zuweisung, the personal_ids currently bound to that one vehicle. This
    is a small per-vehicle page, so it's a far cheaper way to verify a crew assignment than
    diffing the whole ~17 MB /api/vehicles list (docs/missionchief-api.md)."""
    soup = BeautifulSoup(html, "html.parser")
    bound: set[int] = set()
    for link in soup.find_all("a", href=ZUWEISUNG_DO_HREF_RE):
        classes = link.get("class") or []
        text = link.get_text(strip=True).lower()
        if "btn-assigned" in classes or "remove" in text:
            bound.add(int(ZUWEISUNG_DO_HREF_RE.search(link["href"]).group(1)))
    return bound


@dataclass
class BuildResult:
    success: bool
    building: dict | None
    price: int | None
    response_status: int
    # Raw POST /buildings response body, kept only on failure. The failure-response shape has
    # never been captured live (docs/missionchief-api.md), so nothing here is parsed/guessed at —
    # it's just surfaced so a repeated, otherwise-identical failure (e.g. one specific station
    # rejected twice while others at the same price succeed) can finally be diagnosed from the log
    # instead of asking the user to re-run a one-off diagnostic script.
    response_text: str = ""


@dataclass
class ExpandResult:
    success: bool
    level: int
    price: int
    new_level: int | None
    response_status: int
    response_text: str = ""


@dataclass
class ServiceToggleResult:
    success: bool
    enabled: bool | None
    response_status: int
    response_text: str = ""


@dataclass
class VehiclePurchaseResult:
    success: bool
    vehicle: dict | None
    price: int
    response_status: int
    response_text: str = ""
    # Snapshot of all vehicle ids after this purchase, so a chained shopping list can pass it as
    # the next buy_vehicle's `before_ids` instead of re-downloading /api/vehicles each time.
    known_vehicle_ids: set[int] | None = None


@dataclass
class HireResult:
    success: bool
    hiring_phase: int | None
    response_status: int
    response_text: str = ""


@dataclass
class AssignPersonnelResult:
    success: bool
    assigned_personnel_count: int | None
    response_status: int
    response_text: str = ""


@dataclass
class DispatchAssignResult:
    success: bool
    building_id: int
    leitstelle_id: int
    response_status: int
    response_text: str = ""


# Confirmed in docs/missionchief-api.md: the game's frontend JS attaches these to every AJAX
# call to /api/*, distinct from a plain browser navigation request (see auth.py's session
# defaults, which deliberately do NOT set these — Rails responds differently to AJAX-flagged
# requests, and the JSON endpoints need this while the login/homepage HTML fetches must not have it).
API_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def summarize_html_for_log(html: str, max_chars: int = 2000) -> str:
    """Strip tags/scripts and collapse whitespace so a failed page's actual visible text (e.g. a
    Rails flash/validation message we've never seen the shape of) is readable in a log line,
    without guessing at specific markup to pattern-match."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return text[:max_chars]


@dataclass
class RateLimitConfig:
    # A jittered pause between game requests so we don't hammer the site. Kept modest by default
    # so a full station build-out (build + a single-request expand + service/hire + vehicles +
    # crew) finishes in tens of seconds rather than minutes; raise these in config.yaml's
    # `rate_limiting` section if you'd rather trade speed for a gentler footprint.
    min_delay: float = 0.3
    max_delay: float = 0.8
    max_retries: int = 4
    backoff_base: float = 2.0


class MissionChiefClient:
    def __init__(
        self,
        session: requests.Session,
        base_url: str,
        rate_limit: RateLimitConfig | None = None,
    ):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.rate_limit = rate_limit or RateLimitConfig()
        self._request_count = 0

    def _sleep_between_requests(self) -> None:
        if self._request_count > 0:
            time.sleep(random.uniform(self.rate_limit.min_delay, self.rate_limit.max_delay))
        self._request_count += 1

    def _request(self, method: str, path: str, ajax: bool = True, **kwargs) -> requests.Response:
        """`ajax=False` for the plain-link station-management actions (expand, service toggle,
        vehicle purchase, hiring) confirmed in docs/missionchief-api.md to carry neither
        `X-Requested-With` nor `X-CSRF-Token` — unlike `/api/*` and `zuweisungDo`, which do."""
        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        if ajax:
            headers = {**API_HEADERS, **headers}
        attempt = 0
        while True:
            self._sleep_between_requests()
            resp = self.session.request(method, url, timeout=30, headers=headers, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                attempt += 1
                if attempt > self.rate_limit.max_retries:
                    resp.raise_for_status()
                backoff = self.rate_limit.backoff_base**attempt
                logger.warning(
                    "Got %s from %s, retrying in %.1fs (attempt %d/%d)",
                    resp.status_code,
                    path,
                    backoff,
                    attempt,
                    self.rate_limit.max_retries,
                )
                time.sleep(backoff)
                continue
            check_session_alive(resp)
            resp.raise_for_status()
            return resp

    def get_buildings(self) -> list[dict]:
        """GET /api/buildings — every building owned by the authenticated account."""
        resp = self._request("GET", "/api/buildings")
        return resp.json()

    def get_building_detail(self, building_id: int) -> dict:
        """GET /api/buildings/<id> — single-building detail. Confirmed fields (docs/missionchief-api.md)
        include `level`, `enabled`, `hiring_phase` — used to verify expand/toggle/hire actions."""
        resp = self._request("GET", f"/api/buildings/{building_id}")
        return resp.json()

    def get_vehicles(self) -> list[dict]:
        """GET /api/vehicles — every vehicle owned by the authenticated account."""
        resp = self._request("GET", "/api/vehicles")
        return resp.json()

    def get_building_prices(self) -> dict[int, int]:
        """GET /buildings/new — current credit price per building_type. Always live; never
        cache across runs, since prices scale with account/alliance progression."""
        resp = self._request("GET", "/buildings/new")
        return parse_building_prices(resp.text)

    def get_credits_balance(self) -> int:
        """GET / (homepage) — the live credit balance, read from the nav bar's
        `credits-value` span present on every authenticated page (confirmed via a real
        account). Deliberately bypasses this client's default AJAX headers and goes through
        the session directly, same as auth.py's CSRF-token fetch: requests to this page
        flagged as AJAX (X-Requested-With) are confirmed to behave differently and can come
        back without the content we need (see auth.py's session defaults)."""
        self._sleep_between_requests()
        resp = self.session.get(f"{self.base_url}/", timeout=30)
        check_session_alive(resp)
        resp.raise_for_status()
        return parse_credits_balance(resp.text)

    def create_building(
        self,
        building_type: int,
        name: str,
        latitude: float,
        longitude: float,
        leitstelle_building_id: int | None = None,
    ) -> BuildResult:
        """POST /buildings — build a new station. Always pays with Credits, never Coins (real
        premium currency; never automated here).

        The actual POST /buildings response shape was never captured live (see
        docs/missionchief-api.md), so success is verified independently: buildings owned by the
        account are read before and after, and a genuinely new one matching this building_type
        appearing afterward is what counts as success — not any assumption about the response
        body. Always re-fetches /buildings/new immediately beforehand for a current price and
        CSRF token, since prices are dynamic.
        """
        if len(name) > MAX_BUILDING_NAME_LENGTH:
            raise ValueError(
                f"Building name {name!r} is {len(name)} characters, over MissionChief's "
                f"{MAX_BUILDING_NAME_LENGTH}-character limit (confirmed live — see "
                "docs/missionchief-api.md). Regenerate plan.json with `mc-autobuilder plan` so "
                "long names get shortened automatically, or edit this entry's name directly."
            )

        form_html = self._request("GET", "/buildings/new").text
        form = parse_new_building_form(form_html)
        price = form.prices.get(building_type)
        if price is None:
            raise ValueError(f"No price found for building_type {building_type} on /buildings/new")

        before_ids = {b["id"] for b in self.get_buildings()}

        fields = {
            "utf8": "✓",
            "authenticity_token": form.authenticity_token,
            "building[building_type]": str(building_type),
            "building[name]": name,
            "building[latitude]": str(latitude),
            "building[longitude]": str(longitude),
            "commit": f"Build {price:,} Credits",
        }
        if leitstelle_building_id is not None:
            fields["building[leitstelle_building_id]"] = str(leitstelle_building_id)
        if building_type in form.start_vehicle_fields:
            field_name, default_value = form.start_vehicle_fields[building_type]
            fields[f"building[{field_name}]"] = default_value

        # Don't follow the redirect: its real target/behavior was never confirmed (see
        # docs/missionchief-api.md), and success is verified independently below anyway by
        # diffing /api/buildings rather than trusting whatever that page returns.
        resp = self._request("POST", "/buildings", data=fields, allow_redirects=False)

        after = self.get_buildings()
        new_building = next(
            (b for b in after if b["id"] not in before_ids and b["building_type"] == building_type),
            None,
        )
        success = new_building is not None
        return BuildResult(
            success=success,
            building=new_building,
            price=price,
            response_status=resp.status_code,
            response_text="" if success else resp.text,
        )

    def get_expand_prices(self, building_id: int) -> dict[int, int]:
        """GET /buildings/<id>/expand — live credit price per expansion level. Always live; never
        cache, since prices scale with the level already reached (docs/missionchief-api.md)."""
        resp = self._request("GET", f"/buildings/{building_id}/expand", ajax=False)
        return parse_expand_prices(resp.text)

    def expand_building(self, building_id: int, level: int) -> ExpandResult:
        """GET /buildings/<id>/expand_do/credits?level=<n> — pay Credits to expand to `level`.
        Always Credits, never Coins (never automated here). The failure-response shape is
        unconfirmed (docs/missionchief-api.md), so success is verified independently via
        /api/buildings/<id>'s `level` field increasing, same before/after pattern as
        `create_building`."""
        prices = self.get_expand_prices(building_id)
        price = prices.get(level)
        if price is None:
            raise ValueError(f"No price found for expand level {level} on /buildings/{building_id}/expand")

        before_level = self.get_building_detail(building_id)["level"]
        resp = self._request(
            "GET",
            f"/buildings/{building_id}/expand_do/credits",
            params={"level": level},
            ajax=False,
            allow_redirects=False,
        )
        after_level = self.get_building_detail(building_id)["level"]
        success = after_level > before_level
        return ExpandResult(
            success=success,
            level=level,
            price=price,
            new_level=after_level,
            response_status=resp.status_code,
            response_text="" if success else resp.text,
        )

    def toggle_service(self, building_id: int) -> ServiceToggleResult:
        """GET /buildings/<id>/active — toggles a station's enabled/disabled service state (free,
        no credits spent). Verified via /api/buildings/<id>'s `enabled` field flipping."""
        before_enabled = self.get_building_detail(building_id)["enabled"]
        resp = self._request(
            "GET", f"/buildings/{building_id}/active", ajax=False, allow_redirects=False
        )
        after_enabled = self.get_building_detail(building_id)["enabled"]
        success = after_enabled != before_enabled
        return ServiceToggleResult(
            success=success,
            enabled=after_enabled,
            response_status=resp.status_code,
            response_text="" if success else resp.text,
        )

    def get_vehicle_purchase_options(self, building_id: int) -> dict[int, VehicleOption]:
        """GET /buildings/<id>/vehicles/new — live purchasable vehicle types + Credits prices for
        this station. Always live; never cache (docs/missionchief-api.md)."""
        resp = self._request("GET", f"/buildings/{building_id}/vehicles/new", ajax=False)
        return parse_vehicle_purchase_options(resp.text)

    def buy_vehicle(
        self, building_id: int, vehicle_type_id: int, before_ids: set[int] | None = None
    ) -> VehiclePurchaseResult:
        """GET /buildings/<id>/vehicle/<id>/<vehicle_type_id>/credits — buy one vehicle of
        `vehicle_type_id` for this station. Always Credits, never Coins. Verified via
        /api/vehicles, diffing for a new entry with matching building_id (failure-response shape
        unconfirmed — docs/missionchief-api.md).

        `before_ids` is an optional pre-fetched set of the account's current vehicle ids. When
        buying several vehicles in a row (a preset shopping list), the caller passes the previous
        purchase's post-state so we don't re-download the whole ~17 MB /api/vehicles list before
        every single purchase — the returned result's `known_vehicle_ids` carries the new state
        forward for the next call."""
        options = self.get_vehicle_purchase_options(building_id)
        option = options.get(vehicle_type_id)
        if option is None:
            raise ValueError(
                f"No purchase option for vehicle_type_id {vehicle_type_id} on "
                f"/buildings/{building_id}/vehicles/new (offered: {sorted(options)}). A brand-new or "
                "out-of-service station may list no purchasable vehicles until it's in service."
            )

        if before_ids is None:
            before_ids = {v["id"] for v in self.get_vehicles()}
        # Use the page's exact purchase link rather than reconstructing it from building_id: the
        # two path ids in .../vehicle/<a>/<b>/credits aren't guaranteed to both be the building_id.
        resp = self._request("GET", option.purchase_href, ajax=False, allow_redirects=False)
        after = self.get_vehicles()
        new_vehicle = next(
            (v for v in after if v["id"] not in before_ids and v["building_id"] == building_id),
            None,
        )
        success = new_vehicle is not None
        return VehiclePurchaseResult(
            success=success,
            vehicle=new_vehicle,
            price=option.price_credits,
            known_vehicle_ids={v["id"] for v in after},
            response_status=resp.status_code,
            # On failure keep both the response body and which URL we hit, so a purchase that 302s
            # but doesn't produce a vehicle can be diagnosed without a fresh capture.
            response_text="" if success else f"POST {option.purchase_href} -> {resp.status_code}\n{resp.text}",
        )

    def get_hire_day_options(self, building_id: int) -> list[int]:
        """GET /buildings/<id>/hire — live list of free day-based recruiting options. Varies by
        account (confirmed 1/2/3 in one account); always parsed live, never hardcoded
        (docs/missionchief-api.md)."""
        resp = self._request("GET", f"/buildings/{building_id}/hire", ajax=False)
        return parse_hire_day_options(resp.text)

    def hire(self, building_id: int, days: int) -> HireResult:
        """GET /buildings/<id>/hire_do/<days> — start (or extend) a free, day-based recruiting
        phase. Never the Coins-based instant-hire links. This does not add personnel immediately —
        it starts a timed phase (docs/missionchief-api.md) — so success here only confirms the
        phase itself changed, verified via /api/buildings/<id>'s `hiring_phase` field; it does not
        mean new personnel have actually arrived yet."""
        options = self.get_hire_day_options(building_id)
        if days not in options:
            raise ValueError(
                f"No {days}-day hiring option found on /buildings/{building_id}/hire "
                f"(available: {options})"
            )

        before_phase = self.get_building_detail(building_id)["hiring_phase"]
        resp = self._request(
            "GET", f"/buildings/{building_id}/hire_do/{days}", ajax=False, allow_redirects=False
        )
        after_phase = self.get_building_detail(building_id)["hiring_phase"]
        success = after_phase != before_phase
        return HireResult(
            success=success,
            hiring_phase=after_phase,
            response_status=resp.status_code,
            response_text="" if success else resp.text,
        )

    def get_personnel_roster(self, building_id: int) -> list[PersonnelEntry]:
        """GET /buildings/<id>/personals — this station's employee roster, including each
        person's personal_id (needed by `assign_personnel`, and otherwise undiscoverable —
        docs/missionchief-api.md)."""
        resp = self._request("GET", f"/buildings/{building_id}/personals", ajax=False)
        return parse_personnel_roster(resp.text)

    def get_vehicle_bound_personnel_ids(self, vehicle_id: int) -> set[int]:
        """GET /vehicles/<id>/zuweisung — personal_ids currently bound to this one vehicle. Small
        per-vehicle page, used to verify assignments without pulling all ~17 MB of /api/vehicles."""
        resp = self._request("GET", f"/vehicles/{vehicle_id}/zuweisung", ajax=False)
        return parse_vehicle_bound_personnel_ids(resp.text)

    def assign_personnel(self, vehicle_id: int, personal_id: int) -> AssignPersonnelResult:
        """POST /vehicles/<vehicle_id>/zuweisungDo/<personal_id> — toggle this person's permanent
        crew-roster binding to this vehicle: assigns if unbound, unassigns if already bound
        (confirmed docs/missionchief-api.md). No POST body; both IDs are in the URL.

        Verified via the small per-vehicle /vehicles/<id>/zuweisung page's binding state flipping
        for this person, rather than diffing the whole ~17 MB /api/vehicles list — the latter,
        called twice per person, is what made staffing a station take minutes."""
        before_bound = self.get_vehicle_bound_personnel_ids(vehicle_id)
        resp = self._request("POST", f"/vehicles/{vehicle_id}/zuweisungDo/{personal_id}")
        after_bound = self.get_vehicle_bound_personnel_ids(vehicle_id)
        # A successful toggle flips this person's membership in the vehicle's bound set.
        success = (personal_id in after_bound) != (personal_id in before_bound)
        return AssignPersonnelResult(
            success=success,
            assigned_personnel_count=len(after_bound),
            response_status=resp.status_code,
            response_text="" if success else resp.text,
        )

    def set_dispatch_center(self, building_id: int, leitstelle_id: int) -> DispatchAssignResult:
        """GET /buildings/<building_id>/leitstelle-set/<leitstelle_id> — (re)assign a station's
        dispatch center; `leitstelle_id=0` unassigns it. Confirmed AJAX call (unlike
        expand/toggle/vehicle-purchase/hire, which are plain links — docs/missionchief-api.md).
        Uniquely among the write actions here, the response body directly echoes the change as
        JSON (`{"building_id": ..., "leitstelle_id": ...}`), so success is read from the response
        itself rather than a before/after diff."""
        resp = self._request("GET", f"/buildings/{building_id}/leitstelle-set/{leitstelle_id}")
        try:
            data = resp.json() or {}
        except ValueError:
            data = {}
        success = data.get("building_id") == building_id and data.get("leitstelle_id") == leitstelle_id
        return DispatchAssignResult(
            success=success,
            building_id=building_id,
            leitstelle_id=leitstelle_id,
            response_status=resp.status_code,
            response_text="" if success else resp.text,
        )
