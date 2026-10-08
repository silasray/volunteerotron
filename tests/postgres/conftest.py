"""Tests against a real Postgres, the database production runs (RDS, Postgres 18).

Where the server comes from, in order:
  TEST_DATABASE_URL  an existing server, e.g. postgresql+psycopg://user:pass@host/db
                     (the user needs CREATEDB; the named database is only used to
                     create and drop the test databases)
  Docker             a postgres:18 container, started once per run with testcontainers
                     and removed afterwards (its Ryuk sidecar cleans up even if
                     pytest is killed)
With neither, every test here is skipped. All of them carry the `postgres`
marker, so `pytest -m "not postgres"` leaves them out.

Fixture layers, cheapest reset last:
  pg_server    run    the server: an admin engine for CREATE/DROP DATABASE
  pg_template  run    a database built by the real migrations (alembic upgrade head),
                      not create_all, so tests see the schema production gets
  pg_url       test   a fresh copy of the template (CREATE DATABASE ... TEMPLATE,
                      ~60ms), dropped afterwards; every test starts clean
  api_app      test   the API app on that copy (overrides the SQLite one), so the
                      tests/api factories (make_user, make_window, ...) work unchanged
"""
import contextlib
import logging
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.api.conftest import *  # noqa: F401,F403  (the tests/api factories and fixtures)

IMAGE = "postgres:18"  # match template.yaml's EngineVersion


def pytest_collection_modifyitems(items):
    here = os.path.dirname(__file__)
    for item in items:
        if str(item.path).startswith(here):
            item.add_marker(pytest.mark.postgres)


def _start_container():
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:
        pytest.skip("Postgres tests need testcontainers (requirements-dev.txt) or TEST_DATABASE_URL")
    from docker.errors import DockerException

    try:
        # Durability off: nothing here needs to survive a crash, and it makes
        # each test's CREATE/DROP DATABASE ~20ms instead of ~400ms.
        return (PostgresContainer(IMAGE, driver="psycopg")
                .with_command("postgres -c fsync=off -c synchronous_commit=off -c full_page_writes=off")
                .start())
    except DockerException as err:
        pytest.skip(f"Postgres tests need Docker running or TEST_DATABASE_URL ({err})")


def _patient_port_lookup(mp):
    """Docker Desktop sometimes answers a port lookup with nothing for ~100ms
    after a container starts, though the port is already mapped; testcontainers
    asks once and gives up (seen with its Ryuk sidecar on about half of runs).
    Ask again for up to 2s."""
    import time

    from testcontainers.core.docker_client import DockerClient

    lookup = DockerClient.port

    def port(self, container_id, port):
        for _ in range(20):
            try:
                return lookup(self, container_id, port)
            except ConnectionError:
                time.sleep(0.1)
        return lookup(self, container_id, port)

    mp.setattr(DockerClient, "port", port)


@pytest.fixture(scope="session")
def pg_server():
    """An AUTOCOMMIT engine on the server's maintenance database (CREATE DATABASE
    can't run in a transaction)."""
    container = None
    with pytest.MonkeyPatch.context() as mp:
        if url := os.environ.get("TEST_DATABASE_URL"):
            url = make_url(url)
            if url.drivername == "postgresql":
                url = url.set(drivername="postgresql+psycopg")
        else:
            _patient_port_lookup(mp)
            container = _start_container()
            url = make_url(container.get_connection_url())
        engine = create_engine(url, isolation_level="AUTOCOMMIT")
        yield engine
        engine.dispose()
        if container is not None:
            container.stop()


def _create_database(server, name, template=None):
    clause = f' TEMPLATE "{template}"' if template else ""
    if template and server.dialect.server_version_info >= (15,):
        clause += " STRATEGY FILE_COPY"  # much faster than WAL_LOG for a database this small
    with server.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"{clause}'))
    return server.url.set(database=name)


def _drop_database(server, name):
    with server.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def app_on(url, **env):
    """An API app on the database at url, built the way production builds it
    (create_app's Postgres engine options included)."""
    from api import create_app

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
        mp.setenv("AUTO_CREATE_TABLES", "0")
        mp.setenv("API_SECRET_KEY", "test-api-secret")
        for name, value in env.items():
            mp.setenv(name, value)
        return create_app()


def dispose(app):
    from api.models import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@contextlib.contextmanager
def logging_preserved():
    """Undo migrations/env.py's logging.config.fileConfig(), which replaces the
    root logger's handlers and level (and sets levels on a few others) for the
    rest of the process, so later tests' log capture isn't affected."""
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    loggers = {name: (lg.disabled, lg.level) for name, lg in logging.root.manager.loggerDict.items()
               if isinstance(lg, logging.Logger)}
    try:
        yield
    finally:
        root.setLevel(level)
        root.handlers[:] = handlers
        for name, lg in logging.root.manager.loggerDict.items():
            if isinstance(lg, logging.Logger):
                lg.disabled, lg.level = loggers.get(name, (False, logging.NOTSET))


@pytest.fixture(autouse=True)
def _logging_preserved():
    # Tests here run migrations directly or through lambda_handlers' migrate command.
    with logging_preserved():
        yield


def migrate(app, revision="head", down=False):
    import flask_migrate

    with app.app_context(), logging_preserved():
        (flask_migrate.downgrade if down else flask_migrate.upgrade)(revision=revision)


@pytest.fixture(scope="session")
def pg_template(pg_server):
    """Name of a database at the latest migration, to copy for each test."""
    name = f"vs_template_{uuid.uuid4().hex[:8]}"
    url = _create_database(pg_server, name)
    app = app_on(url)
    migrate(app)
    dispose(app)  # CREATE DATABASE ... TEMPLATE needs it to have no connections
    yield name
    _drop_database(pg_server, name)


@pytest.fixture
def pg_url(pg_server, pg_template):
    """URL of a fresh, migrated database for this test only."""
    name = f"vs_test_{uuid.uuid4().hex[:12]}"
    yield _create_database(pg_server, name, template=pg_template)
    _drop_database(pg_server, name)


@pytest.fixture
def api_app(pg_url, monkeypatch):
    # The environment stays set for the whole test, as with the SQLite api_app,
    # so a test can build a second app instance on the same database.
    from api import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url.render_as_string(hide_password=False))
    monkeypatch.setenv("AUTO_CREATE_TABLES", "0")
    monkeypatch.setenv("API_SECRET_KEY", "test-api-secret")
    app = create_app()
    app.config["TESTING"] = True
    yield app
    dispose(app)
