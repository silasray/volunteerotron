"""The volunteer sign-up endpoints: the form, loading and saving an offer.

PUT /<org>/<event>/offers/<email> replaces the volunteer's selections:
  - email (with event) identifies the volunteer; it's normalized
  - unchecked windows are cancelled, not deleted, so the calendar UID survives;
    any change to a window's state raises its iCalendar sequence
  - updated_on moves only when the volunteer's own data changes
"""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.models import VolunteerOffer, VolunteerOfferEnrichment, VolunteerWindowOffer, db

BASE = "/api/acme/spring-fair"
EMAIL = "vol@example.com"


def put(api, body, email=EMAIL, base=BASE):
    return api.put(f"{base}/offers/{email}", json=body)


def ids(*models):
    return [str(m.id) for m in models]


def offer_row(db_ctx, email=EMAIL):
    db_ctx.expire_all()
    return db_ctx.scalar(select(VolunteerOffer).filter_by(email=email))


def signup_row(db_ctx, window, email=EMAIL):
    db_ctx.expire_all()
    return db_ctx.scalar(
        select(VolunteerWindowOffer)
        .join(VolunteerOffer)
        .filter(VolunteerOffer.email == email, VolunteerWindowOffer.volunteer_window_id == window.id)
    )


# ---------------------------------------------------------------- the form


def test_form_lists_windows_by_start_then_end(api, event, make_window, db_ctx):
    late = make_window(event, day=3)
    long_ = make_window(event, day=1, hours=5)
    short = make_window(event, day=1, hours=2)
    resp = api.get(f"{BASE}/volunteer-form")
    assert resp.status_code == 200
    assert [w["id"] for w in resp.json["windows"]] == ids(short, long_, late)
    assert resp.json["organization"]["name"] == "acme"
    assert resp.json["event"]["name"] == "spring-fair"


def test_form_for_unknown_event_is_404(api, event):
    assert api.get("/api/acme/nope/volunteer-form").status_code == 404
    assert api.get("/api/nope/spring-fair/volunteer-form").status_code == 404


# ---------------------------------------------------------------- create and load


def test_create_then_update(api, event, make_window):
    a, b = make_window(event, day=1), make_window(event, day=2)
    created = put(api, {"name": "Vol", "window_ids": ids(a)})
    assert created.status_code == 201
    assert created.json["window_ids"] == ids(a)
    updated = put(api, {"name": "Vol", "window_ids": ids(a, b)})
    assert updated.status_code == 200
    assert updated.json["id"] == created.json["id"]
    assert updated.json["window_ids"] == sorted(ids(a, b))


def test_load_returns_saved_selections(api, event, make_window, make_type, make_option):
    window = make_window(event)
    etype = make_type(event)
    option = make_option(etype, {"Label": "Gate"})
    put(api, {"name": "Vol", "window_ids": ids(window), "enrichment_ids": ids(option)})
    resp = api.get(f"{BASE}/offers/{EMAIL}")
    assert resp.status_code == 200
    assert resp.json["name"] == "Vol"
    assert resp.json["window_ids"] == ids(window)
    assert resp.json["enrichment_ids"] == ids(option)


def test_load_unknown_offer_is_404(api, event):
    assert api.get(f"{BASE}/offers/{EMAIL}").status_code == 404


def test_offers_are_per_event(api, event, make_event, make_window):
    make_event(event.organization, name="gala", pretty_name="Gala")
    put(api, {"name": "At the fair"})
    assert api.get(f"/api/acme/gala/offers/{EMAIL}").status_code == 404
    assert put(api, {"name": "At the gala"}, base="/api/acme/gala").status_code == 201
    assert api.get(f"{BASE}/offers/{EMAIL}").json["name"] == "At the fair"


# ---------------------------------------------------------------- email


@pytest.mark.parametrize("spelling", ["VOL@Example.com", "%20vol@example.com%20", "Vol@EXAMPLE.COM"])
def test_email_is_normalized(api, event, db_ctx, spelling):
    assert put(api, {"name": "Vol"}, email=spelling).status_code == 201
    assert offer_row(db_ctx).email == EMAIL
    # Any spelling finds the same offer.
    assert api.get(f"{BASE}/offers/{spelling}").status_code == 200
    assert put(api, {"name": "Vol"}, email=EMAIL).status_code == 200


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "a b@example.com", "@example.com"])
def test_invalid_email_is_refused(api, event, db_ctx, email):
    assert put(api, {"name": "Vol"}, email=email).status_code == 400
    assert api.get(f"{BASE}/offers/{email}").status_code == 400
    assert offer_row(db_ctx) is None


