"""PostgreSQL integration contracts for local web authentication."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from mevo2glpi_database import create_session_factory
from mevo2glpi_web.application import build_application
from mevo2glpi_web.auth.admin import LocalAccountAdminService
from mevo2glpi_web.auth.http import LOGIN_CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from mevo2glpi_web.auth.passwords import Argon2PasswordHasher
from mevo2glpi_web.auth.persistence import SqlAlchemyLocalAccountStore
from mevo2glpi_web.settings import WebSettings

pytestmark = pytest.mark.database

_USERNAME = "synthetic.operator"
_PASSWORD = "synthetic local passphrase"
_SESSION_SECRET = "synthetic-integration-session-secret-at-least-32-characters"


def test_local_login_and_account_disable_use_the_real_database_composition(
    tmp_path: Path,
    migrated_engine: Engine,
    migrated_database_url: str,
) -> None:
    audio_root = tmp_path / "audios"
    audio_root.mkdir()
    admin = LocalAccountAdminService(
        SqlAlchemyLocalAccountStore(create_session_factory(migrated_engine)),
        Argon2PasswordHasher(),
    )
    admin.create(_USERNAME, _PASSWORD)
    application = build_application(
        WebSettings(
            audio_root=audio_root,
            database_url=migrated_database_url,
            web_auth_mode="local",
            web_auth_session_secret=_SESSION_SECRET,
            web_auth_cookie_secure=False,
            _env_file=None,
        )
    )

    with TestClient(application) as client:
        login_page = client.get("/auth/login")
        assert login_page.status_code == 200
        csrf_token = client.cookies.get(LOGIN_CSRF_COOKIE_NAME)
        assert csrf_token is not None

        login = client.post(
            "/auth/login",
            data={
                "username": _USERNAME,
                "password": _PASSWORD,
                "csrf_token": csrf_token,
                "next_path": "/api/version",
            },
            follow_redirects=False,
        )

        assert login.status_code == 303
        assert login.headers["location"] == "/api/version"
        assert client.cookies.get(SESSION_COOKIE_NAME) is not None
        assert client.get("/api/version").status_code == 200

        admin.disable(_USERNAME)
        rejected = client.get("/api/version", follow_redirects=False)
        assert rejected.status_code == 401
        assert rejected.json() == {"detail": "Authentication required"}
        assert client.cookies.get(SESSION_COOKIE_NAME) is None
