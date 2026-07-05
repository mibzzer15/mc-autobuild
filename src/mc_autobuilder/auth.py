"""Session/auth handling for MissionChief: username/password, a pasted session cookie, or
Playwright interactive login.

See docs/missionchief-api.md for the confirmed auth mechanism: a Rails session cookie plus
an `X-CSRF-Token` header whose value is also embedded as `<meta name="csrf-token">` on every
authenticated page.

The username/password sign-in form at `/users/sign_in` has been confirmed against a real
capture (see docs/missionchief-api.md): Devise-style `user[email]` / `user[password]` fields,
POSTing to `/users/sign_in`. `login_with_credentials` still scrapes the form at runtime rather
than hardcoding those names, since that's more robust to the page changing later, but the field
names it discovers are now expected, not guessed.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .constants import DEFAULT_BASE_URL

# Substrings that only appear on MissionChief's logged-out / sign-in page, never on an
# authenticated page or a JSON API response. Confirmed 2026-07 against a real capture of
# https://www.missionchief.com/users/sign_in (see docs/missionchief-api.md).
LOGIN_PAGE_MARKERS = ("user[email]", "/users/sign_in")

ALERT_TEXT_RE = re.compile(r'class="[^"]*alert[^"]*"[^>]*>\s*([^<]{3,200})<')


class SessionExpiredError(RuntimeError):
    """Raised when the MissionChief session is missing, invalid, or has expired."""


class LoginFailedError(SessionExpiredError):
    """Raised when a username/password login attempt didn't succeed (bad credentials, changed
    login form, CAPTCHA/2FA, etc.)."""


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
    username: str | None
    password: str | None
    storage_state_path: Path

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "AuthConfig":
        values = load_config(env_path)
        return cls(
            base_url=values.get("MC_BASE_URL", DEFAULT_BASE_URL),
            auth_mode=values.get("MC_AUTH_MODE", "cookie"),
            session_cookie=values.get("MC_SESSION_COOKIE") or None,
            username=values.get("MC_USERNAME") or None,
            password=values.get("MC_PASSWORD") or None,
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
    """Load cookies for `host` out of a Playwright-style `storage_state.json` file (also used
    to cache sessions established via username/password login — see `_save_session_cookies`)."""
    if not storage_state_path.exists():
        raise SessionExpiredError(f"No saved session found at {storage_state_path}.")
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
        raise SessionExpiredError(f"No cookies for {host} found in {storage_state_path}.")
    return jar


def _save_session_cookies(jar: requests.cookies.RequestsCookieJar, path: Path) -> None:
    """Persist a session's cookies in the same shape as Playwright's storage_state.json, so a
    credentials-mode login doesn't have to re-authenticate with a POST on every run."""
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/"} for c in jar
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies}))


def extract_csrf_token(html: str) -> str:
    """Pull the Rails CSRF token out of a page's `<meta name="csrf-token">` tag.

    Uses BeautifulSoup rather than a regex on purpose: the real page emits this tag as
    `<meta content="..." name="csrf-token" />` (content *before* name — confirmed from a real
    capture), so an order-sensitive regex silently fails to match it.
    """
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("meta", attrs={"name": "csrf-token"})
    if not tag or not tag.get("content"):
        raise SessionExpiredError(
            "Could not find a CSRF token on the MissionChief homepage — the session is likely "
            "invalid or expired."
        )
    return tag["content"]


def check_session_alive(response: requests.Response) -> None:
    """Raise SessionExpiredError if a response looks like a logged-out/login page."""
    if response.status_code in (401, 403):
        raise SessionExpiredError(
            f"MissionChief returned HTTP {response.status_code} — the session has likely expired."
        )
    text_sample = response.text[:5000] if response.text else ""
    if any(marker in text_sample for marker in LOGIN_PAGE_MARKERS):
        raise SessionExpiredError(
            "MissionChief returned a login page instead of the expected content — the session has "
            "expired."
        )


def fetch_csrf_token(session: requests.Session, base_url: str) -> str:
    resp = session.get(base_url, timeout=30)
    check_session_alive(resp)
    return extract_csrf_token(resp.text)