# ---------------------------------------------------------------- validation (nothing saved)


def test_name_is_optional_and_stripped(api, event, db_ctx):
    put(api, {})
    assert offer_row(db_ctx).name == ""
    put(api, {"name": "  Vol  "})
    assert offer_row(db_ctx).name == "Vol"


@pytest.mark.parametrize("body, problem", [
    (["not", "an", "object"], "a JSON object body is required"),
    ({"name": 5}, "name must be a string"),
    ({"name": "x" * 256}, "name must be at most 255 characters"),
    ({"window_ids": "abc"}, "window_ids must be a list of ids"),
    ({"window_ids": ["not-a-uuid"]}, "window_ids contains an invalid id"),
    ({"enrichment_ids": {}}, "enrichment_ids must be a list of ids"),
    ({"enrichment_ids": ["nope"]}, "enrichment_ids contains an invalid id"),
])
def test_bad_body_is_refused(api, event, db_ctx, body, problem):
    resp = put(api, body)
    assert resp.status_code == 400
    assert resp.json["error"] == problem
    assert offer_row(db_ctx) is None


def test_window_from_another_event_is_refused(api, event, make_event, make_window, db_ctx):
    other = make_event(event.organization, name="gala", pretty_name="Gala")
    resp = put(api, {"window_ids": ids(make_window(other))})
    assert resp.status_code == 400
    assert "windows not in this event" in resp.json["error"]
    assert offer_row(db_ctx) is None


def test_option_from_another_event_is_refused(api, event, make_event, make_type, make_option, db_ctx):
    other = make_event(event.organization, name="gala", pretty_name="Gala")
    resp = put(api, {"enrichment_ids": ids(make_option(make_type(other), {"Label": "x"}))})
    assert resp.status_code == 400
    assert offer_row(db_ctx) is None


def test_failed_update_keeps_previous_selections(api, event, make_window, db_ctx):
    window = make_window(event)
    put(api, {"name": "Vol", "window_ids": ids(window)})
    assert put(api, {"name": "Changed", "window_ids": ["bad"]}).status_code == 400
    loaded = api.get(f"{BASE}/offers/{EMAIL}").json
    assert loaded["name"] == "Vol" and loaded["window_ids"] == ids(window)


def test_concurrent_save_is_409(api, event, monkeypatch):
    def collide():
        raise IntegrityError("INSERT", {}, Exception("duplicate"))

    monkeypatch.setattr(db.session, "commit", collide)
    resp = put(api, {"name": "Vol"})
    assert resp.status_code == 409
    assert "changed at the same time" in resp.json["error"]


# ---------------------------------------------------------------- windows: cancel, not delete


def test_unchecking_cancels_and_keeps_uid(api, event, make_window, db_ctx):
    window = make_window(event)
    put(api, {"window_ids": ids(window)})
    first = signup_row(db_ctx, window)
    uid, sequence = first.uid, first.sequence

    resp = put(api, {"window_ids": []})
    assert resp.json["window_ids"] == []
    cancelled = signup_row(db_ctx, window)
    assert cancelled.id == first.id and cancelled.cancelled
    assert cancelled.uid == uid
    assert cancelled.sequence == sequence + 1


def test_reselecting_reuses_the_signup(api, event, make_window, db_ctx):
    window = make_window(event)
    put(api, {"window_ids": ids(window)})
    original = signup_row(db_ctx, window)
    uid, row_id = original.uid, original.id
    put(api, {"window_ids": []})
    put(api, {"window_ids": ids(window)})
    again = signup_row(db_ctx, window)
    assert (again.id, again.uid, again.cancelled, again.sequence) == (row_id, uid, False, 2)
    assert db_ctx.scalar(select(db.func.count()).select_from(VolunteerWindowOffer)) == 1


