from api.models import User, db

PASSWORD = "correct horse battery"


def _create_user(api_app, name):
    with api_app.app_context():
        user = User(name=name)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()


def _login(stack, name, password):
    stack.get("/admin")  # sets the CSRF token in the session
    with stack.session_transaction() as session:
        token = session["csrf"]
    return stack.post("/admin/login", data={"csrf": token, "name": name, "password": password})


def test_sign_in_then_see_admin_page(stack, api_app):
    _create_user(api_app, "alice")
    assert _login(stack, "alice", PASSWORD).status_code == 302
    page = stack.get("/admin")
    assert page.status_code == 200
    assert b"alice" in page.data


def test_wrong_password(stack, api_app):
    _create_user(api_app, "alice")
    assert _login(stack, "alice", "wrong password!").status_code == 401


def test_copied_session_cookie_dead_after_logout(stack, api_app, web_app):
    _create_user(api_app, "alice")
    _login(stack, "alice", PASSWORD)
    stack.get("/admin")  # issues the CSRF token for the logout form
    cookie = stack.get_cookie("session").value

    copy = web_app.test_client()  # a second browser holding a stolen copy
    copy.set_cookie("session", cookie)
    assert b"alice" in copy.get("/admin").data

    with stack.session_transaction() as session:
        csrf = session["csrf"]
    assert stack.post("/admin/logout", data={"csrf": csrf}).status_code == 302

    page = copy.get("/admin")
    assert page.status_code == 200
    assert b"alice" not in page.data  # shown the login page instead
