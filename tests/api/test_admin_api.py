"""What the admin endpoints do, beyond who may call them (test_permissions.py):
users and organizations (superusers), and creating events (organization admins).
"""
import pytest
from sqlalchemy import event as sa_event, insert, select

from api.models import Event, Organization, User, UserOrganization, db

from .conftest import PASSWORD

ADMIN = "/api/admin"


@pytest.fixture
def root(make_user, auth):
    return auth(make_user("root", is_superuser=True))


def names(db_ctx, model):
    db_ctx.expire_all()
    return sorted(db_ctx.scalars(select(model.name)))


@pytest.fixture
def lose_race(api_app):
    """lose_race(model, **values): just before the next request inserts a
    `model` row, another connection commits one with `values`, as a
    concurrent request would after this one checked for duplicates."""
    staged = []

    def stage(model, **values):
        def before_insert(mapper, connection, target):
            with db.engine.begin() as conn:
                conn.execute(insert(model).values(**values))

        sa_event.listen(model, "before_insert", before_insert, once=True)
        staged.append((model, before_insert))

    yield stage
    for model, listener in staged:
        if sa_event.contains(model, "before_insert", listener):
            sa_event.remove(model, "before_insert", listener)


@pytest.mark.parametrize("body", [None, [], "x", 5])
@pytest.mark.parametrize("method, path", [
    ("POST", "/users"), ("PATCH", "/users/{user}"), ("POST", "/organizations"),
    ("PATCH", "/organizations/{org}"), ("PUT", "/organizations/{org}/members/{user}"),
    ("POST", "/acme/events"),
])
def test_bodies_must_be_json_objects(api, root, make_user, make_org, add_member, auth, method, path, body):
    org, user = make_org(), make_user("alice")
    add_member(user, org, is_admin=True)
    headers = auth(user) if path.startswith("/acme") else root
    resp = api.open(ADMIN + path.format(user=user.id, org=org.id), method=method, json=body, headers=headers)
    assert resp.status_code == 400
    assert resp.json == {"error": "a JSON object body is required"}


# ---------------------------------------------------------------- users


def test_users_are_listed_by_name_without_hashes(api, root, make_user):
    make_user("zed")
    make_user("amy")
    users = api.get(f"{ADMIN}/users", headers=root).json
    assert [u["name"] for u in users] == ["amy", "root", "zed"]
    assert all("password" not in u for u in users)


def test_create_user(api, root, db_ctx):
    resp = api.post(f"{ADMIN}/users", json={"name": "  alice ", "password": PASSWORD}, headers=root)
    assert resp.status_code == 201
    assert resp.json["name"] == "alice" and resp.json["is_superuser"] is False
    assert api.post("/api/auth/login", json={"name": "alice", "password": PASSWORD}).status_code == 200


@pytest.mark.parametrize("body, message", [
    ({"password": PASSWORD}, "name is required"),
    ({"name": "   ", "password": PASSWORD}, "name is required"),
    ({"name": "x" * 256, "password": PASSWORD}, "name must be at most 255 characters"),
    ({"name": "alice"}, f"password must be at least {User.PASSWORD_MIN_LENGTH} characters"),
    ({"name": "alice", "password": "short"}, f"password must be at least {User.PASSWORD_MIN_LENGTH} characters"),
    ({"name": "alice", "password": "x" * (User.PASSWORD_MAX_LENGTH + 1)},
     f"password must be at most {User.PASSWORD_MAX_LENGTH} characters"),
])
def test_create_user_validation(api, root, db_ctx, body, message):
    resp = api.post(f"{ADMIN}/users", json=body, headers=root)
    assert (resp.status_code, resp.json) == (400, {"error": message})
    assert names(db_ctx, User) == ["root"]


def test_duplicate_user_is_409(api, root, make_user):
    make_user("alice")
    resp = api.post(f"{ADMIN}/users", json={"name": "alice", "password": PASSWORD}, headers=root)
    assert (resp.status_code, resp.json) == (409, {"error": "a user named 'alice' already exists"})


def test_user_created_concurrently_is_409(api, root, db_ctx, lose_race):
    lose_race(User, name="alice", password="scrypt:taken")
    resp = api.post(f"{ADMIN}/users", json={"name": "alice", "password": PASSWORD}, headers=root)
    assert (resp.status_code, resp.json) == (409, {"error": "a user named 'alice' already exists"})
    assert names(db_ctx, User) == ["alice", "root"]


def test_update_password_and_superuser(api, root, make_user):
    alice = make_user("alice")
    resp = api.patch(f"{ADMIN}/users/{alice.id}", json={"password": PASSWORD + "!", "is_superuser": True},
                     headers=root)
    assert resp.status_code == 200 and resp.json["is_superuser"] is True
    assert api.post("/api/auth/login", json={"name": "alice", "password": PASSWORD}).status_code == 401
    assert api.post("/api/auth/login", json={"name": "alice", "password": PASSWORD + "!"}).status_code == 200


