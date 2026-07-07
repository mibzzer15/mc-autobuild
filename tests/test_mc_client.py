from pathlib import Path

import pytest

from mc_autobuilder.mc_client import (
    BuildResult,
    MissionChiefClient,
    parse_building_prices,
    parse_credits_balance,
    parse_expand_prices,
    parse_hire_day_options,
    parse_new_building_form,
    parse_personnel_roster,
    parse_vehicle_purchase_options,
)

FIXTURES = Path(__file__).parent / "fixtures"
BUILDINGS_NEW_HTML = (FIXTURES / "buildings_new.html").read_text()

# Trimmed excerpt of a real captured page (2026-07): the nav bar's credits-value span is
# confirmed EMPTY in the raw HTML - the real number only appears via the inline creditsUpdate()
# call near the bottom of the page.
NAVBAR_WITH_CREDITS_HTML = """
<li title="Credits">
  <a class="lightbox-open" href="/credits" id="navigation_top">
    <img class="navbar-icon" style="margin-right: 2px;" src="data:image/png;base64,abc==" />
    <span class="credits-value"></span>
  </a>
</li>
<script> $(function() { creditsUpdate(2456738985); coinsUpdate(193); messageUnreadUpdate(0); }); </script>
"""


def test_parse_credits_balance_from_real_captured_page():
    assert parse_credits_balance(NAVBAR_WITH_CREDITS_HTML) == 2_456_738_985


def test_parse_credits_balance_missing_raises():
    with pytest.raises(ValueError):
        parse_credits_balance("<html><body>logged out, no creditsUpdate call</body></html>")


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
    # The failure page's body is captured (never parsed/guessed at) so a repeated, unexplained
    # failure can actually be diagnosed from the log instead of asking for another one-off script.
    assert "Not enough credits" in result.response_text


def test_create_building_success_does_not_keep_response_text():
    session = FakeMCSession(
        [
            FakeMCResponse(200, text=BUILDINGS_NEW_HTML),
            FakeMCResponse(200, json_data=EXISTING_BUILDINGS),
            FakeMCResponse(302, text=""),
            FakeMCResponse(200, json_data=[*EXISTING_BUILDINGS, NEW_BUILDING]),
        ]
    )
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    result = client.create_building(
        building_type=5, name="Test Police Station", latitude=10.0, longitude=20.0
    )

    assert result.success is True
    assert result.response_text == ""


def test_create_building_rejects_name_over_40_chars_before_making_any_request():
    # Real case: MissionChief's building[name] has a hard 40-char limit (docs/missionchief-api.md).
    # A stale plan.json generated before this was fixed could still contain an over-long name, so
    # this must fail fast with a clear message rather than burning requests and hitting the game's
    # own rejection.
    session = FakeMCSession([])
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    with pytest.raises(ValueError, match="40-character limit"):
        client.create_building(building_type=5, name="X" * 41, latitude=1.0, longitude=2.0)

    assert session.calls == []


def test_get_credits_balance_reads_creditsupdate_via_plain_get_not_ajax_request():
    session = FakeMCSession([FakeMCResponse(200, text=NAVBAR_WITH_CREDITS_HTML)])
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    balance = client.get_credits_balance()

    assert balance == 2_456_738_985
    # Regression guard: this must NOT go through _request()'s AJAX headers (X-Requested-With),
    # which are confirmed to make MissionChief respond differently to this same URL.
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert "headers" not in kwargs or "X-Requested-With" not in kwargs.get("headers", {})


# Trimmed excerpts of real captured pages (2026-07 HAR, docs/missionchief-api.md's Phase 5 section).

EXPAND_HTML = """
<a class="btn btn-success disable_after_click " href="/buildings/5558174/expand_do/credits?level=0">Expand (10,000 Credits)</a>
<a class="btn btn-success disable_after_click" data-confirm="..." href="/buildings/5558174/expand_do/coins?level=0">Expand (25 Coins)</a>
<a class="btn btn-success disable_after_click " href="/buildings/5558174/expand_do/credits?level=1">Expand (60,000 Credits)</a>
<a class="btn btn-success disable_after_click" data-confirm="..." href="/buildings/5558174/expand_do/coins?level=1">Expand (45 Coins)</a>
"""

