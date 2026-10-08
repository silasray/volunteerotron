"""The configure page (web/static/admin_configure.js), end to end.

Times are shown and typed in the browser's time zone (New York here) as
"YYYY-MM-DD HH:MM" and sent as UTC. Deleting something volunteers chose opens
a dialog offering to move their choices or delete them too. Values are saved
in the background, so problems are listed and highlighted without losing edits.
"""
import pytest
from selenium.webdriver.support.ui import Select
from sqlalchemy import select

from api.models import (
    CalendarEnrichment,
    Enrichment,
    EnrichmentFragmentType,
    EnrichmentType,
    TextEnrichment,
    VolunteerWindow,
    VolunteerWindowOffer,
)
from tests.api.conftest import at, field

CONFIGURE = "/admin/acme/spring-fair/configure"


@pytest.fixture
def admin(page, event, make_user, add_member):
    add_member(make_user("alice"), event.organization, is_admin=True)
    page.sign_in("alice")
    return page


def windows(db_ctx):
    db_ctx.expire_all()
    return [(w.start, w.end) for w in db_ctx.scalars(select(VolunteerWindow).order_by(VolunteerWindow.start))]


def sign_up(api, email, window_ids=(), option_ids=()):
    assert api.put(f"/api/acme/spring-fair/offers/{email}", json={
        "window_ids": [str(i) for i in window_ids], "enrichment_ids": [str(i) for i in option_ids]}
    ).status_code in (200, 201)


def message_dialog(page):
    page.wait(lambda: page.dialog_open("#values-error-dialog"), "the message dialog")
    return page.find("#values-error-summary").text, page.texts("#values-error-list li")


def error_dialog(page):
    page.wait(lambda: page.dialog_open("#form-error-dialog"), "the error dialog")
    return page.find("#form-error-message").text


# ---------------------------------------------------------------- windows


def test_windows_listed_in_local_time_by_day(admin, event, make_window, db_ctx):
    db_ctx.add_all([VolunteerWindow(event=event, start=at(2, 3), end=at(2, 5)),     # Nov 1, 10pm NY
                    VolunteerWindow(event=event, start=at(2, 14), end=at(2, 17))])
    db_ctx.commit()
    admin.open(CONFIGURE)
    assert admin.texts(".window-list > li.day-heading") == ["Sunday, November 1, 2026", "Monday, November 2, 2026"]
    assert admin.texts(".window-time") == ["10:00 PM – Nov 2, 12:00 AM", "9:00 AM – 12:00 PM"]


def test_add_window_converts_local_times_to_utc(admin, db_ctx):
    admin.open(CONFIGURE)
    admin.fill("#window-start", "2026-11-01 09:00")
    admin.fill("#window-end", "2026-11-01T12:30")
    admin.click("#windows .add-button")
    admin.wait_for_text(".notice", "Added the window.")
    assert windows(db_ctx) == [(at(1, 14), at(1, 17).replace(minute=30))]


@pytest.mark.parametrize("text", ["tomorrow 9am", "2026-02-31 09:00", "2026-11-01 25:00", "2026-11-01"])
def test_add_window_refuses_unreadable_times(admin, db_ctx, text):
    admin.open(CONFIGURE)
    admin.fill("#window-start", text)
    admin.fill("#window-end", "2026-11-01 12:00")
    admin.click("#windows .add-button")
    message = admin.js("return document.getElementById('window-start').validationMessage")
    assert message == "Use YYYY-MM-DD HH:MM, e.g. 2026-10-04 09:00"
    assert windows(db_ctx) == []


def test_date_picker(admin):
    admin.open(CONFIGURE)
    admin.fill("#window-start", "2026-11-20 08:15")
    admin.click('.pick-when[data-for="window-start"]')
    assert admin.dialog_open("#when-dialog")
    assert admin.find("#cal-month").text == "November 2026"
    assert admin.find('.cal-day[aria-pressed="true"]').text == "20"
    assert admin.find("#cal-time").get_attribute("value") == "08:15"
    assert admin.texts("#cal-weekdays th") == ["S", "M", "T", "W", "T", "F", "S"]
    admin.click("#cal-next")
    assert admin.find("#cal-month").text == "December 2026"
    admin.find('.cal-day[aria-label="Thursday, December 3, 2026"]').click()
    admin.js("document.getElementById('cal-time').value = '13:30'")
    admin.click("#when-ok")
    assert not admin.dialog_open("#when-dialog")
    assert admin.find("#window-start").get_attribute("value") == "2026-12-03 13:30"
    assert admin.find("#window-start-iso").get_attribute("value") == "2026-12-03T18:30:00.000Z"
    assert admin.js("return document.activeElement.id") == "window-start"


