"""Configuration for the web dashboard (Phase 6) — separate from AuthConfig since it protects
the dashboard itself, not the MissionChief session."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from .auth import load_config


class WebConfigError(RuntimeError):
    pass


@dataclass
class WebConfig:
    password: str
    secret_key: str

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "WebConfig":
        values = load_config(env_path)
        password = values.get("DASHBOARD_PASSWORD")
        if not password:
            raise WebConfigError(
                "DASHBOARD_PASSWORD is not set in .env. Since this dashboard can spend real "
                "credits, it requires its own password separate from your MissionChief "
                "credentials — add a line like DASHBOARD_PASSWORD=<a long random value> to .env."
            )
        # A stable DASHBOARD_SECRET_KEY keeps logins across server restarts; without one, a
        # random key is generated per-process (safe, just logs everyone out on every restart).
        secret_key = values.get("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)
        return cls(password=password, secret_key=secret_key)
