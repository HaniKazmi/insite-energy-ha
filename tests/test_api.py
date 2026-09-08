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
    InsiteTwoFactorInvalid,
    InsiteTwoFactorRequired,
    _parse_view_model,
)
from custom_components.insite_energy.const import (
    CHECK_TWO_FACTOR_URL,
    DETAILS_URL,
    LOGIN_URL,
    REGISTRATION_LOGIN_URL,
    RESEND_CODE_URL,
    RESEND_ENABLE_URL,
    TWO_FACTOR_URL,
    VALIDATE_ACCOUNT_URL,
)

# Carries the inline `genericError` script the real page always renders. Its
# text matches the transient-login regex, so a fixture without it lets the
# client agree with the tests that a wrong password raises InsiteAuthError while
# the live site produces an endless retry instead.
LOGIN_PAGE = (
    "<script>var genericError = 'We are unable to process your request at this "
    "time. Please try again later.'; var base64AuthKey = 'login-key';</script>"
    '<form><input name="__RequestVerificationToken" type="hidden" '
    'value="tok-123" /></form>'
)


VERIFY_PAGE = (
    "<script>var base64AuthKey = 'verify-key';</script>"
    '<form id="verifyForm"><input type="checkbox" name="RememberBrowser"></form>'
)

VERIFY_URL = f"{TWO_FACTOR_URL}?email=user%40example.com&isSubsequent=true"


class FakeCookie:
    """A jar entry, which the client reads by key and value alone."""

    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value


class FakeCookieJar:
    """Records what the client puts in and hands the same back."""

    def __init__(self) -> None:
        self._cookies: dict[str, str] = {}

    def update_cookies(self, cookies, response_url=None) -> None:
        self._cookies.update(cookies)

    def __iter__(self):
        return iter(
            FakeCookie(key, value) for key, value in self._cookies.items()
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
        self.cookie_jar = FakeCookieJar()

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


def queue_login_gate(session: FakeSession, sign_in_status: str = "Success") -> None:
    """Queue the two checks the client makes before it posts the login form."""
    session.add(
        "POST",
        VALIDATE_ACCOUNT_URL,
        FakeResponse(VALIDATE_ACCOUNT_URL, body=json.dumps({"ErrorCode": ""})),
    )
    session.add(
        "POST",
        CHECK_TWO_FACTOR_URL,
        FakeResponse(
            CHECK_TWO_FACTOR_URL,
            body=json.dumps({"IsSuccess": True, "signInStatus": sign_in_status}),
        ),
    )


# What a full login costs: the page, the site's own two checks, then the form.
LOGIN_SEQUENCE = [
    ("GET", LOGIN_URL),
    ("POST", VALIDATE_ACCOUNT_URL),
    ("POST", CHECK_TWO_FACTOR_URL),
    ("POST", LOGIN_URL),
]


def queue_successful_login(session: FakeSession, view_model: dict) -> None:
    """Queue everything a full login asks for."""
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))
    queue_login_gate(session)
    session.add(
        "POST", LOGIN_URL, FakeResponse(DETAILS_URL, body=details_page(view_model))
    )


async def test_cold_login_returns_view_model(session, view_model):
    """A first call logs in and reads the viewModel out of the response."""
    queue_successful_login(session, view_model)

    client = InsiteClient(session, "user@example.com", "pw")
    assert await client.async_get_data() == view_model
    assert session.requests == LOGIN_SEQUENCE


