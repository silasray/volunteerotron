"""Deleting a window that volunteers have signed up for.

DELETE /api/admin/event-config/<org>/<event>/windows/<id> answers 409 with
linked_count while the window has active sign-ups, unless the body says what
to do with them:
  {"cascade": true}       delete them
  {"reassign_to": <id>}   move them to another window of the same event: same
                          calendar UID, sequence + 1 (an update to the invite),
                          and any response is dropped, since it was for the old time
Cancelled sign-ups never count and are always deleted. None of this is a
volunteer change, so the offer's updated_on stays put.
"""
import uuid

import pytest
from sqlalchemy import func, select

from api.models import (
    VolunteerOffer,
    VolunteerOfferResponse,
    VolunteerWindow,
    VolunteerWindowOffer,
)

BASE = "/api/acme/spring-fair"
CONFIG = "/api/admin/event-config/acme/spring-fair"


@pytest.fixture
def admin(event, make_user, add_member, auth):
    user = make_user("admin")
    add_member(user, event.organization, is_admin=True)
    return auth(user)


class Ref:
    """Just a window's id. Tests expire the session to read what a request
    changed, and a deleted model instance can't be read after that."""

    def __init__(self, window):
        self.id = window.id


@pytest.fixture
def windows(event, make_window):
    return Ref(make_window(event, day=1)), Ref(make_window(event, day=2))


def sign_up(api, email, *windows):
    resp = api.put(f"{BASE}/offers/{email}", json={"name": "Vol", "window_ids": [str(w.id) for w in windows]})
    assert resp.status_code in (200, 201)


def delete(api, admin, window, body=None):
    return api.delete(f"{CONFIG}/windows/{window.id}", json=body, headers=admin)


def signups(db_ctx, email):
    """{window id: VolunteerWindowOffer} for the volunteer, cancelled ones included."""
    db_ctx.expire_all()
    rows = db_ctx.scalars(select(VolunteerWindowOffer).join(VolunteerOffer).filter(VolunteerOffer.email == email))
    return {wo.volunteer_window_id: wo for wo in rows}


def respond(db_ctx, signup, accepted=True):
    db_ctx.add(VolunteerOfferResponse(volunteer_window_offer_id=signup.id, accepted=accepted))
    db_ctx.commit()


def count(db_ctx, model):
    db_ctx.expire_all()
    return db_ctx.scalar(select(func.count()).select_from(model))


def window_exists(db_ctx, window):
    db_ctx.expire_all()
    return db_ctx.get(VolunteerWindow, window.id) is not None


# ---------------------------------------------------------------- the conflict


def test_window_without_signups_deletes_outright(api, admin, windows, db_ctx):
    first, _ = windows
    resp = delete(api, admin, first)
    assert resp.status_code == 200
    assert resp.json == {"deleted": True, "moved": 0, "removed": 0}
    assert not window_exists(db_ctx, first)


def test_active_signups_need_a_decision(api, admin, windows, db_ctx):
    first, _ = windows
    sign_up(api, "a@example.com", first)
    sign_up(api, "b@example.com", first)
    resp = delete(api, admin, first)
    assert resp.status_code == 409
    assert resp.json["linked_count"] == 2
    assert window_exists(db_ctx, first)
    assert count(db_ctx, VolunteerWindowOffer) == 2


def test_cancelled_signups_dont_count(api, admin, windows, db_ctx):
    first, second = windows
    sign_up(api, "a@example.com", first, second)
    sign_up(api, "a@example.com", second)  # withdraws from the first window
    resp = delete(api, admin, first)
    assert resp.status_code == 200
    assert resp.json == {"deleted": True, "moved": 0, "removed": 0}
    assert list(signups(db_ctx, "a@example.com")) == [second.id]


# ---------------------------------------------------------------- cascade


def test_cascade_removes_signups_and_their_responses(api, admin, windows, db_ctx):
    first, second = windows
    sign_up(api, "a@example.com", first, second)
    sign_up(api, "b@example.com", first)
    respond(db_ctx, signups(db_ctx, "a@example.com")[first.id])
    resp = delete(api, admin, first, {"cascade": True})
    assert resp.status_code == 200
    assert resp.json == {"deleted": True, "moved": 0, "removed": 2}
    assert not window_exists(db_ctx, first)
    assert list(signups(db_ctx, "a@example.com")) == [second.id]
    assert signups(db_ctx, "b@example.com") == {}
    assert count(db_ctx, VolunteerOfferResponse) == 0
    assert count(db_ctx, VolunteerOffer) == 2  # the offers themselves stay


def test_cascade_wins_over_reassign(api, admin, windows, db_ctx):
    first, second = windows
    sign_up(api, "a@example.com", first)
    resp = delete(api, admin, first, {"cascade": True, "reassign_to": str(second.id)})
    assert resp.json == {"deleted": True, "moved": 0, "removed": 1}
    assert signups(db_ctx, "a@example.com") == {}


# ---------------------------------------------------------------- reassign


def test_reassign_moves_signup_as_a_calendar_update(api, admin, windows, db_ctx):
    first, second = windows
    sign_up(api, "a@example.com", first)
    before = signups(db_ctx, "a@example.com")[first.id]
    uid, sequence = before.uid, before.sequence
    resp = delete(api, admin, first, {"reassign_to": str(second.id)})
    assert resp.status_code == 200
    assert resp.json == {"deleted": True, "moved": 1, "removed": 0}
    after = signups(db_ctx, "a@example.com")
    assert list(after) == [second.id]
    moved = after[second.id]
    assert moved.uid == uid
    assert moved.sequence == sequence + 1
    assert not moved.cancelled


