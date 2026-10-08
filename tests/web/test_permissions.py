"""Permission behaviour that only the web tier can enforce, with the API stubbed.

The API checks every token (tests/api/test_permissions.py), but some things
never reach it or are decided before it is called:
  - CSRF tokens on every form post
  - what admin routes do for a signed-out browser, or one whose token expired
  - the web's own early access checks (from /admin/me), made before calling the API
  - where signing in redirects to
  - the client IP sent to the API for its per-IP login limit
  - escaping of ids put into API paths

Routes are taken from the app's URL map, so a new admin route is covered
automatically and must be given an access level in ACCESS.
"""
import pytest
import requests
from flask import url_for

from web import api_client
from web.api_client import segment

CSRF = "test-csrf"
ORG, EVENT = "acme", "spring-fair"

# Who may use each admin route, as the web tier checks it before calling the API.
PUBLIC = "public"        # works signed out
SIGNED_IN = "signed-in"  # any signed-in user
SUPER = "superuser"
MEMBER = "org member"    # member of the URL's organization
ADMIN = "org admin"      # admin of the URL's (or form's) organization

ACCESS = {
    "admin.index": PUBLIC,  # shows the login page when signed out
    "admin.login": PUBLIC,
    "admin.logout": PUBLIC,
    "admin.users": SUPER,
    "admin.create_user": SUPER,
    "admin.change_password": SUPER,
    "admin.set_superuser": SUPER,
    "admin.delete_user": SUPER,
    "admin.organizations": SUPER,
    "admin.create_organization": SUPER,
    "admin.rename_organization": SUPER,
    "admin.change_membership": SUPER,
    "admin.manage_event": MEMBER,
    "admin.set_signup_response": MEMBER,
    "admin.configure_event": ADMIN,
    "admin.add_window": ADMIN,
    "admin.delete_window": ADMIN,
    "admin.add_enrichment_type": ADMIN,
    "admin.set_interaction": ADMIN,
    "admin.delete_enrichment_type": ADMIN,
    "admin.add_field": ADMIN,
    "admin.set_field_hidden": ADMIN,
    "admin.delete_field": ADMIN,
    "admin.add_row": ADMIN,
    "admin.update_values": ADMIN,
    "admin.delete_enrichment": ADMIN,
    "admin.create_event": ADMIN,
}


def _admin_rules(web_app):
    return [r for r in web_app.url_map.iter_rules() if r.endpoint.startswith("admin.")]


def _cases(web_app_factory, methods, access=None):
    """(endpoint, method, url) for admin routes, as pytest params."""
    app = web_app_factory()
    cases = []
    with app.test_request_context():
        for rule in _admin_rules(app):
            if access and ACCESS.get(rule.endpoint) not in access:
                continue
            args = {name: ORG if name == "org_name" else EVENT if name == "event_name" else "some-id"
                    for name in rule.arguments}
            url = url_for(rule.endpoint, **args)
            for method in sorted(rule.methods & set(methods)):
                cases.append(pytest.param(rule.endpoint, method, url,
                                          id=f"{method} {rule.endpoint}"))
    return cases


def _make_web_app():
    # Built at collection time just to read its URL map.
    from web import create_app

    return create_app()


ALL = _cases(_make_web_app, {"GET", "POST"})
POSTS = _cases(_make_web_app, {"POST"})


def test_every_admin_route_has_an_access_level(web_app):
    endpoints = {r.endpoint for r in _admin_rules(web_app)}
    assert endpoints - ACCESS.keys() == set(), "add new routes to ACCESS"
    assert ACCESS.keys() - endpoints == set(), "remove deleted routes from ACCESS"


def test_no_admin_route_takes_other_methods(web_app):
    """CSRF is checked on POST; PUT/PATCH/DELETE routes would need their own check."""
    for rule in _admin_rules(web_app):
        assert rule.methods <= {"GET", "HEAD", "OPTIONS", "POST"}, rule


def _sign_in(web, token="tok", me=None, fake_api=None):
    with web.session_transaction() as session:
        session["csrf"] = CSRF
        session["api_token"] = token
        session["user"] = {"id": "u1", "name": "alice"}
    if fake_api is not None and me is not None:
        fake_api.on("GET", "/admin/me", 200, me)


def _me(is_superuser=False, orgs=()):
    return {
        "user": {"id": "u1", "name": "alice", "is_superuser": is_superuser},
        "organizations": [
            {"id": "o-" + name, "name": name, "pretty_name": name.title(), "is_admin": is_admin,
             "events": [{"name": EVENT, "pretty_name": "Spring Fair"}]}
            for name, is_admin in orgs
        ],
    }