async def test_warm_call_skips_the_login(session, view_model):
    """Once authenticated we hit the details page only.

    This is the point of the rework: a login costs 6-30s, and while the cookie
    is valid the login page redirects away and yields no CSRF token, so the
    old code failed outright on the second poll.
    """
    queue_successful_login(session, view_model)
    session.add(
        "GET", DETAILS_URL, FakeResponse(DETAILS_URL, body=details_page(view_model))
    )
    session.add(
        "GET", DETAILS_URL, FakeResponse(DETAILS_URL, body=details_page(view_model))
    )

    client = InsiteClient(session, "user@example.com", "pw")
    await client.async_get_data()
    await client.async_get_data()
    await client.async_get_data()

    assert session.requests == [
        *LOGIN_SEQUENCE,
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
    assert session.requests == [("GET", DETAILS_URL), *LOGIN_SEQUENCE]


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
    queue_login_gate(session)
    session.add("POST", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))

    client = InsiteClient(session, "user@example.com", "wrong")
    with pytest.raises(InsiteAuthError):
        await client.async_get_data()


async def test_lockout_message_is_not_reported_as_bad_credentials(session):
    """A lockout notice in the rendered page keeps polling on the retry ladder.

    Treating it as a bad password stops polls until the user re-enters a
    password that was correct all along.
    """
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))
    queue_login_gate(session)
    session.add(
        "POST",
        LOGIN_URL,
        FakeResponse(
            LOGIN_URL,
            body=LOGIN_PAGE + "<p>Your account is locked out.</p>",
        ),
    )

    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteApiError) as err:
        await client.async_get_data()
    assert not isinstance(err.value, InsiteAuthError)


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
    queue_login_gate(session)
    session.add(
        "POST",
        LOGIN_URL,
        FakeResponse("https://my.insite-energy.co.uk/Account/Terms", body="accept"),
    )

    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteApiError) as err:
        await client.async_get_data()
    assert not isinstance(err.value, InsiteAuthError)


async def test_two_factor_landing_page_asks_for_a_code(session):
    """A login that reaches the code form is an auth failure, not a site fault.

    Retrying it can only ever land on the same form, so it has to stop polling
    and ask the user rather than sit on the update ladder.
    """
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))
    queue_login_gate(session)
    session.add("POST", LOGIN_URL, FakeResponse(TWO_FACTOR_URL, body=VERIFY_PAGE))

    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteTwoFactorRequired):
        await client.async_get_data()


async def test_two_factor_is_recognised_by_the_form_it_renders(session):
    """The code form is honoured wherever the site chooses to serve it.

    Keying only on the known path turns a moved form into "unexpected page",
    which retries for ever instead of asking for the code that would fix it.
    """
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))
    queue_login_gate(session)
    session.add(
        "POST",
        LOGIN_URL,
        FakeResponse(
            "https://my.insite-energy.co.uk/Account/SendCode", body=VERIFY_PAGE
        ),
    )

    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteTwoFactorRequired):
        await client.async_get_data()


def queue_two_factor_request(session: FakeSession) -> None:
    """Queue a login that comes back asking for a code, and the verify page."""
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))
    queue_login_gate(session, sign_in_status="RequiresVerification")
    session.add("GET", VERIFY_URL, FakeResponse(VERIFY_URL, body=VERIFY_PAGE))


async def begin_verification(session: FakeSession) -> InsiteClient:
    """Drive a client to the point where it is waiting for a code."""
    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteTwoFactorRequired):
        await client.async_get_data()
    await client.async_start_two_factor()
    return client


async def test_a_remembered_browser_logs_in_without_a_code(session, view_model):
    """The check is what reads the cookie recording a verified browser.

    Posting the login form without asking it first lands on the code form for
    the whole 45 days that cookie is meant to cover, so the user is asked to
    verify a browser the site already trusts.
    """
    queue_successful_login(session, view_model)

    client = InsiteClient(session, "user@example.com", "pw")
    assert await client.async_get_data() == view_model
    assert ("POST", CHECK_TWO_FACTOR_URL) in session.requests
    assert ("GET", VERIFY_URL) not in session.requests


async def test_a_demanded_code_is_refused_before_the_form_is_posted(session):
    """The form is not worth posting once the check has asked for a code.

    It signs the password in without reading the browser-trust cookie, so it
    can only land back on the code form.
    """
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))
    queue_login_gate(session, sign_in_status="RequiresVerification")

    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteTwoFactorRequired):
        await client.async_get_data()
    assert ("POST", LOGIN_URL) not in session.requests


