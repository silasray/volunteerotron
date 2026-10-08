"""The derived columns on the manage view (api/enrichment_providers.py), as the
front end receives them from GET /api/admin/event-manage/<org>/<event>/windows:
a `columns` list, and each sign-up's `values` keyed by column key.

  derived:class-time-overlap  the volunteer chose a "class" whose "time"
                              overlaps the sign-up's window
  derived:adjacent-accepted   the same volunteer is already accepted for an
                              adjacent window (gap of at most 2h, nothing between)
Overlap is half-open throughout: times that only touch don't overlap.
"""
import pytest

from api import enrichment_providers
from api.enrichment_providers import Column, EnrichmentProvider
from api.models import VolunteerWindow

from .conftest import at

BASE = "/api/acme/spring-fair"
MANAGE = "/api/admin/event-manage/acme/spring-fair"
OVERLAP = "derived:class-time-overlap"
ADJACENT = "derived:adjacent-accepted"


@pytest.fixture
def member(event, make_user, add_member, auth):
    user = make_user("member")
    add_member(user, event.organization)
    return auth(user)


@pytest.fixture
def window(db_ctx, event):
    """window(start_hour, end_hour, day=1) -> a window of the event."""
    def make(start, end, day=1):
        w = VolunteerWindow(event=event, start=at(day, start), end=at(day, end))
        db_ctx.add(w)
        db_ctx.commit()
        return w
    return make


def sign_up(api, email, windows=(), options=()):
    resp = api.put(f"{BASE}/offers/{email}", json={
        "name": email.split("@")[0], "window_ids": [str(w.id) for w in windows],
        "enrichment_ids": [str(o.id) for o in options]})
    assert resp.status_code in (200, 201), resp.json


def manage(api, member):
    resp = api.get(f"{MANAGE}/windows", headers=member)
    assert resp.status_code == 200
    return resp.json


def values(api, member, column):
    """{(email, window id): the column's value} for every active sign-up."""
    return {
        (s["volunteer"]["email"], w["id"]): s["values"][column]
        for w in manage(api, member)["windows"] for s in w["signups"]
    }


def accept(api, member, email, w):
    for win in manage(api, member)["windows"]:
        for s in win["signups"]:
            if win["id"] == str(w.id) and s["volunteer"]["email"] == email:
                resp = api.put(f"{MANAGE}/signups/{s['id']}/response", json={"response": "accepted"},
                               headers=member)
                assert resp.status_code == 200
                return
    raise AssertionError(f"no sign-up for {email}")


def test_columns_are_described_for_the_front_end(api, member, event):
    assert manage(api, member)["columns"] == [
        {"key": OVERLAP, "label": "Class overlaps window", "kind": "boolean", "scope": "signup"},
        {"key": ADJACENT, "label": "Adjacent already accepted", "kind": "boolean", "scope": "signup"},
    ]


# ---------------------------------------------------------------- class time overlap


@pytest.fixture
def classes(event, make_type, make_option):
    """class_at(start_hour, end_hour) -> an option of the "Class" type."""
    etype = make_type(event, name="Class", fields=(("Name", "text", False), ("Time", "calendar", False)))

    def class_at(start, end, day=1):
        return make_option(etype, {"Name": f"{start}-{end}", "Time": (at(day, start), at(day, end))})
    return class_at


@pytest.mark.parametrize("class_hours, overlaps", [
    ((10, 11), True),    # inside the window
    ((8, 10), True),     # across its start
    ((11, 13), True),    # across its end
    ((7, 14), True),     # around it
    ((7, 9), False),     # ends as the window starts
    ((12, 13), False),   # starts as the window ends
    ((13, 14), False),   # after it
])
def test_class_overlap(api, member, window, classes, class_hours, overlaps):
    w = window(9, 12)
    sign_up(api, "a@example.com", [w], [classes(*class_hours)])
    assert values(api, member, OVERLAP) == {("a@example.com", str(w.id)): overlaps}


def test_any_chosen_class_overlapping_counts(api, member, window, classes):
    w = window(9, 12)
    sign_up(api, "a@example.com", [w], [classes(13, 14), classes(11, 12)])
    assert values(api, member, OVERLAP) == {("a@example.com", str(w.id)): True}


def test_overlap_is_per_window(api, member, window, classes):
    morning, evening = window(9, 12), window(18, 20)
    sign_up(api, "a@example.com", [morning, evening], [classes(10, 11)])
    assert values(api, member, OVERLAP) == {
        ("a@example.com", str(morning.id)): True, ("a@example.com", str(evening.id)): False}


def test_overlap_is_per_volunteer(api, member, window, classes):
    w = window(9, 12)
    sign_up(api, "a@example.com", [w], [classes(10, 11)])
    sign_up(api, "b@example.com", [w])
    assert values(api, member, OVERLAP) == {
        ("a@example.com", str(w.id)): True, ("b@example.com", str(w.id)): False}


def test_type_and_field_names_ignore_case(api, member, event, window, make_type, make_option):
    etype = make_type(event, name="CLASS", fields=(("tIME", "calendar", False),))
    w = window(9, 12)
    sign_up(api, "a@example.com", [w], [make_option(etype, {"tIME": (at(1, 10), at(1, 11))})])
    assert values(api, member, OVERLAP) == {("a@example.com", str(w.id)): True}


@pytest.mark.parametrize("type_name, field_name", [("Shift", "Time"), ("Class", "Break")])
def test_other_types_and_fields_are_ignored(api, member, event, window, make_type, make_option,
                                            type_name, field_name):
    etype = make_type(event, name=type_name, fields=((field_name, "calendar", False),))
    w = window(9, 12)
    sign_up(api, "a@example.com", [w], [make_option(etype, {field_name: (at(1, 10), at(1, 11))})])
    assert values(api, member, OVERLAP) == {("a@example.com", str(w.id)): False}


