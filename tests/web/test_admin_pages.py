"""How the admin pages handle what the API answers, with the API stubbed:
validation errors, a token that stops working mid-session, an API that's down,
and the background (JSON) form posts made by static/admin_forms.js.

Access control is in test_permissions.py; here the user is always allowed.
"""
import pytest

CSRF = "test-csrf"
JSON = {"Accept": "application/json"}
ORG, EVENT = "acme", "spring-fair"
CONFIGURE = f"/admin/{ORG}/{EVENT}/configure"
MANAGE = f"/admin/{ORG}/{EVENT}/manage"
API_CONFIG = f"/admin/event-config/{ORG}/{EVENT}"
API_MANAGE = f"/admin/event-manage/{ORG}/{EVENT}"

ME = {
    "user": {"id": "u1", "name": "alice", "is_superuser": True},
    "organizations": [{"id": "o1", "name": ORG, "pretty_name": "Acme", "is_admin": True,
                       "events": [{"name": EVENT, "pretty_name": "Spring Fair"}]}],
}
USERS = [{"id": "u1", "name": "alice", "is_superuser": True},
         {"id": "u2", "name": "bob", "is_superuser": False}]
ORGS = [{"id": "o1", "name": ORG, "pretty_name": "Acme", "members": [{"user_id": "u1", "is_admin": True}]}]


@pytest.fixture
def signed_in(web, fake_api):
    with web.session_transaction() as session:
        session["csrf"] = CSRF
        session["api_token"] = "tok"
        session["user"] = {"id": "u1", "name": "alice"}
    fake_api.on("GET", "/admin/me", 200, ME)
    fake_api.on("GET", "/admin/users", 200, USERS)
    fake_api.on("GET", "/admin/organizations", 200, ORGS)
    return web


def post(web, url, background=False, **form):
    return web.post(url, data={"csrf": CSRF, **form}, headers=JSON if background else None)


def flashes(web):
    with web.session_transaction() as session:
        return session.get("_flashes", [])


def api_calls(fake_api, method=None):
    return [(c["method"], c["path"], c["json"]) for c in fake_api.calls
            if c["path"] != "/admin/me" and (method is None or c["method"] == method)]


def assert_session_ended(web):
    with web.session_transaction() as session:
        assert "api_token" not in session and session.get("expired") is True


# ---------------------------------------------------------------- the API down or the token gone


def test_api_down_on_page_load_is_502(signed_in, fake_api):
    fake_api.on("GET", "/admin/me", 502, {"error": "the scheduling service is unavailable"})
    assert signed_in.get("/admin").status_code == 502


@pytest.mark.parametrize("url, api_path", [
    ("/admin/users", "/admin/users"),
    ("/admin/organizations", "/admin/organizations"),
    (MANAGE, f"{API_MANAGE}/windows"),
    (CONFIGURE, f"{API_CONFIG}/config"),
])
def test_api_failure_while_building_a_page(signed_in, fake_api, url, api_path):
    fake_api.on("GET", api_path, 502, {"error": "the scheduling service is unavailable"})
    assert signed_in.get(url).status_code == 502


@pytest.mark.parametrize("url, api_path", [
    ("/admin/users", "/admin/users"),
    (MANAGE, f"{API_MANAGE}/windows"),
    (CONFIGURE, f"{API_CONFIG}/config"),
])
def test_token_rejected_partway_through_a_page(signed_in, fake_api, url, api_path):
    # /admin/me accepted the token, then the next call didn't (it expired
    # in between, or the user was signed out elsewhere).
    fake_api.on("GET", api_path, 401, {"error": "authentication required"})
    resp = signed_in.get(url)
    assert resp.status_code == 302 and resp.location.endswith("/admin")
    assert_session_ended(signed_in)
    with signed_in.session_transaction() as session:
        assert session["next"] == url  # signing in again returns here


def test_configure_page_for_vanished_event_is_404(signed_in, fake_api):
    fake_api.on("GET", f"{API_CONFIG}/config", 404, {"error": "event not found"})
    assert signed_in.get(CONFIGURE).status_code == 404


def test_unknown_event_never_reaches_the_api(signed_in, fake_api):
    assert signed_in.get(f"/admin/{ORG}/no-such-event/manage").status_code == 404
    assert api_calls(fake_api) == []


# ---------------------------------------------------------------- users


def test_create_user_error_keeps_what_was_typed(signed_in, fake_api):
    fake_api.on("POST", "/admin/users", 409, {"error": "a user named 'bob' already exists"})
    resp = post(signed_in, "/admin/users", name=" bob ", password="generated-password-123")
    assert resp.status_code == 409
    page = resp.get_data(as_text=True)
    assert "A user named &#39;bob&#39; already exists" in page
    assert "generated-password-123" in page  # not lost on a failed create
    assert api_calls(fake_api, "POST") == [("POST", "/admin/users",
                                            {"name": "bob", "password": "generated-password-123"})]


