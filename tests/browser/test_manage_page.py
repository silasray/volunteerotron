"""The manage page (web/static/admin_manage.js), end to end: per-window
filters, the Sort panel, approved email lists, and that the view survives the
reload after answering a sign-up (it's kept for the browser session).

Sign-ups come from the volunteer API; the derived columns are the real ones
("Class overlaps window", "Adjacent already accepted").
"""
from datetime import datetime, timezone

import pytest
from selenium.webdriver.support.ui import Select

from api.models import VolunteerWindow

from tests.api.conftest import at

MANAGE = "/admin/acme/spring-fair/manage"


def utc(day, hour):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


@pytest.fixture
def member(page, event, make_user, add_member):
    add_member(make_user("staff"), event.organization)
    page.sign_in("staff")
    return page


@pytest.fixture
def setup(event, db_ctx, api, make_type, make_option, make_user, add_member, auth):
    """Two windows (New York times 9-12 and 1-4pm on Nov 1) and four volunteers
    in the first, in sign-up order: amy (class overlaps), bob, cat (class
    overlaps), dan. respond(email, window, response) answers one."""
    morning = VolunteerWindow(event=event, start=utc(1, 14), end=utc(1, 17))
    afternoon = VolunteerWindow(event=event, start=utc(1, 18), end=utc(1, 21))
    db_ctx.add_all([morning, afternoon])
    db_ctx.commit()
    classes = make_type(event, name="Class", fields=(("Time", "calendar", False),))
    overlapping = make_option(classes, {"Time": (at(1, 15), at(1, 16))})
    for email, choices in (("amy", [overlapping]), ("bob", []), ("cat", [overlapping]), ("dan", [])):
        resp = api.put(f"/api/acme/spring-fair/offers/{email}@example.com", json={
            "name": email.title(), "window_ids": [str(morning.id), str(afternoon.id)] if email == "amy"
            else [str(morning.id)], "enrichment_ids": [str(o.id) for o in choices]})
        assert resp.status_code == 201
    manager = make_user("manager")
    add_member(manager, event.organization)
    headers = auth(manager)

    def respond(email, window, response):
        windows = api.get("/api/admin/event-manage/acme/spring-fair/windows", headers=headers).json["windows"]
        [signup] = [s for w in windows if w["id"] == str(window.id) for s in w["signups"]
                    if s["volunteer"]["email"] == f"{email}@example.com"]
        assert api.put(f"/api/admin/event-manage/acme/spring-fair/signups/{signup['id']}/response",
                       json={"response": response}, headers=headers).status_code == 200

    return {"morning": morning, "afternoon": afternoon, "respond": respond}


def window_section(page, window):
    return page.find(f'section[data-window-id="{window.id}"]')


def names(page, window):
    """Visible volunteers in the window, top to bottom."""
    return [li.find_element("css selector", "strong").text
            for li in window_section(page, window).find_elements("css selector", ".signup-list > li")
            if li.is_displayed()]


def control(page, window, label):
    section = window_section(page, window)
    [lab] = [l for l in section.find_elements("css selector", ".window-controls label") if l.text == label]
    return section.find_element("id", lab.get_attribute("for"))


def sort(page, label, placement):
    [fieldset] = [f for f in page.find_all(".sort-choice")
                  if f.find_element("css selector", "legend").text == label]
    [choice] = [l for l in fieldset.find_elements("css selector", "label") if l.text == placement]
    choice.click()


def shown_count(page, window):
    return window_section(page, window).find_element("css selector", ".shown-count").text


# ---------------------------------------------------------------- headings


def test_window_headings_in_local_time(member, setup):
    member.open(MANAGE)
    assert member.texts(".window-heading") == [
        "Sunday, November 1, 2026 · 9:00 AM – 12:00 PM",
        "Sunday, November 1, 2026 · 1:00 PM – 4:00 PM",
    ]


# ---------------------------------------------------------------- filters


def test_status_filter(member, setup):
    m = setup["morning"]
    setup["respond"]("bob", m, "accepted")
    setup["respond"]("dan", m, "denied")
    member.open(MANAGE)
    assert names(member, m) == ["Amy", "Bob", "Cat", "Dan"]
    assert shown_count(member, m) == "Showing 4 of 4"
    status = Select(control(member, m, "Status"))
    for option, expected in (("Pending", ["Amy", "Cat"]), ("Accepted", ["Bob"]), ("Denied", ["Dan"])):
        status.select_by_visible_text(option)
        assert names(member, m) == expected
    assert shown_count(member, m) == "Showing 1 of 4"


