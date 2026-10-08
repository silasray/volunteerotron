import pytest

from web import api_client


class FakeApi:
    """Stands in for web.api_client.call: canned responses, recorded calls.

    fake_api.on("POST", "/auth/login", 200, {...}) sets a response; any call
    without one fails the test, so every API dependency of a page is explicit.
    """

    def __init__(self):
        self.responses = {}
        self.calls = []

    def on(self, method, path, status=200, body=None):
        self.responses[(method, path)] = (status, {} if body is None else body)

    def __call__(self, method, path, token=None, json=None):
        self.calls.append({"method": method, "path": path, "token": token, "json": json})
        try:
            return self.responses[(method, path)]
        except KeyError:
            raise AssertionError(f"unexpected API call: {method} {path}") from None


@pytest.fixture
def fake_api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(api_client, "call", fake)
    return fake


@pytest.fixture
def csrf(web):
    """A CSRF token placed in the test client's session, for form posts."""
    with web.session_transaction() as session:
        session["csrf"] = "test-csrf"
    return "test-csrf"