async def test_a_verified_code_returns_the_account(session, view_model):
    """The whole code exchange lands on the details page."""
    queue_two_factor_request(session)
    session.add(
        "POST",
        TWO_FACTOR_URL,
        FakeResponse(
            TWO_FACTOR_URL,
            body=json.dumps({"IsSuccess": True, "signInStatus": "Success"}),
        ),
    )
    session.add(
        "POST",
        REGISTRATION_LOGIN_URL,
        FakeResponse(REGISTRATION_LOGIN_URL, body=json.dumps({"IsSuccess": True})),
    )
    session.add(
        "GET", DETAILS_URL, FakeResponse(DETAILS_URL, body=details_page(view_model))
    )

    client = await begin_verification(session)
    assert await client.async_submit_two_factor("123456") == view_model
    assert ("GET", VERIFY_URL) in session.requests
    assert ("POST", REGISTRATION_LOGIN_URL) in session.requests


async def test_a_code_cannot_be_asked_for_unprompted(session):
    """Nothing has been checked yet, so there is no verification to further."""
    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteApiError):
        await client.async_start_two_factor()


async def test_a_wrong_password_is_named_before_any_code_is_sent(session):
    """A rejected password must not become an unexplained connection failure.

    The validate call is the only step that says why the site refused, and
    getting it wrong means the user is told to check their network over a typo.
    """
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))
    session.add(
        "POST",
        VALIDATE_ACCOUNT_URL,
        FakeResponse(
            VALIDATE_ACCOUNT_URL,
            body=json.dumps(
                {
                    "ErrorCode": "WrongUserNamePassword",
                    "ErrorDescription": "Your login details are incorrect#1",
                }
            ),
        ),
    )

    client = InsiteClient(session, "user@example.com", "wrong")
    with pytest.raises(InsiteAuthError):
        await client.async_get_data()
    assert ("POST", CHECK_TWO_FACTOR_URL) not in session.requests


async def test_a_locked_account_is_not_named_a_wrong_password(session):
    """A lockout has to stay retryable, not send the user to a reauth prompt."""
    session.add("GET", LOGIN_URL, FakeResponse(LOGIN_URL, body=LOGIN_PAGE))
    session.add(
        "POST",
        VALIDATE_ACCOUNT_URL,
        FakeResponse(
            VALIDATE_ACCOUNT_URL,
            body=json.dumps(
                {
                    "ErrorCode": "locked_out",
                    "ErrorDescription": "User is locked out",
                }
            ),
        ),
    )

    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteApiError) as err:
        await client.async_get_data()
    assert not isinstance(err.value, InsiteAuthError)


async def test_a_wrong_code_is_told_apart_from_a_site_failure(session):
    """A rejected code re-prompts; anything else must not look like one."""
    queue_two_factor_request(session)
    session.add(
        "POST",
        TWO_FACTOR_URL,
        FakeResponse(
            TWO_FACTOR_URL,
            body=json.dumps({"IsSuccess": True, "signInStatus": "Failure"}),
        ),
    )

    client = await begin_verification(session)
    with pytest.raises(InsiteTwoFactorInvalid):
        await client.async_submit_two_factor("000000")


async def test_a_lockout_during_verification_is_not_a_wrong_code(session):
    """Being locked out must not send the user round the code prompt again."""
    queue_two_factor_request(session)
    session.add(
        "POST",
        TWO_FACTOR_URL,
        FakeResponse(
            TWO_FACTOR_URL,
            body=json.dumps({"IsSuccess": True, "signInStatus": "LockedOut"}),
        ),
    )

    client = await begin_verification(session)
    with pytest.raises(InsiteApiError) as err:
        await client.async_submit_two_factor("123456")
    assert not isinstance(err.value, InsiteTwoFactorInvalid)


