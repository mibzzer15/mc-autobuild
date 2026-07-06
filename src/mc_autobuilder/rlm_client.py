"""Client for the RLM (Realism Location Marker) community POI database.

See docs/rlm-api.md for the confirmed API contract this is built against: no MissionChief auth
is required, `/api/pois` takes an optional bounding box plus a required `poi_type`, and
coordinate field names are inconsistent across POI types (some return `lat`/`lng`, others
`latitude`/`longitude`) — `normalize_poi` below papers over that.

Every live request is cached to disk with a TTL, since this is a community-run, unauthenticated
API and the project's non-negotiable rate-limiting requirement is to never hammer it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

RLM_BASE_URL = "https://realism-location-marker.com"

# The API's own default (confirmed in docs/rlm-api.md) is 10,000; we default lower to keep any
# single live request modest, paginating instead when a region genuinely has more than this.
DEFAULT_PAGE_SIZE = 1000

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires a descriptive User-Agent and at most ~1 request/second.
NOMINATIM_USER_AGENT = "mc-autobuilder (https://github.com/mibzzer15/mc-autobuild)"


@dataclass
class BoundingBox:
    north: float
    south: float
    east: float
    west: float

    @classmethod
    def from_center_radius(cls, lat: float, lng: float, radius_km: float) -> "BoundingBox":
        """Approximate a bounding box around a center point and radius in kilometers."""
        radius_m = radius_km * 1000
        lat_delta = radius_m / 111_320  # ~meters per degree of latitude, roughly constant
        lng_denominator = 111_320 * math.cos(math.radians(lat))
        lng_delta = radius_m / lng_denominator if abs(lng_denominator) > 1e-6 else 180.0
        return cls(
            north=lat + lat_delta,
            south=lat - lat_delta,
            east=lng + lng_delta,
            west=lng - lng_delta,
        )


def normalize_poi(poi: dict) -> dict:
    """Normalize inconsistent coordinate field names (see docs/rlm-api.md) to `lat`/`lng`."""
    lat = poi.get("lat", poi.get("latitude"))
    lng = poi.get("lng", poi.get("lon", poi.get("longitude")))
    return {**poi, "lat": lat, "lng": lng}


def geocode_city(city: str, session: requests.Session | None = None) -> BoundingBox:
    """Forward-geocode a city/place name to a bounding box via OpenStreetMap Nominatim — the
    same free geocoder RLM's own map page uses for its search feature."""
    session = session or requests.Session()
    resp = session.get(
        NOMINATIM_URL,
        params={"format": "json", "q": city, "limit": 1},
        headers={"User-Agent": NOMINATIM_USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Nominatim found no results for city {city!r}")
    south, north, west, east = (float(v) for v in results[0]["boundingbox"])
    return BoundingBox(north=north, south=south, east=east, west=west)


@dataclass
class RLMClientConfig:
    base_url: str = RLM_BASE_URL
    cache_dir: Path = Path(".rlm_cache")
    cache_ttl_hours: float = 24.0
    min_delay_seconds: float = 1.0
    max_delay_seconds: float = 2.0
    rlm_user_id: str | None = None
    rlm_username: str | None = None
    rlm_alliance_id: str | None = None


class RLMClient:
    def __init__(self, config: RLMClientConfig | None = None, session: requests.Session | None = None):
        self.config = config or RLMClientConfig()
        self.session = session or requests.Session()
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        self._request_count = 0

    def _headers(self) -> dict:
        headers = {"X-RLM-Script": "true", "X-RLM-Version": "mc-autobuilder"}
        if self.config.rlm_user_id:
            headers["X-RLM-UserID"] = str(self.config.rlm_user_id)
        if self.config.rlm_username:
            headers["X-RLM-Username"] = self.config.rlm_username
        if self.config.rlm_alliance_id:
            headers["X-RLM-AllianceID"] = str(self.config.rlm_alliance_id)
        return headers

    def _cache_path(self, path: str, params: dict) -> Path:
        cache_key = f"{path}?{json.dumps(params, sort_keys=True)}"
        digest = hashlib.sha256(cache_key.encode()).hexdigest()[:24]
        return self.config.cache_dir / f"{digest}.json"

    def _cached_get(self, path: str, params: dict) -> dict | list:
        cache_path = self._cache_path(path, params)
        if cache_path.exists():
            cached = json.loads(cache_path.read_text())
            age_hours = (time.time() - cached["fetched_at"]) / 3600
            if age_hours < self.config.cache_ttl_hours:
                logger.debug("RLM cache hit for %s %s (age %.1fh)", path, params, age_hours)
                return cached["data"]

        if self._request_count > 0:
            import random

            time.sleep(random.uniform(self.config.min_delay_seconds, self.config.max_delay_seconds))
        self._request_count += 1

        url = f"{self.config.base_url}{path}"
        logger.info("RLM live request: GET %s %s", path, params)
        resp = self.session.get(url, params=params, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        cache_path.write_text(json.dumps({"fetched_at": time.time(), "data": data}))
        return data

    def get_poi_types(self) -> list[dict]:
        """GET /api/poi-types — the queryable `poi_type` taxonomy."""
        return self._cached_get("/api/poi-types", {})

    def get_building_types(self, game_world: str) -> list[dict]:
        """GET /api/building-types?game_world=X — per-server POI-type -> MissionChief
        building_type id mapping. Always pass the account's real game_world; IDs are confirmed
        to differ between servers (see docs/rlm-api.md)."""
        return self._cached_get("/api/building-types", {"game_world": game_world})

    def get_pois(
        self, poi_type: str, bbox: BoundingBox, page_size: int = DEFAULT_PAGE_SIZE
    ) -> list[dict]:
        """GET /api/pois for one POI type within a bounding box, paginating as needed, and
        normalizing each POI's coordinate fields.

        Confirmed live (docs/rlm-api.md): the response shape genuinely differs depending on
        whether a bounding box is given. Without one, it's `{"total_count": N, "pois": [...]}`.
        **With** one — which is every query this client makes — it's a bare JSON array with no
        pagination metadata at all. So a bbox query can't know the true total count; instead we
        keep paging as long as a page comes back full (`len(pois) == page_size`, implying there
        may be more) and stop on the first partial or empty page.
        """
        all_pois: list[dict] = []
        page = 1
        while True:
            data = self._cached_get(
                "/api/pois",
                {
                    "poi_type": poi_type,
                    "north": bbox.north,
                    "south": bbox.south,
                    "east": bbox.east,
                    "west": bbox.west,
                    "page": page,
                    "page_size": page_size,
                },
            )
            pois = data if isinstance(data, list) else data.get("pois", [])
            all_pois.extend(normalize_poi(p) for p in pois)
            if len(pois) < page_size:
                break
            page += 1
        return all_pois
