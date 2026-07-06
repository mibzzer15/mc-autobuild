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


@dataclass
class BuildResult:
    success: bool
    building: dict | None
    price: int | None
    response_status: int

# Confirmed in docs/missionchief-api.md: the game's frontend JS attaches these to every AJAX
# call to /api/*, distinct from a plain browser navigation request (see auth.py's session
# defaults, which deliberately do NOT set these — Rails responds differently to AJAX-flagged
# requests, and the JSON endpoints need this while the login/homepage HTML fetches must not have it).
API_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


@dataclass
class RateLimitConfig:
    min_delay: float = 2.0
    max_delay: float = 5.0
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

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        headers = {**API_HEADERS, **kwargs.pop("headers", {})}
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

    def get_building_prices(self) -> dict[int, int]:
        """GET /buildings/new — current credit price per building_type. Always live; never
        cache across runs, since prices scale with account/alliance progression."""
        resp = self._request("GET", "/buildings/new")
        return parse_building_prices(resp.text)

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
        return BuildResult(
            success=new_building is not None,
            building=new_building,
            price=price,
            response_status=resp.status_code,
        )
