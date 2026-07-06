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
    """Records every call so tests can assert on cache-hit vs. live-request counts."""

    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.call_count += 1
        return FakeResponse(self.responses)


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


def test_get_pois_normalizes_and_paginates_using_real_fixture(tmp_path):
    fixture_data = json.loads((FIXTURES / "rlm_pois_control_centre_sample.json").read_text())
    session = FakeSession(fixture_data)
    client = RLMClient(RLMClientConfig(cache_dir=tmp_path / "cache"), session=session)

    bbox = BoundingBox(north=90, south=-90, east=180, west=-180)
    pois = client.get_pois("poi_control_centre", bbox, page_size=10000)

    # The real fixture's total_count (320) is far larger than the 3 sample rows it contains, so
    # a real server would keep paginating - but our FakeSession always returns the same fixture,
    # so this also verifies pagination actually stops instead of looping forever once a
    # page's poi list would repeat page_size >= total_count.
    assert len(pois) == 3
    assert pois[0]["name"] == "Incident Control Point"
    assert pois[0]["lat"] == -43.2088427
    assert pois[0]["lng"] == 171.7146897
