import pytest

from mc_autobuilder.auth import (
    LoginFailedError,
    SessionExpiredError,
    _cookie_header_to_jar,
    _find_login_form,
    _has_password_form,
    _save_session_cookies,
    _storage_state_to_jar,
    extract_csrf_token,
)

DEVISE_STYLE_LOGIN_PAGE = """
<html><body>
<form action="/users/sign_in" method="post">
  <input type="hidden" name="authenticity_token" value="tok123">
  <input type="hidden" name="utf8" value="&#x2713;">
  <input type="email" name="user[email]" id="user_email">
  <input type="password" name="user[password]" id="user_password">
  <input type="checkbox" name="user[remember_me]" value="1">
  <input type="submit" name="commit" value="Log in">
</form>
</body></html>
"""


def test_extract_csrf_token_finds_meta_tag():
    html = '<html><head><meta name="csrf-token" content="abc123=="></head></html>'
    assert extract_csrf_token(html) == "abc123=="


def test_extract_csrf_token_missing_raises():
    with pytest.raises(SessionExpiredError):
        extract_csrf_token("<html><head></head></html>")


def test_cookie_header_to_jar_parses_multiple_cookies():
    jar = _cookie_header_to_jar("session_id=abc; other=def; flag", "www.missionchief.com")
    values = {c.name: c.value for c in jar}
    assert values["session_id"] == "abc"
    assert values["other"] == "def"
    assert "flag" not in values  # no '=' means it's skipped, not a valid cookie pair


def test_storage_state_to_jar_matches_leading_dot_domain(tmp_path):
    storage_state = tmp_path / "storage_state.json"
    storage_state.write_text(
        '{"cookies": ['
        '{"name": "session_id", "value": "abc", "domain": ".missionchief.com", "path": "/"},'
        '{"name": "unrelated", "value": "xyz", "domain": ".example.com", "path": "/"}'
        "]}"
    )
    jar = _storage_state_to_jar(storage_state, "www.missionchief.com")
    values = {c.name: c.value for c in jar}
    assert values == {"session_id": "abc"}


def test_storage_state_to_jar_missing_file_raises(tmp_path):
    with pytest.raises(SessionExpiredError):
        _storage_state_to_jar(tmp_path / "missing.json", "www.missionchief.com")


def test_storage_state_to_jar_no_matching_cookies_raises(tmp_path):
    storage_state = tmp_path / "storage_state.json"
    storage_state.write_text('{"cookies": [{"name": "x", "value": "y", "domain": ".example.com", "path": "/"}]}')
    with pytest.raises(SessionExpiredError):
        _storage_state_to_jar(storage_state, "www.missionchief.com")


def test_find_login_form_scrapes_real_field_names():
    action_url, fields, identifier_field, password_field = _find_login_form(
        DEVISE_STYLE_LOGIN_PAGE, "https://www.missionchief.com"
    )
    assert action_url == "https://www.missionchief.com/users/sign_in"
    assert identifier_field == "user[email]"
    assert password_field == "user[password]"
    # hidden fields (CSRF token etc.) are preserved so they get submitted along with credentials
    assert fields["authenticity_token"] == "tok123"


def test_find_login_form_missing_password_field_raises():
    with pytest.raises(LoginFailedError):
        _find_login_form("<html><body>no form here</body></html>", "https://www.missionchief.com")


def test_has_password_form_true_for_login_page():
    assert _has_password_form(DEVISE_STYLE_LOGIN_PAGE) is True


def test_has_password_form_false_for_dashboard_page():
    assert _has_password_form('<html><body><meta name="csrf-token" content="x"></body></html>') is False


def test_save_session_cookies_round_trips_through_storage_state(tmp_path):
    import requests

    jar = requests.cookies.RequestsCookieJar()
    jar.set("session_id", "abc", domain=".missionchief.com", path="/")
    path = tmp_path / "cache.json"

    _save_session_cookies(jar, path)
    loaded = _storage_state_to_jar(path, "www.missionchief.com")

    assert {c.name: c.value for c in loaded} == {"session_id": "abc"}
