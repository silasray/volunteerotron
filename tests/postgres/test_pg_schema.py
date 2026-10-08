"""The migrations, on the database they're written for.

Production's schema comes only from `alembic upgrade head` (AUTO_CREATE_TABLES=0),
so the models and migrations must agree, every migration must apply and revert,
and the constraints the models declare must exist in the real database.
"""
import logging

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from api import MIGRATIONS_DIR
from api.models import db

from .conftest import at, migrate


def revisions():
    """Every revision id, oldest first."""
    script = ScriptDirectory(MIGRATIONS_DIR)
    return [r.revision for r in reversed(list(script.walk_revisions()))]


def schema_diff(app):
    with app.app_context(), db.engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": True})
        return compare_metadata(context, db.metadata)


def tables(app):
    with app.app_context():
        return set(inspect(db.engine).get_table_names()) - {"alembic_version"}


def enum_types(app):
    with app.app_context(), db.engine.connect() as conn:
        return set(conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'")).scalars())


def current_revision(app):
    with app.app_context(), db.engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


# ---------------------------------------------------------------- models and migrations agree


def test_migrations_match_the_models(api_app):
    # A non-empty diff means a model changed without a migration (or the reverse).
    assert schema_diff(api_app) == []


def test_template_is_at_head(api_app):
    assert current_revision(api_app) == revisions()[-1]


def test_migrate_command_leaves_app_loggers_enabled(api_app, monkeypatch):
    # The API Lambda that ran "migrate" stays warm and serves requests; its
    # loggers must still work afterwards.
    import lambda_handlers

    loggers = [api_app.logger, logging.getLogger("flask_limiter")]  # exist before, as when warm
    monkeypatch.setattr(lambda_handlers, "_apps", {"api": api_app})
    migrate(api_app, "-1", down=True)
    assert lambda_handlers.api_handler({"command": "migrate"}, None)["ok"] is True
    assert [lg.disabled for lg in loggers] == [False, False]


# ---------------------------------------------------------------- every migration reverts


def test_downgrade_to_base_removes_everything(api_app):
    migrate(api_app, "base", down=True)
    assert tables(api_app) == set()
    assert enum_types(api_app) == set()
    migrate(api_app)
    assert schema_diff(api_app) == []


@pytest.mark.parametrize("revision", revisions()[1:])
def test_each_migration_reverts_and_reapplies(api_app, revision):
    previous = revisions()[revisions().index(revision) - 1]
    migrate(api_app, revision, down=True)  # from head (a no-op for the latest)
    assert current_revision(api_app) == revision
    migrate(api_app, "-1", down=True)
    assert current_revision(api_app) == previous
    migrate(api_app, revision)
    assert current_revision(api_app) == revision
    migrate(api_app)
    assert schema_diff(api_app) == []


def test_content_type_rename_keeps_data(api_app, event, make_type):
    # The rename migration renames a native enum type and its column in place.
    etype = make_type(event, fields=(("Label", "text", False), ("When", "calendar", False)))
    rename = "6596b3d7e49b"
    before_rename = revisions()[revisions().index(rename) - 1]

    def kinds(column):
        with api_app.app_context(), db.engine.connect() as conn:
            return sorted(conn.execute(text(
                f"SELECT name, {column}::text FROM enrichment_fragment_type WHERE enrichment_type_id = :t"),
                {"t": etype.id}).all())

    db.session.remove()
    migrate(api_app, before_rename, down=True)
    assert "fragment_type" in enum_types(api_app)
    assert kinds("fragment_type") == [("Label", "text"), ("When", "calendar")]
    migrate(api_app)
    assert "content_type" in enum_types(api_app) and "fragment_type" not in enum_types(api_app)
    assert kinds("content_type") == [("Label", "text"), ("When", "calendar")]


# ---------------------------------------------------------------- constraints exist in the database


def insert_raw(app, sql, **params):
    with app.app_context(), db.engine.begin() as conn:
        conn.execute(text(sql), params)


def test_window_must_not_end_before_it_starts(api_app, event):
    with pytest.raises(IntegrityError, match="ck_volunteer_window_start_before_end"):
        insert_raw(api_app, 'INSERT INTO volunteer_window (id, event_id, start, "end")'
                   " VALUES (gen_random_uuid(), :e, :s, :end)", e=event.id, s=at(2), end=at(1))


def test_calendar_value_must_not_end_before_it_starts(api_app, event, make_type, make_option):
    etype = make_type(event, fields=(("When", "calendar", False),))
    option = make_option(etype)
    with pytest.raises(IntegrityError, match="ck_calendar_enrichment_start_before_end"):
        insert_raw(api_app, "INSERT INTO calendar_enrichment"
                   ' (id, enrichment_id, enrichment_fragment_type_id, start, "end")'
                   " VALUES (gen_random_uuid(), :o, :f, :s, :end)",
                   o=option.id, f=etype.fragment_types[0].id, s=at(2), end=at(1))


def test_enums_reject_unknown_values(api_app, event):
    with pytest.raises(Exception, match="invalid input value for enum volunteer_interaction"):
        insert_raw(api_app, "INSERT INTO enrichment_type (id, event_id, name, volunteer_interaction)"
                   " VALUES (gen_random_uuid(), :e, 'X', 'sometimes')", e=event.id)


def test_times_round_trip_as_utc(api, api_app, event, make_user, add_member, auth):
    # Stored as timestamptz; an offset in the request comes back as the same instant in UTC.
    user = make_user("admin")
    add_member(user, event.organization, is_admin=True)
    resp = api.post("/api/admin/event-config/acme/spring-fair/windows", headers=auth(user),
                    json={"start": "2026-11-01T09:00:00-05:00", "end": "2026-11-01T10:30:00-05:00"})
    assert resp.status_code == 201
    assert (resp.json["start"], resp.json["end"]) == ("2026-11-01T14:00:00+00:00", "2026-11-01T15:30:00+00:00")
    with api_app.app_context(), db.engine.connect() as conn:
        assert conn.execute(text("SELECT data_type FROM information_schema.columns"
                                 " WHERE table_name = 'volunteer_window' AND column_name = 'start'")
                            ).scalar() == "timestamp with time zone"
