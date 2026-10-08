"""Login rate limits, and the database-backed counters behind them.

POST /api/auth/login has two limits (see api/routes.py):
  per account name  LOGIN_RATE_LIMIT, counting failed attempts only
  per client IP     LOGIN_IP_RATE_LIMIT, counting every attempt
The client IP comes from TRUSTED_CLIENT_IP_HEADER when that's configured
(deployed: the web tier sets X-Client-IP), otherwise the connecting address.
Counters live in the rate_limit table (RATELIMIT_STORAGE_URI=appdb://), so
every app instance shares them.
"""
import pytest
from sqlalchemy import func, select, text

from api import ratelimit_storage
from api.models import db
from api.ratelimit_storage import AppDatabaseStorage

from .conftest import PASSWORD

LOGIN = "/api/auth/login"
WRONG = "wrong password!"
LIMITED = {"error": "too many attempts; try again later"}


@pytest.fixture
def limits(api_app):
    """limits(name=..., ip=..., header=...) sets the app's login limits."""
    def set_(name="3 per minute", ip="100 per minute", header=None):
        api_app.config.update(LOGIN_RATE_LIMIT=name, LOGIN_IP_RATE_LIMIT=ip, TRUSTED_CLIENT_IP_HEADER=header)
    return set_


def login(api, name, password=PASSWORD, ip="127.0.0.1", headers=None):
    return api.post(LOGIN, json={"name": name, "password": password},
                    headers=headers, environ_base={"REMOTE_ADDR": ip})


# ---------------------------------------------------------------- per account name


def test_failed_logins_lock_the_name(api, make_user, limits):
    limits(name="3 per minute")
    make_user("alice")
    assert [login(api, "alice", WRONG).status_code for _ in range(3)] == [401] * 3
    resp = login(api, "alice", WRONG)
    assert resp.status_code == 429
    assert resp.json == LIMITED
    # Even the right password is refused until the window passes.
    assert login(api, "alice").status_code == 429


def test_name_lock_follows_the_name_across_ips(api, make_user, limits):
    limits(name="3 per minute")
    make_user("alice")
    for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        assert login(api, "alice", WRONG, ip=ip).status_code == 401
    assert login(api, "alice", ip="10.0.0.4").status_code == 429


def test_successful_logins_dont_count_against_the_name(api, make_user, limits):
    limits(name="3 per minute")
    make_user("alice")
    for _ in range(5):
        assert login(api, "alice").status_code == 200
    assert login(api, "alice", WRONG).status_code == 401


def test_name_key_ignores_case_and_spaces(api, make_user, limits):
    limits(name="3 per minute")
    make_user("alice")
    for name in ("alice", "ALICE", " Alice "):
        login(api, name, WRONG)
    assert login(api, "alice").status_code == 429


def test_unknown_names_are_limited_too(api, limits):
    limits(name="2 per minute")
    assert [login(api, "nobody", WRONG).status_code for _ in range(3)] == [401, 401, 429]


def test_other_names_are_unaffected(api, make_user, limits):
    limits(name="2 per minute")
    make_user("alice")
    make_user("bob")
    login(api, "alice", WRONG)
    login(api, "alice", WRONG)
    assert login(api, "alice").status_code == 429
    assert login(api, "bob").status_code == 200


def test_bad_requests_dont_count_against_the_name(api, make_user, limits):
    limits(name="2 per minute")
    make_user("alice")
    for _ in range(3):
        assert api.post(LOGIN, json={"name": "alice"}).status_code == 400
    assert login(api, "alice").status_code == 200


# ---------------------------------------------------------------- per client IP


def test_ip_limit_counts_every_attempt(api, make_user, limits):
    limits(name="100 per minute", ip="3 per minute")
    make_user("alice")
    assert login(api, "alice").status_code == 200
    assert login(api, "alice", WRONG).status_code == 401
    assert login(api, "someone", WRONG).status_code == 401
    assert login(api, "alice").status_code == 429
    assert login(api, "alice", ip="10.9.9.9").status_code == 200


def test_client_ip_header_ignored_unless_trusted(api, make_user, limits):
    # Locally (no TRUSTED_CLIENT_IP_HEADER) a forged header mustn't dodge the limit.
    limits(name="100 per minute", ip="2 per minute", header=None)
    make_user("alice")
    for n in range(2):
        assert login(api, "alice", headers={"X-Client-IP": f"10.0.0.{n}"}).status_code == 200
    assert login(api, "alice", headers={"X-Client-IP": "10.0.0.99"}).status_code == 429


def test_trusted_header_is_the_client_ip(api, make_user, limits):
    # Deployed: every call arrives from the web function, so the header is
    # what tells clients apart.
    limits(name="100 per minute", ip="2 per minute", header="X-Client-IP")
    make_user("alice")
    browser = {"X-Client-IP": "203.0.113.7"}
    assert login(api, "alice", headers=browser).status_code == 200
    assert login(api, "alice", headers=browser).status_code == 200
    assert login(api, "alice", headers=browser).status_code == 429
    assert login(api, "alice", headers={"X-Client-IP": "198.51.100.1"}).status_code == 200


