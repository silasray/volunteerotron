"""The AWS Lambda entry points in lambda_handlers.py.

api_handler takes {"internal_request": {...}} from the web function and
returns {"status", "body"}, or runs an admin command ({"command": ...}).
web_handler serves API Gateway (HTTP API, payload v2) events through apig-wsgi.
The apps are the test apps; nothing here touches AWS.
"""
import json

import pytest
from sqlalchemy import select

import lambda_handlers
from api.models import User

from .conftest import PASSWORD


@pytest.fixture
def handlers(monkeypatch, api_app):
    """lambda_handlers with its cached apps replaced by the test API app."""
    monkeypatch.setattr(lambda_handlers, "_apps", {"api": api_app})
    monkeypatch.setattr(lambda_handlers, "_web", None)
    return lambda_handlers


def internal(handlers, method, path, body=None, headers=None):
    req = {"method": method, "path": path, "headers": headers}
    if body is not None:
        req["body"] = json.dumps(body)
    return handlers.api_handler({"internal_request": req}, None)


def users(db_ctx):
    db_ctx.expire_all()
    return {u.name: u.is_superuser for u in db_ctx.scalars(select(User))}


# ---------------------------------------------------------------- internal requests


def test_internal_request_returns_status_and_body(handlers):
    result = internal(handlers, "GET", "/api/health")
    assert result["status"] == 200
    assert json.loads(result["body"]) == {"status": "ok"}


def test_internal_request_passes_body_and_headers(handlers, make_user):
    make_user("alice")
    result = internal(handlers, "POST", "/api/auth/login", {"name": "alice", "password": PASSWORD})
    assert result["status"] == 200
    token = json.loads(result["body"])["token"]
    me = internal(handlers, "GET", "/api/admin/me", headers={"Authorization": "Bearer " + token})
    assert me["status"] == 200
    assert json.loads(me["body"])["user"]["name"] == "alice"


def test_internal_request_relays_errors(handlers):
    assert internal(handlers, "GET", "/api/admin/me")["status"] == 401
    assert internal(handlers, "GET", "/api/nope/nothing/volunteer-form")["status"] == 404
    bad = internal(handlers, "POST", "/api/auth/login", {"name": "alice"})
    assert bad["status"] == 400
    assert json.loads(bad["body"]) == {"error": "name and password are required"}


def test_internal_request_without_headers_key(handlers):
    result = handlers.api_handler({"internal_request": {"method": "GET", "path": "/api/health"}}, None)
    assert result["status"] == 200


def test_unsupported_event_is_refused(handlers):
    with pytest.raises(ValueError, match="unsupported event"):
        handlers.api_handler({"rawPath": "/api/health"}, None)


# ---------------------------------------------------------------- commands


def test_create_user_command(handlers, db_ctx):
    result = handlers.api_handler({"command": "create-user", "name": "root", "password": PASSWORD,
                                   "superuser": True}, None)
    assert result == {"ok": True, "output": "created user 'root' (superuser)"}
    plain = handlers.api_handler({"command": "create-user", "name": "alice", "password": PASSWORD}, None)
    assert plain == {"ok": True, "output": "created user 'alice'"}
    assert users(db_ctx) == {"root": True, "alice": False}


@pytest.mark.parametrize("name, password, message", [
    ("alice", PASSWORD, "user 'alice' already exists"),
    ("bob", "short", "password"),
])
def test_create_user_command_failures(handlers, make_user, db_ctx, name, password, message):
    make_user("alice")
    result = handlers.api_handler({"command": "create-user", "name": name, "password": password}, None)
    assert result["ok"] is False
    assert message in result["output"]
    assert password not in result["output"]
    assert set(users(db_ctx)) == {"alice"}


def test_migrate_command_upgrades_in_the_app_context(handlers, monkeypatch, api_app):
    # The real migration run is covered against Postgres; here just the wiring.
    import flask_migrate
    from flask import current_app

    calls = []
    monkeypatch.setattr(flask_migrate, "upgrade", lambda: calls.append(current_app._get_current_object()))
    result = handlers.api_handler({"command": "migrate"}, None)
    assert result == {"ok": True, "output": "database upgraded to the latest migration"}
    assert calls == [api_app]


