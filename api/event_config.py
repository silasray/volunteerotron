"""Endpoints behind an event's configure page.

Every route requires a bearer token from an admin of the event's organization;
anyone else gets 404, so the routes don't reveal which events exist.

Deleting something volunteers have chosen (a window, an enrichment, an
enrichment type) answers 409 with the number of affected choices unless the
request says what to do with them: {"reassign_to": <id>} moves them to another
item of the same kind, {"cascade": true} deletes them.
"""
from datetime import datetime

from flask import Blueprint, g, jsonify, request
from sqlalchemy import func, select

from .auth import require_user
from .errors import ApiError
from .models import (
    CalendarEnrichment,
    Enrichment,
    EnrichmentFragmentType,
    EnrichmentType,
    Event,
    ContentType,
    Organization,
    TextEnrichment,
    UserOrganization,
    VolunteerInteraction,
    VolunteerOfferEnrichment,
    VolunteerWindow,
    VolunteerWindowOffer,
    db,
)

bp = Blueprint("api_event_config", __name__)


def _event_for_admin(org_name, event_name):
    event = db.session.scalar(
        select(Event)
        .join(Organization)
        .join(UserOrganization, UserOrganization.organization_id == Organization.id)
        .filter(
            Organization.name == org_name,
            Event.name == event_name,
            UserOrganization.user_id == g.user.id,
            UserOrganization.is_admin.is_(True),
        )
    )
    if event is None:
        raise ApiError("event not found", 404)
    return event


def _body():
    data = request.get_json(silent=True)
    if data is None and not request.data:
        return {}
    if not isinstance(data, dict):
        raise ApiError("a JSON object body is required")
    return data


def _text(data, key, label, max_len=255):
    value = data.get(key)
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise ApiError(f"{label} is required")
    if len(value) > max_len:
        raise ApiError(f"{label} must be at most {max_len} characters")
    return value


def _when(value, label):
    """An ISO 8601 date-time with a UTC offset, e.g. 2026-10-04T13:00:00Z."""
    if not isinstance(value, str) or not value.strip():
        raise ApiError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(f"{label} is not a valid date and time")
    if parsed.tzinfo is None:
        raise ApiError(f"{label} must include a timezone offset")
    return parsed


def _range(start_value, end_value):
    start = _when(start_value, "start")
    end = _when(end_value, "end")
    if start > end:
        raise ApiError("start must not be after end")
    return start, end


def _enum(enum_cls, value, label):
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ApiError(f"{label} must be one of: {allowed}")


def _one(model, item_id, event, label):
    obj = db.session.get(model, item_id)
    owner = getattr(obj, "event_id", None)
    if obj is not None and owner is None:  # nested under an enrichment type
        owner = obj.enrichment_type.event_id
    if obj is None or owner != event.id:
        raise ApiError(f"{label} not found", 404)
    return obj


# ---------------------------------------------------------------- read


def _iso(dt):
    return dt.isoformat()


def _enrichment_values(enrichment, fields_by_id):
    values = {}
    for frag in enrichment.text_fragments:
        if frag.enrichment_fragment_type_id in fields_by_id:
            values.setdefault(str(frag.enrichment_fragment_type_id), {"value": frag.value})
    for frag in enrichment.calendar_fragments:
        if frag.enrichment_fragment_type_id in fields_by_id:
            values.setdefault(
                str(frag.enrichment_fragment_type_id),
                {"start": _iso(frag.start), "end": _iso(frag.end)},
            )
    return values


@bp.get("/<org>/<event_name>/config")
@require_user
def get_config(org, event_name):
    event = _event_for_admin(org, event_name)

    active_counts = dict(db.session.execute(
        select(VolunteerWindowOffer.volunteer_window_id, func.count())
        .join(VolunteerWindow)
        .filter(VolunteerWindow.event_id == event.id, VolunteerWindowOffer.cancelled.is_(False))
        .group_by(VolunteerWindowOffer.volunteer_window_id)
    ).all())
    windows = sorted(event.windows, key=lambda w: (w.start, w.end))

    link_counts = dict(db.session.execute(
        select(VolunteerOfferEnrichment.enrichment_id, func.count())
        .join(Enrichment)
        .join(EnrichmentType)
        .filter(EnrichmentType.event_id == event.id)
        .group_by(VolunteerOfferEnrichment.enrichment_id)
    ).all())

    types = []
    for et in sorted(event.enrichment_types, key=lambda t: t.name.lower()):
        fields = sorted(et.fragment_types, key=lambda f: f.name.lower())
        fields_by_id = {f.id: f for f in fields}
        enrichments = sorted(et.enrichments, key=lambda e: str(e.id))
        types.append({
            **et.to_dict(),
            "link_count": sum(link_counts.get(e.id, 0) for e in enrichments),
            "fields": [{**f.to_dict(), "value_count": _value_count(f)} for f in fields],
            "enrichments": [
                {
                    "id": str(e.id),
                    "link_count": link_counts.get(e.id, 0),
                    "values": _enrichment_values(e, fields_by_id),
                }
                for e in enrichments
            ],
        })

    return jsonify(
        organization=event.organization.to_dict(),
        event=event.to_dict(),
        volunteer_interactions=[m.value for m in VolunteerInteraction],
        content_types=[m.value for m in ContentType],
        windows=[
            {"id": str(w.id), "start": _iso(w.start), "end": _iso(w.end),
             "offer_count": active_counts.get(w.id, 0)}
            for w in windows
        ],
        enrichment_types=types,
    )