def test_create_user_error_in_the_background(signed_in, fake_api):
    fake_api.on("POST", "/admin/users", 400, {"error": "password must be at least 12 characters"})
    resp = post(signed_in, "/admin/users", background=True, name="bob", password="x")
    assert (resp.status_code, resp.json) == (400, {"ok": False, "error": "Password must be at least 12 characters"})


def test_create_user_success_in_the_background_says_where_to_go(signed_in, fake_api):
    fake_api.on("POST", "/admin/users", 201, {"id": "u3", "name": "carol"})
    resp = post(signed_in, "/admin/users", background=True, name="carol", password="x" * 12)
    assert resp.status_code == 200
    assert resp.json == {"ok": True, "reload": "/admin/users?user=u3"}
    assert ("message", "Created user carol.") in flashes(signed_in)


@pytest.mark.parametrize("background", [False, True])
def test_update_user_error(signed_in, fake_api, background):
    fake_api.on("PATCH", "/admin/users/u2", 400, {"error": "password must be at least 12 characters"})
    resp = post(signed_in, "/admin/users/u2/password", background=background, password="short")
    assert resp.status_code == 400
    if background:
        assert resp.json == {"ok": False, "error": "Password must be at least 12 characters"}
    else:
        assert "Password must be at least 12 characters" in resp.get_data(as_text=True)


def test_vanished_user_is_404(signed_in, fake_api):
    fake_api.on("PATCH", "/admin/users/u9", 404, {"error": "user not found"})
    assert post(signed_in, "/admin/users/u9/password", password="x" * 12).status_code == 404


def test_delete_user_error_is_shown(signed_in, fake_api):
    fake_api.on("DELETE", "/admin/users/u1", 400, {"error": "you can't delete yourself"})
    resp = post(signed_in, "/admin/users/u1/delete", name="alice")
    assert resp.status_code == 400
    assert "You can&#39;t delete yourself" in resp.get_data(as_text=True)


def test_admin_api_token_rejected_on_a_post(signed_in, fake_api):
    fake_api.on("POST", "/admin/users", 401, {"error": "authentication required"})
    resp = post(signed_in, "/admin/users", name="bob", password="x" * 12)
    assert resp.status_code == 302
    assert_session_ended(signed_in)


def test_admin_api_down_on_a_post(signed_in, fake_api):
    fake_api.on("POST", "/admin/users", 502, {"error": "the scheduling service is unavailable"})
    assert post(signed_in, "/admin/users", name="bob", password="x" * 12).status_code == 502


# ---------------------------------------------------------------- organizations


@pytest.mark.parametrize("background", [False, True])
def test_create_organization_error(signed_in, fake_api, background):
    fake_api.on("POST", "/admin/organizations", 409, {"error": "an organization named 'acme' already exists"})
    resp = post(signed_in, "/admin/organizations", background=background, name="acme", pretty_name="Acme 2")
    assert resp.status_code == 409
    if background:
        assert resp.json == {"ok": False, "error": "An organization named 'acme' already exists"}
    else:
        page = resp.get_data(as_text=True)
        assert "An organization named &#39;acme&#39; already exists" in page and "Acme 2" in page


@pytest.mark.parametrize("background", [False, True])
def test_rename_organization_error(signed_in, fake_api, background):
    fake_api.on("PATCH", "/admin/organizations/o1", 400, {"error": "pretty name is required"})
    resp = post(signed_in, "/admin/organizations/o1/pretty-name", background=background, pretty_name=" ")
    assert resp.status_code == 400
    if background:
        assert resp.json == {"ok": False, "error": "Pretty name is required"}
    else:
        assert "Pretty name is required" in resp.get_data(as_text=True)


def test_removing_someone_already_removed(signed_in, fake_api):
    fake_api.on("DELETE", "/admin/organizations/o1/members/u2", 404,
                {"error": "that user isn't a member of this organization"})
    resp = post(signed_in, "/admin/organizations/o1/members/u2", action="remove", user_name="bob", q="bo")
    assert resp.status_code == 302
    assert resp.location == "/admin/organizations?manage=o1&q=bo#user-u2"
    assert ("message", "bob wasn't a member.") in flashes(signed_in)


def test_adding_to_a_vanished_organization_is_404(signed_in, fake_api):
    fake_api.on("PUT", "/admin/organizations/o9/members/u2", 404, {"error": "organization not found"})
    assert post(signed_in, "/admin/organizations/o9/members/u2", action="add").status_code == 404


