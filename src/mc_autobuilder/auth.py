"""Session/auth handling for MissionChief: cookie mode or Playwright interactive login.

See docs/missionchief-api.md for the confirmed auth mechanism: a Rails session cookie plus
an `X-CSRF-Token` header whose value is also embedded as `<meta name="csrf-token">` on every
authenticated page.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from .constants import DEFAULT_BASE_URL

CSRF_META_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')

# Substrings that only appear on MissionChief's logged-out / sign-in page, never on an
# authenticated page or a JSON API response.
LOGIN_PAGE_MARKERS = ("user_session_email", "user[email]", "/users/sign_in")


class SessionExpiredError(RuntimeError):
    """Raised when the MissionChief session is missing, invalid, or has expired."""


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def load_config(env_path: str | Path = ".env") -> dict[str, str]:
    """Merge `.env` file values with real environment variables (the environment wins)."""
    values = _load_env_file(Path(env_path))
    values.update({k: v for k, v in os.environ.items() if k.startswith("MC_")})
    return values


@dataclass
class AuthConfig:
    base_url: str
    auth_mode: str
    session_cookie: str | None
    storage_state_path: Path

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "AuthConfig":
        values = load_config(env_path)
        return cls(
            base_url=values.get("MC_BASE_URL", DEFAULT_BASE_URL),
            auth_mode=values.get("MC_AUTH_MODE", "cookie"),
            session_cookie=values.get("MC_SESSION_COOKIE") or None,
            storage_state_path=Path(values.get("MC_STORAGE_STATE_PATH", "storage_state.json")),
        )


def _cookie_header_to_jar(cookie_header: str, domain: str) -> requests.cookies.RequestsCookieJar:
    """Parse a raw `Cookie:` request header value (as copied from browser devtools)."""
    jar = requests.cookies.RequestsCookieJar()
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        if name:
            jar.set(name, value, domain=domain)
    return jar


def _storage_state_to_jar(storage_state_path: Path, host: str) -> requests.cookies.RequestsCookieJar:
    """Load cookies for `host` out of a Playwright `storage_state.json` file."""
    if not storage_state_path.exists():
        raise SessionExpiredError(
            f"No saved Playwright session found at {storage_state_path}. "
            "Run `mc-autobuilder login` to log in interactively first."
        )
    data = json.loads(storage_state_path.read_text())
    jar = requests.cookies.RequestsCookieJar()
    for cookie in data.get("cookies", []):
        cookie_domain = cookie.get("domain", "").lstrip(".")
        if host == cookie_domain or host.endswith("." + cookie_domain):
            jar.set(
                cookie["name"],
                cookie["value"],
                domain=cookie["domain"],
                path=cookie.get("path", "/"),
            )
    if not jar:
        raise SessionExpiredError(
            f"No cookies for {host} found in {storage_state_path}. "
            "Run `mc-autobuilder login` again to refresh the saved session."
        )
    return jar


def extract_csrf_token(html: str) -> str:
    """Pull the Rails CSRF token out of a page's `<meta name="csrf-token">` tag."""
    match = CSRF_META_RE.search(html)
    if not match:
        raise SessionExpiredError(
            "Could not find a CSRF token on the MissionChief homepage — the session is likely "
            "invalid or expired. Please refresh MC_SESSION_COOKIE or re-run `mc-autobuilder login`."
        )
    return match.group(1)


def check_session_alive(response: requests.Response) -> None:
    """Raise SessionExpiredError if a response looks like a logged-out/login page."""
    if response.status_code in (401, 403):
        raise SessionExpiredError(
            f"MissionChief returned HTTP {response.status_code} — the session has likely expired. "
            "Please refresh MC_SESSION_COOKIE or re-run `mc-autobuilder login`."
        )
    text_sample = response.text[:5000] if response.text else ""
    if any(marker in text_sample for marker in LOGIN_PAGE_MARKERS):
        raise SessionExpiredError(
            "MissionChief returned a login page instead of the expected content — the session has "
            "expired. Please refresh MC_SESSION_COOKIE or re-run `mc-autobuilder login`."
        )


def fetch_csrf_token(session: requests.Session, base_url: str) -> str:
    resp = session.get(base_url, timeout=30)
    check_session_alive(resp)
    return extract_csrf_token(resp.text)


def build_session(config: AuthConfig) -> requests.Session:
    """Build an authenticated `requests.Session` for either auth mode, with a CSRF token attached."""
    host = urlparse(config.base_url).netloc

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; mc-autobuilder/0.1)",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
    )

    if config.auth_mode == "cookie":
        if not config.session_cookie:
            raise SessionExpiredError(
                "MC_SESSION_COOKIE is not set. Paste your MissionChief session cookie into .env, "
                "or set MC_AUTH_MODE=playwright and run `mc-autobuilder login`."
            )
        session.cookies = _cookie_header_to_jar(config.session_cookie, host)
    elif config.auth_mode == "playwright":
        session.cookies = _storage_state_to_jar(config.storage_state_path, host)
    else:
        raise ValueError(f"Unknown MC_AUTH_MODE: {config.auth_mode!r} (expected 'cookie' or 'playwright')")

    session.headers["X-CSRF-Token"] = fetch_csrf_token(session, config.base_url)
    return session


def interactive_playwright_login(config: AuthConfig) -> None:
    """Open a real browser, let the user log in manually, and persist the session to disk."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Install it with `pip install mc-autobuilder[playwright]` "
            "and then run `playwright install chromium`."
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(config.base_url)
        print("A browser window has opened. Log in to MissionChief, then return here and press Enter.")
        input()
        config.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(config.storage_state_path))
        browser.close()
    print(f"Session saved to {config.storage_state_path}")
