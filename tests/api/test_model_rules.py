"""Rules the models enforce on every write, whichever code path makes it
(api/models.py's after_flush check): a value (fragment) must belong to a real
row (enrichment) and field (fragment type) of the same enrichment type, and
its class must match the field's content type."""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from api.models import CalendarEnrichment, TextEnrichment

from .conftest import at, field


@pytest.fixture
def slots(event, make_type, make_option):
    etype = make_type(event, name="Slots", fields=(("Label", "text", False), ("When", "calendar", False)))
    return etype, make_option(etype)


def test_value_must_reference_real_rows(db_ctx, slots):
    # The foreign key refuses the INSERT, on SQLite (create_app turns them on)
    # as on Postgres, before the model check gets to look.
    etype, _ = slots
    db_ctx.add(TextEnrichment(enrichment_id=uuid.uuid4(), enrichment_fragment_type_id=field(etype, "Label").id,
                              value="x"))
    with pytest.raises(IntegrityError, match="(?i)foreign key"):
        db_ctx.commit()


def test_sqlite_enforces_foreign_keys(api_app):
    from sqlalchemy import text

    from api.models import db

    with api_app.app_context(), db.engine.connect() as conn:
        if conn.dialect.name != "sqlite":
            pytest.skip("Postgres always enforces them")
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_value_class_must_match_content_type(db_ctx, slots):
    etype, option = slots
    db_ctx.add(TextEnrichment(enrichment=option, enrichment_fragment_type=field(etype, "When"), value="9am"))
    with pytest.raises(ValueError, match="with content type 'calendar', expected 'text'"):
        db_ctx.commit()


def test_value_field_must_be_of_the_rows_type(db_ctx, slots, event, make_type):
    _, option = slots
    other = make_type(event, name="Other", fields=(("When", "calendar", False),))
    db_ctx.add(CalendarEnrichment(enrichment=option, enrichment_fragment_type=field(other, "When"),
                                  start=at(1), end=at(1, 10)))
    with pytest.raises(ValueError, match="from enrichment type"):
        db_ctx.commit()


def test_changing_a_fields_content_type_rechecks_its_values(db_ctx, slots):
    from api.models import ContentType

    etype, option = slots
    db_ctx.add(TextEnrichment(enrichment=option, enrichment_fragment_type=field(etype, "Label"), value="Gate"))
    db_ctx.commit()
    field(etype, "Label").content_type = ContentType.CALENDAR
    with pytest.raises(ValueError, match="expected 'text'"):
        db_ctx.commit()