# ---------------------------------------------------------------- CSRF


@pytest.mark.parametrize("endpoint, method, url", POSTS)
@pytest.mark.parametrize("sent", [None, "", "wrong"], ids=["missing", "empty", "wrong"])
def test_post_without_valid_csrf_is_refused(web, fake_api, endpoint, method, url, sent):
    """Checked before anything else, so the API is never called."""
    _sign_in(web)
    data = {} if sent is None else {"csrf": sent}
    assert web.post(url, data=data).status_code == 400
    assert fake_api.calls == []


@pytest.mark.parametrize("endpoint, method, url", POSTS)
def test_post_with_no_csrf_in_session_is_refused(web, fake_api, endpoint, method, url):
    with web.session_transaction() as session:
        session["api_token"] = "tok"
    assert web.post(url, data={"csrf": ""}).status_code == 400
    assert fake_api.calls == []


def test_volunteer_relay_only_accepts_json(web, fake_api):
    """The public save relay has no CSRF token; a cross-site page can only send
    JSON to it after a CORS preflight, which this site never allows."""
    body = '{"email": "vol@example.com", "window_ids": []}'
    resp = web.put(f"/{ORG}/{EVENT}/volunteer/offer", data=body, content_type="text/plain")
    assert resp.status_code == 400
    assert fake_api.calls == []


# ---------------------------------------------------------------- signed out


@pytest.mark.parametrize("endpoint, method, url", [
    c for c in ALL if c.values[0] != "admin.login"  # login's job is to call the API
])
def test_signed_out_never_reaches_api(web, fake_api, endpoint, method, url):
    with web.session_transaction() as session:
        session["csrf"] = CSRF  # so POSTs get past CSRF to the sign-in check
    resp = web.open(url, method=method, data={"csrf": CSRF} if method == "POST" else None)
    if endpoint == "admin.index":
        assert resp.status_code == 200 and b'name="password"' in resp.data
    elif endpoint == "admin.logout":
        assert resp.status_code == 302
    else:
        assert resp.status_code == 404  # doesn't reveal the route exists
    assert fake_api.calls == []


# ---------------------------------------------------------------- expired token


@pytest.mark.parametrize("endpoint, method, url", [
    c for c in ALL if ACCESS[c.values[0]] != PUBLIC
])
def test_expired_token_ends_session_without_further_calls(web, fake_api, endpoint, method, url):
    _sign_in(web, token="stale")
    fake_api.on("GET", "/admin/me", 401, {"error": "authentication required"})
    resp = web.open(url, method=method, data={"csrf": CSRF} if method == "POST" else None)
    if method == "GET":
        assert resp.status_code == 302 and resp.location.endswith("/admin")
    else:
        assert resp.status_code == 404
    assert [c["path"] for c in fake_api.calls] == ["/admin/me"]
    with web.session_transaction() as session:
        assert "api_token" not in session and session.get("expired")


# ---------------------------------------------------------------- web-side access checks
# Made from /admin/me before any other API call. The API would refuse anyway;
# these stop the web from calling it (or rendering anything) for the wrong user.


ROLE_ME = {
    "no orgs": _me(),
    "member": _me(orgs=[(ORG, False)]),
    "admin": _me(orgs=[(ORG, True)]),
    "other org admin": _me(orgs=[("globex", True)]),
    "superuser": _me(is_superuser=True),
}
ALLOWED = {
    SUPER: {"superuser"},
    MEMBER: {"member", "admin"},
    ADMIN: {"admin"},
}


def _denied_cases():
    out = []
    for c in ALL:
        endpoint, method, url = c.values
        level = ACCESS[endpoint]
        if level not in ALLOWED:
            continue
        for role in ROLE_ME:
            if role not in ALLOWED[level]:
                out.append(pytest.param(endpoint, method, url, role, id=f"{c.id}-{role}"))
    return out


@pytest.mark.parametrize("endpoint, method, url, role", _denied_cases())
def test_web_refuses_before_calling_api(web, fake_api, endpoint, method, url, role):
    _sign_in(web, me=ROLE_ME[role], fake_api=fake_api)
    data = {"csrf": CSRF, "organization": ORG} if method == "POST" else None
    assert web.open(url, method=method, data=data).status_code == 404
    assert [c["path"] for c in fake_api.calls] == ["/admin/me"]