def test_reassign_drops_the_response(api, admin, windows, db_ctx):
    first, second = windows
    sign_up(api, "a@example.com", first)
    respond(db_ctx, signups(db_ctx, "a@example.com")[first.id], accepted=True)
    assert delete(api, admin, first, {"reassign_to": str(second.id)}).status_code == 200
    assert signups(db_ctx, "a@example.com")[second.id].response is None
    assert count(db_ctx, VolunteerOfferResponse) == 0


def test_reassign_onto_an_active_signup_merges(api, admin, windows, db_ctx):
    first, second = windows
    sign_up(api, "a@example.com", first, second)
    kept = signups(db_ctx, "a@example.com")[second.id]
    respond(db_ctx, kept, accepted=False)
    row_id, sequence = kept.id, kept.sequence
    resp = delete(api, admin, first, {"reassign_to": str(second.id)})
    assert resp.json == {"deleted": True, "moved": 1, "removed": 0}
    after = signups(db_ctx, "a@example.com")
    assert list(after) == [second.id]
    # The target sign-up is untouched: same row, sequence and response.
    assert after[second.id].id == row_id
    assert after[second.id].sequence == sequence
    assert after[second.id].response.accepted is False


def test_reassign_onto_a_cancelled_signup_reactivates_it(api, admin, windows, db_ctx):
    first, second = windows
    sign_up(api, "a@example.com", first, second)
    sign_up(api, "a@example.com", first)  # withdraws from the second window
    cancelled = signups(db_ctx, "a@example.com")[second.id]
    assert cancelled.cancelled
    row_id, uid, sequence = cancelled.id, cancelled.uid, cancelled.sequence
    resp = delete(api, admin, first, {"reassign_to": str(second.id)})
    assert resp.json == {"deleted": True, "moved": 1, "removed": 0}
    after = signups(db_ctx, "a@example.com")
    assert list(after) == [second.id]
    revived = after[second.id]
    assert (revived.id, revived.uid) == (row_id, uid)
    assert not revived.cancelled
    assert revived.sequence == sequence + 1


def test_reassign_reactivation_keeps_the_targets_old_response(api, admin, windows, db_ctx):
    # Consistent with a volunteer re-selecting a window: the earlier answer was
    # for this same time, so it's kept (intended for now).
    first, second = windows
    sign_up(api, "a@example.com", first, second)
    respond(db_ctx, signups(db_ctx, "a@example.com")[second.id], accepted=True)
    sign_up(api, "a@example.com", first)
    assert delete(api, admin, first, {"reassign_to": str(second.id)}).status_code == 200
    assert signups(db_ctx, "a@example.com")[second.id].response.accepted is True


def test_reassign_deletes_cancelled_signups_instead_of_moving_them(api, admin, windows, db_ctx):
    first, second = windows
    sign_up(api, "a@example.com", first)
    sign_up(api, "a@example.com")  # withdraws
    sign_up(api, "b@example.com", first)
    resp = delete(api, admin, first, {"reassign_to": str(second.id)})
    assert resp.json == {"deleted": True, "moved": 1, "removed": 0}
    assert signups(db_ctx, "a@example.com") == {}
    assert list(signups(db_ctx, "b@example.com")) == [second.id]


def test_reassign_several_volunteers(api, admin, windows, db_ctx):
    first, second = windows
    sign_up(api, "a@example.com", first)               # moved
    sign_up(api, "b@example.com", first, second)       # merged
    sign_up(api, "c@example.com", second)              # not involved
    resp = delete(api, admin, first, {"reassign_to": str(second.id)})
    assert resp.json == {"deleted": True, "moved": 2, "removed": 0}
    for email in ("a@example.com", "b@example.com", "c@example.com"):
        assert list(signups(db_ctx, email)) == [second.id]


@pytest.mark.parametrize("target", ["same", "other event", "unknown", "not a uuid"])
def test_bad_reassign_target_changes_nothing(api, admin, windows, db_ctx, make_org, make_event, make_window,
                                             target):
    first, _ = windows
    sign_up(api, "a@example.com", first)
    if target == "same":
        target_id, status = str(first.id), 400
    elif target == "other event":
        other = make_window(make_event(make_org("globex", "Globex"), name="gala"))
        target_id, status = str(other.id), 404
    elif target == "unknown":
        target_id, status = str(uuid.uuid4()), 404
    else:
        target_id, status = "nope", 400
    resp = delete(api, admin, first, {"reassign_to": target_id})
    assert resp.status_code == status
    assert window_exists(db_ctx, first)
    assert list(signups(db_ctx, "a@example.com")) == [first.id]


def test_reassign_target_ignored_when_nothing_to_move(api, admin, windows, db_ctx):
    first, _ = windows
    assert delete(api, admin, first, {"reassign_to": "nope"}).status_code == 200
    assert not window_exists(db_ctx, first)


# ---------------------------------------------------------------- not a volunteer change


@pytest.mark.parametrize("body", [{"cascade": True}, "reassign"])
def test_window_delete_leaves_offer_updated_on(api, admin, windows, db_ctx, body):
    first, second = windows
    sign_up(api, "a@example.com", first, second)
    db_ctx.expire_all()
    offer = db_ctx.scalar(select(VolunteerOffer))
    updated_on = offer.updated_on
    if body == "reassign":
        body = {"reassign_to": str(second.id)}
    assert delete(api, admin, first, body).status_code == 200
    db_ctx.expire_all()
    assert db_ctx.get(VolunteerOffer, offer.id).updated_on == updated_on
