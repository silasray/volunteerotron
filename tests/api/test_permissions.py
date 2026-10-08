"""Who may call which authenticated API route.

The rules (see the docstrings of api/admin.py, event_config.py, event_manage.py):
  - no valid token                       -> 401 everywhere
  - superuser routes (users, orgs)       -> superusers only; others 404
  - create event                         -> org admins; plain members 403, others 404
  - event configure routes               -> admins of the event's org; others 404
  - event manage routes                  -> members of the event's org; others 404
Superusers get no event access from being superusers: only membership counts.

Every route is called by every role. Allowed roles send a valid request and
must get the route's success status; denied roles must get the denial status
and leave the database unchanged.
"""
from dataclasses import dataclass, field

import pytest
from itsdangerous import URLSafeTimedSerializer

from tests.world import PASSWORD, ROLES, build_world, row_counts


@pytest.fixture
def world(db_ctx):
    """See tests/world.py."""
    return build_world(db_ctx)


def bearer(token):
    return {"Authorization": "Bearer " + token}


# ---------------------------------------------------------------- the matrix


@dataclass
class Route:
    method: str
    path: str  # formatted with the world's ids
    ok: int
    allowed: tuple
    body: dict = None
    # Status for roles that aren't allowed; overrides per role in `denied_as`.
    denied: int = 404
    denied_as: dict = field(default_factory=dict)

    def __str__(self):
        return f"{self.method} {self.path}"


SUPER = ("superuser",)
ORG_ADMIN = ("admin",)
ORG_MEMBERS = ("member", "admin")
C = "/api/admin/event-config/acme/spring-fair"
M = "/api/admin/event-manage/acme/spring-fair"
WINDOW = {"start": "2026-11-02T09:00:00Z", "end": "2026-11-02T12:00:00Z"}

ROUTES = [
    Route("GET", "/api/admin/me", 200, ROLES),
    Route("POST", "/api/auth/logout", 204, ROLES),

    Route("POST", "/api/admin/acme/events", 201, ORG_ADMIN,
          {"name": "winter-fair", "pretty_name": "Winter Fair"}, denied_as={"member": 403}),

    Route("GET", "/api/admin/users", 200, SUPER),
    Route("POST", "/api/admin/users", 201, SUPER, {"name": "newbie", "password": PASSWORD}),
    Route("PATCH", "/api/admin/users/{member_id}", 200, SUPER, {"password": PASSWORD + "!"}),
    Route("DELETE", "/api/admin/users/{outsider_id}", 204, SUPER),
    Route("GET", "/api/admin/organizations", 200, SUPER),
    Route("POST", "/api/admin/organizations", 201, SUPER,
          {"name": "initech", "pretty_name": "Initech"}),
    Route("PATCH", "/api/admin/organizations/{org}", 200, SUPER, {"pretty_name": "Acme Co"}),
    Route("PUT", "/api/admin/organizations/{org}/members/{outsider_id}", 201, SUPER,
          {"is_admin": False}),
    Route("DELETE", "/api/admin/organizations/{org}/members/{member_id}", 204, SUPER),

    Route("GET", C + "/config", 200, ORG_ADMIN),
    Route("POST", C + "/windows", 201, ORG_ADMIN, WINDOW),
    Route("DELETE", C + "/windows/{window}", 200, ORG_ADMIN, {"cascade": True}),
    Route("POST", C + "/enrichment-types", 201, ORG_ADMIN,
          {"name": "Diet", "volunteer_interaction": "multiselect"}),
    Route("PATCH", C + "/enrichment-types/{etype}", 200, ORG_ADMIN,
          {"volunteer_interaction": "multiselect"}),
    Route("DELETE", C + "/enrichment-types/{etype}", 200, ORG_ADMIN, {"cascade": True}),
    Route("POST", C + "/enrichment-types/{etype}/fields", 201, ORG_ADMIN,
          {"name": "Notes", "content_type": "text"}),
    Route("PATCH", C + "/fields/{field}", 200, ORG_ADMIN, {"hidden": True}),
    Route("DELETE", C + "/fields/{field}", 200, ORG_ADMIN, {"cascade": True}),
    Route("POST", C + "/enrichment-types/{etype}/enrichments", 201, ORG_ADMIN),
    Route("PUT", C + "/enrichment-types/{etype}/values", 200, ORG_ADMIN,
          {"values": {"{enrichment}": {"{field}": "Large"}}}),
    Route("DELETE", C + "/enrichments/{enrichment}", 200, ORG_ADMIN, {"cascade": True}),

    Route("GET", M + "/windows", 200, ORG_MEMBERS),
    Route("PUT", M + "/signups/{signup}/response", 200, ORG_MEMBERS, {"response": "accepted"}),
]


