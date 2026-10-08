"""Admin responses to sign-ups, and the manage view that lists them.

PUT /api/admin/event-manage/<org>/<event>/signups/<id>/response with
{"response": "accepted" | "denied" | null}. Responses live in their own table:
they must never change the volunteer's offer (its updated_on) or the sign-up's
calendar sequence.
"""
import uuid

import pytest
from sqlalchemy import select

from api.models import VolunteerOffer, VolunteerOfferResponse, VolunteerWindowOffer, db

BASE = "/api/acme/spring-fair"
MANAGE = "/api/admin/event-manage/acme/spring-fair"


@pytest.fixture
def member_headers(event, make_user, add_member, auth):
    user = make_user("member")
    add_member(user, event.organization)
    return auth(user)


def sign_up(api, email, *windows, name="Vol"):
    resp = api.put(f"{BASE}/offers/{email}", json={
        "name": name, "window_ids": [str(w.id) for w in windows]})
    assert resp.status_code in (200, 201)


def manage_view(api, headers):
    resp = api.get(f"{MANAGE}/windows", headers=headers)
    assert resp.status_code == 200
    return resp.json


def signup_id(api, headers, email, window):
    """The sign-up's id, as the manage view reports it."""
    for w in manage_view(api, headers)["windows"]:
        if w["id"] == str(window.id):
            for s in w["signups"]:
                if s["volunteer"]["email"] == email:
                    return s["id"]
    raise AssertionError(f"no active sign-up for {email}")


def respond(api, headers, sid, response):
    return api.put(f"{MANAGE}/signups/{sid}/response", json={"response": response}, headers=headers)


def response_of(api, headers, sid):
    for w in manage_view(api, headers)["windows"]:
        for s in w["signups"]:
            if s["id"] == sid:
                return s["response"], s["responded_on"]
    return None


@pytest.fixture
def signup(api, event, make_window, member_headers):
    window = make_window(event)
    sign_up(api, "vol@example.com", window)
    return {"window": window, "id": signup_id(api, member_headers, "vol@example.com", window)}


# ---------------------------------------------------------------- setting a response


def test_new_signup_is_pending(api, member_headers, signup):
    assert response_of(api, member_headers, signup["id"]) == (None, None)


@pytest.mark.parametrize("sequence", [
    ["accepted"], ["denied"], ["accepted", "denied"], ["denied", "accepted"],
    ["accepted", None], ["accepted", None, "denied"], [None],
])
def test_response_transitions(api, member_headers, signup, db_ctx, sequence):
    for value in sequence:
        resp = respond(api, member_headers, signup["id"], value)
        assert resp.status_code == 200
        assert resp.json["response"] == value
        assert resp.json["volunteer"] == {"name": "Vol", "email": "vol@example.com"}
    final, responded_on = response_of(api, member_headers, signup["id"])
    assert final == sequence[-1]
    assert (responded_on is None) == (sequence[-1] is None)
    # Back to pending deletes the row rather than storing a third state.
    db_ctx.expire_all()
    rows = db_ctx.scalar(select(db.func.count()).select_from(VolunteerOfferResponse))
    assert rows == (0 if sequence[-1] is None else 1)


def test_changing_a_response_moves_responded_on(api, member_headers, signup):
    respond(api, member_headers, signup["id"], "accepted")
    _, first = response_of(api, member_headers, signup["id"])
    respond(api, member_headers, signup["id"], "denied")
    _, second = response_of(api, member_headers, signup["id"])
    assert second > first


@pytest.mark.parametrize("body", [{}, {"response": "maybe"}, {"response": True}, {"response": "Accepted"}])
def test_invalid_response_is_refused(api, member_headers, signup, body):
    resp = api.put(f"{MANAGE}/signups/{signup['id']}/response", json=body, headers=member_headers)
    assert resp.status_code == 400
    assert response_of(api, member_headers, signup["id"]) == (None, None)


def test_unknown_signup_is_404(api, member_headers, signup):
    resp = respond(api, member_headers, "00000000-0000-0000-0000-000000000000", "accepted")
    assert resp.status_code == 404


def test_withdrawn_signup_cannot_be_answered(api, member_headers, signup, db_ctx):
    respond(api, member_headers, signup["id"], "accepted")
    sign_up(api, "vol@example.com")  # withdraws from every window
    resp = respond(api, member_headers, signup["id"], "denied")
    assert resp.status_code == 409
    db_ctx.expire_all()
    assert db_ctx.get(VolunteerWindowOffer, uuid.UUID(signup["id"])).response.accepted is True


# ---------------------------------------------------------------- responses never touch the offer


@pytest.mark.parametrize("value", ["accepted", "denied", None])
def test_response_leaves_offer_and_sequence_alone(api, member_headers, signup, db_ctx, value):
    def state():
        db_ctx.expire_all()
        signup_row = db_ctx.get(VolunteerWindowOffer, uuid.UUID(signup["id"]))
        return signup_row.volunteer_offer.updated_on, signup_row.sequence

    before = state()
    respond(api, member_headers, signup["id"], "accepted")
    respond(api, member_headers, signup["id"], value)
    assert state() == before


# ---------------------------------------------------------------- re-signing up


def test_resignup_keeps_earlier_response(api, member_headers, signup):
    """Intended for now (reviewed 2026-10-01): withdrawing and re-selecting a
    window brings back the same sign-up with its earlier response. Change this
    test only if responses should reset on re-sign-up."""
    respond(api, member_headers, signup["id"], "accepted")
    sign_up(api, "vol@example.com")
    sign_up(api, "vol@example.com", signup["window"])
    again = signup_id(api, member_headers, "vol@example.com", signup["window"])
    assert again == signup["id"]
    assert response_of(api, member_headers, again)[0] == "accepted"


# ---------------------------------------------------------------- the manage view


def test_manage_view_lists_active_signups_in_order(api, event, make_window, member_headers):
    late, early = make_window(event, day=2), make_window(event, day=1)
    sign_up(api, "first@example.com", early, late, name="First")
    sign_up(api, "second@example.com", early, name="Second")
    sign_up(api, "gone@example.com", early)
    sign_up(api, "gone@example.com")  # withdrew

    view = manage_view(api, member_headers)
    assert [w["id"] for w in view["windows"]] == [str(early.id), str(late.id)]
    emails = [[s["volunteer"]["email"] for s in w["signups"]] for w in view["windows"]]
    assert emails == [["first@example.com", "second@example.com"], ["first@example.com"]]
    assert [r["value"] for r in view["responses"]] == ["accepted", "denied"]


def test_manage_view_shows_offer_time(api, event, make_window, member_headers, db_ctx):
    window = make_window(event)
    sign_up(api, "vol@example.com", window)
    db_ctx.expire_all()
    offer = db_ctx.scalar(select(VolunteerOffer))
    listed = manage_view(api, member_headers)["windows"][0]["signups"][0]
    assert listed["offered_on"] == offer.created_on.isoformat()