def test_date_picker_cancel(admin):
    admin.open(CONFIGURE)
    admin.fill("#window-start", "2026-11-20 08:15")
    admin.click('.pick-when[data-for="window-start"]')
    admin.click("#cal-prev")
    admin.find(".cal-day").click()
    admin.click("#when-cancel")
    assert admin.find("#window-start").get_attribute("value") == "2026-11-20 08:15"


# ---------------------------------------------------------------- the delete dialog


def test_window_without_offers_deletes_directly(admin, event, make_window, db_ctx):
    make_window(event)
    admin.open(CONFIGURE)
    admin.click(".window-row button.danger")
    admin.wait_for_text(".notice", "Deleted the window.")
    assert windows(db_ctx) == []


@pytest.fixture
def two_windows(event, make_window, api):
    first, second = make_window(event, day=1), make_window(event, day=2)
    sign_up(api, "a@example.com", [first.id])
    return first, second


def test_delete_dialog_offers_the_other_windows(admin, two_windows):
    admin.open(CONFIGURE)
    admin.find_all(".window-row button.danger")[0].click()
    assert admin.dialog_open("#delete-dialog")
    assert admin.find("#delete-dialog-title").text == "Delete this window?"
    assert admin.find("#delete-dialog-message").text == "1 volunteer has already offered their time in this window."
    assert admin.find("#delete-dialog-target-label").text == "Move their offers to"
    assert [o.text for o in Select(admin.find("#delete-dialog-target")).options] == [
        "Monday, November 2, 2026, 4:00 AM – 7:00 AM"]
    assert admin.find("#delete-dialog-cascade").text == "Delete window and their offers"
    admin.click("#delete-dialog-cancel")
    assert not admin.dialog_open("#delete-dialog")


def test_delete_dialog_move(admin, two_windows, db_ctx):
    first, second = two_windows
    admin.open(CONFIGURE)
    admin.find_all(".window-row button.danger")[0].click()
    admin.click("#delete-dialog-move")
    admin.wait_for_text(".notice", "Deleted the window and moved 1 offer(s).")
    db_ctx.expire_all()
    assert [wo.volunteer_window_id for wo in db_ctx.scalars(select(VolunteerWindowOffer))] == [second.id]


def test_delete_dialog_cascade(admin, two_windows, db_ctx):
    admin.open(CONFIGURE)
    admin.find_all(".window-row button.danger")[0].click()
    admin.click("#delete-dialog-cascade")
    admin.wait_for_text(".notice", "Deleted the window.")
    db_ctx.expire_all()
    assert db_ctx.scalars(select(VolunteerWindowOffer)).all() == []


def test_delete_dialog_with_nowhere_to_move(admin, event, make_window, api):
    only = make_window(event)
    sign_up(api, "a@example.com", [only.id])
    admin.open(CONFIGURE)
    admin.click(".window-row button.danger")
    assert [o.text for o in Select(admin.find("#delete-dialog-target")).options] == ["(nothing else to move them to)"]
    assert not admin.find("#delete-dialog-move").is_enabled()


# ---------------------------------------------------------------- enrichments


@pytest.fixture
def slots(event, make_type, make_option, api):
    """"Slots" with a Label (text) and When (calendar) field and two rows; a volunteer chose the first."""
    etype = make_type(event, name="Slots", fields=(("Label", "text", False), ("When", "calendar", False)))
    early = make_option(etype, {"Label": "Early", "When": (at(1, 13), at(1, 14))})
    late = make_option(etype, {"Label": "Late"})
    sign_up(api, "a@example.com", option_ids=[early.id])
    return etype, early, late


def block(page, etype):
    return page.find(f"#type-{etype.id}")


def row_number(row, *rows):
    """The row's number as the page shows it: rows are listed by id."""
    return sorted(str(r.id) for r in rows).index(str(row.id)) + 1


def delete_row_button(page, row):
    return page.find(f'button[form="delete-row-{row.id}"]')


def is_open(page, etype):
    return block(page, etype).find_element("css selector", ".etype-body").is_displayed()


def test_types_expand_and_stay_expanded(admin, slots):
    etype, _, _ = slots
    admin.open(CONFIGURE)
    assert not is_open(admin, etype)
    block(admin, etype).find_element("css selector", ".twisty").click()
    assert is_open(admin, etype)
    assert block(admin, etype).find_element("css selector", ".reload-type").is_displayed()
    admin.open(CONFIGURE)  # remembered for the browser session
    assert is_open(admin, etype)
    block(admin, etype).find_element("css selector", ".twisty").click()
    admin.open(CONFIGURE)
    assert not is_open(admin, etype)


