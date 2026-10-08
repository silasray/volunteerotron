import pytest

from api.models import User, url_name_problem


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
