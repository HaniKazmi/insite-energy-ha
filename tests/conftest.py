"""Fixtures for Insite Energy tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.insite_energy.const import DOMAIN

USERNAME = "user@example.com"
PASSWORD = "hunter2"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load the integration from custom_components/."""
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
    instance.async_get_data = AsyncMock(return_value=view_model)

    with patch(
        "custom_components.insite_energy.coordinator.InsiteClient",
        return_value=instance,
    ), patch(
        "custom_components.insite_energy.config_flow.InsiteClient",
        return_value=instance,
    ):
        yield instance