def test_bootstrap_command_runs_without_building_the_app(monkeypatch):
    # It runs before the app's database role exists, so the app mustn't connect.
    from api import db_bootstrap

    monkeypatch.setattr(lambda_handlers, "_apps", {})
    seen = []
    monkeypatch.setattr(db_bootstrap, "bootstrap", lambda pw: seen.append(pw) or "created role scheduler_app")
    result = lambda_handlers.api_handler({"command": "bootstrap-db", "master_password": "m4ster"}, None)
    assert result == {"ok": True, "output": "created role scheduler_app"}
    assert seen == ["m4ster"]
    assert lambda_handlers._apps == {}


def test_unknown_command_is_refused(handlers):
    with pytest.raises(ValueError, match="unknown command 'drop-everything'"):
        handlers.api_handler({"command": "drop-everything"}, None)


# ---------------------------------------------------------------- app caching


def test_each_app_is_built_once(monkeypatch):
    import api
    import web

    built = []
    monkeypatch.setattr(lambda_handlers, "_apps", {})
    monkeypatch.setattr(api, "create_app", lambda: built.append("api") or "API")
    monkeypatch.setattr(web, "create_app", lambda: built.append("web") or "WEB")
    assert [lambda_handlers._app("api") for _ in range(3)] == ["API"] * 3
    assert lambda_handlers._app("web") == "WEB"
    assert built == ["api", "web"]


# ---------------------------------------------------------------- web_handler


def http_event(path, method="GET", headers=None, body=None, query=""):
    """A minimal API Gateway HTTP API (payload v2) event."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": query,
        "headers": {"host": "volunteerotron.test", **(headers or {})},
        "requestContext": {
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1", "sourceIp": "203.0.113.7"},
            "stage": "$default",
        },
        "body": body,
        "isBase64Encoded": False,
    }


def test_web_handler_serves_pages_through_api_gateway(monkeypatch, web_app):
    monkeypatch.setattr(lambda_handlers, "_apps", {"web": web_app})
    monkeypatch.setattr(lambda_handlers, "_web", None)
    resp = lambda_handlers.web_handler(http_event("/admin"), None)
    assert resp["statusCode"] == 200
    assert "text/html" in resp["headers"]["content-type"]
    assert "<form" in resp["body"]
    # The wrapped handler is kept for warm invocations.
    first = lambda_handlers._web
    lambda_handlers.web_handler(http_event("/admin"), None)
    assert lambda_handlers._web is first


def test_web_handler_sees_the_browser_ip(monkeypatch, web_app, fake_api_call):
    # The IP the web forwards to the API (and the API rate-limits on) is API
    # Gateway's sourceIp, not anything the browser sends.
    monkeypatch.setattr(lambda_handlers, "_apps", {"web": web_app})
    monkeypatch.setattr(lambda_handlers, "_web", None)
    resp = lambda_handlers.web_handler(http_event(
        "/acme/spring-fair/volunteer/offer", query="email=v%40example.com",
        headers={"x-forwarded-for": "10.6.6.6", "x-client-ip": "10.6.6.6"}), None)
    assert resp["statusCode"] == 200
    assert fake_api_call.calls == [("GET", "/acme/spring-fair/offers/v@example.com", "203.0.113.7")]


@pytest.fixture
def fake_api_call(monkeypatch, web_app):
    """Replaces the web tier's transport; records each call and the client IP it forwards."""
    from web import api_client

    base = web_app.config["API_URL"]
    calls = []

    def http(method, url, headers, body):
        calls.append((method, url.removeprefix(base), headers.get(api_client.CLIENT_IP_HEADER)))
        return 200, "{}"

    monkeypatch.setattr(api_client, "_http", http)
    return type("Fake", (), {"calls": calls})
