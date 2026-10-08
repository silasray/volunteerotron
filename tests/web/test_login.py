USER = {"id": "00000000-0000-0000-0000-000000000001", "name": "alice", "is_superuser": False}


def test_admin_shows_login_when_signed_out(web, fake_api):
    resp = web.get("/admin")
    assert resp.status_code == 200
    assert fake_api.calls == []  # no token, so no call to the API


def test_login_stores_token_and_redirects(web, fake_api, csrf):
    fake_api.on("POST", "/auth/login", 200, {"user": USER, "token": "tok"})
    resp = web.post("/admin/login", data={"csrf": csrf, "name": "alice", "password": "pw"})
    assert resp.status_code == 302
    with web.session_transaction() as session:
        assert session["api_token"] == "tok"
    assert fake_api.calls[0]["json"] == {"name": "alice", "password": "pw"}


def test_login_rejected(web, fake_api, csrf):
    fake_api.on("POST", "/auth/login", 401, {"error": "invalid name or password"})
    resp = web.post("/admin/login", data={"csrf": csrf, "name": "alice", "password": "pw"})
    assert resp.status_code == 401
    assert b"Invalid name or password." in resp.data


def test_login_without_csrf_is_refused(web, fake_api):
    resp = web.post("/admin/login", data={"name": "alice", "password": "pw"})
    assert resp.status_code == 400
    assert fake_api.calls == []


def test_login_with_wrong_csrf_is_refused(web, fake_api, csrf):
    resp = web.post("/admin/login", data={"csrf": "wrong", "name": "alice", "password": "pw"})
    assert resp.status_code == 400
    assert fake_api.calls == []


def _signed_in(web):
    with web.session_transaction() as session:
        session["csrf"] = "test-csrf"
        session["api_token"] = "tok"
        session["user"] = {"id": USER["id"], "name": "alice"}


def test_logout_revokes_token_in_api(web, fake_api):
    _signed_in(web)
    fake_api.on("POST", "/auth/logout", 204)
    resp = web.post("/admin/logout", data={"csrf": "test-csrf"})
    assert resp.status_code == 302
    assert fake_api.calls == [{"method": "POST", "path": "/auth/logout", "token": "tok", "json": None}]
    with web.session_transaction() as session:
        assert "api_token" not in session


def test_logout_still_signs_out_when_api_unavailable(web, fake_api):
    _signed_in(web)
    fake_api.on("POST", "/auth/logout", 502, {"error": "the scheduling service is unavailable"})
    assert web.post("/admin/logout", data={"csrf": "test-csrf"}).status_code == 302
    with web.session_transaction() as session:
        assert "api_token" not in session
