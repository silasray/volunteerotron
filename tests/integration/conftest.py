import pytest

from web import api_client


@pytest.fixture
def stack(monkeypatch, web_app, api):
    """The web app talking to a real API app in-process, instead of over HTTP.

    Replaces only the HTTP transport (api_client._http), so everything else in
    api_client runs as in production. Returns the web test client.
    """
    base = web_app.config["API_URL"].removesuffix("/api")

    def http(method, url, headers, body):
        assert url.startswith(base), url
        resp = api.open(url[len(base):], method=method, headers=headers, json=body)
        return resp.status_code, resp.get_data(as_text=True)

    monkeypatch.setattr(api_client, "_http", http)
    return web_app.test_client()


@pytest.fixture
def db_ctx(api_app):
    """A database session owned by the test, inside an app context.

    Separate from db.session: test-client requests reuse this app context and
    close db.session when they finish, which would detach the test's objects.
    Objects keep their loaded values across commits; call expire_all() before
    reading what a request changed.
    """
    from sqlalchemy.orm import Session

    from api.models import db

    with api_app.app_context():
        session = Session(db.engine, expire_on_commit=False)
        yield session
        session.close()


@pytest.fixture
def world(db_ctx):
    """See tests/world.py."""
    from tests.world import build_world

    return build_world(db_ctx)