def test_trusted_header_missing_falls_back_to_the_address(api, make_user, limits):
    limits(name="100 per minute", ip="1 per minute", header="X-Client-IP")
    make_user("alice")
    assert login(api, "alice", ip="10.0.0.1").status_code == 200
    assert login(api, "alice", ip="10.0.0.1").status_code == 429
    assert login(api, "alice", ip="10.0.0.2").status_code == 200


def test_only_login_is_limited(api, make_user, auth, limits):
    limits(name="1 per minute", ip="1 per minute")
    user = make_user("alice")
    login(api, "alice")
    for _ in range(3):
        assert api.get("/api/health").status_code == 200
        assert api.get("/api/admin/me", headers=auth(user)).status_code == 200


# ---------------------------------------------------------------- shared, durable counters


def rate_limit_rows(api_app):
    with api_app.app_context():
        with db.engine.connect() as conn:
            return conn.execute(text("SELECT key_hash, hits FROM rate_limit")).all()


def test_counters_are_shared_between_app_instances(api, api_app, make_user, limits):
    # Lambda runs several instances against one database.
    from api import create_app

    limits(name="2 per minute")
    make_user("alice")
    login(api, "alice", WRONG)
    other = create_app()  # same DATABASE_URL as api_app
    other.config.update(api_app.config)
    login(other.test_client(), "alice", WRONG)
    assert login(api, "alice").status_code == 429
    with other.app_context():
        db.engine.dispose()


def test_counters_store_hashed_keys_only(api, api_app, make_user, limits):
    limits()
    make_user("alice")
    login(api, "alice", WRONG, ip="203.0.113.7")
    rows = rate_limit_rows(api_app)
    assert rows
    for key_hash, _ in rows:
        assert len(key_hash) == 64
        assert "alice" not in key_hash and "203.0.113.7" not in key_hash


@pytest.fixture
def pool_of_one(monkeypatch):
    """Build the next api_app with a single-connection pool, like Lambda (DB_POOL_SIZE=1)."""
    original = db.init_app

    def init_app(app):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_size": 1, "max_overflow": 0, "pool_timeout": 2}
        original(app)

    monkeypatch.setattr(db, "init_app", init_app)


def test_failed_login_is_counted_with_a_pool_of_one(pool_of_one, api, api_app, make_user, limits):
    # The limiter records a failure on its own connection after the request.
    # The request's connection must be released first or this waits on itself.
    with api_app.app_context():
        assert db.engine.pool.size() == 1
    limits(name="2 per minute")
    make_user("alice")
    assert login(api, "alice", WRONG).status_code == 401
    assert login(api, "alice", WRONG).status_code == 401
    assert login(api, "alice").status_code == 429


# ---------------------------------------------------------------- the storage backend


@pytest.fixture
def storage(api_app):
    with api_app.app_context():
        yield AppDatabaseStorage("appdb://")


@pytest.fixture
def clock(monkeypatch):
    """Controls the storage's time.time()."""
    class Clock:
        now = 1_000_000.0

    monkeypatch.setattr(ratelimit_storage.time, "time", lambda: Clock.now)
    return Clock


def test_storage_counts_and_expires(storage, clock):
    assert storage.get("k") == 0
    assert storage.incr("k", 60) == 1
    assert storage.incr("k", 60, amount=2) == 3
    assert storage.get("k") == 3
    assert storage.get_expiry("k") == clock.now + 60
    clock.now += 30
    assert storage.incr("k", 60) == 4
    assert storage.get_expiry("k") == clock.now - 30 + 60  # the window doesn't slide
    clock.now += 30  # exactly at expiry: the window is over
    assert storage.get("k") == 0
    assert storage.get_expiry("k") == clock.now
    assert storage.incr("k", 60) == 1  # a fresh window
    assert storage.get_expiry("k") == clock.now + 60


def test_storage_keys_are_independent(storage, clock):
    storage.incr("a", 60)
    storage.incr("a", 60)
    storage.incr("b", 60)
    assert (storage.get("a"), storage.get("b")) == (2, 1)
    storage.clear("a")
    assert (storage.get("a"), storage.get("b")) == (0, 1)


def test_storage_reset_clears_everything(storage, clock):
    storage.incr("a", 60)
    storage.incr("b", 60)
    assert storage.reset() == 2
    assert (storage.get("a"), storage.get("b")) == (0, 0)


def count_rows():
    with db.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(text("rate_limit"))).scalar()


def test_storage_occasionally_deletes_expired_rows(storage, clock, monkeypatch):
    storage.incr("old", 10)
    storage.incr("current", 600)
    clock.now += 60
    monkeypatch.setattr(ratelimit_storage.random, "randrange", lambda n: 1)  # not this time
    storage.incr("new", 60)
    assert count_rows() == 3
    monkeypatch.setattr(ratelimit_storage.random, "randrange", lambda n: 0)
    storage.incr("new", 60)
    assert count_rows() == 2
    assert (storage.get("current"), storage.get("new")) == (1, 2)


def test_storage_check(storage, monkeypatch):
    assert storage.check() is True
    assert storage.base_exceptions is ratelimit_storage.SQLAlchemyError

    from sqlalchemy.exc import OperationalError

    def broken():
        raise OperationalError("SELECT 1", {}, Exception("database is down"))

    monkeypatch.setattr(db.engine, "connect", broken)
    assert storage.check() is False
