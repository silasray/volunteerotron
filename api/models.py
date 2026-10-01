import enum
import uuid
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, select
from sqlalchemy.orm import Session, validates
from sqlalchemy.types import DateTime, TypeDecorator
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetime, stored as UTC.

    SQLite discards tzinfo, so values are normalized to UTC on write and
    tagged as UTC on read. Naive datetimes are rejected.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed; attach a timezone")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _utcnow():
    return datetime.now(timezone.utc)


def _same_as_created_on(context):
    return context.get_current_parameters()["created_on"]


class TimestampMixin:
    """created_on is set on insert; updated_on on insert and on every update of the row."""

    created_on = db.Column(UTCDateTime, nullable=False, default=_utcnow)
    updated_on = db.Column(
        UTCDateTime, nullable=False, default=_same_as_created_on, onupdate=_utcnow
    )

    def touch(self):
        """Mark the row updated when only related rows changed."""
        self.updated_on = _utcnow()

    def timestamps_dict(self):
        return {
            "created_on": self.created_on.isoformat(),
            "updated_on": self.updated_on.isoformat(),
        }


class Organization(db.Model):
    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(255), unique=True, nullable=False)
    pretty_name = db.Column(db.String(255), nullable=False)

    events = db.relationship("Event", back_populates="organization")
    memberships = db.relationship("UserOrganization", back_populates="organization")

    def to_dict(self):
        return {"id": str(self.id), "name": self.name, "pretty_name": self.pretty_name}


class User(db.Model):
    # "user" is reserved in Postgres; avoid needing to quote it in raw SQL.
    __tablename__ = "app_user"

    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(255), unique=True, nullable=False)
    # Holds a salted scrypt hash (~160 chars), never the plain password. Hashing
    # happens only here in the API; use set_password()/check_password().
    password = db.Column(db.String(255), nullable=False)
    is_superuser = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    memberships = db.relationship("UserOrganization", back_populates="user")

    PASSWORD_HASH_METHOD = "scrypt"

    @validates("password")
    def _require_hash(self, key, value):
        # Guard against a plain password being assigned directly.
        if not isinstance(value, str) or not value.startswith(self.PASSWORD_HASH_METHOD + ":"):
            raise ValueError("password must be set with set_password(), not assigned directly")
        return value

    def set_password(self, plain):
        self.password = generate_password_hash(plain, method=self.PASSWORD_HASH_METHOD)

    def check_password(self, plain):
        return check_password_hash(self.password, plain)

    def to_dict(self):
        # The password hash is deliberately left out.
        return {"id": str(self.id), "name": self.name, "is_superuser": self.is_superuser}


class UserOrganization(db.Model):
    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    user_id = db.Column(db.Uuid, db.ForeignKey("app_user.id"), nullable=False, index=True)
    organization_id = db.Column(
        db.Uuid, db.ForeignKey("organization.id"), nullable=False, index=True
    )
    is_admin = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    user = db.relationship("User", back_populates="memberships")
    organization = db.relationship("Organization", back_populates="memberships")

    __table_args__ = (
        db.UniqueConstraint("user_id", "organization_id", name="uq_user_organization_user_org"),
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "organization_id": str(self.organization_id),
            "is_admin": self.is_admin,
        }


class Event(db.Model):
    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(255), nullable=False)
    pretty_name = db.Column(db.String(255), nullable=False)
    organization_id = db.Column(
        db.Uuid, db.ForeignKey("organization.id"), nullable=False, index=True
    )

    __table_args__ = (
        db.UniqueConstraint("organization_id", "name", name="uq_event_organization_name"),
    )

    organization = db.relationship("Organization", back_populates="events")
    windows = db.relationship("VolunteerWindow", back_populates="event")
    offers = db.relationship("VolunteerOffer", back_populates="event")
    enrichment_types = db.relationship("EnrichmentType", back_populates="event")

    def to_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "pretty_name": self.pretty_name,
            "organization_id": str(self.organization_id),
        }


class VolunteerWindow(db.Model):
    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    event_id = db.Column(db.Uuid, db.ForeignKey("event.id"), nullable=False, index=True)
    start = db.Column(UTCDateTime, nullable=False)
    end = db.Column(UTCDateTime, nullable=False)

    event = db.relationship("Event", back_populates="windows")
    window_offers = db.relationship("VolunteerWindowOffer", back_populates="volunteer_window")

    __table_args__ = (
        db.CheckConstraint('start <= "end"', name="ck_volunteer_window_start_before_end"),
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "event_id": str(self.event_id),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


class VolunteerOffer(TimestampMixin, db.Model):
    # updated_on tracks changes to volunteer-provided data only. Admin actions on
    # an offer are recorded in their own table and must not touch() it.
    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    # email (with event) is the volunteer's identity. name is optional and not
    # unique; it only exists for friendlier messaging. Blank is stored as "".
    email = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=False, default="", server_default="")
    event_id = db.Column(db.Uuid, db.ForeignKey("event.id"), nullable=False, index=True)

    event = db.relationship("Event", back_populates="offers")
    window_offers = db.relationship("VolunteerWindowOffer", back_populates="volunteer_offer")
    offer_enrichments = db.relationship(
        "VolunteerOfferEnrichment", back_populates="volunteer_offer"
    )

    __table_args__ = (
        db.UniqueConstraint("event_id", "email", name="uq_volunteer_offer_event_email"),
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "email": self.email,
            "name": self.name,
            "event_id": str(self.event_id),
            **self.timestamps_dict(),
        }