# ---------------------------------------------------------------- windows


@bp.post("/<org>/<event_name>/windows")
@require_user
def add_window(org, event_name):
    event = _event_for_admin(org, event_name)
    data = _body()
    start, end = _range(data.get("start"), data.get("end"))
    window = VolunteerWindow(event=event, start=start, end=end)
    db.session.add(window)
    db.session.commit()
    return jsonify({"id": str(window.id), "start": _iso(window.start), "end": _iso(window.end)}), 201


@bp.delete("/<org>/<event_name>/windows/<uuid:window_id>")
@require_user
def delete_window(org, event_name, window_id):
    event = _event_for_admin(org, event_name)
    window = _one(VolunteerWindow, window_id, event, "window")
    data = _body()
    active = [wo for wo in window.window_offers if not wo.cancelled]

    if active and not data.get("cascade") and not data.get("reassign_to"):
        raise_conflict(f"{len(active)} volunteer(s) have offered their time in this window", len(active))

    target = None
    if active and data.get("reassign_to") and not data.get("cascade"):
        target = _one(VolunteerWindow, _uuid(data["reassign_to"]), event, "target window")
        if target.id == window.id:
            raise ApiError("choose a different window to move the offers to")

    moved = 0
    for wo in list(window.window_offers):
        if target is not None and not wo.cancelled:
            existing = db.session.scalar(select(VolunteerWindowOffer).filter_by(
                volunteer_offer_id=wo.volunteer_offer_id, volunteer_window_id=target.id))
            if existing is None:
                # Same calendar UID, new time: a later invite becomes an update.
                wo.volunteer_window = target
                wo.sequence = (wo.sequence or 0) + 1
                if wo.response is not None:  # the answer was for the old time
                    db.session.delete(wo.response)
                moved += 1
                continue
            if existing.cancelled:
                existing.cancelled = False
                existing.sequence = (existing.sequence or 0) + 1
            moved += 1
        if wo.response is not None:
            db.session.delete(wo.response)
        db.session.delete(wo)
    db.session.flush()
    db.session.delete(window)
    db.session.commit()
    return jsonify(deleted=True, moved=moved, removed=len(active) - moved)


def raise_conflict(message, count):
    raise ApiError(message, 409, linked_count=count)


def _uuid(value):
    import uuid

    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ApiError("invalid id")


# ---------------------------------------------------------------- enrichment types


@bp.post("/<org>/<event_name>/enrichment-types")
@require_user
def add_enrichment_type(org, event_name):
    event = _event_for_admin(org, event_name)
    data = _body()
    name = _text(data, "name", "name")
    interaction = _enum(VolunteerInteraction, data.get("volunteer_interaction"), "volunteer interaction")
    et = EnrichmentType(event=event, name=name, volunteer_interaction=interaction)
    db.session.add(et)
    db.session.commit()
    return jsonify(et.to_dict()), 201


@bp.patch("/<org>/<event_name>/enrichment-types/<uuid:type_id>")
@require_user
def update_enrichment_type(org, event_name, type_id):
    event = _event_for_admin(org, event_name)
    et = _one(EnrichmentType, type_id, event, "enrichment type")
    interaction = _enum(VolunteerInteraction, _body().get("volunteer_interaction"), "volunteer interaction")
    if interaction is VolunteerInteraction.SELECT and et.volunteer_interaction is not interaction:
        # A single-choice type can't keep volunteers who picked several.
        crowded = db.session.scalar(
            select(func.count()).select_from(
                select(VolunteerOfferEnrichment.volunteer_offer_id)
                .join(Enrichment)
                .filter(Enrichment.enrichment_type_id == et.id)
                .group_by(VolunteerOfferEnrichment.volunteer_offer_id)
                .having(func.count() > 1)
                .subquery()
            )
        )
        if crowded:
            raise ApiError(
                f"{crowded} volunteer(s) chose more than one option of this type; "
                "it can't become single-choice until they have at most one", 409
            )
    et.volunteer_interaction = interaction
    db.session.commit()
    return jsonify(et.to_dict())


