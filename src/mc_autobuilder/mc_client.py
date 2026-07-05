"""Read/write client for MissionChief's authenticated JSON endpoints.

Endpoint behavior documented in docs/missionchief-api.md. Every request goes through a shared
rate limiter: a randomized delay between requests, and exponential backoff on 429/5xx, per the
project's non-negotiable rate-limiting requirement.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import requests

from .auth import check_session_alive

logger = logging.getLogger(__name__)

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