VEHICLES_NEW_HTML = """
<div class="col-sm-3">
  <div class="vehicle_type well ">
    <h3>Type 1 fire engine</h3>
    Max. Crew: 6<br>
    <a class="btn btn-success disable_after_click buy-vehicle-btn " data-confirm="..." href="/buildings/5558174/vehicle/5558174/0/coins?building=5558174&amp;return_tab=fire_engine">25 Coins</a>
    <a class="btn btn-success disable_after_click buy-vehicle-btn " href="/buildings/5558174/vehicle/5558174/0/credits?building=5558174&amp;return_tab=fire_engine">5,000 Credits</a>
  </div>
</div>
"""

HIRE_HTML = """
<a class="btn btn-success" data-confirm="..." href="/buildings/5558174/hire_do/coins">Recruit now (5 coins)</a>
<a class="btn btn-default" href="/buildings/5558174/hire_do/1">Recruit 1 day</a>
<a class="btn btn-default" href="/buildings/5558174/hire_do/2">Recruit 2 days</a>
<a class="btn btn-default" href="/buildings/5558174/hire_do/3">Recruit 3 days</a>
"""

PERSONALS_HTML = """
<table class="table table-striped" id="personal_table">
  <thead><tr><th></th><th>Name</th><th>Education</th><th>Assigned to</th><th>Status</th><th>Options</th></tr></thead>
  <tbody>
    <tr data-filterable-by="[]">
      <td><input type="checkbox" class="personal-delete-checkbox" value="135847194"></td>
      <td>Paul G.</td>
      <td></td>
      <td>Type 1 fire engine</td>
      <td><span class="label label-success"><i>Available</i></span></td>
      <td></td>
    </tr>
    <tr data-filterable-by="[]">
      <td><input type="checkbox" class="personal-delete-checkbox" value="135847189"></td>
      <td>Owen A.</td>
      <td></td>
      <td></td>
      <td><span class="label label-success"><i>Available</i></span></td>
      <td></td>
    </tr>
  </tbody>
</table>
"""


def test_parse_expand_prices_from_real_captured_page():
    prices = parse_expand_prices(EXPAND_HTML)
    assert prices == {0: 10_000, 1: 60_000}


def test_parse_vehicle_purchase_options_from_real_captured_page():
    options = parse_vehicle_purchase_options(VEHICLES_NEW_HTML)
    assert options[0].name == "Type 1 fire engine"
    assert options[0].price_credits == 5_000
    assert options[0].return_tab == "fire_engine"


def test_parse_hire_day_options_excludes_cancel_and_coins_links():
    assert parse_hire_day_options(HIRE_HTML) == [1, 2, 3]


def test_parse_personnel_roster_from_real_captured_page():
    roster = parse_personnel_roster(PERSONALS_HTML)
    assert len(roster) == 2
    assert roster[0].personal_id == 135847194
    assert roster[0].name == "Paul G."
    assert roster[0].assigned_to == "Type 1 fire engine"
    assert roster[1].assigned_to is None  # unassigned


BUILDING_DETAIL = {"id": 5558174, "level": 0, "enabled": True, "hiring_phase": 0}


def test_expand_building_verified_by_level_increasing():
    session = FakeMCSession(
        [
            FakeMCResponse(200, text=EXPAND_HTML),  # GET expand
            FakeMCResponse(200, json_data=BUILDING_DETAIL),  # before
            FakeMCResponse(302, text=""),  # GET expand_do
            FakeMCResponse(200, json_data={**BUILDING_DETAIL, "level": 1}),  # after
        ]
    )
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    result = client.expand_building(5558174, level=0)

    assert result.success is True
    assert result.price == 10_000
    assert result.new_level == 1
    # Regression guard: expand_do is a plain link, confirmed to carry no AJAX/CSRF headers.
    method, url, kwargs = session.calls[2]
    assert "X-Requested-With" not in kwargs.get("headers", {})
    assert kwargs["allow_redirects"] is False


def test_toggle_service_verified_by_enabled_flipping():
    session = FakeMCSession(
        [
            FakeMCResponse(200, json_data=BUILDING_DETAIL),  # before (enabled=True)
            FakeMCResponse(302, text=""),  # GET active
            FakeMCResponse(200, json_data={**BUILDING_DETAIL, "enabled": False}),  # after
        ]
    )
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    result = client.toggle_service(5558174)

    assert result.success is True
    assert result.enabled is False