def test_unknown_membership_action_is_400(signed_in, fake_api):
    assert post(signed_in, "/admin/organizations/o1/members/u2", action="promote").status_code == 400
    assert api_calls(fake_api, "PUT") == []


def test_organizations_page_with_none(signed_in, fake_api):
    fake_api.on("GET", "/admin/organizations", 200, [])
    assert signed_in.get("/admin/organizations").status_code == 200


# ---------------------------------------------------------------- manage page


def test_response_error_is_flashed(signed_in, fake_api):
    fake_api.on("PUT", f"{API_MANAGE}/signups/s1/response", 409,
                {"error": "this sign-up was withdrawn and can't be answered"})
    resp = post(signed_in, f"{MANAGE}/signups/s1/response", response="accepted", window_id="w1")
    assert resp.location == f"{MANAGE}#window-w1"
    assert ("error", "This sign-up was withdrawn and can't be answered") in flashes(signed_in)


def test_response_success_names_the_volunteer(signed_in, fake_api):
    fake_api.on("PUT", f"{API_MANAGE}/signups/s1/response", 200,
                {"volunteer": {"name": "", "email": "v@example.com"}})
    post(signed_in, f"{MANAGE}/signups/s1/response", response="pending")
    assert ("message", "v@example.com is back to pending.") in flashes(signed_in)
    assert api_calls(fake_api, "PUT")[0][2] == {"response": None}


def test_unknown_response_is_400(signed_in, fake_api):
    assert post(signed_in, f"{MANAGE}/signups/s1/response", response="maybe").status_code == 400


@pytest.mark.parametrize("status, expected", [(401, 302), (404, 404), (502, 502)])
def test_manage_api_failures_on_a_post(signed_in, fake_api, status, expected):
    fake_api.on("PUT", f"{API_MANAGE}/signups/s1/response", status, {"error": "x"})
    assert post(signed_in, f"{MANAGE}/signups/s1/response", response="accepted").status_code == expected


# ---------------------------------------------------------------- configure page


@pytest.mark.parametrize("form, sent", [
    ({"mode": "cascade"}, {"cascade": True}),
    ({"mode": "reassign", "reassign_to": "w2"}, {"reassign_to": "w2"}),
    ({"mode": "reassign", "reassign_to": ""}, {}),  # no target chosen: the API reports the conflict
    ({}, {}),
])
def test_delete_dialog_choice_is_sent(signed_in, fake_api, form, sent):
    fake_api.on("DELETE", f"{API_CONFIG}/windows/w1", 200, {"deleted": True, "moved": 0})
    post(signed_in, f"{CONFIGURE}/windows/w1/delete", **form)
    assert api_calls(fake_api, "DELETE") == [("DELETE", f"{API_CONFIG}/windows/w1", sent)]


def test_reassigned_count_is_reported(signed_in, fake_api):
    fake_api.on("DELETE", f"{API_CONFIG}/windows/w1", 200, {"deleted": True, "moved": 3})
    post(signed_in, f"{CONFIGURE}/windows/w1/delete", mode="reassign", reassign_to="w2")
    assert ("message", "Deleted the window and moved 3 offer(s).") in flashes(signed_in)


def test_delete_conflict_is_flashed(signed_in, fake_api):
    fake_api.on("DELETE", f"{API_CONFIG}/windows/w1", 409,
                {"error": "2 volunteer(s) have offered their time in this window", "linked_count": 2})
    resp = post(signed_in, f"{CONFIGURE}/windows/w1/delete")
    assert resp.location == f"{CONFIGURE}#windows"
    assert ("error", "2 volunteer(s) have offered their time in this window") in flashes(signed_in)


def test_window_needs_start_and_end(signed_in, fake_api):
    post(signed_in, f"{CONFIGURE}/windows", start_iso="2026-11-01T09:00:00Z")
    assert ("error", "Enter a start and an end.") in flashes(signed_in)
    assert api_calls(fake_api, "POST") == []


@pytest.mark.parametrize("status, expected", [(401, 302), (404, 404), (502, 502)])
def test_config_api_failures_on_a_post(signed_in, fake_api, status, expected):
    fake_api.on("POST", f"{API_CONFIG}/enrichment-types/t1/enrichments", status, {"error": "x"})
    assert post(signed_in, f"{CONFIGURE}/enrichment-types/t1/rows").status_code == expected


VALUES_FORM = {
    "t|r1|f1": "Gate",
    "c|r1|f2|start": "2026-11-01T09:00:00Z",
    "c|r1|f2|end": "2026-11-01T10:00:00Z",
    "t|r2|f1": "",
    # ignored: not value fields, or malformed
    "type_id": "t1", "t|r3": "x", "c|r3|f2|middle": "x", "x|r3|f1": "x",
}


