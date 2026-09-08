"""API helpers for Insite Energy."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from urllib.parse import quote

import aiohttp
from yarl import URL

from .const import (
    BASE_URL,
    CHECK_TWO_FACTOR_URL,
    DETAILS_PATH,
    DETAILS_URL,
    LOGIN_PATH,
    LOGIN_URL,
    REGISTRATION_LOGIN_URL,
    REQUEST_TIMEOUT,
    RESEND_CODE_URL,
    RESEND_ENABLE_URL,
    TWO_FACTOR_PATH,
    TWO_FACTOR_URL,
    USER_AGENT,
    VALIDATE_ACCOUNT_URL,
    VIEW_MODEL_MARKER,
)

_LOGGER = logging.getLogger(__name__)

_TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*?value="(.*?)"', re.DOTALL
)

# A failed login re-renders the login page, and so do several things that are
# nothing to do with the password. Telling them apart matters more than the
# wording suggests: an auth error raises ConfigEntryAuthFailed, and the
# coordinator does not schedule another poll after one of those, so a lockout
# misread as a bad password stops polling until the user re-enters a password
# that was always correct.
#
# Only the cases below are reclassified. Anything else landing back on the login
# page is still treated as bad credentials, so a genuinely wrong password
# prompts for reauth exactly as before.
_TRANSIENT_LOGIN_RE = re.compile(
    r"locked\s*out"
    r"|too many (?:failed |unsuccessful )?(?:login )?attempts"
    r"|try again (?:later|in a)"
    r"|temporarily (?:unavailable|disabled|suspended)"
    r"|under maintenance|scheduled maintenance",
    re.IGNORECASE,
)

# Every render of the login page carries
# `var genericError = '...Please try again later.'` in an inline script, which
# matches the "try again later" alternative above. Searching the raw HTML
# therefore reads every failed login as transient, which leaves InsiteAuthError
# unreachable and a genuinely wrong password retrying for ever instead of
# prompting for reauth. Only text the site actually renders can carry a reason.
_SCRIPT_RE = re.compile(r"(?is)<script\b[^>]*>.*?</script\s*>")

# Every page that talks to the JSON endpoints carries a per-render key it must
# echo back as HTTP Basic credentials. It is not an account secret and it is not
# interchangeable between renders: the verify page's key is the only one its own
# endpoint accepts, so each page is scraped for its own.
_AUTH_KEY_RE = re.compile(r"base64AuthKey\s*=\s*'([^']*)'")

# The verify page is what a login POST now lands on when the account has 2FA.
# Matched on content as well as path so that a redirect target we have not seen
# still routes to a code prompt rather than to "unexpected page".
_VERIFY_FORM_RE = re.compile(r'id="verifyForm"|name="RememberBrowser"', re.IGNORECASE)


class InsiteClient:
    """Client for the Insite Energy customer portal.

    Logging in takes several seconds and occasionally over thirty, while the
    details page serves the same payload in under a second once the session
    cookie is set. That cookie lasts about twenty minutes and does not slide,
    so we try the cheap request first and fall back to a full login.

    Reusing the session also matters for correctness: while the cookie is
    valid the site redirects the login page to the details page, so a blind
    re-login finds no CSRF token and fails.
    """

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._username = username
        self._password = password
        self._authenticated = False
        # Set while a verification is in flight, holding the verify page's own
        # key. Cleared once a code is accepted.
        self._two_factor_key: str | None = None
        # The login page key the gate last used, kept so that asking for a code
        # does not have to fetch that page a second time.
        self._login_key: str | None = None
        # Raised when a login has put a cookie in the jar that is worth keeping
        # across restarts. The coordinator clears it once it has written them.
        self.cookies_changed = False

    def load_cookies(self, cookies: dict[str, str]) -> None:
        """Seed the jar with cookies kept from a previous run.

        Chief among them the one saying this browser has already been verified,
        which is what keeps a restart from costing the user another code.
        """
        if not cookies:
            return
        self._session.cookie_jar.update_cookies(cookies, URL(BASE_URL))
        _LOGGER.debug("Restored %d cookies for %s", len(cookies), self._username)

    def dump_cookies(self) -> dict[str, str]:
        """Return the jar's cookies, for keeping across restarts."""
        cookies = {cookie.key: cookie.value for cookie in self._session.cookie_jar}
        # Names only: which cookie carries the 45 day browser trust is not
        # documented, so seeing the set is how a failure to stay verified gets
        # diagnosed.
        _LOGGER.debug("Keeping cookies: %s", sorted(cookies))
        return cookies

    async def async_get_data(self) -> dict:
        """Return the account viewModel.

        Raises:
            InsiteAuthError: If the credentials are invalid.
            InsiteApiError: For any other API communication error.
        """
        try:
            if self._authenticated:
                # Any failure on the warm path means the session is unusable,
                # so fall through to a login rather than getting stuck
                # re-issuing a request that will keep failing.
                try:
                    if (view_model := await self._async_fetch_details()) is not None:
                        return view_model
                    _LOGGER.debug("Session no longer valid, logging in again")
                except InsiteApiError as err:
                    _LOGGER.debug("Session unusable (%s), logging in again", err)
                self._authenticated = False

            return await self._async_login()
        except asyncio.TimeoutError as err:
            self._authenticated = False
            raise InsiteApiError("Timed out talking to Insite Energy") from err
        except aiohttp.ClientError as err:
            self._authenticated = False
            raise InsiteApiError(f"Error talking to Insite Energy: {err}") from err

    async def _async_fetch_details(self) -> dict | None:
        """Fetch the details page using the existing session cookie.

        Returns None if the session has expired and a login is needed.
        """
        async with self._session.get(DETAILS_URL, **self._request_kwargs) as response:
            if response.status != 200:
                raise InsiteApiError(
                    f"Failed to fetch account details (Status: {response.status})"
                )
            # An expired session bounces us to /Account/Login?ReturnUrl=...
            if not _is_path(response, DETAILS_PATH):
                return None
            content = await response.text()

        return _parse_view_model(content)

    async def _async_login(self) -> dict:
        """Log in. The login response itself carries the viewModel."""
        async with self._session.get(LOGIN_URL, **self._request_kwargs) as response:
            if response.status != 200:
                raise InsiteApiError(
                    f"Failed to fetch login page (Status: {response.status})"
                )
            # Already-valid cookies redirect the login page to the details page.
            redirected_to_details = _is_path(response, DETAILS_PATH)
            content = await response.text()

        if redirected_to_details:
            if (view_model := _parse_view_model(content)) is None:
                raise InsiteApiError("Redirected away from the login page")
            self._authenticated = True
            return view_model

        token_match = _TOKEN_RE.search(content)
        if not token_match:
            _LOGGER.debug("No CSRF token in login page: %s", content[:500])
            raise InsiteApiError("Failed to find CSRF token")

        await self._async_refuse_if_code_needed(content)

        payload = {
            "__RequestVerificationToken": token_match.group(1),
            "email": self._username,
            "password": self._password,
        }

        async with self._session.post(
            LOGIN_URL, data=payload, **self._request_kwargs
        ) as response:
            if response.status != 200:
                raise InsiteApiError(f"Login failed (Status: {response.status})")
            landed = response.url.path
            on_login = _is_path(response, LOGIN_PATH)
            on_details = _is_path(response, DETAILS_PATH)
            content = await response.text()

        _LOGGER.debug("Login POST landed on %s (%d bytes)", landed, len(content))

        # An account with 2FA never reaches the details page from here: the
        # server accepts the password, finds verification outstanding and
        # redirects to the code form. Only the user can clear that, so it is
        # raised as an auth failure - a poll that keeps landing here is a poll
        # that can never succeed, and retrying it just burns logins.
        if _is_two_factor_page(landed, content):
            raise InsiteTwoFactorRequired(
                "Insite Energy is asking for a verification code"
            )

        # Bad credentials re-render the login page; success lands on the
        # details page. Anything else is a site problem, not a bad password.
        if on_login:
            if (
                transient := _TRANSIENT_LOGIN_RE.search(_SCRIPT_RE.sub(" ", content))
            ) is not None:
                raise InsiteApiError(
                    "Login refused for a reason other than the password "
                    f"({transient.group(0)!r}); will retry"
                )
            raise InsiteAuthError("Invalid username or password")
        if not on_details:
            raise InsiteApiError(f"Unexpected page after login: {landed}")

        if (view_model := _parse_view_model(content)) is None:
            raise InsiteApiError("No account data found in the response")

        self._authenticated = True
        self.cookies_changed = True
        return view_model

    async def _async_refuse_if_code_needed(self, login_page: str) -> None:
        """Run the site's own gate before submitting the login form.

        `/Account/Login` signs the password in without reading `insite_2fa_rb`,
        the cookie recording that this browser has already been verified, so a
        form POST lands on the code form for the whole 45 days that cookie is
        meant to cover. `CheckIsTwoFactorRequired` is the step that reads it,
        which makes it the only way to spend that trust instead of asking the
        user for a fresh code every poll.

        A code is sent only when one is genuinely needed, and that outcome
        becomes ConfigEntryAuthFailed, which stops polling - so this costs one
        email per expiry rather than one per poll.
        """
        if (key_match := _AUTH_KEY_RE.search(login_page)) is None:
            # The POST still recognises the code form, so a login page that
            # stops carrying a key degrades to prompting the user for a code
            # rather than failing outright.
            _LOGGER.warning("No page key on the login page; skipping the 2FA check")
            return

        self._login_key = key_match.group(1)
        await self._async_validate_account(self._login_key)
        sign_in_status = await self._async_check_two_factor(self._login_key)
        _LOGGER.debug(
            "CheckIsTwoFactorRequired for %s -> signInStatus=%s",
            self._username,
            sign_in_status,
        )
        if sign_in_status == "RequiresVerification":
            raise InsiteTwoFactorRequired(
                "Insite Energy is asking for a verification code"
            )

    async def async_start_two_factor(self) -> None:
        """Get a verification code sent, once the gate has asked for one.

        The gate has already run the account and two-factor checks against this
        session, so only the verify page is left. It is one of the two steps
        that sends the email - the site does not say which - and the gate
        performed the other, so between them the code is on its way.

        Leaves the client holding the verify page's own key, which is the only
        one its endpoint accepts.
        """
        if not self._login_key:
            raise InsiteApiError("No verification was asked for")

        with _transport_errors():
            self._two_factor_key = await self._async_page_key(self._verify_page_url)
            _LOGGER.debug("Verify page reached; code should be in the user's inbox")

    async def async_submit_two_factor(self, code: str) -> dict:
        """Verify an emailed code and return the account viewModel.

        `RememberBrowser` is always set: the whole point of asking the user for
        a code is to not have to ask again for another 45 days.
        """
        if not self._two_factor_key:
            raise InsiteApiError("No verification in progress")

        form = aiohttp.FormData()
        # The site's own script encodeURIComponent()s the address before putting
        # it in the form body, so the server is given a percent-encoded address
        # and decodes it itself. Sending the plain address instead is not the
        # same string.
        _add_text_field(form, "Email", _encode_uri_component(self._username))
        _add_text_field(form, "Code", code)
        # Sign-in persistence and browser trust are separate: IsPersistent would
        # extend the *session*, which is not what avoids the next code.
        _add_text_field(form, "IsPersistent", "false")
        _add_text_field(form, "RememberBrowser", "true")

        with _transport_errors():
            _LOGGER.debug(
                "Submitting a %d character code for %s", len(code), self._username
            )
            result = await self._async_post_json(
                TWO_FACTOR_URL, self._two_factor_key, data=form
            )
            sign_in_status = result.get("signInStatus")
            _LOGGER.debug(
                "VerifyTwoFactorAuthentication -> IsSuccess=%s signInStatus=%s"
                " message=%s",
                result.get("IsSuccess"),
                sign_in_status,
                result.get("Message"),
            )

            if not result.get("IsSuccess"):
                raise InsiteApiError(
                    f"Verification refused: {result.get('Message') or 'no reason given'}"
                )
            if sign_in_status == "LockedOut":
                raise InsiteApiError("Account is locked out; try again later")
            if sign_in_status != "Success":
                raise InsiteTwoFactorInvalid(
                    f"Verification code rejected ({sign_in_status})"
                )

            # The site treats this, not the verify call, as the point the
            # session becomes usable; without it the details page bounces back
            # to the login page.
            await self._async_post_json(REGISTRATION_LOGIN_URL, self._two_factor_key)

            if (view_model := await self._async_fetch_details()) is None:
                raise InsiteApiError("Verified, but the session was not accepted")

        self._two_factor_key = None
        self._authenticated = True
        self.cookies_changed = True
        _LOGGER.debug("Two-factor verification complete for %s", self._username)
        return view_model

    async def async_resend_two_factor(self) -> None:
        """Ask for the verification code to be sent again."""
        if not self._two_factor_key:
            raise InsiteApiError("No verification in progress")

        # The site's script double-encodes here: it encodeURIComponent()s the
        # address and then lets jQuery form-encode that, so the '%' arrives
        # escaped in turn. aiohttp encodes the body the same way.
        payload = {"email": _encode_uri_component(self._username)}

        with _transport_errors():
            enabled = await self._async_post_json(
                RESEND_ENABLE_URL, self._two_factor_key, data=payload
            )
            _LOGGER.debug(
                "EnableDisableResend2FACode -> IsSuccess=%s message=%s",
                enabled.get("IsSuccess"),
                enabled.get("Message"),
            )
            if not enabled.get("IsSuccess"):
                raise InsiteApiError(
                    "No more resends available; start the flow again to get a "
                    "fresh code"
                )

            resent = await self._async_post_json(
                RESEND_CODE_URL, self._two_factor_key, data=payload
            )
            _LOGGER.debug(
                "ResendTwoFactorVerification -> IsSuccess=%s message=%s",
                resent.get("IsSuccess"),
                resent.get("Message"),
            )
            if not resent.get("IsSuccess"):
                raise InsiteApiError(
                    f"Resend refused: {resent.get('Message') or 'no reason given'}"
                )

    @property
    def _verify_page_url(self) -> str:
        """The verify page as the site's own script navigates to it."""
        return (
            f"{TWO_FACTOR_URL}?email={_encode_uri_component(self._username)}"
            "&isSubsequent=true"
        )

    async def _async_page_key(self, url: str) -> str:
        """Return the per-render Basic key carried by a page."""
        async with self._session.get(url, **self._request_kwargs) as response:
            if response.status != 200:
                raise InsiteApiError(
                    f"Failed to fetch {url} (Status: {response.status})"
                )
            content = await response.text()
            landed = response.url.path

        _LOGGER.debug("GET %s landed on %s (%d bytes)", url, landed, len(content))
        if (match := _AUTH_KEY_RE.search(content)) is None:
            raise InsiteApiError(f"No page key found on {landed}")
        return match.group(1)

    async def _async_validate_account(self, key: str) -> None:
        """Run the site's own credential and role check.

        It is the only step that names why a login is refused, which is what
        lets a wrong password stay a wrong password here rather than becoming an
        unexplained failure to connect.
        """
        result = await self._async_post_json(
            VALIDATE_ACCOUNT_URL,
            key,
            data={"Email": self._username, "Password": self._password},
        )
        if not (error_code := str(result.get("ErrorCode") or "")):
            return

        description = str(result.get("ErrorDescription") or "")
        _LOGGER.debug(
            "ValidateUserRoleAndConsumerStatus -> %s: %s", error_code, description
        )
        if error_code == "WrongUserNamePassword":
            raise InsiteAuthError("Invalid username or password")
        raise InsiteApiError(
            f"Insite Energy refused the login ({error_code}): {description}"
        )

    async def _async_check_two_factor(self, key: str) -> str:
        """Ask whether this sign-in needs a code, and return its signInStatus."""
        result = await self._async_post_json(
            CHECK_TWO_FACTOR_URL,
            key,
            data={"Email": self._username, "Password": self._password},
        )
        if not result.get("IsSuccess"):
            message = str(result.get("Message") or "no reason given")
            _LOGGER.debug("CheckIsTwoFactorRequired refused: %s", message)
            if re.search(r"incorrect|invalid|do not match", message, re.IGNORECASE):
                raise InsiteAuthError("Invalid username or password")
            raise InsiteApiError(f"Insite Energy refused the check: {message}")
        return str(result.get("signInStatus") or "")

    async def _async_post_json(self, url: str, key: str, data=None) -> dict:
        """POST to one of the JSON endpoints, echoing the page key back."""
        kwargs = self._request_kwargs
        headers = {**kwargs.pop("headers"), "Authorization": f"Basic {key}"}
        async with self._session.post(
            url, data=data, headers=headers, **kwargs
        ) as response:
            if response.status != 200:
                raise InsiteApiError(f"{url} failed (Status: {response.status})")
            # These endpoints are not consistent about their content type, and
            # a refusal is still a 200 with a JSON body explaining itself.
            body = await response.text()

        try:
            result = json.loads(body)
        except json.JSONDecodeError as err:
            _LOGGER.debug("Non-JSON reply from %s: %s", url, body[:300])
            raise InsiteApiError(f"Unreadable reply from {url}") from err

        if not isinstance(result, dict):
            raise InsiteApiError(f"Unexpected reply from {url}")
        return result

    @property
    def _request_kwargs(self) -> dict:
        """Common kwargs for every request."""
        return {
            "headers": {"User-Agent": USER_AGENT},
            "timeout": aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        }


