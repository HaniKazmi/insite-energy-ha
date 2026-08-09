"""API helpers for Insite Energy."""
from __future__ import annotations

import asyncio
import json
import logging
import re

import aiohttp

from .const import (
    DETAILS_PATH,
    DETAILS_URL,
    LOGIN_PATH,
    LOGIN_URL,
    REQUEST_TIMEOUT,
    USER_AGENT,
    VIEW_MODEL_MARKER,
)

_LOGGER = logging.getLogger(__name__)

_TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*?value="(.*?)"', re.DOTALL
)


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

    async def async_get_data(self) -> dict:
        """Return the account viewModel.

        Raises:
            InsiteAuthError: If the credentials are invalid.
            InsiteApiError: For any other API communication error.
        """
        try:
            if self._authenticated:
                if (view_model := await self._async_fetch_details()) is not None:
                    return view_model
                _LOGGER.debug("Session no longer valid, logging in again")
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
            # Bad credentials re-render the login page; success lands on the
            # details page. Anything else is a site problem, not a bad password.
            if _is_path(response, LOGIN_PATH):
                raise InsiteAuthError("Invalid username or password")
            if not _is_path(response, DETAILS_PATH):
                raise InsiteApiError(
                    f"Unexpected page after login: {response.url.path}"
                )
            content = await response.text()

        if (view_model := _parse_view_model(content)) is None:
            raise InsiteApiError("No account data found in the response")

        self._authenticated = True
        return view_model

    @property
    def _request_kwargs(self) -> dict:
        """Common kwargs for every request."""
        return {
            "headers": {"User-Agent": USER_AGENT},
            "timeout": aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        }


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