def _has_password_form(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return any(form.find("input", attrs={"type": "password"}) for form in soup.find_all("form"))


def _find_login_form(html: str, base_url: str) -> tuple[str, dict[str, str], str, str]:
    """Locate the sign-in form on a page. Returns (action_url, other_fields, identifier_field_name,
    password_field_name). Scrapes the real form rather than assuming Devise's usual
    `user[email]`/`user[password]` names, since this page has never been captured live."""
    soup = BeautifulSoup(html, "html.parser")
    form = None
    password_field = None
    for candidate in soup.find_all("form"):
        pw_input = candidate.find("input", attrs={"type": "password"})
        if pw_input and pw_input.get("name"):
            form = candidate
            password_field = pw_input["name"]
            break

    if form is None:
        raise LoginFailedError(
            "Could not find a login form with a password field on the sign-in page. "
            "MissionChief's login page may differ from what this code expects — please open "
            "an issue with a copy of the sign-in page's HTML."
        )

    fields: dict[str, str] = {}
    identifier_field = None
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        input_type = (inp.get("type") or "text").lower()
        if input_type == "submit":
            continue
        fields[name] = inp.get("value", "")
        if input_type in ("email", "text") and identifier_field is None and name != password_field:
            identifier_field = name

    if identifier_field is None:
        raise LoginFailedError(
            "Could not find a username/email field on the sign-in form. "
            "MissionChief's login page may differ from what this code expects — please open "
            "an issue with a copy of the sign-in page's HTML."
        )

    action = form.get("action") or "/users/sign_in"
    action_url = urljoin(base_url, action)
    return action_url, fields, identifier_field, password_field


def login_with_credentials(session: requests.Session, base_url: str, username: str, password: str) -> None:
    """Log in with a username/password by scraping and submitting the real sign-in form.

    Field names/form action confirmed against a real capture of /users/sign_in (see
    docs/missionchief-api.md). Still scrapes at runtime rather than hardcoding those values.
    """
    sign_in_url = urljoin(base_url, "/users/sign_in")
    resp = session.get(sign_in_url, timeout=30)
    if resp.status_code == 404:
        resp = session.get(base_url, timeout=30)
    resp.raise_for_status()

    action_url, fields, identifier_field, password_field = _find_login_form(resp.text, base_url)
    fields[identifier_field] = username
    fields[password_field] = password

    login_resp = session.post(action_url, data=fields, timeout=30)
    login_resp.raise_for_status()

    if _has_password_form(login_resp.text):
        alert_match = ALERT_TEXT_RE.search(login_resp.text)
        detail = (
            f" MissionChief said: {alert_match.group(1).strip()}"
            if alert_match
            else " (still on the sign-in page after submitting — check MC_USERNAME/MC_PASSWORD, "
            "or the account may require a CAPTCHA/2FA step this tool can't complete)"
        )
        raise LoginFailedError(f"Login failed.{detail}")


def build_session(config: AuthConfig) -> requests.Session:
    """Build an authenticated `requests.Session` for any auth mode, with a CSRF token attached."""
    host = urlparse(config.base_url).netloc

    session = requests.Session()
    # Plain browser-navigation headers by default. Deliberately NOT X-Requested-With/AJAX
    # Accept here — Rails responds differently to requests flagged as AJAX (confirmed: with
    # those headers on a plain GET /, the CSRF meta tag goes missing from the response). The
    # JSON API endpoints need those headers, so mc_client.py adds them per-request instead.
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; mc-autobuilder/0.1)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    if config.auth_mode == "cookie":
        if not config.session_cookie:
            raise SessionExpiredError(
                "MC_SESSION_COOKIE is not set. Paste your MissionChief session cookie into .env, "
                "set MC_AUTH_MODE=credentials with MC_USERNAME/MC_PASSWORD, or set "
                "MC_AUTH_MODE=playwright and run `mc-autobuilder login`."
            )
        session.cookies = _cookie_header_to_jar(config.session_cookie, host)

    elif config.auth_mode == "playwright":
        try:
            session.cookies = _storage_state_to_jar(config.storage_state_path, host)
        except SessionExpiredError as exc:
            raise SessionExpiredError(
                f"{exc} Run `mc-autobuilder login` to log in interactively first."
            ) from exc

    elif config.auth_mode == "credentials":
        if not (config.username and config.password):
            raise SessionExpiredError(
                "MC_AUTH_MODE=credentials requires both MC_USERNAME and MC_PASSWORD in .env."
            )
        try:
            session.cookies = _storage_state_to_jar(config.storage_state_path, host)
            session.headers["X-CSRF-Token"] = fetch_csrf_token(session, config.base_url)
            return session  # cached session from a previous login is still valid
        except SessionExpiredError:
            pass  # no cache, or it expired — fall through to a fresh login
        login_with_credentials(session, config.base_url, config.username, config.password)
        _save_session_cookies(session.cookies, config.storage_state_path)

    else:
        raise ValueError(
            f"Unknown MC_AUTH_MODE: {config.auth_mode!r} (expected 'cookie', 'credentials', or 'playwright')"
        )

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