@pytest.mark.parametrize("endpoint, method, url", [
    c for c in ALL if ACCESS[c.values[0]] not in (PUBLIC,)
])
def test_admin_calls_carry_the_session_token(web, fake_api, endpoint, method, url):
    """Whatever the outcome, every API call is made as the signed-in user."""
    _sign_in(web, token="the-users-token",
             me=_me(is_superuser=True, orgs=[(ORG, True)]), fake_api=fake_api)
    fake_api.responses = _Anything(fake_api.responses)
    web.open(url, method=method, data={"csrf": CSRF, "organization": ORG} if method == "POST" else None)
    assert fake_api.calls, "expected at least the /admin/me call"
    assert {c["token"] for c in fake_api.calls} == {"the-users-token"}


class _Anything(dict):
    """Responses for the token test: /admin/me as set, anything else a 404, so
    each route gets as far as its first real API call and stops."""

    def __missing__(self, key):
        return 404, {"error": "not found"}


# ---------------------------------------------------------------- redirect after sign-in


@pytest.mark.parametrize("next_url, expected", [
    ("/admin/users", "/admin/users"),
    ("/admin?org=acme", "/admin?org=acme"),
    ("/admin/acme/spring-fair/manage", "/admin/acme/spring-fair/manage"),
    ("//evil.example/admin", "/admin"),
    ("https://evil.example/admin", "/admin"),
    ("/admin\\@evil.example", "/admin"),
    ("/admin/\\evil.example", "/admin"),  # under /admin/, but browsers read "\" as "/"
    ("/adminevil", "/admin"),
    ("/acme/spring-fair/volunteer", "/admin"),
    (None, "/admin"),
])
def test_login_only_redirects_within_admin(web, fake_api, next_url, expected):
    with web.session_transaction() as session:
        session["csrf"] = CSRF
        if next_url is not None:
            session["next"] = next_url
    fake_api.on("POST", "/auth/login", 200, {
        "user": {"id": "u1", "name": "alice", "is_superuser": False}, "token": "tok"})
    resp = web.post("/admin/login", data={"csrf": CSRF, "name": "alice", "password": "pw"})
    assert resp.status_code == 302
    assert resp.location == expected


# ---------------------------------------------------------------- client IP for rate limits


@pytest.fixture
def sent_headers(monkeypatch):
    """Headers of each HTTP call to the API (transport stubbed)."""
    sent = []

    def http(method, url, headers, body):
        sent.append(headers)
        return 401, '{"error": "invalid name or password"}'

    monkeypatch.setattr(api_client, "_http", http)
    return sent


SPOOF = {"X-Client-IP": "198.51.100.66", "X-Forwarded-For": "198.51.100.66"}


def test_login_forwards_real_client_ip(web, sent_headers):
    with web.session_transaction() as session:
        session["csrf"] = CSRF
    web.post("/admin/login", data={"csrf": CSRF, "name": "alice", "password": "pw"},
             headers=SPOOF, environ_base={"REMOTE_ADDR": "203.0.113.7"})
    assert [h["X-Client-IP"] for h in sent_headers] == ["203.0.113.7"]


def test_volunteer_relay_forwards_real_client_ip(web, sent_headers):
    web.get(f"/{ORG}/{EVENT}/volunteer/offer?email=vol@example.com",
            headers=SPOOF, environ_base={"REMOTE_ADDR": "203.0.113.7"})
    assert [h["X-Client-IP"] for h in sent_headers] == ["203.0.113.7"]
    assert "Authorization" not in sent_headers[0]  # public relays never send a token


# ---------------------------------------------------------------- ids in API paths


@pytest.mark.parametrize("value", [
    pytest.param(".", marks=pytest.mark.xfail(strict=True, reason="segment() doesn't escape dot segments")),
    pytest.param("..", marks=pytest.mark.xfail(strict=True, reason="segment() doesn't escape dot segments")),
    "a/b", "a?b", "a#b", "%2e%2e", "a b",
])
def test_segment_stays_one_path_segment(value):
    """A value from the URL must land in the API path as exactly one segment,
    after requests normalizes the URL, so it can't redirect the call elsewhere."""
    url = "http://api.test/api/admin/users/" + segment(value) + "/after"
    path = requests.Request("POST", url).prepare().path_url.split("?")[0].split("#")[0]
    parts = path.split("/")
    assert parts[:4] == ["", "api", "admin", "users"]
    assert parts[5:] == ["after"], path
