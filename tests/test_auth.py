import pytest

from mc_autobuilder.auth import (
    LoginFailedError,
    SessionExpiredError,
    _cookie_header_to_jar,
    _find_login_form,
    _has_password_form,
    _save_session_cookies,
    _storage_state_to_jar,
    check_session_alive,
    extract_csrf_token,
    load_config,
    update_env_file,
)


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

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

# Trimmed excerpt of a real capture of https://www.missionchief.com/users/sign_in
# (2026-07), including the exact attribute order the real site uses.
REAL_SIGN_IN_PAGE = """
<meta content="authenticity_token" name="csrf-param" />
<meta content="Wkp3S9SICwpH8VkEqAUni2J1DOhWgW30dG+v19vTDS4=" name="csrf-token" />
<form accept-charset="UTF-8" action="/users/sign_in" class="simple_form form-horizontal" id="new_user" method="post" novalidate="novalidate">
<div style="margin:0;padding:0;display:inline">
<input name="utf8" type="hidden" value="&#x2713;" />
<input name="authenticity_token" type="hidden" value="Wkp3S9SICwpH8VkEqAUni2J1DOhWgW30dG+v19vTDS4=" />
</div>
<input class="string email optional form-control" id="user_email" name="user[email]" type="email" value="" />
<input class="password optional form-control" id="user_password" name="user[password]" type="password" />
<input name="user[remember_me]" type="hidden" value="0" />
<input class="boolean optional" id="user_remember_me" name="user[remember_me]" type="checkbox" value="1" />
<input class="btn btn btn-success pull-right btn-lg" name="commit" type="submit" value="Login" />
</form>
"""


def test_extract_csrf_token_finds_meta_tag():
    html = '<html><head><meta name="csrf-token" content="abc123=="></head></html>'
    assert extract_csrf_token(html) == "abc123=="


def test_extract_csrf_token_handles_content_before_name_attribute_order():
    # Regression test: the real site emits `content` before `name` (confirmed from a live
    # capture of missionchief.com), which an order-sensitive regex previously missed entirely.
    html = '<meta content="authenticity_token" name="csrf-param" />' '<meta content="realtoken==" name="csrf-token" />'
    assert extract_csrf_token(html) == "realtoken=="


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


def test_find_login_form_matches_real_captured_sign_in_page():
    action_url, fields, identifier_field, password_field = _find_login_form(
        REAL_SIGN_IN_PAGE, "https://www.missionchief.com"
    )
    assert action_url == "https://www.missionchief.com/users/sign_in"
    assert identifier_field == "user[email]"
    assert password_field == "user[password]"
    assert fields["authenticity_token"] == "Wkp3S9SICwpH8VkEqAUni2J1DOhWgW30dG+v19vTDS4="


def test_extract_csrf_token_matches_real_captured_sign_in_page():
    assert extract_csrf_token(REAL_SIGN_IN_PAGE) == "Wkp3S9SICwpH8VkEqAUni2J1DOhWgW30dG+v19vTDS4="


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


def test_check_session_alive_raises_on_401():
    with pytest.raises(SessionExpiredError):
        check_session_alive(FakeResponse(401, ""))


def test_check_session_alive_raises_on_403():
    with pytest.raises(SessionExpiredError):
        check_session_alive(FakeResponse(403, ""))


def test_check_session_alive_passes_for_authenticated_page():
    check_session_alive(FakeResponse(200, '<html><meta name="csrf-token" content="x"></html>'))


def test_check_session_alive_detects_login_page_beyond_first_5000_chars():
    # Regression test: a real captured MissionChief homepage/sign-in page puts a large inline
    # <script> block of building-type constants before the nav/login markers, easily pushing
    # them past 5,000 characters. An earlier version of check_session_alive only searched
    # response.text[:5000] and silently failed to detect a logged-out session as a result.
    padding = "x" * 6000
    html = f"<html><body>{padding}<a href=\"/users/sign_in\">Login</a></body></html>"
    with pytest.raises(SessionExpiredError):
        check_session_alive(FakeResponse(200, html))


def test_update_env_file_replaces_existing_key_in_place(tmp_path):
    path = tmp_path / ".env"
    path.write_text("MC_AUTH_MODE=cookie\nMC_USERNAME=old\nDASHBOARD_PASSWORD=secret123\n")

    update_env_file(path, {"MC_USERNAME": "new"})

    text = path.read_text()
    assert "MC_USERNAME=new" in text
    assert "MC_AUTH_MODE=cookie" in text
    # Preserves a key it knows nothing about, in the same file.
    assert "DASHBOARD_PASSWORD=secret123" in text


def test_update_env_file_appends_missing_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text("MC_AUTH_MODE=cookie\n")

    update_env_file(path, {"MC_PASSWORD": "hunter2"})

    assert "MC_PASSWORD=hunter2" in path.read_text()


def test_update_env_file_omitted_keys_are_left_untouched(tmp_path):
    # Simulates a form submission where the password field was left blank (meaning "keep the
    # current value") - the caller should simply omit MC_PASSWORD from `updates` entirely.
    path = tmp_path / ".env"
    path.write_text("MC_PASSWORD=hunter2\nMC_USERNAME=alice\n")

    update_env_file(path, {"MC_USERNAME": "bob"})

    text = path.read_text()
    assert "MC_PASSWORD=hunter2" in text
    assert "MC_USERNAME=bob" in text


def test_update_env_file_creates_file_if_missing(tmp_path):
    path = tmp_path / ".env"
    update_env_file(path, {"MC_USERNAME": "alice"})
    assert load_config(path)["MC_USERNAME"] == "alice"