VEHICLES_LIST = [{"id": 5579596, "building_id": 5558174, "assigned_personnel_count": 0}]


def test_buy_vehicle_verified_by_diffing_vehicles_list():
    new_vehicle = {"id": 9999, "building_id": 5558174, "assigned_personnel_count": 0}
    session = FakeMCSession(
        [
            FakeMCResponse(200, text=VEHICLES_NEW_HTML),  # GET vehicles/new
            FakeMCResponse(200, json_data=VEHICLES_LIST),  # before
            FakeMCResponse(302, text=""),  # GET vehicle/.../credits
            FakeMCResponse(200, json_data=[*VEHICLES_LIST, new_vehicle]),  # after
        ]
    )
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    result = client.buy_vehicle(5558174, vehicle_type_id=0)

    assert result.success is True
    assert result.vehicle == new_vehicle
    assert result.price == 5_000


def test_hire_verified_by_hiring_phase_changing():
    session = FakeMCSession(
        [
            FakeMCResponse(200, text=HIRE_HTML),  # GET hire
            FakeMCResponse(200, json_data=BUILDING_DETAIL),  # before (hiring_phase=0)
            FakeMCResponse(302, text=""),  # GET hire_do/1
            FakeMCResponse(200, json_data={**BUILDING_DETAIL, "hiring_phase": 1}),  # after
        ]
    )
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    result = client.hire(5558174, days=1)

    assert result.success is True
    assert result.hiring_phase == 1


def test_hire_rejects_day_option_not_offered():
    session = FakeMCSession([FakeMCResponse(200, text=HIRE_HTML)])
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    with pytest.raises(ValueError, match="No 7-day hiring option"):
        client.hire(5558174, days=7)


def test_assign_personnel_verified_by_assigned_personnel_count_changing():
    vehicle = {"id": 14577420, "building_id": 5558174, "assigned_personnel_count": 2}
    session = FakeMCSession(
        [
            FakeMCResponse(200, json_data=[vehicle]),  # before
            FakeMCResponse(200, text="<td>Paul G.</td>"),  # POST zuweisungDo
            FakeMCResponse(200, json_data=[{**vehicle, "assigned_personnel_count": 3}]),  # after
        ]
    )
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    result = client.assign_personnel(vehicle_id=14577420, personal_id=135847194)

    assert result.success is True
    assert result.assigned_personnel_count == 3
    # Regression guard: unlike expand/active/vehicle-purchase, zuweisungDo IS a confirmed AJAX call.
    method, url, kwargs = session.calls[1]
    assert kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"


def test_set_dispatch_center_verified_by_response_json_echoing_the_change():
    # Real captured response: {"building_id":5558268,"leitstelle_id":2534509} - uniquely among
    # these write actions, success is read straight from the response body, not a before/after diff.
    session = FakeMCSession(
        [FakeMCResponse(200, json_data={"building_id": 5558268, "leitstelle_id": 2534509})]
    )
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    result = client.set_dispatch_center(5558268, 2534509)

    assert result.success is True
    assert result.building_id == 5558268
    assert result.leitstelle_id == 2534509
    # Confirmed AJAX call (unlike expand/active/vehicle-purchase/hire, which are plain links).
    method, url, kwargs = session.calls[0]
    assert kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"


def test_set_dispatch_center_supports_unassigning_with_zero():
    session = FakeMCSession([FakeMCResponse(200, json_data={"building_id": 5558268, "leitstelle_id": 0})])
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    result = client.set_dispatch_center(5558268, 0)

    assert result.success is True
    assert result.leitstelle_id == 0


def test_set_dispatch_center_reports_failure_when_response_does_not_match():
    session = FakeMCSession([FakeMCResponse(200, text="<html>error</html>", json_data=None)])
    client = MissionChiefClient(session, "https://www.missionchief.com")
    client.rate_limit.min_delay = client.rate_limit.max_delay = 0

    result = client.set_dispatch_center(5558268, 2534509)

    assert result.success is False
    assert "error" in result.response_text