def test_calendar_cells_show_local_times(admin, slots):
    etype, early, _ = slots
    admin.open(f"{CONFIGURE}?open={etype.id}")
    when = field(etype, "When")
    assert admin.find(f"#c-{early.id}-{when.id}-start-text").get_attribute("value") == "2026-11-01 08:00"
    assert admin.find(f"#c-{early.id}-{when.id}-end-text").get_attribute("value") == "2026-11-01 09:00"


def test_add_type_opens_it(admin, db_ctx):
    admin.open(CONFIGURE)
    admin.fill("#new-type-name", "Shirts")
    Select(admin.find("#new-type-interaction")).select_by_visible_text("select")
    admin.click(".add-type .add-button")
    admin.wait_for_text(".notice", "Added Shirts.")
    db_ctx.expire_all()
    etype = db_ctx.scalar(select(EnrichmentType))
    assert is_open(admin, etype)


def test_add_field_dialog(admin, slots, db_ctx):
    etype, _, _ = slots
    admin.open(f"{CONFIGURE}?open={etype.id}")
    block(admin, etype).find_element("css selector", ".open-add-field").click()
    assert admin.find("#add-field-title").text == "Add field to Slots"
    assert admin.js("return document.activeElement.id") == "add-field-name"
    admin.fill("#add-field-name", "Notes")
    admin.find("#add-field-name").submit()
    admin.wait_for_text(".notice", "Added the field Notes.")
    db_ctx.expire_all()
    assert "Notes" in db_ctx.scalars(select(EnrichmentFragmentType.name)).all()


def test_interaction_change_refused_shows_why(admin, slots, api):
    etype, early, late = slots
    sign_up(api, "b@example.com", option_ids=[early.id, late.id])
    admin.open(CONFIGURE)
    Select(admin.find(f'#type-{etype.id} select[name="volunteer_interaction"]')).select_by_visible_text("select")
    block(admin, etype).find_element("css selector", ".inline-form button").click()
    assert error_dialog(admin).startswith("1 volunteer(s) chose more than one option of this type")


def test_hide_a_field(admin, slots, db_ctx):
    etype, _, _ = slots
    admin.open(f"{CONFIGURE}?open={etype.id}")
    block(admin, etype).find_element("css selector", "button.toggle").click()
    admin.wait_for_text(".notice", "Label is now hidden from volunteers.")
    assert block(admin, etype).find_element("css selector", "button.toggle").text == "Hidden"


@pytest.mark.parametrize("kind, css, title, message, reassign", [
    ("row", None, "Delete this row?", "1 volunteer has already chosen this option.", ["Late"]),
    ("type", ".etype-header button.danger", "Delete Slots?",
     "1 volunteer choice uses this enrichment. Deleting it removes them, along with all its fields and rows.", None),
    ("field", "th button.col-delete", "Delete the Label column?",
     "2 rows have values in this column. Deleting it removes those values; volunteers' choices aren't affected.", None),
])
def test_delete_dialog_for_enrichments(admin, slots, kind, css, title, message, reassign):
    etype, early, _ = slots
    admin.open(f"{CONFIGURE}?open={etype.id}")
    if css is None:
        delete_row_button(admin, early).click()
    else:
        block(admin, etype).find_elements("css selector", css)[0].click()
    assert admin.find("#delete-dialog-title").text == title
    assert admin.find("#delete-dialog-message").text == message
    reassign_area = admin.find("#delete-dialog-reassign")
    if reassign is None:
        assert not reassign_area.is_displayed()
    else:
        assert [o.text for o in Select(admin.find("#delete-dialog-target")).options] == reassign


def test_row_reassign_labels_include_times(admin, slots, make_option):
    etype, early, late = slots
    make_option(etype, {"When": (at(2, 13), at(2, 15))})
    admin.open(f"{CONFIGURE}?open={etype.id}")
    delete_row_button(admin, early).click()
    assert sorted(o.text for o in Select(admin.find("#delete-dialog-target")).options) == [
        "Late", "Nov 2, 8:00 AM – 10:00 AM"]


# ---------------------------------------------------------------- saving values


def cell(page, row, fld, part=None):
    if part is None:
        return page.find(f'input[name="t|{row.id}|{fld.id}"]')
    return page.find(f"#c-{row.id}-{fld.id}-{part}-text")


def update_values(page, etype):
    block(page, etype).find_element("css selector", ".etype-actions .primary").click()


