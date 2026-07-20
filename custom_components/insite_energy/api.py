"""API helpers for Insite Energy."""
from __future__ import annotations

import json
import logging
import re

import aiohttp

from .const import LOGIN_URL, USER_AGENT

_LOGGER = logging.getLogger(__name__)


async def async_login_and_fetch(
    session: aiohttp.ClientSession, username: str, password: str
) -> dict:
    """Log in to Insite Energy and return the viewModel data.

    Raises:
        InsiteAuthError: If the credentials are invalid.
        InsiteApiError: For any other API communication error.
    """
    headers = {"User-Agent": USER_AGENT}

    # 1. Fetch CSRF token from the login page
    async with session.get(LOGIN_URL, headers=headers) as response:
        if response.status != 200:
            raise InsiteApiError(
                f"Failed to fetch login page (Status: {response.status})"
            )
        html = await response.text()
        token_match = re.search(
            r'name="__RequestVerificationToken"[^>]*?value="(.*?)"',
            html,
            re.DOTALL,
        )
        if not token_match:
            _LOGGER.error("Failed to find CSRF token in HTML: %s", html[:500])
            raise InsiteApiError("Failed to find CSRF token")
        token = token_match.group(1)

    # 2. Perform login
    payload = {
        "__RequestVerificationToken": token,
        "email": username,
        "password": password,
    }

    async with session.post(LOGIN_URL, data=payload, headers=headers) as login_res:
        content = await login_res.text()
        match = re.search(r"var viewModel = (\{.*?\});", content, re.DOTALL)
        if not match:
            raise InsiteAuthError("Invalid username or password")

        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as err:
            raise InsiteApiError(f"Failed to parse API response: {err}") from err


class InsiteApiError(Exception):
    """Error communicating with the Insite Energy API."""


class InsiteAuthError(InsiteApiError):
    """Error indicating invalid authentication credentials."""