class VolunteerWindowOffer(db.Model):
    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    volunteer_offer_id = db.Column(
        db.Uuid, db.ForeignKey("volunteer_offer.id"), nullable=False, index=True
    )
    volunteer_window_id = db.Column(
        db.Uuid, db.ForeignKey("volunteer_window.id"), nullable=False, index=True
    )
    # iCalendar UID (RFC 5545) for this offer's calendar event. Must stay
    # stable for the event's lifetime so updates/cancels match the original.
    uid = db.Column(
        db.String(255), unique=True, nullable=False, default=lambda: str(uuid.uuid4())
    )
    # iCalendar SEQUENCE: bump on every significant change to the event so
    # clients accept the update. Gaps are allowed; it only has to increase.
    sequence = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # Unchecking a window cancels rather than deletes, so the UID survives for
    # an iCalendar CANCEL and is reused if the volunteer re-selects the window.
    cancelled = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    volunteer_offer = db.relationship("VolunteerOffer", back_populates="window_offers")
    volunteer_window = db.relationship("VolunteerWindow", back_populates="window_offers")
    response = db.relationship(
        "VolunteerOfferResponse", back_populates="volunteer_window_offer", uselist=False
    )

    __table_args__ = (
        db.UniqueConstraint(
            "volunteer_offer_id",
            "volunteer_window_id",
            name="uq_volunteer_window_offer_offer_window",
        ),
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "volunteer_offer_id": str(self.volunteer_offer_id),
            "volunteer_window_id": str(self.volunteer_window_id),
            "uid": self.uid,
            "sequence": self.sequence,
            "cancelled": self.cancelled,
        }


class VolunteerOfferResponse(TimestampMixin, db.Model):
    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    volunteer_window_offer_id = db.Column(
        db.Uuid, db.ForeignKey("volunteer_window_offer.id"), nullable=False, unique=True
    )
    accepted = db.Column(db.Boolean, nullable=False)

    volunteer_window_offer = db.relationship("VolunteerWindowOffer", back_populates="response")

    def to_dict(self):
        return {
            "id": str(self.id),
            "volunteer_window_offer_id": str(self.volunteer_window_offer_id),
            "accepted": self.accepted,
            **self.timestamps_dict(),
        }


def _enum_column(enum_cls, name):
    """Store the enum's values (not member names), enforced by a CHECK on SQLite."""
    return db.Enum(
        enum_cls,
        name=name,
        values_callable=lambda cls: [m.value for m in cls],
        create_constraint=True,
    )


class VolunteerInteraction(enum.Enum):
    MULTISELECT = "multiselect"
    SELECT = "select"


class FragmentType(enum.Enum):
    TEXT = "text"
    CALENDAR = "calendar"


class EnrichmentType(db.Model):
    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(255), nullable=False)
    event_id = db.Column(db.Uuid, db.ForeignKey("event.id"), nullable=False, index=True)
    volunteer_interaction = db.Column(
        _enum_column(VolunteerInteraction, "volunteer_interaction"), nullable=False
    )

    event = db.relationship("Event", back_populates="enrichment_types")
    fragment_types = db.relationship("EnrichmentFragmentType", back_populates="enrichment_type")
    enrichments = db.relationship("Enrichment", back_populates="enrichment_type")

    def to_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "event_id": str(self.event_id),
            "volunteer_interaction": self.volunteer_interaction.value,
        }


class EnrichmentFragmentType(db.Model):
    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(255), nullable=False)
    hidden = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    enrichment_type_id = db.Column(
        db.Uuid, db.ForeignKey("enrichment_type.id"), nullable=False, index=True
    )
    fragment_type = db.Column(_enum_column(FragmentType, "fragment_type"), nullable=False)

    enrichment_type = db.relationship("EnrichmentType", back_populates="fragment_types")

    def to_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "hidden": self.hidden,
            "enrichment_type_id": str(self.enrichment_type_id),
            "fragment_type": self.fragment_type.value,
        }


class Enrichment(db.Model):
    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    enrichment_type_id = db.Column(
        db.Uuid, db.ForeignKey("enrichment_type.id"), nullable=False, index=True
    )

    enrichment_type = db.relationship("EnrichmentType", back_populates="enrichments")
    text_fragments = db.relationship("TextEnrichment", back_populates="enrichment")
    calendar_fragments = db.relationship("CalendarEnrichment", back_populates="enrichment")
    offer_enrichments = db.relationship("VolunteerOfferEnrichment", back_populates="enrichment")

    def to_dict(self):
        return {"id": str(self.id), "enrichment_type_id": str(self.enrichment_type_id)}