def _fill(value, w):
    """Put the world's ids into a route's path or body (keys included)."""
    if isinstance(value, str):
        return value.format(**w)
    if isinstance(value, dict):
        return {_fill(k, w): _fill(v, w) for k, v in value.items()}
    return value


def call(api, route, w, headers=None):
    return api.open(_fill(route.path, w), method=route.method,
                    json=_fill(route.body, w), headers=headers or {})


CASES = [pytest.param(r, role, id=f"{r}-{role}") for r in ROUTES for role in ROLES]


@pytest.mark.parametrize("route, role", CASES)
def test_role_access(api, world, db_ctx, route, role):
    if role in route.allowed:
        resp = call(api, route, world, bearer(world["tokens"][role]))
        assert resp.status_code == route.ok, resp.get_json()
    else:
        before = row_counts(db_ctx)
        resp = call(api, route, world, bearer(world["tokens"][role]))
        assert resp.status_code == route.denied_as.get(role, route.denied), resp.get_json()
        assert row_counts(db_ctx) == before


# ---------------------------------------------------------------- no valid token


def _foreign_token(world):
    """Well-formed, but signed with a different key."""
    return URLSafeTimedSerializer("not-the-api-key", salt="api-auth-token").dumps(
        {"uid": world["superuser_id"]})


BAD_CREDENTIALS = {
    "none": lambda w: {},
    "garbage": lambda w: bearer("not-a-token"),
    "wrong scheme": lambda w: {"Authorization": "Basic " + w["tokens"]["superuser"]},
    "foreign key": lambda w: bearer(_foreign_token(w)),
}


@pytest.mark.parametrize("credentials", BAD_CREDENTIALS)
@pytest.mark.parametrize("route", ROUTES, ids=str)
def test_requires_valid_token(api, world, db_ctx, route, credentials):
    before = row_counts(db_ctx)
    resp = call(api, route, world, BAD_CREDENTIALS[credentials](world))
    assert resp.status_code == 401
    assert row_counts(db_ctx) == before


def test_expired_token_rejected(api, api_app, world):
    api_app.config["AUTH_TOKEN_MAX_AGE"] = -1  # every token is already too old
    resp = api.get("/api/admin/users", headers=bearer(world["tokens"]["superuser"]))
    assert resp.status_code == 401


# ---------------------------------------------------------------- changes take effect at once
# Tokens carry only the user id; rights are looked up on every request.


def test_deleted_user_token_stops_working(api, world):
    super_headers = bearer(world["tokens"]["superuser"])
    assert api.delete(f"/api/admin/users/{world['admin_id']}", headers=super_headers).status_code == 204
    assert api.get("/api/admin/me", headers=bearer(world["tokens"]["admin"])).status_code == 401


def test_removed_member_loses_event_access(api, world):
    path = f"{M}/windows"
    member = bearer(world["tokens"]["member"])
    assert api.get(path, headers=member).status_code == 200
    api.delete(f"/api/admin/organizations/{world['org']}/members/{world['member_id']}",
               headers=bearer(world["tokens"]["superuser"]))
    assert api.get(path, headers=member).status_code == 404


def test_demoted_admin_loses_configure_access(api, world):
    path = f"{C}/config"
    admin = bearer(world["tokens"]["admin"])
    assert api.get(path, headers=admin).status_code == 200
    api.put(f"/api/admin/organizations/{world['org']}/members/{world['admin_id']}",
            json={"is_admin": False}, headers=bearer(world["tokens"]["superuser"]))
    assert api.get(path, headers=admin).status_code == 404
    assert api.get(f"{M}/windows", headers=admin).status_code == 200  # still a member


def test_promoted_member_gains_configure_access(api, world):
    path = f"{C}/config"
    member = bearer(world["tokens"]["member"])
    assert api.get(path, headers=member).status_code == 404
    api.put(f"/api/admin/organizations/{world['org']}/members/{world['member_id']}",
            json={"is_admin": True}, headers=bearer(world["tokens"]["superuser"]))
    assert api.get(path, headers=member).status_code == 200


