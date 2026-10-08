"""Tests that run the pages' JavaScript (web/static/*.js) in a real browser.

A headless Edge (or Chrome) is started once per run with Selenium, which finds
or downloads the matching driver itself. With neither browser installed,
every test here is skipped. All of them carry the `browser` marker, so
`pytest -m "not browser"` leaves them out.

Each test gets the real stack: the API on its own fresh SQLite database (the
api/ factories, make_user, make_window, ..., fill it) and the web app talking
to it over HTTP, both served from background threads on free ports. Nothing
is stubbed.

The browser's time zone is America/New_York and its locale en-US, so times
on the pages are predictable; a test can switch zone with `page.timezone(...)`.
An uncaught JavaScript error on any page fails the test.
"""
import os
import threading

import pytest
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from werkzeug.serving import make_server

from tests.api.conftest import *  # noqa: F401,F403  (the tests/api factories and fixtures)
from tests.world import PASSWORD

TIMEZONE = "America/New_York"
WAIT = 5  # seconds; the pages are tiny and everything is local


def pytest_collection_modifyitems(items):
    here = os.path.dirname(__file__)
    for item in items:
        if str(item.path).startswith(here):
            item.add_marker(pytest.mark.browser)


# ---------------------------------------------------------------- the browser


def _start_browser():
    from selenium import webdriver

    problems = []
    for name, options_cls, driver_cls in (
        ("Edge", webdriver.EdgeOptions, webdriver.Edge),
        ("Chrome", webdriver.ChromeOptions, webdriver.Chrome),
    ):
        options = options_cls()
        for arg in ("--headless=new", "--window-size=1280,1000", "--lang=en-US"):
            options.add_argument(arg)
        options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
        try:
            return driver_cls(options=options)
        except WebDriverException as err:
            problems.append(f"{name}: {err.msg or err}".splitlines()[0])
    pytest.skip("browser tests need Edge or Chrome installed (" + "; ".join(problems) + ")")


@pytest.fixture(scope="session")
def _driver():
    driver = _start_browser()
    yield driver
    driver.quit()


class Page:
    """The browser, pointed at this test's servers, with a few helpers."""

    def __init__(self, driver, base_url):
        self.driver = driver
        self.base_url = base_url

    # navigation
    def open(self, path):
        self.driver.get(self.base_url + path)

    @property
    def path(self):
        url = self.driver.current_url
        return url[len(self.base_url):] if url.startswith(self.base_url) else url

    def timezone(self, zone):
        self.driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": zone})

    # finding
    def find(self, css):
        return self.driver.find_element(By.CSS_SELECTOR, css)

    def find_all(self, css):
        return self.driver.find_elements(By.CSS_SELECTOR, css)

    def texts(self, css):
        return [e.text for e in self.find_all(css)]

    def visible_texts(self, css):
        return [e.text for e in self.find_all(css) if e.is_displayed()]

    def js(self, script, *args):
        return self.driver.execute_script(script, *args)

    # waiting
    def wait(self, condition, message="", timeout=WAIT):
        """Wait until condition() is truthy and return its value."""
        try:
            return WebDriverWait(self.driver, timeout, ignored_exceptions=(AssertionError,)).until(
                lambda _: condition(), message)
        except TimeoutException:
            raise AssertionError(f"timed out waiting: {message}") from None

    def wait_for_text(self, css, text):
        return self.wait(lambda: text in self.find(css).text, f"{text!r} in {css}")

    def wait_for_path(self, path):
        return self.wait(lambda: self.path == path, f"page {path} (at {self.path})")

    # input
    def fill(self, css, value):
        field = self.find(css)
        field.clear()
        field.send_keys(value)
        return field

    def click(self, css):
        self.find(css).click()

    def dialog_open(self, css):
        return self.js("return arguments[0].open", self.find(css))

    # signing in
    def sign_in(self, name, password=PASSWORD):
        self.open("/admin")
        self.fill("#name", name)
        self.fill("#password", password)
        self.find("#password").submit()
        self.wait(lambda: not self.find_all("#password"), "signed in")

    def js_errors(self):
        return [e["message"] for e in self.driver.get_log("browser")
                if e["level"] == "SEVERE" and e["message"].startswith("javascript")]


# ---------------------------------------------------------------- the servers


class _Server:
    def __init__(self, app):
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        # serve_forever checks for shutdown every poll_interval (default 0.5s,
        # which made stopping two servers a second of every test).
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.thread.join()


@pytest.fixture
def live(api_app, monkeypatch):
    """The API and the web app, served over HTTP for this test. Returns the web's base URL."""
    from web import create_app

    api_server = _Server(api_app)
    monkeypatch.setenv("SECRET_KEY", "test-web-secret")
    monkeypatch.setenv("API_URL", api_server.url + "/api")
    web_server = _Server(create_app())
    yield web_server.url
    web_server.stop()
    api_server.stop()


@pytest.fixture
def page(_driver, live):
    _driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
    _driver.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": "en-US"})
    page = Page(_driver, live)
    page.timezone(TIMEZONE)
    _driver.get_log("browser")  # drop anything left from the previous test
    yield page
    errors = page.js_errors()
    _driver.get("about:blank")  # stop the page before its servers go away
    assert errors == [], "JavaScript errors on the page"