def test_superuser_can_change_own_password(api, make_user, auth):
    me = make_user("root", is_superuser=True)
    resp = api.patch(f"{ADMIN}/users/{me.id}", json={"password": PASSWORD + "!"}, headers=auth(me))
    assert resp.status_code == 200


@pytest.mark.parametrize("body, message", [
    ({}, "nothing to update: send password and/or is_superuser"),
    ({"name": "bob"}, "nothing to update: send password and/or is_superuser"),
    ({"is_superuser": "yes"}, "is_superuser must be true or false"),
    ({"is_superuser": 1}, "is_superuser must be true or false"),
    ({"password": "short"}, f"password must be at least {User.PASSWORD_MIN_LENGTH} characters"),
])
def test_update_user_validation(api, root, make_user, db_ctx, body, message):
    alice = make_user("alice")
    resp = api.patch(f"{ADMIN}/users/{alice.id}", json=body, headers=root)
    assert (resp.status_code, resp.json) == (400, {"error": message})
    db_ctx.expire_all()
    assert db_ctx.get(User, alice.id).is_superuser is False


def test_bad_superuser_flag_leaves_password_alone(api, root, make_user):
    alice = make_user("alice")
    api.patch(f"{ADMIN}/users/{alice.id}", json={"password": PASSWORD + "!", "is_superuser": "yes"}, headers=root)
    assert api.post("/api/auth/login", json={"name": "alice", "password": PASSWORD}).status_code == 200


def test_delete_user_removes_memberships(api, root, make_user, make_org, add_member, db_ctx):
    alice = make_user("alice")
    add_member(alice, make_org())
    assert api.delete(f"{ADMIN}/users/{alice.id}", headers=root).status_code == 204
    assert names(db_ctx, User) == ["root"]
    assert db_ctx.scalars(select(UserOrganization)).all() == []


@pytest.mark.parametrize("method, body", [("PATCH", {"password": PASSWORD}), ("DELETE", None)])
def test_unknown_user_is_404(api, root, method, body):
    resp = api.open(f"{ADMIN}/users/00000000-0000-0000-0000-000000000000", method=method, json=body,
                    headers=root)
    assert (resp.status_code, resp.json) == (404, {"error": "user not found"})


# ---------------------------------------------------------------- organizations


def test_create_organization(api, root):
    resp = api.post(f"{ADMIN}/organizations", json={"name": "acme", "pretty_name": " Acme Co "}, headers=root)
    assert resp.status_code == 201
    assert (resp.json["name"], resp.json["pretty_name"], resp.json["members"]) == ("acme", "Acme Co", [])


@pytest.mark.parametrize("body, message", [
    ({"pretty_name": "Acme"}, "name is required"),
    ({"name": "acme"}, "pretty name is required"),
    ({"name": "acme", "pretty_name": "x" * 256}, "pretty name must be at most 255 characters"),
    ({"name": "has space", "pretty_name": "Acme"}, None),
    ({"name": "..", "pretty_name": "Acme"}, "name can't be '.' or '..'"),
])
def test_create_organization_validation(api, root, db_ctx, body, message):
    resp = api.post(f"{ADMIN}/organizations", json=body, headers=root)
    assert resp.status_code == 400
    if message:
        assert resp.json == {"error": message}
    assert names(db_ctx, Organization) == []


def test_duplicate_organization_is_409(api, root, make_org):
    make_org("acme")
    resp = api.post(f"{ADMIN}/organizations", json={"name": "acme", "pretty_name": "Other"}, headers=root)
    assert (resp.status_code, resp.json) == (409, {"error": "an organization named 'acme' already exists"})


def test_organization_created_concurrently_is_409(api, root, db_ctx, lose_race):
    lose_race(Organization, name="acme", pretty_name="First")
    resp = api.post(f"{ADMIN}/organizations", json={"name": "acme", "pretty_name": "Second"}, headers=root)
    assert resp.status_code == 409
    db_ctx.expire_all()
    assert db_ctx.scalars(select(Organization.pretty_name)).all() == ["First"]


def test_rename_organization(api, root, make_org):
    org = make_org()
    resp = api.patch(f"{ADMIN}/organizations/{org.id}", json={"pretty_name": "Acme Co"}, headers=root)
    assert resp.status_code == 200 and resp.json["pretty_name"] == "Acme Co"
    assert api.patch(f"{ADMIN}/organizations/{org.id}", json={"pretty_name": ""}, headers=root).status_code == 400


def test_organizations_list_members_by_name(api, root, make_org, make_user, add_member):
    org = make_org()
    zed, amy = make_user("zed"), make_user("amy")
    add_member(zed, org, is_admin=True)
    add_member(amy, org)
    [listed] = api.get(f"{ADMIN}/organizations", headers=root).json
    assert listed["members"] == [{"user_id": str(amy.id), "is_admin": False},
                                 {"user_id": str(zed.id), "is_admin": True}]


