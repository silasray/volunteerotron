"""Every web admin route, used by every role, through the real API.

The API matrix (tests/api/test_permissions.py) proves the API's rules; this
proves each web route reaches them as the signed-in user, so its outcome
matches: allowed roles succeed, everyone else gets a 404 and nothing in the
database changes. "signed out" is a browser with no sign-in at all.
"""
import pytest

from tests.world import PASSWORD, ROLES, row_counts

CSRF = "test-csrf"
A = "/admin/acme/spring-fair"
C = A + "/configure"

SUPER = ("superuser",)
ORG_ADMIN = ("admin",)
ORG_MEMBERS = ("member", "admin")
ALL_ROLES = ROLES + ("signed out",)

# (method, path, form, allowed roles); paths and form are formatted with the world's ids.
ROUTES = [
    ("GET", "/admin/users", None, SUPER),
    ("POST", "/admin/users", {"name": "newbie", "password": PASSWORD}, SUPER),
    ("POST", "/admin/users/{member_id}/password", {"password": PASSWORD + "!"}, SUPER),
    ("POST", "/admin/users/{member_id}/superuser", {"is_superuser": "on"}, SUPER),
    ("POST", "/admin/users/{outsider_id}/delete", {"name": "outsider"}, SUPER),
    ("GET", "/admin/organizations", None, SUPER),
    ("POST", "/admin/organizations", {"name": "initech", "pretty_name": "Initech"}, SUPER),
    ("POST", "/admin/organizations/{org}/pretty-name", {"pretty_name": "Acme Co"}, SUPER),
    ("POST", "/admin/organizations/{org}/members/{outsider_id}", {"action": "add"}, SUPER),

    ("POST", "/admin/events",
     {"organization": "acme", "name": "winter-fair", "pretty_name": "Winter Fair"}, ORG_ADMIN),

    ("GET", A + "/manage", None, ORG_MEMBERS),
    ("POST", A + "/manage/signups/{signup}/response", {"response": "accepted"}, ORG_MEMBERS),

    ("GET", C, None, ORG_ADMIN),
    ("POST", C + "/windows",
     {"start_iso": "2026-11-02T09:00:00Z", "end_iso": "2026-11-02T12:00:00Z"}, ORG_ADMIN),
    ("POST", C + "/windows/{window}/delete", {"mode": "cascade"}, ORG_ADMIN),
    ("POST", C + "/enrichment-types",
     {"name": "Diet", "volunteer_interaction": "multiselect"}, ORG_ADMIN),
    ("POST", C + "/enrichment-types/{etype}/interaction",
     {"volunteer_interaction": "multiselect"}, ORG_ADMIN),
    ("POST", C + "/enrichment-types/{etype}/delete", {"mode": "cascade"}, ORG_ADMIN),
    ("POST", C + "/enrichment-types/{etype}/fields",
     {"name": "Notes", "content_type": "text"}, ORG_ADMIN),
    ("POST", C + "/fields/{field}/hidden", {"hidden": "1"}, ORG_ADMIN),
    ("POST", C + "/fields/{field}/delete", {"mode": "cascade"}, ORG_ADMIN),
    ("POST", C + "/enrichment-types/{etype}/rows", {}, ORG_ADMIN),
    ("POST", C + "/enrichment-types/{etype}/values", {"t|{enrichment}|{field}": "Large"}, ORG_ADMIN),
    ("POST", C + "/enrichments/{enrichment}/delete", {"mode": "cascade"}, ORG_ADMIN),
]


def _fill(value, w):
    if value is None:
        return None
    if isinstance(value, str):
        return value.format(**w)
    return {_fill(k, w): _fill(v, w) for k, v in value.items()}


def _sign_in(client, world, role):
    with client.session_transaction() as session:
        session["csrf"] = CSRF
        if role != "signed out":
            session["api_token"] = world["tokens"][role]
            session["user"] = {"id": world[role + "_id"], "name": role}


def _errors(client):
    with client.session_transaction() as session:
        return [m for category, m in session.get("_flashes", []) if category == "error"]


CASES = [
    pytest.param(method, path, form, allowed, role, id=f"{method} {path}-{role}")
    for method, path, form, allowed in ROUTES
    for role in ALL_ROLES
]


@pytest.mark.parametrize("method, path, form, allowed, role", CASES)
def test_web_route_access(stack, world, db_ctx, method, path, form, allowed, role):
    _sign_in(stack, world, role)
    data = None if method == "GET" else {"csrf": CSRF, **_fill(form, world)}
    before = row_counts(db_ctx)
    resp = stack.open(_fill(path, world), method=method, data=data)
    if role in allowed:
        assert resp.status_code in (200, 302), resp.get_data(as_text=True)[:500]
        assert _errors(stack) == []
    else:
        assert resp.status_code == 404
        assert row_counts(db_ctx) == before


@pytest.mark.parametrize("role", ALL_ROLES)
def test_volunteer_data_hidden_from_non_members(stack, world, role):
    """The manage and configure pages show volunteers' details; only the
    roles allowed to see them get the page at all."""
    _sign_in(stack, world, role)
    for path, allowed in ((A + "/manage", ORG_MEMBERS), (C, ORG_ADMIN)):
        page = stack.get(path)
        assert (b"vol@example.com" in page.data) == (role in allowed and path.endswith("manage")), path
        assert (page.status_code == 200) == (role in allowed), path
