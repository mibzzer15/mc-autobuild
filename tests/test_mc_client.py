from pathlib import Path

import pytest

from mc_autobuilder.mc_client import (
    BuildResult,
    MissionChiefClient,
    parse_building_prices,
    parse_credits_balance,
    parse_new_building_form,
)

FIXTURES = Path(__file__).parent / "fixtures"
BUILDINGS_NEW_HTML = (FIXTURES / "buildings_new.html").read_text()

# Trimmed excerpt of the real nav bar markup (2026-07 DevTools inspection of a live account).
NAVBAR_WITH_CREDITS_HTML = """
<li title="Credits">
  <a class="lightbox-open" href="/credits" id="navigation_top">
    <img class="navbar-icon" style="margin-right: 2px;" src="data:image/png;base64,abc==" />
    <span class="credits-value">2,456,656,440</span>
  </a>
</li>
"""


def test_parse_credits_balance_from_real_captured_navbar():
    assert parse_credits_balance(NAVBAR_WITH_CREDITS_HTML) == 2_456_656_440


def test_parse_credits_balance_missing_raises():
    with pytest.raises(ValueError):
        parse_credits_balance("<html><body>logged out</body></html>")


def test_parse_building_prices_from_real_captured_page():
    prices = parse_building_prices(BUILDINGS_NEW_HTML)

    # Real values captured from /buildings/new (docs/missionchief-api.md) - dynamic per account,
    # but this fixture pins a known snapshot so the parser itself is what's under test.
    assert prices[0] == 2_066_894  # Fire station
    assert prices[13] == 1_000_000  # Fire station (Small station)
    assert prices[1] == 0  # Dispatch Center is free to build
    assert 5 in prices  # Police station
    assert len(prices) == 27  # one entry per building_type on the page


def test_parse_new_building_form_from_real_captured_page():
    form = parse_new_building_form(BUILDINGS_NEW_HTML)

    assert form.authenticity_token  # present and non-empty (real value scrubbed in the fixture)
    assert len(form.prices) == 27

    # Confirmed in docs/missionchief-api.md: only Fire station and its small variant have a
    # required start-vehicle select; everything else (Police, Ambulance, Hospital, ...) doesn't.
    assert form.start_vehicle_fields[0] == ("start_vehicle_feuerwache", "0")
    assert form.start_vehicle_fields[13] == ("start_vehicle_feuerwache_kleinwache", "0")
    assert 5 not in form.start_vehicle_fields  # Police station


class FakeMCResponse:
    def __init__(self, status_code, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        return self._json_data

    def raise_for_status(self):
        pass


class FakeMCSession:
    """Scripted responses for a sequence of expected requests, keyed by call order."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses[len(self.calls) - 1]

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


EXISTING_BUILDINGS = [{"id": 1, "building_type": 5, "latitude": 1.0, "longitude": 2.0}]
NEW_BUILDING = {"id": 2, "building_type": 5, "latitude": 10.0, "longitude": 20.0}


def test_create_building_success_verified_by_diffing_buildings_list():
    session = FakeMCSession(
        [
            FakeMCResponse(200, text=BUILDINGS_NEW_HTML),  # GET /buildings/new
            FakeMCResponse(200, json_data=EXISTING_BUILDINGS),  # GET /api/buildings (before)
            FakeMCResponse(302, text=""),  # POST /buildings
            FakeMCResponse(200, json_data=[*EXISTING_BUILDINGS, NEW_BUILDING]),  # GET /api/buildings (after)
        ]
    )
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0  # no sleeping in tests

    result = client.create_building(
        building_type=5, name="Test Police Station", latitude=10.0, longitude=20.0
    )

    assert isinstance(result, BuildResult)
    assert result.success is True
    assert result.building == NEW_BUILDING
    assert result.price == parse_building_prices(BUILDINGS_NEW_HTML)[5]

    post_call = session.calls[2]
    assert post_call[0] == "POST"
    posted_fields = post_call[2]["data"]
    assert posted_fields["building[building_type]"] == "5"
    assert posted_fields["building[name]"] == "Test Police Station"
    assert "Credits" in posted_fields["commit"]
    assert "Coins" not in posted_fields["commit"]
    assert "building[start_vehicle_polizeiwache]" not in posted_fields  # Police station has none
    # Regression: requests follows a 302 by default, which previously landed on a plain GET
    # /buildings whose real behavior was never confirmed and could be treated as a failure even
    # after a successful build. Success is verified independently (the diff above), so the
    # redirect must not be followed.
    assert post_call[2]["allow_redirects"] is False


def test_create_building_includes_start_vehicle_field_for_fire_station():
    session = FakeMCSession(
        [
            FakeMCResponse(200, text=BUILDINGS_NEW_HTML),
            FakeMCResponse(200, json_data=[]),
            FakeMCResponse(302, text=""),
            FakeMCResponse(200, json_data=[{"id": 99, "building_type": 0, "latitude": 1.0, "longitude": 2.0}]),
        ]
    )
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    client.create_building(building_type=0, name="Test Fire Station", latitude=1.0, longitude=2.0)

    posted_fields = session.calls[2][2]["data"]
    assert posted_fields["building[start_vehicle_feuerwache]"] == "0"


def test_create_building_reports_failure_when_no_new_building_appears():
    # e.g. insufficient funds - POST "succeeds" at the HTTP level but nothing new is created.
    session = FakeMCSession(
        [
            FakeMCResponse(200, text=BUILDINGS_NEW_HTML),
            FakeMCResponse(200, json_data=EXISTING_BUILDINGS),
            FakeMCResponse(200, text="<html>Not enough credits</html>"),
            FakeMCResponse(200, json_data=EXISTING_BUILDINGS),  # unchanged
        ]
    )
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    result = client.create_building(building_type=5, name="Should Fail", latitude=1.0, longitude=2.0)

    assert result.success is False
    assert result.building is None


def test_get_credits_balance_reads_navbar_via_plain_get_not_ajax_request():
    session = FakeMCSession([FakeMCResponse(200, text=NAVBAR_WITH_CREDITS_HTML)])
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    balance = client.get_credits_balance()

    assert balance == 2_456_656_440
    # Regression guard: this must NOT go through _request()'s AJAX headers (X-Requested-With),
    # which are confirmed to make MissionChief respond differently to this same URL.
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert "headers" not in kwargs or "X-Requested-With" not in kwargs.get("headers", {})