# ---------------------------------------------------------------- memberships


def members(api, root):
    [org] = api.get(f"{ADMIN}/organizations", headers=root).json
    return {m["user_id"]: m["is_admin"] for m in org["members"]}


def test_add_then_promote_then_remove(api, root, make_org, make_user):
    org, alice = make_org(), make_user("alice")
    url = f"{ADMIN}/organizations/{org.id}/members/{alice.id}"
    assert api.put(url, json={}, headers=root).status_code == 201  # is_admin defaults to false
    assert members(api, root) == {str(alice.id): False}
    assert api.put(url, json={"is_admin": True}, headers=root).status_code == 200
    assert members(api, root) == {str(alice.id): True}
    assert api.delete(url, headers=root).status_code == 204
    assert members(api, root) == {}


def test_membership_added_concurrently_takes_the_admin_flag(api, root, make_org, make_user, lose_race):
    org, alice = make_org(), make_user("alice")
    lose_race(UserOrganization, organization_id=org.id, user_id=alice.id, is_admin=False)
    resp = api.put(f"{ADMIN}/organizations/{org.id}/members/{alice.id}", json={"is_admin": True}, headers=root)
    assert resp.status_code == 200  # not created by this request
    assert members(api, root) == {str(alice.id): True}


def test_admin_flag_must_be_boolean(api, root, make_org, make_user):
    org, alice = make_org(), make_user("alice")
    resp = api.put(f"{ADMIN}/organizations/{org.id}/members/{alice.id}", json={"is_admin": "true"},
                   headers=root)
    assert (resp.status_code, resp.json) == (400, {"error": "is_admin must be true or false"})
    assert members(api, root) == {}


def test_removing_a_non_member_is_404(api, root, make_org, make_user):
    org, alice = make_org(), make_user("alice")
    resp = api.delete(f"{ADMIN}/organizations/{org.id}/members/{alice.id}", headers=root)
    assert (resp.status_code, resp.json) == (404, {"error": "that user isn't a member of this organization"})


@pytest.mark.parametrize("missing", ["organization", "user"])
def test_membership_of_unknown_org_or_user_is_404(api, root, make_org, make_user, missing):
    org, alice = make_org(), make_user("alice")
    unknown = "00000000-0000-0000-0000-000000000000"
    org_id, user_id = (unknown, alice.id) if missing == "organization" else (org.id, unknown)
    resp = api.put(f"{ADMIN}/organizations/{org_id}/members/{user_id}", json={}, headers=root)
    assert (resp.status_code, resp.json) == (404, {"error": f"{missing} not found"})


# ---------------------------------------------------------------- creating events


@pytest.fixture
def org_admin(make_org, make_user, add_member, auth):
    user = make_user("alice")
    add_member(user, make_org("acme"), is_admin=True)
    return auth(user)


def test_create_event(api, org_admin, db_ctx):
    resp = api.post(f"{ADMIN}/acme/events", json={"name": "gala", "pretty_name": " The Gala "}, headers=org_admin)
    assert resp.status_code == 201
    assert (resp.json["name"], resp.json["pretty_name"]) == ("gala", "The Gala")
    assert names(db_ctx, Event) == ["gala"]


@pytest.mark.parametrize("body, message", [
    ({"pretty_name": "Gala"}, "name is required"),
    ({"name": "gala"}, "pretty name is required"),
    ({"name": "a/b", "pretty_name": "Gala"}, None),
])
def test_create_event_validation(api, org_admin, db_ctx, body, message):
    resp = api.post(f"{ADMIN}/acme/events", json=body, headers=org_admin)
    assert resp.status_code == 400
    if message:
        assert resp.json == {"error": message}
    assert names(db_ctx, Event) == []


def test_event_names_are_per_organization(api, org_admin, make_org, make_event, db_ctx):
    make_event(make_org("globex", "Globex"), name="gala")
    assert api.post(f"{ADMIN}/acme/events", json={"name": "gala", "pretty_name": "Gala"},
                    headers=org_admin).status_code == 201
    resp = api.post(f"{ADMIN}/acme/events", json={"name": "gala", "pretty_name": "Again"}, headers=org_admin)
    assert (resp.status_code, resp.json) == (409, {"error": "an event named 'gala' already exists in this organization"})


def test_event_created_concurrently_is_409(api, org_admin, db_ctx, lose_race):
    db_ctx.expire_all()
    org_id = db_ctx.scalar(select(Organization.id))
    lose_race(Event, organization_id=org_id, name="gala", pretty_name="First")
    resp = api.post(f"{ADMIN}/acme/events", json={"name": "gala", "pretty_name": "Second"}, headers=org_admin)
    assert resp.status_code == 409
    db_ctx.expire_all()
    assert db_ctx.scalars(select(Event.pretty_name)).all() == ["First"]