async def test_a_resend_goes_through_both_site_steps(session):
    """Enabling the resend and asking for one are separate calls.

    Asking for a code without enabling it first is refused, so a resend that
    skips the first call silently sends nothing.
    """
    queue_two_factor_request(session)
    session.add(
        "POST",
        RESEND_ENABLE_URL,
        FakeResponse(
            RESEND_ENABLE_URL,
            body=json.dumps({"IsSuccess": True, "ResetAttemptsCount": "1"}),
        ),
    )
    session.add(
        "POST",
        RESEND_CODE_URL,
        FakeResponse(RESEND_CODE_URL, body=json.dumps({"IsSuccess": True})),
    )

    client = await begin_verification(session)
    await client.async_resend_two_factor()

    assert ("POST", RESEND_ENABLE_URL) in session.requests
    assert ("POST", RESEND_CODE_URL) in session.requests


async def test_a_used_up_resend_allowance_does_not_ask_for_a_code(session):
    """Past the site's limit, asking anyway just fails less clearly."""
    queue_two_factor_request(session)
    session.add(
        "POST",
        RESEND_ENABLE_URL,
        FakeResponse(
            RESEND_ENABLE_URL,
            body=json.dumps({"IsSuccess": False, "Message": "limit reached"}),
        ),
    )

    client = await begin_verification(session)
    with pytest.raises(InsiteApiError):
        await client.async_resend_two_factor()
    assert ("POST", RESEND_CODE_URL) not in session.requests


async def test_a_resend_needs_a_verification_in_progress(session):
    """Without the verify page's key there is nothing to resend against."""
    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteApiError):
        await client.async_resend_two_factor()


async def test_a_code_cannot_be_submitted_without_a_verification(session):
    """The verify endpoint only accepts the key from its own page render."""
    client = InsiteClient(session, "user@example.com", "pw")
    with pytest.raises(InsiteApiError):
        await client.async_submit_two_factor("123456")


async def test_cookies_survive_a_round_trip(session):
    """What is kept across a restart is what the jar is holding."""
    client = InsiteClient(session, "user@example.com", "pw")
    client.load_cookies({"remember": "yes"})
    assert client.dump_cookies() == {"remember": "yes"}


async def test_a_login_marks_its_cookies_worth_keeping(session, view_model):
    """A fresh session cookie is only useful if someone writes it down."""
    queue_successful_login(session, view_model)

    client = InsiteClient(session, "user@example.com", "pw")
    assert not client.cookies_changed
    await client.async_get_data()
    assert client.cookies_changed


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


async def test_non_200_details_falls_back_to_login(session, view_model):
    """A status error on the warm path must not wedge the client.

    Only transport errors used to clear the authenticated flag, so a session
    revoked with a 403 rather than a redirect looped forever on a request
    that could never succeed.
    """
    queue_successful_login(session, view_model)
    session.add("GET", DETAILS_URL, FakeResponse(DETAILS_URL, status=403, body=""))
    queue_successful_login(session, view_model)

    client = InsiteClient(session, "user@example.com", "pw")
    await client.async_get_data()
    assert await client.async_get_data() == view_model
    assert session.requests[-(len(LOGIN_SEQUENCE) + 1) :] == [
        ("GET", DETAILS_URL),
        *LOGIN_SEQUENCE,
    ]


async def test_unparseable_details_falls_back_to_login(session, view_model):
    """Malformed JSON on the warm path also triggers a fresh login."""
    queue_successful_login(session, view_model)
    session.add(
        "GET", DETAILS_URL, FakeResponse(DETAILS_URL, body="var viewModel = {oops;")
    )
    queue_successful_login(session, view_model)

    client = InsiteClient(session, "user@example.com", "pw")
    await client.async_get_data()
    assert await client.async_get_data() == view_model
