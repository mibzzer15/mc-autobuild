from pathlib import Path

from mc_autobuilder.mc_client import parse_building_prices

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_building_prices_from_real_captured_page():
    html = (FIXTURES / "buildings_new.html").read_text()
    prices = parse_building_prices(html)

    # Real values captured from /buildings/new (docs/missionchief-api.md) - dynamic per account,
    # but this fixture pins a known snapshot so the parser itself is what's under test.
    assert prices[0] == 2_066_894  # Fire station
    assert prices[13] == 1_000_000  # Fire station (Small station)
    assert prices[1] == 0  # Dispatch Center is free to build
    assert 5 in prices  # Police station
    assert len(prices) == 27  # one entry per building_type on the page
