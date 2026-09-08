"""Fixtures for Insite Energy tests."""
from __future__ import annotations

import copy
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.insite_energy.const import DOMAIN

USERNAME = "user@example.com"
PASSWORD = "hunter2"
# Stands in for the jar a verified login leaves behind, the useful part of
# which is the cookie saying this browser has already answered a code.
COOKIES = {"remember-browser": "verified"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load the integration from custom_components/.

    This pulls in `hass`, and being autouse it does so before anything a test
    asked for by name. A test needing the real recorder has to get `recorder_mock`
    in first, which it cannot do from here - see tests/test_statistics_recorder.py.
    """
    return


@pytest.fixture
def view_model() -> dict:
    """A trimmed copy of a real /Customer/Details payload."""
    return {
        "NAME": "Test User",
        "Email": USERNAME,
        "CreditAccountBalance": "-47.53",
        "UtilityDetails": [
            {
                "Name": "Cooling",
                "ShortName": "CO",
                "MeterReadingDate": "2026/08/04 00:00",
                "MeterSerialNumber": "1566TBC22",
                "LastMeterReading": "504.600",
                "Rates": "14.67p",
                "StandingChargeValue": "24.53p",
            },
            {
                "Name": "Heating & Hot Water",
                "ShortName": "HH",
                "MeterReadingDate": "2026/08/08 00:00",
                "MeterSerialNumber": "11291934",
                "LastMeterReading": "2999.000",
                "Rates": "17.11p",
                "StandingChargeValue": "86.68p",
            },
        ],
    }


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A configured entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )


@pytest.fixture
def mock_client(view_model):
    """Patch InsiteClient so no network access happens.

    The coordinator and the config flow share one instance mock, so a test can
    set `mock_client.async_get_data.side_effect` once and have it apply to
    whichever path it is exercising.
    """
    instance = AsyncMock()

    # A fresh copy per poll. Returning the same object would make the
    # coordinator's previous snapshot *be* the new payload, so anything that
    # compares the two - the spread statistics, notably - would see no change
    # and quietly do nothing, in tests only.
    #
    # A test may still set `return_value` to shape the payload; honour it rather
    # than let this side_effect silently win, which is the usual Mock surprise.
    async def _get_data():
        payload = instance.async_get_data.return_value
        if not isinstance(payload, dict):
            payload = view_model
        return copy.deepcopy(payload)

    instance.async_get_data = AsyncMock(side_effect=_get_data)

    # Cookie handling is synchronous, and an AsyncMock would hand back a
    # coroutine where the code expects a dict of cookies to store.
    instance.load_cookies = MagicMock()
    instance.dump_cookies = MagicMock(return_value=dict(COOKIES))
    instance.cookies_changed = False

    with patch(
        "custom_components.insite_energy.coordinator.InsiteClient",
        return_value=instance,
    ), patch(
        "custom_components.insite_energy.config_flow.InsiteClient",
        return_value=instance,
    ):
        yield instance
