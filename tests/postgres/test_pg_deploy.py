"""What only the deployed setup does to the database connection.

  bootstrap-db  (api/db_bootstrap.py) creates the app's login role as the master user
  DB_IAM_AUTH   (api/db_auth.py) logs in with a fresh IAM token per connection
  create_app    sets Postgres engine options: pool size, pre-ping, and no
                server-side prepared statements (for RDS Proxy / PgBouncer)

RDS IAM tokens are just passwords to Postgres, so a role with a known password
and a token provider that returns it stands in for IAM.

Roles are server-wide, unlike the per-test databases, so each test uses
uniquely named roles and drops them afterwards.
"""
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

import lambda_handlers
from api import db_auth
from api.db_bootstrap import bootstrap
from api.models import db
from tests.world import PASSWORD

from .conftest import _create_database, _drop_database, app_on, dispose, migrate

TOKEN = "iam-token-stand-in"


def run(server, sql, **params):
    with server.connect() as conn:
        return conn.execute(text(sql), params)


@pytest.fixture
def fresh_deploy(pg_server):
    """An empty database and the name of the app role to bootstrap into it, as
    on a first deploy. Returns (url as the app role, master user, master password)."""
    database = f"vs_deploy_{uuid.uuid4().hex[:8]}"
    role = f"scheduler_app_{uuid.uuid4().hex[:8]}"
    _create_database(pg_server, database)
    master = pg_server.url
    yield master.set(database=database, username=role, password=None), master.username, master.password
    _drop_database(pg_server, database)
    try:
        run(pg_server, f'DROP ROLE IF EXISTS "{role}"')
    except Exception:
        pass  # couldn't have been created either


@pytest.fixture
def bootstrapped(fresh_deploy, monkeypatch):
    url, master, master_password = fresh_deploy
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    monkeypatch.setenv("DB_MASTER_USER", master)
    try:
        output = bootstrap(master_password)
    except psycopg.errors.InsufficientPrivilege as err:
        pytest.skip(f"the test server user can't create roles ({err})")
    return url, output


@pytest.fixture
def tokens(monkeypatch):
    """The IAM token provider: returns TOKEN and records each (host, port, user) asked for."""
    calls = []

    def provide(host, port, user):
        calls.append((host, port, user))
        return TOKEN

    monkeypatch.setattr(db_auth, "token_provider", provide)
    return calls


def iam_app(url, **env):
    return app_on(url, DB_IAM_AUTH="1", DB_POOL_SIZE="1", **env)


# ---------------------------------------------------------------- bootstrap-db


def test_bootstrap_creates_the_app_role(pg_server, bootstrapped):
    url, output = bootstrapped
    role, database = url.username, url.database
    assert output == (f"created role {role}; rds_iam not present (not RDS); skipped; "
                      f"granted connect on {database} and create on schema public")
    login, password = run(pg_server, "SELECT rolcanlogin, rolpassword FROM pg_authid WHERE rolname = :r",
                          r=role).one()
    assert login is True and password is None  # IAM only: no password to leak
    assert run(pg_server, "SELECT has_database_privilege(:r, :d, 'CONNECT')", r=role, d=database).scalar()


def test_bootstrap_is_safe_to_rerun(bootstrapped, fresh_deploy):
    url, _ = bootstrapped
    _, _, master_password = fresh_deploy
    assert bootstrap(master_password).startswith(f"role {url.username} already exists; ")


def test_bootstrap_grants_rds_iam_where_it_exists(pg_server, fresh_deploy, monkeypatch):
    if run(pg_server, "SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam'").scalar():
        pytest.skip("rds_iam already exists on this server; not touching it")
    url, master, master_password = fresh_deploy
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    monkeypatch.setenv("DB_MASTER_USER", master)
    run(pg_server, "CREATE ROLE rds_iam")
    try:
        assert "granted rds_iam (IAM login)" in bootstrap(master_password)
        assert run(pg_server, "SELECT pg_has_role(:r, 'rds_iam', 'member')", r=url.username).scalar()
    finally:
        run(pg_server, f'REVOKE rds_iam FROM "{url.username}"')
        run(pg_server, "DROP ROLE rds_iam")


# ---------------------------------------------------------------- the whole first deploy


