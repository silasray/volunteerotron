"""Shared setup for every test.

Layout:
  unit/         pure functions and model rules; no app, no database
  api/          the API app through Flask's test client, on a throwaway SQLite file
  web/          the web app with the API replaced by a stub (fake_api)
  integration/  web and API wired together in-process: browser-level flows
"""
import shutil

import pytest

# Settings the app factories read from the environment. Cleared so a developer's
# shell (or a .env) can't point tests at a real database or the Lambda API.
_APP_ENV = (
    "DATABASE_URL", "AUTO_CREATE_TABLES", "API_SECRET_KEY", "AUTH_TOKEN_MAX_AGE",
    "LOGIN_RATE_LIMIT", "LOGIN_IP_RATE_LIMIT", "TRUSTED_CLIENT_IP_HEADER",
    "RATELIMIT_STORAGE_URI", "DB_IAM_AUTH", "DB_POOL_SIZE",
    "SECRET_KEY", "API_FUNCTION_NAME", "API_URL", "SESSION_COOKIE_SECURE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _APP_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _cheap_password_hashing(monkeypatch):
    """Still scrypt (hashes keep the "scrypt:" prefix the model requires), but
    with tiny work factors so creating users doesn't dominate test time."""
    from werkzeug import security

    from api import models

    def cheap(password, method="scrypt", salt_length=16):
        if method == "scrypt":
            method = "scrypt:1024:8:1"
        return security.generate_password_hash(password, method, salt_length)

    monkeypatch.setattr(models, "generate_password_hash", cheap)


@pytest.fixture(scope="session")
def _empty_db_file(tmp_path_factory):
    """A SQLite file with every table created, built once per run. Creating the
    schema costs ~100ms, so each test copies this file instead."""
    from api import create_app
    from api.models import db

    path = tmp_path_factory.mktemp("schema") / "empty.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", "sqlite:///" + str(path))
        app = create_app()  # AUTO_CREATE_TABLES defaults on
    with app.app_context():
        db.engine.dispose()
    return path


@pytest.fixture
def api_app(monkeypatch, tmp_path, _empty_db_file):
    """A fresh API app with its own SQLite database, tables created."""
    from api import create_app
    from api.models import db

    path = tmp_path / "test.db"
    shutil.copyfile(_empty_db_file, path)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(path))
    monkeypatch.setenv("AUTO_CREATE_TABLES", "0")
    monkeypatch.setenv("API_SECRET_KEY", "test-api-secret")
    app = create_app()
    app.config["TESTING"] = True
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()  # release the file so tmp_path can be removed (Windows)


@pytest.fixture
def api(api_app):
    """Test client for the API. Paths include the /api prefix."""
    return api_app.test_client()


@pytest.fixture
def web_app(monkeypatch):
    from web import create_app

    monkeypatch.setenv("SECRET_KEY", "test-web-secret")
    monkeypatch.setenv("API_URL", "http://api.test/api")
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def web(web_app):
    return web_app.test_client()
