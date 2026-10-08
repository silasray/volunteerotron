from datetime import datetime, timedelta, timezone

import pytest

from api.models import User, UTCDateTime, url_name_problem


@pytest.mark.parametrize("name", ["acme", "spring-fair", "a.b_c~d", "x" * 255])
def test_url_name_accepts(name):
    assert url_name_problem(name) is None


@pytest.mark.parametrize("name", ["", "has space", "slash/", ".", "..", "x" * 256, None])
def test_url_name_rejects(name):
    assert url_name_problem(name) is not None


def test_password_length_limits():
    assert User.password_problem("x" * (User.PASSWORD_MIN_LENGTH - 1))
    assert User.password_problem("x" * User.PASSWORD_MIN_LENGTH) is None
    assert User.password_problem("x" * (User.PASSWORD_MAX_LENGTH + 1))


# ---------------------------------------------------------------- UTCDateTime


def test_datetimes_are_stored_as_utc():
    column = UTCDateTime()
    eastern = timezone(timedelta(hours=-5))
    stored = column.process_bind_param(datetime(2026, 11, 1, 9, tzinfo=eastern), None)
    assert stored == datetime(2026, 11, 1, 14, tzinfo=timezone.utc) and stored.tzinfo is timezone.utc
    assert column.process_bind_param(None, None) is None
    with pytest.raises(ValueError, match="naive datetime not allowed"):
        column.process_bind_param(datetime(2026, 11, 1, 9), None)


def test_datetimes_read_back_as_utc():
    column = UTCDateTime()
    # SQLite returns naive values (always stored as UTC); Postgres returns aware ones.
    assert column.process_result_value(datetime(2026, 11, 1, 14), None) == datetime(
        2026, 11, 1, 14, tzinfo=timezone.utc)
    aware = datetime(2026, 11, 1, 9, tzinfo=timezone(timedelta(hours=-5)))
    assert column.process_result_value(aware, None).tzinfo is timezone.utc
    assert column.process_result_value(None, None) is None


@pytest.mark.parametrize("value", ["correct horse battery", None, "pbkdf2:sha256:x"])
def test_password_cant_be_assigned_directly(value):
    with pytest.raises(ValueError, match="set_password"):
        User(name="alice", password=value)


@pytest.mark.parametrize("model", ["Organization", "Event"])
def test_url_names_are_checked_on_the_model_too(model):
    # Whatever path creates one (API, CLI, a migration script), a bad name fails.
    import api.models

    with pytest.raises(ValueError, match="name may contain"):
        getattr(api.models, model)(name="has space", pretty_name="X")