def test_revoked_superuser_loses_user_admin(api, world, make_user, auth):
    second = make_user("second-super", is_superuser=True)
    assert api.get("/api/admin/users", headers=auth(second)).status_code == 200
    api.patch(f"/api/admin/users/{second.id}", json={"is_superuser": False},
              headers=bearer(world["tokens"]["superuser"]))
    assert api.get("/api/admin/users", headers=auth(second)).status_code == 404


# ---------------------------------------------------------------- superuser self-protection


def test_superuser_cannot_delete_self(api, world):
    resp = api.delete(f"/api/admin/users/{world['superuser_id']}",
                      headers=bearer(world["tokens"]["superuser"]))
    assert resp.status_code == 400


def test_superuser_cannot_change_own_superuser_status(api, world):
    resp = api.patch(f"/api/admin/users/{world['superuser_id']}", json={"is_superuser": False},
                     headers=bearer(world["tokens"]["superuser"]))
    assert resp.status_code == 400


# ---------------------------------------------------------------- ids from another organization
# An admin of globex, on globex's own event, naming acme's objects by id. The
# route's event check passes, so this tests that each object is matched to it.

G = "/api/admin/event-config/globex/gala"
CROSS_ORG = [
    Route("DELETE", G + "/windows/{window}", 404, ("other_admin",), {"cascade": True}),
    Route("DELETE", G + "/windows/{globex_window}", 404, ("other_admin",),
          {"reassign_to": "{window}"}),
    Route("PATCH", G + "/enrichment-types/{etype}", 404, ("other_admin",),
          {"volunteer_interaction": "multiselect"}),
    Route("DELETE", G + "/enrichment-types/{etype}", 404, ("other_admin",), {"cascade": True}),
    Route("POST", G + "/enrichment-types/{etype}/fields", 404, ("other_admin",),
          {"name": "Notes", "content_type": "text"}),
    Route("POST", G + "/enrichment-types/{etype}/enrichments", 404, ("other_admin",)),
    Route("PUT", G + "/enrichment-types/{etype}/values", 404, ("other_admin",),
          {"values": {"{enrichment}": {"{field}": "Large"}}}),
    Route("PATCH", G + "/fields/{field}", 404, ("other_admin",), {"hidden": True}),
    Route("DELETE", G + "/fields/{field}", 404, ("other_admin",), {"cascade": True}),
    Route("DELETE", G + "/enrichments/{enrichment}", 404, ("other_admin",), {"cascade": True}),
    Route("DELETE", G + "/enrichments/{globex_enrichment}", 404, ("other_admin",),
          {"reassign_to": "{enrichment}"}),
    Route("PUT", "/api/admin/event-manage/globex/gala/signups/{signup}/response", 404,
          ("other_admin",), {"response": "accepted"}),
]


@pytest.mark.parametrize("route", CROSS_ORG, ids=str)
def test_cannot_reach_another_orgs_objects(api, world, db_ctx, route):
    before = row_counts(db_ctx)
    resp = call(api, route, world, bearer(world["tokens"]["other_admin"]))
    assert resp.status_code == route.ok, resp.get_json()
    assert row_counts(db_ctx) == before


def test_cross_org_values_put_leaves_acme_untouched(api, world, db_ctx):
    """values are keyed by id inside the body; ids from another type are rejected."""
    route = Route("PUT", G + "/enrichment-types/{globex_etype}/values", 400, ("other_admin",),
                  {"values": {"{enrichment}": {"{field}": "Large"}}})
    before = row_counts(db_ctx)
    resp = call(api, route, world, bearer(world["tokens"]["other_admin"]))
    assert resp.status_code == 400
    assert row_counts(db_ctx) == before


# ---------------------------------------------------------------- public routes


@pytest.mark.parametrize("method, path, body, ok", [
    ("GET", "/api/health", None, 200),
    ("GET", "/api/acme/spring-fair/volunteer-form", None, 200),
    ("GET", "/api/acme/spring-fair/offers/vol@example.com", None, 200),
    ("PUT", "/api/acme/spring-fair/offers/new@example.com", {"window_ids": []}, 201),
])
def test_public_routes_need_no_token(api, world, method, path, body, ok):
    assert api.open(path, method=method, json=body).status_code == ok