def _delete_enrichment_rows(enrichment):
    for link in list(enrichment.offer_enrichments):
        db.session.delete(link)
    for frag in list(enrichment.text_fragments) + list(enrichment.calendar_fragments):
        db.session.delete(frag)
    db.session.flush()
    db.session.delete(enrichment)


@bp.delete("/<org>/<event_name>/enrichment-types/<uuid:type_id>")
@require_user
def delete_enrichment_type(org, event_name, type_id):
    event = _event_for_admin(org, event_name)
    et = _one(EnrichmentType, type_id, event, "enrichment type")
    links = sum(len(e.offer_enrichments) for e in et.enrichments)
    if links and not _body().get("cascade"):
        raise_conflict(f"{links} volunteer choice(s) use this enrichment type", links)
    for enrichment in list(et.enrichments):
        _delete_enrichment_rows(enrichment)
    db.session.flush()
    for field in list(et.fragment_types):
        db.session.delete(field)
    db.session.flush()
    db.session.delete(et)
    db.session.commit()
    return jsonify(deleted=True, removed=links)


# ---------------------------------------------------------------- fields


@bp.post("/<org>/<event_name>/enrichment-types/<uuid:type_id>/fields")
@require_user
def add_field(org, event_name, type_id):
    event = _event_for_admin(org, event_name)
    et = _one(EnrichmentType, type_id, event, "enrichment type")
    data = _body()
    name = _text(data, "name", "name")
    kind = _enum(ContentType, data.get("content_type"), "content type")
    if any(f.name.lower() == name.lower() for f in et.fragment_types):
        raise ApiError(f"this enrichment type already has a field named {name!r}", 409)
    field = EnrichmentFragmentType(enrichment_type=et, name=name, content_type=kind)
    db.session.add(field)
    db.session.commit()
    return jsonify(field.to_dict()), 201


def _field_fragments(field):
    model = TextEnrichment if field.content_type is ContentType.TEXT else CalendarEnrichment
    return db.session.scalars(select(model).filter_by(enrichment_fragment_type_id=field.id)).all()


def _value_count(field):
    """How many rows have a value in this field (column)."""
    return len({f.enrichment_id for f in _field_fragments(field)})


@bp.patch("/<org>/<event_name>/fields/<uuid:field_id>")
@require_user
def update_field(org, event_name, field_id):
    event = _event_for_admin(org, event_name)
    field = _one(EnrichmentFragmentType, field_id, event, "field")
    hidden = _body().get("hidden")
    if not isinstance(hidden, bool):
        raise ApiError("hidden must be true or false")
    field.hidden = hidden
    db.session.commit()
    return jsonify(field.to_dict())


@bp.delete("/<org>/<event_name>/fields/<uuid:field_id>")
@require_user
def delete_field(org, event_name, field_id):
    """Delete a field (column) and every row's value in it.

    Volunteers link to rows, not fields, so no choices are affected. If any
    row has a value here, the request must say {"cascade": true}.
    """
    event = _event_for_admin(org, event_name)
    field = _one(EnrichmentFragmentType, field_id, event, "field")
    fragments = _field_fragments(field)
    rows = len({f.enrichment_id for f in fragments})
    if rows and not _body().get("cascade"):
        raise_conflict(f"{rows} row(s) have a value in this column", rows)
    for fragment in fragments:
        db.session.delete(fragment)
    db.session.flush()
    db.session.delete(field)
    db.session.commit()
    return jsonify(deleted=True, removed=rows)


# ---------------------------------------------------------------- enrichments


@bp.post("/<org>/<event_name>/enrichment-types/<uuid:type_id>/enrichments")
@require_user
def add_enrichment(org, event_name, type_id):
    event = _event_for_admin(org, event_name)
    et = _one(EnrichmentType, type_id, event, "enrichment type")
    enrichment = Enrichment(enrichment_type=et)
    db.session.add(enrichment)
    db.session.commit()
    return jsonify({"id": str(enrichment.id)}), 201


