"""Tests for the Insite Energy client."""
from __future__ import annotations

import json

import aiohttp
import pytest
from yarl import URL

from custom_components.insite_energy.api import (
    InsiteApiError,
    InsiteAuthError,
    InsiteClient,
    _parse_view_model,
)
from custom_components.insite_energy.const import DETAILS_URL, LOGIN_URL

LOGIN_PAGE = (
    '<form><input name="__RequestVerificationToken" type="hidden" '
    'value="tok-123" /></form>'
)


def details_page(view_model: dict) -> str:
    """Render a details page carrying the given viewModel."""
    return f"<html><script>var viewModel = {json.dumps(view_model)};</script></html>"


class FakeResponse:
    """Stands in for a ClientResponse, with a settable final URL.

    aioresponses doesn't work against the pinned aiohttp, and the client's
    auth detection turns on the URL it *landed* on after redirects, so the
    fake needs to control that directly.
    """

    def __init__(self, final_url: str, status: int = 200, body: str = "") -> None:
        self.url = URL(final_url)
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class FakeSession:
    """Serves queued responses and records the request sequence."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self._queues: dict[tuple[str, str], list] = {}

    def add(self, method: str, url: str, response) -> None:
        self._queues.setdefault((method, url), []).append(response)

    def get(self, url: str, **kwargs):
        return self._take("GET", url)

    def post(self, url: str, **kwargs):
        return self._take("POST", url)

    def _take(self, method: str, url: str):
        self.requests.append((method, url))
        queue = self._queues.get((method, url))
        if not queue:
            raise AssertionError(f"unexpected request: {method} {url}")
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def session() -> FakeSession:
    """A fake session with no responses queued."""
    return FakeSession()


def queue_successful_login(session: FakeSession, view_model: dict) -> None:
    """Queue the GET+POST pair for a full login."""
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))
    session.add(
        "POST", LOGIN_URL, FakeResponse(DETAILS_URL, body=details_page(view_model))
    )


async def test_cold_login_returns_view_model(session, view_model):
    """A first call logs in and reads the viewModel out of the response."""
    queue_successful_login(session, view_model)

    client = InsiteClient(session, "user@example.com", "pw")
    assert await client.async_get_data() == view_model
    assert session.requests == [("GET", LOGIN_URL), ("POST", LOGIN_URL)]


async def test_warm_call_skips_the_login(session, view_model):
    """Once authenticated we hit the details page only.

    This is the point of the rework: a login costs 6-30s, and while the cookie
    is valid the login page redirects away and yields no CSRF token, so the
    old code failed outright on the second poll.
    """
    queue_successful_login(session, view_model)
    session.add("GET", DETAILS_URL, FakeResponse(DETAILS_URL, body=details_page(view_model)))
    session.add("GET", DETAILS_URL, FakeResponse(DETAILS_URL, body=details_page(view_model)))

    client = InsiteClient(session, "user@example.com", "pw")
    await client.async_get_data()
    await client.async_get_data()
    await client.async_get_data()

    assert session.requests == [
        ("GET", LOGIN_URL),
        ("POST", LOGIN_URL),
        ("GET", DETAILS_URL),
        ("GET", DETAILS_URL),
    ]


async def test_expired_session_falls_back_to_login(session, view_model):
    """A bounce to the login page triggers a fresh login."""
    # Logged out, /Customer/Details lands on /Account/Login?ReturnUrl=...
    session.add(
        "GET",
        DETAILS_URL,
        FakeResponse(f"{LOGIN_URL}?ReturnUrl=/Customer/Details", body=LOGIN_PAGE),
    )
    queue_successful_login(session, view_model)

    client = InsiteClient(session, "user@example.com", "pw")
    client._authenticated = True  # stale
    assert await client.async_get_data() == view_model
    assert session.requests == [
        ("GET", DETAILS_URL),
        ("GET", LOGIN_URL),
        ("POST", LOGIN_URL),
    ]


async def test_already_logged_in_login_page_redirect(session, view_model):
    """A login page that redirects to details is used, not treated as an error."""
    session.add(
        "GET", LOGIN_URL, FakeResponse(DETAILS_URL, body=details_page(view_model))
    )

    client = InsiteClient(session, "user@example.com", "pw")
    assert await client.async_get_data() == view_model


async def test_bad_password_raises_auth_error(session):
    """A login that lands back on the login page means bad credentials."""
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))
    session.add("POST", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))

    client = InsiteClient(session, "user@example.com", "wrong")
    with pytest.raises(InsiteAuthError):
        await client.async_get_data()


async def test_outage_is_not_reported_as_bad_credentials(session):
    """A 503 must not look like an invalid password."""
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, status=503, body="maintenance"))

    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteApiError) as err:
        await client.async_get_data()
    assert not isinstance(err.value, InsiteAuthError)


async def test_unexpected_landing_page_is_not_an_auth_error(session):
    """Being redirected somewhere unknown is a site problem, not a bad password."""
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))
    session.add(
        "POST",
        LOGIN_URL,
        FakeResponse("https://my.insite-energy.co.uk/Account/Terms", body="accept"),
    )

    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteApiError) as err:
        await client.async_get_data()
    assert not isinstance(err.value, InsiteAuthError)


async def test_connection_error_is_wrapped(session):
    """Transport failures surface as InsiteApiError, not raw aiohttp errors."""
    session.add("GET", LOGIN_URL, aiohttp.ClientConnectionError("boom"))

    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteApiError):
        await client.async_get_data()


async def test_transport_error_clears_authenticated_flag(session, view_model):
    """After a network failure the next call must not assume a live session."""
    queue_successful_login(session, view_model)
    session.add("GET", DETAILS_URL, aiohttp.ClientConnectionError("boom"))
    queue_successful_login(session, view_model)

    client = InsiteClient(session, "user@example.com", "pw")
    await client.async_get_data()
    with pytest.raises(InsiteApiError):
        await client.async_get_data()
    assert await client.async_get_data() == view_model


async def test_missing_csrf_token_is_an_api_error(session):
    """A login page with no token is reported, not silently retried."""
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body="<html>nope</html>"))

    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteApiError, match="CSRF"):
        await client.async_get_data()


def test_parse_view_model_handles_braces_in_strings():
    """raw_decode must not truncate where the old non-greedy regex would."""
    payload = {"Address": "Flat 1};\nLondon", "CreditAccountBalance": "-47.53"}
    assert _parse_view_model(details_page(payload)) == payload


def test_parse_view_model_missing_returns_none():
    """A page with no viewModel is reported as absent, not as an error."""
    assert _parse_view_model("<html>nothing here</html>") is None


def test_parse_view_model_invalid_json_raises():
    """Malformed JSON is a hard error."""
    with pytest.raises(InsiteApiError):
        _parse_view_model("var viewModel = {broken;")