@contextlib.contextmanager
def _transport_errors():
    """Present transport failures as InsiteApiError, the way a poll does."""
    try:
        yield
    except asyncio.TimeoutError as err:
        raise InsiteApiError("Timed out talking to Insite Energy") from err
    except aiohttp.ClientError as err:
        raise InsiteApiError(f"Error talking to Insite Energy: {err}") from err


def _encode_uri_component(value: str) -> str:
    """Escape as the site's own scripts do, so the server sees the same bytes."""
    return quote(value, safe="!*'()")


def _add_text_field(form: aiohttp.FormData, name: str, value: str) -> None:
    """Add a field, keeping the form multipart.

    The site builds these calls from a FormData, so the parts arrive named and
    separate; a urlencoded body is a different request shape. Giving a field an
    explicit content type is what keeps aiohttp from collapsing the form back
    into one.
    """
    form.add_field(name, value, content_type="text/plain")


def _is_two_factor_page(path: str, content: str) -> bool:
    """Return True if a response is the verification code form."""
    return (
        path.casefold() == TWO_FACTOR_PATH.casefold()
        or _VERIFY_FORM_RE.search(content) is not None
    )


def _is_path(response: aiohttp.ClientResponse, path: str) -> bool:
    """Return True if the response landed on the given path."""
    return response.url.path.casefold() == path.casefold()


def _parse_view_model(content: str) -> dict | None:
    """Extract the embedded viewModel JSON, or None if the page has none.

    Decoded with raw_decode rather than a regex so that a brace or semicolon
    inside a string value can't truncate the payload.
    """
    index = content.find(VIEW_MODEL_MARKER)
    if index == -1:
        return None

    try:
        view_model, _ = json.JSONDecoder().raw_decode(
            content, index + len(VIEW_MODEL_MARKER)
        )
    except json.JSONDecodeError as err:
        raise InsiteApiError(f"Failed to parse API response: {err}") from err

    if not isinstance(view_model, dict):
        raise InsiteApiError("Unexpected viewModel payload")

    return view_model


class InsiteApiError(Exception):
    """Error communicating with the Insite Energy API."""


class InsiteAuthError(InsiteApiError):
    """Error indicating invalid authentication credentials."""


class InsiteTwoFactorRequired(InsiteAuthError):
    """The account needs a verification code before it will serve data.

    An auth error, so that polling stops and the user is asked to act, rather
    than a retry ladder that can only ever land on the same code form.
    """


class InsiteTwoFactorInvalid(InsiteAuthError):
    """The verification code was not accepted."""
