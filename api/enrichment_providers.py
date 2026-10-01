"""Enrichments for the manage view, from pluggable providers.

The manage endpoints never compute enrichments themselves: they build a
ManageContext for the event and ask every provider in the registry for
columns and values. Each provider is self-contained and describes its output
with Column descriptors, so the API (and the front end, which only sees
{key, label, kind, scope} and plain values) never depends on how a value was
produced.

Today the registry holds hard-coded derived providers. The plan is for
providers to be built at runtime from stored configuration; that only needs
providers_for() to construct them from config, with no change elsewhere.
"""
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .models import (
    CalendarEnrichment,
    Enrichment,
    VolunteerOffer,
    VolunteerOfferEnrichment,
    VolunteerWindow,
    VolunteerWindowOffer,
    db,
)

# Value kinds the front end knows how to display, sort and filter.
KINDS = {"boolean", "text", "number"}
# What a value describes: one volunteer's sign-up for one window.
SCOPE_SIGNUP = "signup"


@dataclass(frozen=True)
class Column:
    key: str  # opaque and stable, e.g. "derived:class-time-overlap"
    label: str
    kind: str  # one of KINDS
    scope: str = SCOPE_SIGNUP

    def to_dict(self):
        return asdict(self)


class ManageContext:
    """Everything providers may need for one event, loaded up front in a few
    queries so providers don't query per sign-up."""

    def __init__(self, event):
        self.event = event
        self.windows = db.session.scalars(
            select(VolunteerWindow)
            .filter_by(event_id=event.id)
            .order_by(VolunteerWindow.start, VolunteerWindow.end)
        ).all()
        window_ids = [w.id for w in self.windows]
        # Active sign-ups only: cancelled ones are no longer offers of time.
        self.signups = db.session.scalars(
            select(VolunteerWindowOffer)
            .filter(VolunteerWindowOffer.volunteer_window_id.in_(window_ids),
                    VolunteerWindowOffer.cancelled.is_(False))
            .options(
                selectinload(VolunteerWindowOffer.response),
                selectinload(VolunteerWindowOffer.volunteer_offer)
                .selectinload(VolunteerOffer.offer_enrichments)
                .selectinload(VolunteerOfferEnrichment.enrichment)
                .options(
                    selectinload(Enrichment.enrichment_type),
                    selectinload(Enrichment.calendar_fragments)
                    .selectinload(CalendarEnrichment.enrichment_fragment_type),
                    selectinload(Enrichment.text_fragments),
                ),
            )
        ).all() if window_ids else []
        self.window_by_id = {w.id: w for w in self.windows}

    def enrichments_of(self, signup):
        """The enrichments the volunteer chose on their offer."""
        return [link.enrichment for link in signup.volunteer_offer.offer_enrichments]


class EnrichmentProvider(ABC):
    @abstractmethod
    def columns(self, ctx):
        """Column descriptors this provider contributes for the event."""

    @abstractmethod
    def values(self, ctx):
        """{signup_id: {column_key: value}} for every sign-up in ctx."""


class CalendarOverlapProvider(EnrichmentProvider):
    """True when any calendar value the volunteer chose overlaps the window.

    Looks at enrichments linked to the volunteer's offer whose type is named
    `enrichment_type_name` and uses their calendar field named
    `calendar_field_name` (both case-insensitive). Overlap is half-open:
    start < window end and end > window start, so back-to-back times don't
    overlap. No matching enrichment means False.
    """

    def __init__(self, key, label, enrichment_type_name, calendar_field_name):
        self.column = Column(key=key, label=label, kind="boolean")
        self.type_name = enrichment_type_name.casefold()
        self.field_name = calendar_field_name.casefold()

    def columns(self, ctx):
        return [self.column]

    def _times(self, ctx, signup):
        for enrichment in ctx.enrichments_of(signup):
            if enrichment.enrichment_type.name.casefold() != self.type_name:
                continue
            for fragment in enrichment.calendar_fragments:
                if fragment.enrichment_fragment_type.name.casefold() == self.field_name:
                    yield fragment.start, fragment.end

    def values(self, ctx):
        out = {}
        for signup in ctx.signups:
            window = ctx.window_by_id[signup.volunteer_window_id]
            overlaps = any(start < window.end and end > window.start for start, end in self._times(ctx, signup))
            out[signup.id] = {self.column.key: overlaps}
        return out


class AdjacentAcceptedProvider(EnrichmentProvider):
    """True when the same volunteer offer is already accepted for a window
    adjacent to this sign-up's window.

    Two windows of the event are adjacent when they don't overlap (half-open,
    so back-to-back windows don't), the gap from the earlier one's end to the
    later one's start is at most `max_gap`, and no other window of the event
    occupies any part of that gap. Only active (not cancelled) sign-ups count.
    """

    def __init__(self, key, label, max_gap):
        self.column = Column(key=key, label=label, kind="boolean")
        self.max_gap = max_gap

    def columns(self, ctx):
        return [self.column]

    def _adjacent(self, ctx):
        """{window_id: set of adjacent window ids} for every window of the event."""
        adjacent = defaultdict(set)
        for a in ctx.windows:
            for b in ctx.windows:
                # a is the earlier window, b the later; ends may touch
                if a.id == b.id or a.end > b.start or b.start - a.end > self.max_gap:
                    continue
                gap_start, gap_end = a.end, b.start
                blocked = gap_start < gap_end and any(
                    c.start < gap_end and c.end > gap_start
                    for c in ctx.windows if c.id not in (a.id, b.id)
                )
                if not blocked:
                    adjacent[a.id].add(b.id)
                    adjacent[b.id].add(a.id)
        return adjacent

    def values(self, ctx):
        adjacent = self._adjacent(ctx)
        accepted_windows = defaultdict(set)  # offer id -> windows it's accepted for
        for signup in ctx.signups:
            if signup.response is not None and signup.response.accepted:
                accepted_windows[signup.volunteer_offer_id].add(signup.volunteer_window_id)
        return {
            signup.id: {self.column.key: bool(
                adjacent[signup.volunteer_window_id] & accepted_windows[signup.volunteer_offer_id]
            )}
            for signup in ctx.signups
        }


def providers_for(event):
    """The providers that apply to an event.

    Hard-coded for now; later built from stored configuration.
    """
    return [
        CalendarOverlapProvider(
            key="derived:class-time-overlap",
            label="Class overlaps window",
            enrichment_type_name="class",
            calendar_field_name="time",
        ),
        AdjacentAcceptedProvider(
            key="derived:adjacent-accepted",
            label="Adjacent already accepted",
            max_gap=timedelta(hours=2),
        ),
    ]


def compute(ctx):
    """Columns and per-sign-up values from every provider for ctx.event."""
    columns, values = [], {s.id: {} for s in ctx.signups}
    seen = set()
    for provider in providers_for(ctx.event):
        for column in provider.columns(ctx):
            if column.key in seen:
                raise ValueError(f"two providers both produce column {column.key!r}")
            if column.kind not in KINDS:
                raise ValueError(f"column {column.key!r} has unknown kind {column.kind!r}")
            seen.add(column.key)
            columns.append(column)
        for signup_id, row in provider.values(ctx).items():
            values.setdefault(signup_id, {}).update(row)
    return columns, values