def test_values_save_and_reload(admin, slots, db_ctx):
    etype, early, late = slots
    label, when = field(etype, "Label"), field(etype, "When")
    admin.open(f"{CONFIGURE}?open={etype.id}")
    cell(admin, late, label).clear()
    cell(admin, late, label).send_keys("Evening")
    admin.fill(f"#c-{late.id}-{when.id}-start-text", "2026-11-01 18:00")
    admin.fill(f"#c-{late.id}-{when.id}-end-text", "2026-11-01 20:00")
    update_values(admin, etype)
    admin.wait_for_text(".notice", "Saved the values.")
    assert admin.path == f"{CONFIGURE}?open={etype.id}#type-{etype.id}"
    assert is_open(admin, etype)
    db_ctx.expire_all()
    assert db_ctx.scalar(select(TextEnrichment.value).filter_by(enrichment_id=late.id)) == "Evening"
    saved = db_ctx.scalar(select(CalendarEnrichment).filter_by(enrichment_id=late.id))
    assert (saved.start, saved.end) == (at(1, 23), at(2, 1))


def test_unreadable_time_is_caught_before_sending(admin, slots, db_ctx):
    etype, early, late = slots
    when = field(etype, "When")
    admin.open(f"{CONFIGURE}?open={etype.id}")
    admin.fill(f"#c-{late.id}-{when.id}-start-text", "soon")
    update_values(admin, etype)
    summary, items = message_dialog(admin)
    assert summary == "Some values need fixing before they can be saved."
    n = row_number(late, early, late)
    assert items == [f"Row {n}, When: start isn't a date and time (use YYYY-MM-DD HH:MM)"]
    bad = cell(admin, late, when, "start")
    assert bad.get_attribute("aria-invalid") == "true"
    admin.click("#values-error-ok")
    assert admin.js("return document.activeElement.id") == bad.get_attribute("id")


def test_server_problems_are_listed_and_edits_kept(admin, slots, db_ctx):
    etype, early, late = slots
    label, when = field(etype, "Label"), field(etype, "When")
    admin.open(f"{CONFIGURE}?open={etype.id}")
    cell(admin, early, label).clear()
    cell(admin, early, label).send_keys("Dawn")
    admin.fill(f"#c-{late.id}-{when.id}-start-text", "2026-11-01 20:00")
    admin.fill(f"#c-{late.id}-{when.id}-end-text", "2026-11-01 18:00")
    update_values(admin, etype)
    summary, items = message_dialog(admin)
    assert summary == "1 value needs fixing; nothing was saved"
    assert items == [f"Row {row_number(late, early, late)}, When: start must not be after end"]
    assert cell(admin, late, when, "start").get_attribute("aria-invalid") == "true"
    assert cell(admin, early, label).get_attribute("value") == "Dawn"  # still there
    db_ctx.expire_all()
    assert db_ctx.scalar(select(TextEnrichment.value).filter_by(enrichment_id=early.id)) == "Early"


def test_values_save_with_the_server_unreachable(admin, slots):
    etype, _, _ = slots
    admin.open(f"{CONFIGURE}?open={etype.id}")
    admin.js("window.fetch = () => Promise.reject(new TypeError('Failed to fetch'))")
    update_values(admin, etype)
    summary, _ = message_dialog(admin)
    assert summary.startswith("The values couldn't be saved: the server didn't respond as expected.")


# ---------------------------------------------------------------- reload details


def test_reload_details_discards_edits_and_shows_the_latest(admin, slots, db_ctx):
    etype, early, late = slots
    label = field(etype, "Label")
    admin.open(f"{CONFIGURE}?open={etype.id}")
    cell(admin, early, label).clear()
    cell(admin, early, label).send_keys("Unsaved")
    # Someone else changes the row meanwhile.
    db_ctx.scalar(select(TextEnrichment).filter_by(enrichment_id=late.id)).value = "Changed elsewhere"
    db_ctx.commit()
    block(admin, etype).find_element("css selector", ".reload-type").click()
    admin.wait(lambda: cell(admin, late, label).get_attribute("value") == "Changed elsewhere", "fresh values")
    assert cell(admin, early, label).get_attribute("value") == "Early"
    assert is_open(admin, etype)
    # The new block works like the old one: its delete dialog knows the new label.
    delete_row_button(admin, early).click()
    assert [o.text for o in Select(admin.find("#delete-dialog-target")).options] == ["Changed elsewhere"]


def test_reload_details_of_a_deleted_type(admin, slots, api, event, make_user, add_member, auth):
    etype, _, _ = slots
    admin.open(f"{CONFIGURE}?open={etype.id}")
    other = make_user("other")
    add_member(other, event.organization, is_admin=True)
    assert api.delete(f"/api/admin/event-config/acme/spring-fair/enrichment-types/{etype.id}",
                      json={"cascade": True}, headers=auth(other)).status_code == 200
    block(admin, etype).find_element("css selector", ".reload-type").click()
    summary, _ = message_dialog(admin)
    assert summary == "It was deleted since this page was loaded."
    assert admin.find_all(f"#type-{etype.id}") == []
