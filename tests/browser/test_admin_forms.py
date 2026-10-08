"""The admin pages' shared scripts, end to end:

  admin_forms.js  forms marked data-background-form post without leaving the
                  page: success reloads to show the result, failure opens an
                  error dialog and keeps everything typed (toggles flip back)
  admin.js        the organization picker and the New Event dialog
  admin_users.js  password generator, user picker, superuser toggle, delete confirm
  admin_orgs.js   the user filter, carried through each membership action
"""
import re

import pytest
from selenium.webdriver.support.ui import Select
from sqlalchemy import select

from api.models import Event, User, UserOrganization


@pytest.fixture
def root(page, make_user):
    """Signed in as a superuser on the users page."""
    make_user("root", is_superuser=True)
    page.sign_in("root")
    return page


def notice(page):
    return page.find(".notice").text


def users(db_ctx):
    db_ctx.expire_all()
    return {u.name: u for u in db_ctx.scalars(select(User))}


def error_dialog(page):
    page.wait(lambda: page.dialog_open("#form-error-dialog"), "the error dialog")
    return page.find("#form-error-message").text


# ---------------------------------------------------------------- background forms (users page)


def test_background_create_reloads_with_the_result(root, db_ctx):
    root.open("/admin/users")
    root.fill("#create-name", "carol")
    root.fill("#create-password", "x" * 12)
    root.find("#create-name").submit()
    root.wait_for_text(".notice", "Created user carol.")
    assert root.path == f"/admin/users?user={users(db_ctx)['carol'].id}"


def test_background_failure_keeps_what_was_typed(root, make_user):
    make_user("carol")
    root.open("/admin/users")
    root.fill("#create-name", "carol")
    root.fill("#create-password", "typed-password-123")
    root.find("#create-name").submit()
    assert error_dialog(root) == "A user named 'carol' already exists"
    assert root.path == "/admin/users"  # never left the page
    assert root.find("#create-password").get_attribute("value") == "typed-password-123"
    root.click("#form-error-ok")
    assert not root.dialog_open("#form-error-dialog")
    assert root.js("return document.activeElement.id") == "create-name"  # back to the first field


def test_toggle_reverts_when_the_change_fails(root, make_user, db_ctx):
    bob = make_user("bob")
    root.open(f"/admin/users?user={bob.id}")
    # Deleted elsewhere after this page loaded: the API says 404.
    db_ctx.delete(db_ctx.get(User, bob.id))
    db_ctx.commit()
    root.click('input[name="is_superuser"]')
    assert error_dialog(root).startswith("That's no longer available.")
    assert not root.find('input[name="is_superuser"]').is_selected()


def test_toggle_saves_on_change(root, make_user, db_ctx):
    bob = make_user("bob")
    root.open(f"/admin/users?user={bob.id}")
    root.click('input[name="is_superuser"]')
    root.wait_for_text(".notice", "bob")
    assert users(db_ctx)["bob"].is_superuser is True
    assert root.find('input[name="is_superuser"]').is_selected()


def test_server_unreachable_in_the_background(root):
    root.open("/admin/users")
    root.js("window.fetch = () => Promise.reject(new TypeError('Failed to fetch'))")
    root.fill("#create-name", "carol")
    root.fill("#create-password", "x" * 12)
    root.find("#create-name").submit()
    assert error_dialog(root).startswith("The change couldn't be saved: the server didn't respond as expected.")
    assert root.find("button.primary").is_enabled()  # buttons come back


def test_own_superuser_box_and_delete_are_disabled(root, db_ctx):
    root.open(f"/admin/users?user={users(db_ctx)['root'].id}")
    assert not root.find('input[name="is_superuser"]').is_enabled()
    assert not root.find(".delete-form button").is_enabled()


# ---------------------------------------------------------------- users page helpers


def test_generated_passwords(root):
    root.open("/admin/users")
    seen = set()
    for _ in range(3):
        root.click('.generate-password[data-target="create-password"]')
        value = root.find("#create-password").get_attribute("value")
        assert re.fullmatch(r"[A-Za-z0-9]{25}", value)
        seen.add(value)
    assert len(seen) == 3
    assert root.js("return document.activeElement.id") == "create-password"


def test_picking_a_user_shows_them(root, make_user, db_ctx):
    make_user("bob")
    root.open("/admin/users")  # the first user by name, bob, is shown
    Select(root.find("#manage-user")).select_by_visible_text("root (super user)")
    root.wait_for_path(f"/admin/users?user={users(db_ctx)['root'].id}")


@pytest.mark.parametrize("confirmed", [False, True])
def test_delete_asks_first(root, make_user, db_ctx, confirmed):
    bob = make_user("bob")
    root.open(f"/admin/users?user={bob.id}")
    root.click(".delete-form button")
    alert = root.driver.switch_to.alert
    assert alert.text == "Delete user bob? This can't be undone."
    alert.accept() if confirmed else alert.dismiss()
    if confirmed:
        root.wait_for_text(".notice", "Deleted user bob.")
    assert ("bob" in users(db_ctx)) is not confirmed