@bp.put("/<org>/<event_name>/enrichment-types/<uuid:type_id>/values")
@require_user
def update_values(org, event_name, type_id):
    """Body: {"values": {enrichment_id: {field_id: value}}}.

    Text fields take a string ("" clears it). Calendar fields take
    {"start": iso, "end": iso}, or null / both empty to clear it.
    All-or-nothing: if any value is invalid nothing is saved, and the 400
    lists every problem as {"row": enrichment_id, "field": field_id,
    "message": ...} so the page can point at each one.
    """
    event = _event_for_admin(org, event_name)
    et = _one(EnrichmentType, type_id, event, "enrichment type")
    values = _body().get("values")
    if not isinstance(values, dict):
        raise ApiError("values must be an object of {enrichment_id: {field_id: value}}")
    enrichments = {str(e.id): e for e in et.enrichments}
    fields = {str(f.id): f for f in et.fragment_types}

    problems = []
    for enrichment_id, row in values.items():
        enrichment = enrichments.get(enrichment_id)
        if enrichment is None or not isinstance(row, dict):
            problems.append({"row": enrichment_id, "field": None,
                             "message": "this row no longer exists (reload the page)"})
            continue
        for field_id, value in row.items():
            field = fields.get(field_id)
            if field is None:
                problems.append({"row": enrichment_id, "field": field_id,
                                 "message": "this field no longer exists (reload the page)"})
                continue
            try:
                if field.content_type is ContentType.TEXT:
                    _set_text(enrichment, field, value)
                else:
                    _set_calendar(enrichment, field, value)
            except ApiError as err:
                problems.append({"row": enrichment_id, "field": field_id, "message": str(err)})
    if problems:
        # Nothing is committed; the request's session is discarded.
        noun = "value needs" if len(problems) == 1 else "values need"
        raise ApiError(f"{len(problems)} {noun} fixing; nothing was saved", 400, problems=problems)
    db.session.commit()
    return jsonify(updated=len(values))


def _set_text(enrichment, field, value):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError("must be text")
    value = value.strip()
    if len(value) > 255:
        raise ApiError("must be at most 255 characters")
    existing = [f for f in enrichment.text_fragments if f.enrichment_fragment_type_id == field.id]
    if not value:
        for frag in existing:
            db.session.delete(frag)
        return
    if existing:
        existing[0].value = value
        for extra in existing[1:]:
            db.session.delete(extra)
    else:
        db.session.add(TextEnrichment(enrichment=enrichment, enrichment_fragment_type=field, value=value))


def _set_calendar(enrichment, field, value):
    existing = [f for f in enrichment.calendar_fragments if f.enrichment_fragment_type_id == field.id]
    if value is not None and not isinstance(value, dict):
        raise ApiError("needs both a start and an end")
    parts = [] if value is None else [value.get("start"), value.get("end")]
    for part in parts:
        if part is not None and not isinstance(part, str):
            raise ApiError("start and end must be text")
    if not any((part or "").strip() for part in parts):
        for frag in existing:
            db.session.delete(frag)
        return
    start, end = _range(value.get("start"), value.get("end"))
    if existing:
        existing[0].start, existing[0].end = start, end
        for extra in existing[1:]:
            db.session.delete(extra)
    else:
        db.session.add(CalendarEnrichment(
            enrichment=enrichment, enrichment_fragment_type=field, start=start, end=end))


@bp.delete("/<org>/<event_name>/enrichments/<uuid:enrichment_id>")
@require_user
def delete_enrichment(org, event_name, enrichment_id):
    event = _event_for_admin(org, event_name)
    enrichment = _one(Enrichment, enrichment_id, event, "enrichment")
    data = _body()
    links = list(enrichment.offer_enrichments)
    if links and not data.get("cascade") and not data.get("reassign_to"):
        raise_conflict(f"{len(links)} volunteer(s) chose this option", len(links))

    moved = 0
    if links and data.get("reassign_to") and not data.get("cascade"):
        target = _one(Enrichment, _uuid(data["reassign_to"]), event, "target option")
        if target.id == enrichment.id or target.enrichment_type_id != enrichment.enrichment_type_id:
            raise ApiError("choose a different option of the same enrichment type")
        for link in links:
            already = db.session.scalar(select(VolunteerOfferEnrichment).filter_by(
                volunteer_offer_id=link.volunteer_offer_id, enrichment_id=target.id))
            if already is None:
                link.enrichment = target
                moved += 1
            else:
                db.session.delete(link)
                # Moved links leave the collection through the backref; this one
                # must too, or _delete_enrichment_rows deletes it a second time.
                enrichment.offer_enrichments.remove(link)
        db.session.flush()
    _delete_enrichment_rows(enrichment)
    db.session.commit()
    return jsonify(deleted=True, moved=moved)