def test_boolean_column_filter_and_no_match(member, setup):
    m = setup["morning"]
    member.open(MANAGE)
    overlap = Select(control(member, m, "Class overlaps window"))
    overlap.select_by_visible_text("Yes")
    assert names(member, m) == ["Amy", "Cat"]
    overlap.select_by_visible_text("No")
    assert names(member, m) == ["Bob", "Dan"]
    Select(control(member, m, "Status")).select_by_visible_text("Accepted")
    assert names(member, m) == []
    assert window_section(member, m).find_element("css selector", ".no-match").is_displayed()


def test_filters_are_per_window(member, setup):
    member.open(MANAGE)
    Select(control(member, setup["morning"], "Class overlaps window")).select_by_visible_text("No")
    assert names(member, setup["afternoon"]) == ["Amy"]


# ---------------------------------------------------------------- sorting


def test_sort_criteria_come_from_columns_and_responses(member, setup):
    member.open(MANAGE)
    assert member.texts(".sort-choice legend") == [
        "Class overlaps window", "Adjacent already accepted", "Accepted", "Denied"]


def test_to_top_and_to_bottom(member, setup):
    m = setup["morning"]
    member.open(MANAGE)
    sort(member, "Class overlaps window", "To Bottom")
    assert names(member, m) == ["Bob", "Dan", "Amy", "Cat"]  # ties keep sign-up order
    sort(member, "Class overlaps window", "To Top")
    assert names(member, m) == ["Amy", "Cat", "Bob", "Dan"]
    sort(member, "Class overlaps window", "None")
    assert names(member, m) == ["Amy", "Bob", "Cat", "Dan"]


def test_criteria_add_up(member, setup):
    m = setup["morning"]
    setup["respond"]("cat", m, "accepted")
    setup["respond"]("dan", m, "accepted")
    member.open(MANAGE)
    sort(member, "Accepted", "To Top")             # cat +1, dan +1
    sort(member, "Class overlaps window", "To Top")  # amy +1, cat +1
    assert names(member, m) == ["Cat", "Amy", "Dan", "Bob"]


# ---------------------------------------------------------------- the view survives a reload


def test_answering_keeps_the_view(member, setup):
    m = setup["morning"]
    member.open(MANAGE)
    sort(member, "Class overlaps window", "To Bottom")
    Select(control(member, m, "Status")).select_by_visible_text("Pending")
    bob = [li for li in window_section(member, m).find_elements("css selector", ".signup-list > li")
           if "Bob" in li.text][0]
    bob.find_element("css selector", "button.respond-accepted").click()
    member.wait_for_text(".notice", "Accepted Bob.")
    assert member.path == f"{MANAGE}#window-{m.id}"
    # Same filter and sort after the reload: Bob is no longer pending.
    assert Select(control(member, m, "Status")).first_selected_option.text == "Pending"
    assert names(member, m) == ["Dan", "Amy", "Cat"]


def test_stale_remembered_value_falls_back(member, setup, event):
    m = setup["morning"]
    member.open(MANAGE)
    member.js("sessionStorage.setItem(arguments[0], JSON.stringify({status: 'maybe'}))",
              f"manage-filters:{event.id}:{m.id}")
    member.open(MANAGE)
    assert Select(control(member, m, "Status")).first_selected_option.text == "All"
    assert names(member, m) == ["Amy", "Bob", "Cat", "Dan"]


# ---------------------------------------------------------------- approved email lists


def test_email_lists(member, setup):
    m, a = setup["morning"], setup["afternoon"]
    setup["respond"]("amy", m, "accepted")
    setup["respond"]("cat", m, "accepted")
    setup["respond"]("amy", a, "accepted")
    member.open(MANAGE)
    output = "#email-list-output"
    assert member.find(output).text == "Select one or more windows."
    buttons = member.find_all(".email-window")
    assert [b.text for b in buttons] == [
        "Sunday, November 1, 2026 · 9:00 AM – 12:00 PM", "Sunday, November 1, 2026 · 1:00 PM – 4:00 PM"]
    buttons[1].click()
    assert member.find(output).text == "amy@example.com"
    buttons[0].click()  # amy is in both: listed once, in window order
    assert member.find(output).text == "amy@example.com, cat@example.com"
    assert [b.get_attribute("aria-pressed") for b in buttons] == ["true", "true"]
    member.open(MANAGE)  # remembered for the session
    assert member.find(output).text == "amy@example.com, cat@example.com"


def test_email_list_without_approved(member, setup):
    member.open(MANAGE)
    member.find_all(".email-window")[0].click()
    assert member.find("#email-list-output").text == "No approved volunteers in the selected windows."


def test_event_without_windows(member, event):
    member.open(MANAGE)
    assert "This event has no volunteer windows yet." in member.find(".manage-windows").text
    assert len(member.find_all(".sort-choice")) == 4  # the panel is still there
