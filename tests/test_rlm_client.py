import json
from pathlib import Path

import pytest

from mc_autobuilder.rlm_client import (
    BoundingBox,
    RLMClient,
    RLMClientConfig,
    normalize_poi,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_normalize_poi_prefers_lat_lng_fields():
    poi = {"id": 1, "lat": 1.5, "lng": 2.5}
    assert normalize_poi(poi) == {"id": 1, "lat": 1.5, "lng": 2.5}


def test_normalize_poi_falls_back_to_latitude_longitude():
    # Confirmed in docs/rlm-api.md: some poi_types (e.g. fire stations) return
    # latitude/longitude instead of lat/lng.
    poi = {"id": 2, "latitude": 37.7, "longitude": -122.1}
    normalized = normalize_poi(poi)
    assert normalized["lat"] == 37.7
    assert normalized["lng"] == -122.1
    # original fields are preserved alongside the normalized ones
    assert normalized["latitude"] == 37.7


def test_normalize_poi_handles_lon_variant():
    poi = {"id": 3, "lat": 1.0, "lon": 2.0}
    assert normalize_poi(poi)["lng"] == 2.0


def test_bounding_box_from_center_radius_is_symmetric():
    bbox = BoundingBox.from_center_radius(lat=37.7749, lng=-122.4194, radius_km=10)
    assert bbox.north > 37.7749 > bbox.south
    assert bbox.east > -122.4194 > bbox.west
    # roughly symmetric around the center
    assert abs((bbox.north - 37.7749) - (37.7749 - bbox.south)) < 0.001


def test_bounding_box_shrinks_longitude_span_near_poles():
    # At high latitude, a degree of longitude covers much less ground distance, so the bbox
    # should span more degrees of longitude than at the equator for the same radius.
    equator_bbox = BoundingBox.from_center_radius(lat=0.0, lng=0.0, radius_km=100)
    polar_bbox = BoundingBox.from_center_radius(lat=80.0, lng=0.0, radius_km=100)
    equator_lng_span = equator_bbox.east - equator_bbox.west
    polar_lng_span = polar_bbox.east - polar_bbox.west
    assert polar_lng_span > equator_lng_span


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    """Records every call so tests can assert on cache-hit vs. live-request counts.

    `responses` can be a single value (returned every call) or a list (one per call, in order -
    for testing multi-page pagination where each page differs).
    """

    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0

    def get(self, url, params=None, headers=None, timeout=None):
        if isinstance(self.responses, list) and self.responses and isinstance(self.responses[0], list):
            data = self.responses[min(self.call_count, len(self.responses) - 1)]
        else:
            data = self.responses
        self.call_count += 1
        return FakeResponse(data)


def test_get_poi_types_uses_real_captured_fixture(tmp_path):
    fixture_data = json.loads((FIXTURES / "rlm_poi_types.json").read_text())
    session = FakeSession(fixture_data)
    client = RLMClient(RLMClientConfig(cache_dir=tmp_path / "cache"), session=session)

    poi_types = client.get_poi_types()

    assert session.call_count == 1
    table_names = {t["table_name"] for t in poi_types}
    assert "poi_fire_station" in table_names
    assert "poi_control_centre" in table_names


def test_cached_get_reuses_cache_within_ttl(tmp_path):
    session = FakeSession([{"table_name": "poi_fire_station", "friendly_name": "Fire Stations"}])
    config = RLMClientConfig(cache_dir=tmp_path / "cache", cache_ttl_hours=24.0)
    client = RLMClient(config, session=session)

    client.get_poi_types()
    client.get_poi_types()
    client.get_poi_types()

    assert session.call_count == 1  # only the first call actually hit the "network"


def test_cached_get_refetches_after_ttl_expires(tmp_path):
    session = FakeSession([{"table_name": "poi_fire_station"}])
    config = RLMClientConfig(cache_dir=tmp_path / "cache", cache_ttl_hours=0.0)
    client = RLMClient(config, session=session)

    client.get_poi_types()
    client.get_poi_types()

    assert session.call_count == 2  # TTL of 0 means every call is a live request


def test_get_pois_handles_real_bbox_array_response_shape(tmp_path):
    # Confirmed live (docs/rlm-api.md): /api/pois with a bounding box returns a *bare JSON
    # array*, not the {"total_count", "pois": [...]} shape it returns without one. This fixture
    # is a real capture of exactly that - a bbox query our client always makes.
    fixture_data = json.loads((FIXTURES / "rlm_pois_bbox_array_sample.json").read_text())
    assert isinstance(fixture_data, list)  # sanity-check the fixture itself is the bare-array shape

    session = FakeSession(fixture_data)
    client = RLMClient(RLMClientConfig(cache_dir=tmp_path / "cache"), session=session)

    bbox = BoundingBox(north=38.0, south=37.5, east=-121.8, west=-122.3)
    pois = client.get_pois("poi_prison", bbox, page_size=100)

    assert session.call_count == 1  # a partial page (7 < page_size 100) means no next page fetched
    assert len(pois) == 7
    assert pois[0]["name"] == "Alameda Juvenile Detention"
    assert pois[0]["lat"] == 37.7159032  # normalized from the fixture's latitude/longitude fields
    assert pois[0]["lng"] == -122.1183077


def test_get_pois_keeps_paginating_while_a_page_is_full(tmp_path):
    # With no total_count available (see above), pagination has to stop-on-partial-page instead.
    page_1 = [{"id": i, "name": f"Station {i}", "lat": 1.0, "lng": 2.0} for i in range(3)]
    page_2 = [{"id": i, "name": f"Station {i}", "lat": 1.0, "lng": 2.0} for i in range(3, 5)]
    session = FakeSession([page_1, page_2])
    client = RLMClient(RLMClientConfig(cache_dir=tmp_path / "cache"), session=session)

    bbox = BoundingBox(north=1, south=0, east=1, west=0)
    pois = client.get_pois("poi_fire_station", bbox, page_size=3)

    assert session.call_count == 2  # page 1 was full (3 == page_size), so page 2 was fetched
    assert len(pois) == 5
