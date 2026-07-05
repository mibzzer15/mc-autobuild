import pytest

from mc_autobuilder.auth import (
    SessionExpiredError,
    _cookie_header_to_jar,
    _storage_state_to_jar,
    extract_csrf_token,
)


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
