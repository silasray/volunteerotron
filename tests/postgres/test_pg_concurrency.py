"""Real races on Postgres: two saves of the same volunteer offer at once.

A volunteer with the form open in two tabs (or a double submit) sends two
saves together. Both read the offer as it was, and the second to write hits a
unique constraint. It must get 409 ("changed at the same time; please try
again") and leave the first save's data intact; never a 500.

The race is staged deterministically: after the request has read the offer,
just before it inserts its row, a second connection commits the same row, as
the other tab would have.
"""
import threading

import pytest
from sqlalchemy import event, func, insert, select

from api.models import (
    VolunteerOffer,
    VolunteerOfferEnrichment,
    VolunteerWindowOffer,
    db,
)
from api.ratelimit_storage import AppDatabaseStorage

BASE = "/api/acme/spring-fair"
EMAIL = "vol@example.com"
RACE_LOST = "this sign-up was changed at the same time; please try again"


def save(api, **body):
    return api.put(f"{BASE}/offers/{EMAIL}", json={"name": "Vol", **body})


def ids(*objs):
    return [str(o.id) for o in objs]


@pytest.fixture
def other_tab(api_app):
    """other_tab(model, **values): the next time a request is about to insert a
    `model` row, a second connection first inserts and commits one with `values`."""
    staged = []

    def stage(model, **values):
        def before_insert(mapper, connection, target):
            with db.engine.begin() as conn:
                conn.execute(insert(model).values(**values))

        event.listen(model, "before_insert", before_insert, once=True)
        staged.append((model, before_insert))

    yield stage
    for model, listener in staged:  # in case a test failed before it fired
        if event.contains(model, "before_insert", listener):
            event.remove(model, "before_insert", listener)


def offer_id(db_ctx):
    db_ctx.expire_all()
    return db_ctx.scalar(select(VolunteerOffer.id).filter_by(email=EMAIL))


def count(db_ctx, model):
    db_ctx.expire_all()
    return db_ctx.scalar(select(func.count()).select_from(model))


def test_two_first_saves(api, event, make_window, db_ctx, other_tab):
    window = make_window(event)
    other_tab(VolunteerOffer, event_id=event.id, email=EMAIL, name="Other tab")
    resp = save(api, window_ids=ids(window))
    assert resp.status_code == 409
    assert resp.json == {"error": RACE_LOST}
    # The other tab's save stands, and nothing of this one was written.
    db_ctx.expire_all()
    assert db_ctx.scalar(select(VolunteerOffer.name)) == "Other tab"
    assert count(db_ctx, VolunteerWindowOffer) == 0
    # Retrying, as the message asks, works.
    assert save(api, window_ids=ids(window)).status_code == 200


@pytest.mark.xfail(strict=True, reason=(
    "TODO: losing this race is a 500, not a 409. save_offer adds the new window sign-ups, "
    "then reads offer.offer_enrichments; that query autoflushes the INSERT, so the "
    "IntegrityError is raised outside the try around commit()."))
def test_two_saves_adding_the_same_window(api, event, make_window, db_ctx, other_tab):
    first, second = make_window(event, day=1), make_window(event, day=2)
    assert save(api, window_ids=ids(first)).status_code == 201
    offer = offer_id(db_ctx)
    other_tab(VolunteerWindowOffer, volunteer_offer_id=offer, volunteer_window_id=second.id)
    resp = save(api, window_ids=ids(first, second))
    assert resp.status_code == 409
    assert resp.json == {"error": RACE_LOST}
    assert count(db_ctx, VolunteerWindowOffer) == 2


def test_two_saves_choosing_the_same_option(api, event, make_type, make_option, db_ctx, other_tab):
    etype = make_type(event)
    a, b = make_option(etype, {"Label": "a"}), make_option(etype, {"Label": "b"})
    assert save(api, enrichment_ids=ids(a)).status_code == 201
    offer = offer_id(db_ctx)
    other_tab(VolunteerOfferEnrichment, volunteer_offer_id=offer, enrichment_id=b.id)
    resp = save(api, enrichment_ids=ids(a, b))
    assert resp.status_code == 409
    assert resp.json == {"error": RACE_LOST}
    assert count(db_ctx, VolunteerOfferEnrichment) == 2


# ---------------------------------------------------------------- rate-limit counters


def test_concurrent_counts_are_exact(api_app):
    """Lambda instances count the same key at once; the upsert mustn't lose hits."""
    hits, errors = [], []

    def count_once():
        try:
            with api_app.app_context():
                hits.append(AppDatabaseStorage("appdb://").incr("login-name:alice", 60))
        except Exception as err:  # surfaced below; a thread can't fail the test
            errors.append(err)

    threads = [threading.Thread(target=count_once) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(hits) == list(range(1, 21))
