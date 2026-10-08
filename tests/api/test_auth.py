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