def test_unchanged_window_keeps_its_sequence(api, event, make_window, db_ctx):
    kept, dropped = make_window(event, day=1), make_window(event, day=2)
    put(api, {"window_ids": ids(kept, dropped)})
    put(api, {"window_ids": ids(kept)})
    put(api, {"window_ids": ids(kept)})
    assert signup_row(db_ctx, kept).sequence == 0
    assert signup_row(db_ctx, dropped).sequence == 1


def test_uids_are_distinct(api, event, make_window, db_ctx):
    a, b = make_window(event, day=1), make_window(event, day=2)
    put(api, {"window_ids": ids(a, b)})
    put(api, {"window_ids": ids(a)}, email="other@example.com")
    uids = db_ctx.scalars(select(VolunteerWindowOffer.uid)).all()
    assert len(uids) == len(set(uids)) == 3


# ---------------------------------------------------------------- enrichment choices


def test_choices_are_added_and_removed(api, event, make_type, make_option, db_ctx):
    etype = make_type(event)
    a, b, c = (make_option(etype, {"Label": v}) for v in "abc")
    assert put(api, {"enrichment_ids": ids(a, b)}).json["enrichment_ids"] == sorted(ids(a, b))
    assert put(api, {"enrichment_ids": ids(b, c)}).json["enrichment_ids"] == sorted(ids(b, c))
    db_ctx.expire_all()
    assert db_ctx.scalar(select(db.func.count()).select_from(VolunteerOfferEnrichment)) == 2


def test_single_choice_type_takes_at_most_one(api, event, make_type, make_option, db_ctx):
    etype = make_type(event, name="Shirt", interaction="select")
    small, large = make_option(etype, {"Label": "S"}), make_option(etype, {"Label": "L"})
    resp = put(api, {"enrichment_ids": ids(small, large)})
    assert resp.status_code == 400
    assert resp.json["error"] == "choose at most one option for 'Shirt'"
    assert offer_row(db_ctx) is None
    assert put(api, {"enrichment_ids": ids(large)}).status_code == 201


def test_single_choice_limit_is_per_type(api, event, make_type, make_option):
    shirt = make_type(event, name="Shirt", interaction="select")
    diet = make_type(event, name="Diet", interaction="select")
    chosen = [make_option(shirt, {"Label": "S"}), make_option(diet, {"Label": "Vegan"})]
    assert put(api, {"enrichment_ids": ids(*chosen)}).status_code == 201


# ---------------------------------------------------------------- updated_on: volunteer changes only


@pytest.fixture
def saved(api, event, make_window, make_type, make_option, db_ctx):
    """An offer with one window and one choice; returns what tests change."""
    w1, w2 = make_window(event, day=1), make_window(event, day=2)
    etype = make_type(event)
    o1, o2 = make_option(etype, {"Label": "a"}), make_option(etype, {"Label": "b"})
    body = {"name": "Vol", "window_ids": ids(w1), "enrichment_ids": ids(o1)}
    put(api, body)
    offer = offer_row(db_ctx)
    return {"body": body, "w2": w2, "o2": o2,
            "created_on": offer.created_on, "updated_on": offer.updated_on}


@pytest.mark.parametrize("change", ["name", "add window", "drop window", "add choice", "drop choice"])
def test_volunteer_changes_move_updated_on(api, saved, db_ctx, change):
    body = dict(saved["body"])
    if change == "name":
        body["name"] = "New name"
    elif change == "add window":
        body["window_ids"] = body["window_ids"] + ids(saved["w2"])
    elif change == "drop window":
        body["window_ids"] = []
    elif change == "add choice":
        body["enrichment_ids"] = body["enrichment_ids"] + ids(saved["o2"])
    else:
        body["enrichment_ids"] = []
    assert put(api, body).status_code == 200
    offer = offer_row(db_ctx)
    assert offer.updated_on > saved["updated_on"]
    assert offer.created_on == saved["created_on"]


def test_identical_resubmit_leaves_updated_on(api, saved, db_ctx):
    assert put(api, saved["body"]).status_code == 200
    assert offer_row(db_ctx).updated_on == saved["updated_on"]


def test_new_offer_updated_on_equals_created_on(api, saved, db_ctx):
    assert saved["updated_on"] == saved["created_on"]
