import pytest

from web import api_client


@pytest.fixture
def stack(monkeypatch, web_app, api):
    """The web app talking to a real API app in-process, instead of over HTTP.

    Replaces only the HTTP transport (api_client._http), so everything else in
    api_client runs as in production. Returns the web test client.
    """
    base = web_app.config["API_URL"].removesuffix("/api")

    def http(method, url, headers, body):
        assert url.startswith(base), url
        resp = api.open(url[len(base):], method=method, headers=headers, json=body)
        return resp.status_code, resp.get_data(as_text=True)

    monkeypatch.setattr(api_client, "_http", http)
    return web_app.test_client()