def test_values_form_becomes_the_values_body(signed_in, fake_api):
    fake_api.on("PUT", f"{API_CONFIG}/enrichment-types/t1/values", 200, {"updated": 2})
    resp = post(signed_in, f"{CONFIGURE}/enrichment-types/t1/values", **VALUES_FORM)
    assert resp.location == f"{CONFIGURE}?open=t1#type-t1"
    assert api_calls(fake_api, "PUT")[0][2] == {"values": {
        "r1": {"f1": "Gate", "f2": {"start": "2026-11-01T09:00:00Z", "end": "2026-11-01T10:00:00Z"}},
        "r2": {"f1": ""},
    }}


def test_values_saved_in_the_background(signed_in, fake_api):
    fake_api.on("PUT", f"{API_CONFIG}/enrichment-types/t1/values", 200, {"updated": 2})
    resp = post(signed_in, f"{CONFIGURE}/enrichment-types/t1/values", background=True, **VALUES_FORM)
    assert resp.json == {"ok": True, "reload": f"{CONFIGURE}?open=t1#type-t1"}
    assert ("message", "Saved the values.") in flashes(signed_in)


def test_values_problems_in_the_background(signed_in, fake_api):
    problems = [{"row": "r1", "field": "f2", "message": "start must not be after end"}]
    fake_api.on("PUT", f"{API_CONFIG}/enrichment-types/t1/values", 400,
                {"error": "1 value needs fixing; nothing was saved", "problems": problems})
    resp = post(signed_in, f"{CONFIGURE}/enrichment-types/t1/values", background=True, **VALUES_FORM)
    assert resp.status_code == 400
    assert resp.json == {"ok": False, "error": "1 value needs fixing; nothing was saved", "problems": problems}
    assert flashes(signed_in) == []


def test_values_problems_without_the_script(signed_in, fake_api):
    fake_api.on("PUT", f"{API_CONFIG}/enrichment-types/t1/values", 400,
                {"error": "1 value needs fixing; nothing was saved", "problems": []})
    post(signed_in, f"{CONFIGURE}/enrichment-types/t1/values", **VALUES_FORM)
    assert ("error", "1 value needs fixing; nothing was saved") in flashes(signed_in)


# ---------------------------------------------------------------- creating an event


def create_event(web, background=False):
    return post(web, "/admin/events", background=background, organization=ORG, name="gala", pretty_name="Gala")


@pytest.mark.parametrize("background", [False, True])
def test_create_event_error(signed_in, fake_api, background):
    fake_api.on("POST", f"/admin/{ORG}/events", 409,
                {"error": "an event named 'gala' already exists in this organization"})
    resp = create_event(signed_in, background)
    assert resp.status_code == 409
    message = "An event named 'gala' already exists in this organization"
    if background:
        assert resp.json == {"ok": False, "error": message}
    else:
        assert message.replace("'", "&#39;") in resp.get_data(as_text=True)


@pytest.mark.parametrize("background", [False, True])
def test_create_event_with_the_api_down(signed_in, fake_api, background):
    fake_api.on("POST", f"/admin/{ORG}/events", 502, {"error": "the scheduling service is unavailable"})
    resp = create_event(signed_in, background)
    message = "The scheduling service is unavailable. Please try again."
    if background:
        assert (resp.status_code, resp.json) == (400, {"ok": False, "error": message})
    else:
        assert resp.status_code == 502 and message in resp.get_data(as_text=True)


@pytest.mark.parametrize("status", [403, 404])
def test_create_event_after_losing_admin_rights(signed_in, fake_api, status):
    fake_api.on("POST", f"/admin/{ORG}/events", status, {"error": "only organization admins can create events"})
    assert create_event(signed_in).status_code == 404


def test_create_event_with_an_expired_token(signed_in, fake_api):
    fake_api.on("POST", f"/admin/{ORG}/events", 401, {"error": "authentication required"})
    assert create_event(signed_in).status_code == 302
    assert_session_ended(signed_in)


def test_create_event_goes_to_its_configure_page(signed_in, fake_api):
    fake_api.on("POST", f"/admin/{ORG}/events", 201, {"name": "gala"})
    assert create_event(signed_in).location == f"/admin/{ORG}/gala/configure"


# ---------------------------------------------------------------- login form


@pytest.mark.parametrize("form", [{"name": "alice"}, {"password": "pw"}, {"name": "  ", "password": "pw"}])
def test_login_needs_name_and_password(web, fake_api, form):
    with web.session_transaction() as session:
        session["csrf"] = CSRF
    resp = web.post("/admin/login", data={"csrf": CSRF, **form})
    assert resp.status_code == 400
    assert "Enter your name and password." in resp.get_data(as_text=True)
    assert fake_api.calls == []