# ---------------------------------------------------------------- organizations page


@pytest.fixture
def orgs_page(root, make_org, make_user):
    org = make_org("acme", "Acme")
    for name in ("bob", "bobby", "carol"):
        make_user(name)
    root.open(f"/admin/organizations?manage={org.id}")
    return root, org


def shown_users(page):
    return [li.get_attribute("data-name") for li in page.find_all(".member-list > li") if li.is_displayed()]


def test_user_filter(orgs_page):
    page, _ = orgs_page
    page.fill("#user-filter", " BOB")
    assert shown_users(page) == ["bob", "bobby"]
    page.fill("#user-filter", "zz")
    assert shown_users(page) == [] and page.find("#no-user-match").is_displayed()


def test_membership_action_keeps_the_filter_and_place(orgs_page, db_ctx):
    page, org = orgs_page
    page.fill("#user-filter", "bob")
    bobby = users(db_ctx)["bobby"]
    page.find(f"#user-{bobby.id} button[value=add]").click()
    page.wait(lambda: page.find_all(f"#user-{bobby.id} .badge"), "bobby's badge")
    assert page.find(f"#user-{bobby.id} .badge").text == "member"
    assert page.path == f"/admin/organizations?manage={org.id}&q=bob#user-{bobby.id}"
    assert page.find("#user-filter").get_attribute("value") == "bob"
    assert shown_users(page) == ["bob", "bobby"]
    db_ctx.expire_all()
    assert db_ctx.scalar(select(UserOrganization.is_admin).filter_by(user_id=bobby.id)) is False

    page.find(f"#user-{bobby.id} button[value=make-admin]").click()
    page.wait(lambda: page.find(f"#user-{bobby.id} .badge").text == "admin", "admin badge")


def test_picking_an_organization_to_manage(orgs_page, make_org):
    page, _ = orgs_page
    other = make_org("globex", "Globex")
    page.open("/admin/organizations")
    Select(page.find("#manage-org")).select_by_visible_text("globex")  # listed by URL name
    page.wait(lambda: f"manage={other.id}" in page.path, "the Globex page")


# ---------------------------------------------------------------- New Event dialog and org picker


@pytest.fixture
def org_admin(page, make_org, make_user, add_member, make_event):
    acme = make_org("acme", "Acme")
    make_event(acme, name="spring-fair", pretty_name="Spring Fair")
    globex = make_org("globex", "Globex")
    alice = make_user("alice")
    add_member(alice, acme, is_admin=True)
    add_member(alice, globex)
    page.sign_in("alice")
    page.open("/admin?org=acme")
    return page


def test_new_event_dialog_creates_and_goes_to_configure(org_admin, db_ctx):
    page = org_admin
    page.click("#new-event-open")
    assert page.dialog_open("#new-event-dialog")
    assert page.js("return document.activeElement.id") == "new-event-name"
    page.fill("#new-event-name", "gala")
    page.fill("#new-event-pretty-name", "The Gala")
    page.find("#new-event-name").submit()
    page.wait_for_path("/admin/acme/gala/configure")
    db_ctx.expire_all()
    assert db_ctx.scalar(select(Event.pretty_name).filter_by(name="gala")) == "The Gala"


def test_new_event_error_keeps_the_dialog(org_admin):
    page = org_admin
    page.click("#new-event-open")
    page.fill("#new-event-name", "spring-fair")
    page.fill("#new-event-pretty-name", "Again")
    page.find("#new-event-name").submit()
    assert error_dialog(page) == "An event named 'spring-fair' already exists in this organization"
    page.click("#form-error-ok")
    assert page.find("#new-event-pretty-name").get_attribute("value") == "Again"


def test_new_event_error_without_the_background_script(org_admin):
    # As a plain post (no admin_forms.js), the page comes back with the dialog
    # reopened, showing the error and what was typed.
    page = org_admin
    page.click("#new-event-open")
    page.js("document.querySelector('#new-event-dialog form').removeAttribute('data-background-form')")
    page.fill("#new-event-name", "spring-fair")
    page.fill("#new-event-pretty-name", "Again")
    page.find("#new-event-name").submit()
    page.wait(lambda: page.dialog_open("#new-event-dialog"), "the reopened dialog")
    assert "already exists" in page.find("#new-event-dialog").text
    assert page.find("#new-event-pretty-name").get_attribute("value") == "Again"


def test_new_event_cancel(org_admin):
    page = org_admin
    page.click("#new-event-open")
    page.click("#new-event-cancel")
    assert not page.dialog_open("#new-event-dialog")


def test_organization_picker(org_admin):
    page = org_admin
    Select(page.find(".org-picker select")).select_by_visible_text("Globex")
    page.wait_for_path("/admin?org=globex")
    assert not page.find_all("#new-event-open")  # not an admin of Globex
