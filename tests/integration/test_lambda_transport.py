"""The deployed path through both tiers: the web app invokes the API "Lambda"
(lambda_handlers.api_handler) with the same JSON payload AWS would carry, and
the API trusts the client IP the web forwards (TRUSTED_CLIENT_IP_HEADER), as
template.yaml configures it. Only boto3's invoke is replaced.
"""
import io
import json
from datetime import datetime, timezone

import pytest

import lambda_handlers
from api.models import Event, Organization, User, VolunteerWindow
from tests.world import PASSWORD
from web import api_client


@pytest.fixture
def deployed(monkeypatch, web_app, api_app):
    """The web test client, wired to the API app through api_handler. Returns
    (client, invocations), invocations being every payload the web sent."""
    web_app.config["API_FUNCTION_NAME"] = "scheduler-ApiFunction"
    api_app.config["TRUSTED_CLIENT_IP_HEADER"] = "X-Client-IP"
    monkeypatch.setattr(lambda_handlers, "_apps", {"api": api_app})
    invocations = []

    class Client:
        def invoke(self, FunctionName, Payload):
            event = json.loads(Payload)  # what AWS carries is just bytes
            invocations.append(event)
            result = lambda_handlers.api_handler(event, None)
            return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(result).encode())}

    monkeypatch.setattr(api_client, "_lambda_client", lambda: Client())

    def http(*args):
        raise AssertionError("the deployed web tier must not use HTTP")

    monkeypatch.setattr(api_client, "_http", http)
    return web_app.test_client(), invocations


def browser(web_app, ip):
    client = web_app.test_client()
    client.environ_base["REMOTE_ADDR"] = ip
    return client


def sign_in(client, name, password):
    client.get("/admin")  # sets the CSRF token in the session
    with client.session_transaction() as session:
        csrf = session["csrf"]
    return client.post("/admin/login", data={"csrf": csrf, "name": name, "password": password})


@pytest.fixture
def alice(db_ctx):
    user = User(name="alice")
    user.set_password(PASSWORD)
    db_ctx.add(user)
    db_ctx.commit()
    return user


def test_admin_signs_in_and_out(deployed, alice):
    client, invocations = deployed
    assert sign_in(client, "alice", PASSWORD).status_code == 302
    page = client.get("/admin")
    assert b"alice" in page.data
    with client.session_transaction() as session:
        csrf = session["csrf"]
    assert client.post("/admin/logout", data={"csrf": csrf}).status_code == 302
    paths = [i["internal_request"]["path"] for i in invocations]
    assert paths == ["/api/auth/login", "/api/admin/me", "/api/auth/logout"]
    # The plain password reaches the API inside the invoke payload, and nowhere else.
    login = invocations[0]["internal_request"]
    assert json.loads(login["body"]) == {"name": "alice", "password": PASSWORD}


def test_volunteer_saves_and_loads_an_offer(deployed, db_ctx):
    client, _ = deployed
    event = Event(organization=Organization(name="acme", pretty_name="Acme"),
                  name="spring-fair", pretty_name="Spring Fair")
    window = VolunteerWindow(event=event, start=datetime(2026, 11, 1, 9, tzinfo=timezone.utc),
                             end=datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
    db_ctx.add_all([event, window])
    db_ctx.commit()

    assert client.get("/acme/spring-fair/volunteer").status_code == 200
    saved = client.put("/acme/spring-fair/volunteer/offer",
                       json={"email": "V@Example.com", "name": "Vol", "window_ids": [str(window.id)]})
    assert saved.status_code == 201
    loaded = client.get("/acme/spring-fair/volunteer/offer?email=v%40example.com")
    assert loaded.status_code == 200
    assert loaded.json["window_ids"] == [str(window.id)]
    assert client.get("/acme/nope/volunteer").status_code == 404


def test_ip_limit_is_per_browser_not_per_web_function(deployed, web_app, api_app, alice):
    # Every invoke comes from the web function; only the forwarded IP tells
    # browsers apart, so one browser hitting the limit mustn't lock out others.
    api_app.config.update(LOGIN_RATE_LIMIT="100 per minute", LOGIN_IP_RATE_LIMIT="2 per minute")
    noisy, quiet = browser(web_app, "203.0.113.7"), browser(web_app, "198.51.100.1")
    assert [sign_in(noisy, "alice", "wrong password!").status_code for _ in range(2)] == [401, 401]
    blocked = sign_in(noisy, "alice", PASSWORD)
    assert blocked.status_code == 429
    assert b"Too many sign-in attempts" in blocked.data
    assert sign_in(quiet, "alice", PASSWORD).status_code == 302


def test_name_lock_shows_the_too_many_message(deployed, web_app, api_app, alice):
    api_app.config.update(LOGIN_RATE_LIMIT="2 per minute", LOGIN_IP_RATE_LIMIT="100 per minute")
    for ip in ("203.0.113.1", "203.0.113.2"):
        assert sign_in(browser(web_app, ip), "alice", "wrong password!").status_code == 401
    page = sign_in(browser(web_app, "203.0.113.3"), "alice", PASSWORD)
    assert page.status_code == 429
    assert b"Too many sign-in attempts" in page.data


def test_api_crash_shows_unavailable(deployed, monkeypatch, alice):
    # An unhandled error in the API function reaches the web as a FunctionError.
    client, _ = deployed

    class Crashing:
        def invoke(self, FunctionName, Payload):
            return {"FunctionError": "Unhandled",
                    "Payload": io.BytesIO(b'{"errorMessage": "connection refused"}')}

    monkeypatch.setattr(api_client, "_lambda_client", lambda: Crashing())
    page = sign_in(client, "alice", PASSWORD)
    assert page.status_code == 502
    assert b"Sign-in is unavailable right now" in page.data