def test_first_deploy_end_to_end(pg_server, bootstrapped, tokens, monkeypatch):
    """README's "After the first deploy": bootstrap-db, then migrate and
    create-user as the app role over IAM, then serve a sign-in."""
    url, _ = bootstrapped
    run(pg_server, f"ALTER ROLE \"{url.username}\" PASSWORD '{TOKEN}'")  # what IAM would accept

    app = iam_app(url)
    monkeypatch.setattr(lambda_handlers, "_apps", {"api": app})
    try:
        assert lambda_handlers.api_handler({"command": "migrate"}, None)["ok"] is True
        created = lambda_handlers.api_handler(
            {"command": "create-user", "name": "root", "password": PASSWORD, "superuser": True}, None)
        assert created == {"ok": True, "output": "created user 'root' (superuser)"}
        login = lambda_handlers.api_handler({"internal_request": {
            "method": "POST", "path": "/api/auth/login",
            "body": f'{{"name": "root", "password": "{PASSWORD}"}}'}}, None)
        assert login["status"] == 200
        # The app role owns what it created, so later migrations can alter it.
        admin = _admin_on(pg_server, url)
        owners = run(admin, "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
        assert owners.scalars().all() == [url.username]
        admin.dispose()
    finally:
        dispose(app)
    assert tokens and set(tokens) == {(url.host, url.port, url.username)}


def _admin_on(pg_server, url):
    """An engine as the master user on url's database (the server engine is on another one)."""
    return create_engine(pg_server.url.set(database=url.database), poolclass=NullPool)


# ---------------------------------------------------------------- IAM tokens


@pytest.fixture
def token_role(pg_server, pg_url):
    """A role that logs in with TOKEN and may use the test database."""
    role = f"scheduler_app_{uuid.uuid4().hex[:8]}"
    try:
        run(pg_server, f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{TOKEN}'")
    except Exception as err:
        pytest.skip(f"the test server user can't create roles ({err})")
    admin = _admin_on(pg_server, pg_url)
    with admin.begin() as conn:
        conn.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"'))
    yield pg_url.set(username=role, password=None)
    with admin.begin() as conn:
        conn.execute(text(f'DROP OWNED BY "{role}"'))
    admin.dispose()
    run(pg_server, f'DROP ROLE "{role}"')


def test_each_new_connection_gets_a_fresh_token(token_role, tokens):
    app = iam_app(token_role)
    try:
        with app.app_context():
            for _ in range(2):
                with db.engine.connect() as conn:
                    assert conn.execute(text("SELECT current_user")).scalar() == token_role.username
            assert len(tokens) == 1  # the pooled connection is reused
            db.engine.dispose()
            with db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        assert len(tokens) == 2  # tokens expire after 15 minutes: never cached
    finally:
        dispose(app)


def test_password_in_the_url_is_not_used(token_role, tokens):
    # Only the token logs in; a stale password left in DATABASE_URL is replaced.
    app = iam_app(token_role.set(password="stale"))
    try:
        with app.app_context(), db.engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        dispose(app)


def test_a_rejected_token_fails_to_connect(token_role, monkeypatch):
    monkeypatch.setattr(db_auth, "token_provider", lambda host, port, user: "expired-token")
    app = iam_app(token_role)
    try:
        with app.app_context(), pytest.raises(OperationalError, match="password authentication failed"):
            db.engine.connect()
    finally:
        dispose(app)


# ---------------------------------------------------------------- engine options


def test_pool_settings_come_from_the_environment(pg_url):
    app = app_on(pg_url, DB_POOL_SIZE="1")
    try:
        options = app.config["SQLALCHEMY_ENGINE_OPTIONS"]
        assert (options["pool_size"], options["max_overflow"], options["pool_pre_ping"]) == (1, 0, True)
        with app.app_context():
            assert db.engine.pool.size() == 1
    finally:
        dispose(app)


def test_no_server_side_prepared_statements(api_app, pg_url):
    # Poolers that share server connections between clients (RDS Proxy,
    # PgBouncer in transaction mode) break if a client prepares statements.
    query = text("SELECT count(*) FROM app_user WHERE name = :n")
    with api_app.app_context(), db.engine.connect() as conn:
        for _ in range(10):
            conn.execute(query, {"n": "alice"})
        assert conn.execute(text("SELECT count(*) FROM pg_prepared_statements")).scalar() == 0

    # Control: psycopg's default prepares a statement after its 5th run, so the
    # check above would have caught a missing setting.
    with psycopg.connect(pg_url.set(drivername="postgresql").render_as_string(hide_password=False)) as raw:
        for _ in range(10):
            raw.execute("SELECT count(*) FROM app_user WHERE name = %s", ["alice"])
        assert raw.execute("SELECT count(*) FROM pg_prepared_statements").fetchone()[0] > 0


def test_dropped_connections_are_replaced(api_app, pg_server):
    # Between Lambda invocations a pooled connection may be closed server-side;
    # pre-ping notices and reconnects instead of failing the next request.
    with api_app.app_context():
        with db.engine.connect() as conn:
            pid = conn.execute(text("SELECT pg_backend_pid()")).scalar()
        run(pg_server, "SELECT pg_terminate_backend(:p)", p=pid)
        with db.engine.connect() as conn:
            assert conn.execute(text("SELECT pg_backend_pid()")).scalar() != pid
