from .conftest import PASSWORD


def test_login_returns_user_and_token(api, make_user):
    make_user("alice")
    resp = api.post("/api/auth/login", json={"name": "alice", "password": PASSWORD})
    assert resp.status_code == 200
    assert resp.json["user"]["name"] == "alice"
    assert "password" not in resp.json["user"]
    assert resp.json["token"]


def test_wrong_password_and_unknown_name_look_the_same(api, make_user):
    make_user("alice")
    wrong = api.post("/api/auth/login", json={"name": "alice", "password": "x" * 12})
    unknown = api.post("/api/auth/login", json={"name": "bob", "password": "x" * 12})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json == unknown.json


def test_admin_me_requires_token(api, make_user, auth):
    assert api.get("/api/admin/me").status_code == 401
    user = make_user("alice")
    assert api.get("/api/admin/me", headers=auth(user)).status_code == 200


def _login(api, name):
    return api.post("/api/auth/login", json={"name": name, "password": PASSWORD}).json["token"]


def test_logout_revokes_every_token_of_the_user(api, make_user):
    make_user("alice")
    make_user("bob")
    first, second, bobs = _login(api, "alice"), _login(api, "alice"), _login(api, "bob")
    resp = api.post("/api/auth/logout", headers={"Authorization": "Bearer " + first})
    assert resp.status_code == 204
    for token in (first, second):
        assert api.get("/api/admin/me", headers={"Authorization": "Bearer " + token}).status_code == 401
    # Other users are unaffected, and signing in again works.
    assert api.get("/api/admin/me", headers={"Authorization": "Bearer " + bobs}).status_code == 200
    fresh = _login(api, "alice")
    assert api.get("/api/admin/me", headers={"Authorization": "Bearer " + fresh}).status_code == 200


def test_logout_requires_valid_token(api):
    assert api.post("/api/auth/logout").status_code == 401


def test_token_without_version_rejected(api, make_user, db_ctx):
    """Tokens issued before token_version existed carry no "ver"."""
    from itsdangerous import URLSafeTimedSerializer

    user = make_user("alice")
    old = URLSafeTimedSerializer("test-api-secret", salt="api-auth-token").dumps({"uid": str(user.id)})
    assert api.get("/api/admin/me", headers={"Authorization": "Bearer " + old}).status_code == 401