class TextEnrichment(db.Model):
    fragment_kind = FragmentType.TEXT

    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    enrichment_id = db.Column(
        db.Uuid, db.ForeignKey("enrichment.id"), nullable=False, index=True
    )
    enrichment_fragment_type_id = db.Column(
        db.Uuid, db.ForeignKey("enrichment_fragment_type.id"), nullable=False, index=True
    )
    value = db.Column(db.String(255), nullable=False)

    enrichment = db.relationship("Enrichment", back_populates="text_fragments")
    enrichment_fragment_type = db.relationship("EnrichmentFragmentType")

    def to_dict(self):
        return {
            "id": str(self.id),
            "enrichment_id": str(self.enrichment_id),
            "enrichment_fragment_type_id": str(self.enrichment_fragment_type_id),
            "value": self.value,
        }


class CalendarEnrichment(db.Model):
    fragment_kind = FragmentType.CALENDAR

    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    enrichment_id = db.Column(
        db.Uuid, db.ForeignKey("enrichment.id"), nullable=False, index=True
    )
    enrichment_fragment_type_id = db.Column(
        db.Uuid, db.ForeignKey("enrichment_fragment_type.id"), nullable=False, index=True
    )
    start = db.Column(UTCDateTime, nullable=False)
    end = db.Column(UTCDateTime, nullable=False)

    enrichment = db.relationship("Enrichment", back_populates="calendar_fragments")
    enrichment_fragment_type = db.relationship("EnrichmentFragmentType")

    __table_args__ = (
        db.CheckConstraint('start <= "end"', name="ck_calendar_enrichment_start_before_end"),
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "enrichment_id": str(self.enrichment_id),
            "enrichment_fragment_type_id": str(self.enrichment_fragment_type_id),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


class VolunteerOfferEnrichment(TimestampMixin, db.Model):
    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    volunteer_offer_id = db.Column(
        db.Uuid, db.ForeignKey("volunteer_offer.id"), nullable=False, index=True
    )
    enrichment_id = db.Column(
        db.Uuid, db.ForeignKey("enrichment.id"), nullable=False, index=True
    )

    volunteer_offer = db.relationship("VolunteerOffer", back_populates="offer_enrichments")
    enrichment = db.relationship("Enrichment", back_populates="offer_enrichments")

    __table_args__ = (
        db.UniqueConstraint(
            "volunteer_offer_id",
            "enrichment_id",
            name="uq_volunteer_offer_enrichment_offer_enrichment",
        ),
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "volunteer_offer_id": str(self.volunteer_offer_id),
            "enrichment_id": str(self.enrichment_id),
            **self.timestamps_dict(),
        }


_FRAGMENT_CLASSES = (TextEnrichment, CalendarEnrichment)


def _check_fragment(session, fragment):
    """A fragment's type must match its class and belong to its enrichment's type."""
    label = f"{type(fragment).__name__} {fragment.id}"
    fragment_type = session.get(EnrichmentFragmentType, fragment.enrichment_fragment_type_id)
    enrichment = session.get(Enrichment, fragment.enrichment_id)
    if fragment_type is None or enrichment is None:
        raise ValueError(f"{label} references a missing enrichment or fragment type")
    if fragment_type.fragment_type is not fragment.fragment_kind:
        raise ValueError(
            f"{label} uses fragment type {fragment_type.id} of kind "
            f"{fragment_type.fragment_type.value!r}, expected {fragment.fragment_kind.value!r}"
        )
    if fragment_type.enrichment_type_id != enrichment.enrichment_type_id:
        raise ValueError(
            f"{label} uses fragment type {fragment_type.id} from enrichment type "
            f"{fragment_type.enrichment_type_id}, but its enrichment {enrichment.id} "
            f"is of enrichment type {enrichment.enrichment_type_id}"
        )


@event.listens_for(Session, "after_flush")
def _validate_enrichment_fragments(session, flush_context):
    """Reject fragment/type mismatches before the transaction can commit.

    Runs after the flush so foreign keys are populated however they were set
    (relationship or raw id). Raising here rolls the flush back. Changes to a
    fragment type or an enrichment re-check every fragment that depends on it.
    """
    fragments = set()
    with session.no_autoflush:
        for obj in (*session.new, *session.dirty):
            if isinstance(obj, _FRAGMENT_CLASSES):
                fragments.add(obj)
            elif isinstance(obj, EnrichmentFragmentType):
                for cls in _FRAGMENT_CLASSES:
                    fragments.update(
                        session.scalars(select(cls).filter_by(enrichment_fragment_type_id=obj.id))
                    )
            elif isinstance(obj, Enrichment):
                for cls in _FRAGMENT_CLASSES:
                    fragments.update(session.scalars(select(cls).filter_by(enrichment_id=obj.id)))

        for fragment in fragments - set(session.deleted):
            _check_fragment(session, fragment)