def test_hidden_time_still_counts(api, member, event, window, make_type, make_option, db_ctx):
    # Hiding a field hides it from volunteers, not from the manage view.
    etype = make_type(event, name="Class", fields=(("Time", "calendar", False),))
    w = window(9, 12)
    sign_up(api, "a@example.com", [w], [make_option(etype, {"Time": (at(1, 10), at(1, 11))})])
    etype.fragment_types[0].hidden = True
    db_ctx.commit()
    assert values(api, member, OVERLAP) == {("a@example.com", str(w.id)): True}


# ---------------------------------------------------------------- adjacent window already accepted


def adjacency(api, member, email, *windows):
    found = values(api, member, ADJACENT)
    return [found[(email, str(w.id))] for w in windows]


@pytest.mark.parametrize("second, adjacent", [
    ((12, 15), True),    # back to back
    ((13, 15), True),    # 1h gap
    ((14, 16), True),    # 2h gap: the limit
    ((15, 17), False),   # 3h gap
    ((11, 14), False),   # overlapping isn't adjacent
])
def test_adjacency_by_gap(api, member, window, second, adjacent):
    first, other = window(9, 12), window(*second)
    sign_up(api, "a@example.com", [first, other])
    accept(api, member, "a@example.com", first)
    assert adjacency(api, member, "a@example.com", first, other) == [False, adjacent]


def test_adjacency_works_both_ways(api, member, window):
    first, second = window(9, 12), window(13, 15)
    sign_up(api, "a@example.com", [first, second])
    accept(api, member, "a@example.com", second)
    assert adjacency(api, member, "a@example.com", first, second) == [True, False]


def test_a_window_in_the_gap_blocks_adjacency(api, member, window):
    first, between, last = window(9, 11), window(11, 12), window(12, 13)
    sign_up(api, "a@example.com", [first, last])
    accept(api, member, "a@example.com", first)
    # first and last are 1h apart, but `between` fills the gap: it's the
    # adjacent one, even though the volunteer didn't sign up for it.
    assert adjacency(api, member, "a@example.com", last) == [False]


@pytest.mark.parametrize("other, blocks", [
    ((12, 12, 2), False),  # another day
    ((10, 11), False),     # within the first window, not the gap
    ((11, 13), True),      # covers part of the gap
])
def test_only_windows_in_the_gap_block(api, member, window, other, blocks):
    first, last = window(9, 12), window(14, 15)
    window(*other)
    sign_up(api, "a@example.com", [first, last])
    accept(api, member, "a@example.com", first)
    assert adjacency(api, member, "a@example.com", last) == [not blocks]


def test_back_to_back_windows_cant_be_blocked(api, member, window):
    first, last = window(9, 12), window(12, 15)
    window(11, 13)  # overlaps both, but there's no gap for it to fill
    sign_up(api, "a@example.com", [first, last])
    accept(api, member, "a@example.com", first)
    assert adjacency(api, member, "a@example.com", last) == [True]


@pytest.mark.parametrize("response", [None, "denied"])
def test_only_accepted_sign_ups_count(api, member, window, response):
    first, second = window(9, 12), window(12, 15)
    sign_up(api, "a@example.com", [first, second])
    if response:
        for w in manage(api, member)["windows"]:
            for s in w["signups"]:
                if w["id"] == str(first.id):
                    api.put(f"{MANAGE}/signups/{s['id']}/response", json={"response": response}, headers=member)
    assert adjacency(api, member, "a@example.com", second) == [False]


def test_withdrawn_acceptance_doesnt_count(api, member, window):
    first, second = window(9, 12), window(12, 15)
    sign_up(api, "a@example.com", [first, second])
    accept(api, member, "a@example.com", first)
    sign_up(api, "a@example.com", [second])  # withdraws from the accepted window
    assert adjacency(api, member, "a@example.com", second) == [False]


def test_another_volunteers_acceptance_doesnt_count(api, member, window):
    first, second = window(9, 12), window(12, 15)
    sign_up(api, "a@example.com", [first])
    sign_up(api, "b@example.com", [second])
    accept(api, member, "a@example.com", first)
    assert values(api, member, ADJACENT) == {
        ("a@example.com", str(first.id)): False, ("b@example.com", str(second.id)): False}


# ---------------------------------------------------------------- the registry's rules


class Fixed(EnrichmentProvider):
    def __init__(self, key, kind="boolean"):
        self.column = Column(key=key, label=key, kind=kind)

    def columns(self, ctx):
        return [self.column]

    def values(self, ctx):
        return {s.id: {self.column.key: True} for s in ctx.signups}


def test_two_providers_cant_share_a_column(api, member, event, monkeypatch):
    monkeypatch.setattr(enrichment_providers, "providers_for", lambda event: [Fixed("x"), Fixed("x")])
    with pytest.raises(ValueError, match="two providers both produce column 'x'"):
        api.get(f"{MANAGE}/windows", headers=member)


def test_columns_need_a_known_kind(api, member, event, monkeypatch):
    monkeypatch.setattr(enrichment_providers, "providers_for", lambda event: [Fixed("x", kind="colour")])
    with pytest.raises(ValueError, match="column 'x' has unknown kind 'colour'"):
        api.get(f"{MANAGE}/windows", headers=member)


def test_event_without_windows(api, member, event):
    body = manage(api, member)
    assert body["windows"] == []
    assert [c["key"] for c in body["columns"]] == [OVERLAP, ADJACENT]
