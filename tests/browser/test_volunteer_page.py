"""The public sign-up page (web/static/volunteer.js), end to end.

Windows are shown grouped by day in the volunteer's own time zone; options
come as checkboxes (multi-select types) or a dropdown (single-choice types),
with a filter on types that have text. Load fills the form from a saved
sign-up, Submit saves it, and errors open a dialog without touching what
the volunteer entered.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from api.models import VolunteerOffer, VolunteerWindow

from tests.api.conftest import at

PATH = "/acme/spring-fair/volunteer"


def utc(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def window(db_ctx, event):
    def make(start, end):
        w = VolunteerWindow(event=event, start=start, end=end)
        db_ctx.add(w)
        db_ctx.commit()
        return w
    return make


def checked(page, name):
    return sorted(b.get_attribute("value") for b in page.find_all(f'input[name="{name}"]') if b.is_selected())


def box(page, value):
    return page.find(f'input[value="{value}"]')


def offer(db_ctx, email="vol@example.com"):
    db_ctx.expire_all()
    return db_ctx.scalar(select(VolunteerOffer).filter_by(email=email))


# ---------------------------------------------------------------- rendering


def test_windows_grouped_by_local_day(page, event, window):
    # 03:00-05:00 UTC on Nov 2 is still Nov 1 in New York (EST, UTC-5).
    window(utc(2, 3), utc(2, 5))
    window(utc(2, 14), utc(2, 17))
    window(utc(2, 22), utc(3, 2))
    page.open(PATH)
    assert page.texts("#windows h3") == ["Sunday, November 1, 2026", "Monday, November 2, 2026"]
    assert page.texts("#windows label") == [
        "10:00 PM – Nov 2, 12:00 AM",  # ends on another local day: its date is shown
        "9:00 AM – 12:00 PM",
        "5:00 PM – 9:00 PM",
    ]


def test_times_follow_the_volunteers_time_zone(page, event, window):
    window(utc(2, 14), utc(2, 17))
    page.timezone("Asia/Tokyo")  # UTC+9
    page.open(PATH)
    assert page.texts("#windows h3") == ["Monday, November 2, 2026"]
    assert page.texts("#windows label") == ["11:00 PM – Nov 3, 2:00 AM"]


def test_event_without_windows_or_options(page, event):
    page.open(PATH)
    assert page.find("#windows").text == "No volunteer windows are available."
    assert page.texts("#enrichments fieldset") == []


@pytest.fixture
def options(event, make_type, make_option):
    """A multi-select "Roles" type, a single-choice "Shirt" type, and a calendar-only "Slot" type."""
    roles = make_type(event, name="Roles", fields=(("Label", "text", False),))
    shirt = make_type(event, name="Shirt", fields=(("Size", "text", False),), interaction="select")
    slot = make_type(event, name="Slot", fields=(("When", "calendar", False),))
    return {
        "gate": make_option(roles, {"Label": "Gate"}),
        "kitchen": make_option(roles, {"Label": "Kitchen"}),
        "parking": make_option(roles, {"Label": "Parking lot"}),
        "small": make_option(shirt, {"Size": "Small"}),
        "large": make_option(shirt, {"Size": "Large"}),
        "early": make_option(slot, {"When": (at(1, 14), at(1, 15))}),
        "blank": make_option(roles),
    }


def test_options_render_by_interaction(page, options):
    page.open(PATH)
    assert page.texts("#enrichments legend") == ["Roles", "Shirt", "Slot"]
    roles, shirt, slot = page.find_all("#enrichments fieldset")
    # In the order the API gives (by visible values); an option with none says so.
    assert [l.text for l in roles.find_elements("css selector", "label")] == [
        "(no details)", "Gate", "Kitchen", "Parking lot"]
    # Single choice: a dropdown whose blank first choice submits nothing.
    assert [o.text for o in shirt.find_elements("css selector", "option")] == ["", "Large", "Small"]
    # Calendar values in local time, with the date.
    assert slot.find_element("css selector", "label").text == "Nov 1, 9:00 AM – 10:00 AM"
    # Only types with a text field get a filter.
    assert [f.get_attribute("aria-label") for f in page.find_all("input.filter")] == ["Filter Roles", "Filter Shirt"]


# ---------------------------------------------------------------- filters


def test_filter_hides_non_matching_options(page, options):
    page.open(PATH)
    page.fill('input[aria-label="Filter Roles"]', "  PARK ")
    roles = page.find_all("#enrichments fieldset")[0]
    assert [l.text for l in roles.find_elements("css selector", "label") if l.is_displayed()] == ["Parking lot"]
    page.fill('input[aria-label="Filter Roles"]', "zzz")
    assert roles.find_element("css selector", "p.hint").is_displayed()  # "No options match."
    page.find('input[aria-label="Filter Roles"]').clear()
    page.find('input[aria-label="Filter Roles"]').send_keys(" ")
    assert len([l for l in roles.find_elements("css selector", "label") if l.is_displayed()]) == 4


def test_filter_keeps_selected_options_greyed(page, options):
    page.open(PATH)
    box(page, options["gate"].id).click()
    page.fill('input[aria-label="Filter Roles"]', "park")
    gate = box(page, options["gate"].id).find_element("xpath", "..")
    assert gate.is_displayed() and "filtered-out" in gate.get_attribute("class")
    assert checked(page, "enrichment") == [str(options["gate"].id)]  # never dropped


def test_filter_on_a_dropdown(page, options):
    from selenium.webdriver.support.ui import Select

    page.open(PATH)
    shirt = Select(page.find('select[aria-label="Shirt"]'))
    shirt.select_by_visible_text("Small")
    page.fill('input[aria-label="Filter Shirt"]', "large")
    small = page.find(f'option[value="{options["small"].id}"]')
    assert "filtered-out" in small.get_attribute("class")
    assert "filtered-out" in page.find('select[aria-label="Shirt"]').get_attribute("class")
    shirt.select_by_visible_text("Large")
    assert "filtered-out" not in page.find('select[aria-label="Shirt"]').get_attribute("class")


# ---------------------------------------------------------------- submitting and loading


def test_submit_creates_then_updates(page, options, event, window, db_ctx):
    from selenium.webdriver.support.ui import Select

    w = window(utc(2, 14), utc(2, 17))
    page.open(PATH)
    page.fill("#email", "Vol@Example.com")
    page.fill("#name", "  Vol  ")
    box(page, w.id).click()
    box(page, options["kitchen"].id).click()
    Select(page.find('select[aria-label="Shirt"]')).select_by_visible_text("Large")
    page.click("#submit")
    page.wait_for_text("#status", "Thanks! Your sign-up was created.")
    saved = offer(db_ctx)
    assert (saved.email, saved.name) == ("vol@example.com", "Vol")
    assert page.find("#name").get_attribute("value") == "Vol"

    box(page, options["gate"].id).click()
    page.click("#submit")
    page.wait_for_text("#status", "Your sign-up was updated.")
    assert sorted(str(link.enrichment_id) for link in offer(db_ctx).offer_enrichments) == sorted(
        str(options[k].id) for k in ("gate", "kitchen", "large"))


def test_load_fills_the_form(page, options, event, window, api):
    w1, w2 = window(utc(2, 14), utc(2, 17)), window(utc(3, 14), utc(3, 17))
    api.put("/api/acme/spring-fair/offers/vol@example.com", json={
        "name": "Vol", "window_ids": [str(w2.id)],
        "enrichment_ids": [str(options["parking"].id), str(options["small"].id)]})
    page.open(PATH)
    box(page, w1.id).click()  # changed before loading: replaced by what's saved
    page.fill("#name", "Someone else")
    page.fill("#email", "vol@example.com")
    page.click("#load")
    page.wait_for_text("#status", "Loaded your existing sign-up.")
    assert page.find("#name").get_attribute("value") == "Vol"
    assert checked(page, "window") == [str(w2.id)]
    assert checked(page, "enrichment") == [str(options["parking"].id)]
    assert page.find('select[aria-label="Shirt"]').get_attribute("value") == str(options["small"].id)


def test_load_unknown_email_clears_the_form(page, options, event, window):
    w = window(utc(2, 14), utc(2, 17))
    page.open(PATH)
    box(page, w.id).click()
    page.fill("#name", "Previous")
    page.fill("#email", "new@example.com")
    page.click("#load")
    page.wait_for_text("#status", "No sign-up found for that email.")
    assert page.find("#name").get_attribute("value") == ""
    assert checked(page, "window") == []


def test_load_reapplies_the_filter(page, options, api):
    api.put("/api/acme/spring-fair/offers/vol@example.com", json={"enrichment_ids": [str(options["gate"].id)]})
    page.open(PATH)
    page.fill('input[aria-label="Filter Roles"]', "park")
    page.fill("#email", "vol@example.com")
    page.click("#load")
    page.wait_for_text("#status", "Loaded")
    gate = box(page, options["gate"].id).find_element("xpath", "..")
    assert gate.is_displayed() and "filtered-out" in gate.get_attribute("class")


# ---------------------------------------------------------------- errors


@pytest.mark.parametrize("email", ["", "not-an-email"])
def test_invalid_email_opens_the_error_dialog(page, event, email):
    page.open(PATH)
    if email:
        page.fill("#email", email)
    page.click("#submit")
    assert page.dialog_open("#form-error-dialog")
    assert page.find("#form-error-message").text == "Enter a valid email address."
    page.click("#form-error-ok")
    assert not page.dialog_open("#form-error-dialog")
    # Focus goes back to the field to fix.
    assert page.js("return document.activeElement.id") == "email"


def test_server_error_keeps_the_entries(page, options, event, window, db_ctx):
    w = window(utc(2, 14), utc(2, 17))
    page.open(PATH)
    page.fill("#email", "vol@example.com")
    box(page, w.id).click()
    # The window is deleted by staff while the volunteer is filling the form in.
    db_ctx.delete(db_ctx.get(VolunteerWindow, w.id))
    db_ctx.commit()
    page.click("#submit")
    page.wait(lambda: page.dialog_open("#form-error-dialog"), "error dialog")
    assert page.find("#form-error-message").text.startswith("Windows not in this event")
    assert "error" in page.find("#status").get_attribute("class")
    assert checked(page, "window") == [str(w.id)]
    assert offer(db_ctx) is None


def test_unreachable_server(page, event):
    page.open(PATH)
    page.js("window.fetch = () => Promise.reject(new TypeError('Failed to fetch'))")
    page.fill("#email", "vol@example.com")
    page.click("#submit")
    page.wait(lambda: page.dialog_open("#form-error-dialog"), "error dialog")
    assert page.find("#form-error-message").text == "Could not reach the server."
    assert not page.find("#submit").get_attribute("disabled")  # buttons re-enabled
