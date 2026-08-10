"""Tests for setup, caching and the entity identifiers."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.sensor.recorder import DEFAULT_STATISTICS
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.insite_energy.api import InsiteApiError
from custom_components.insite_energy import (
    INITIAL_RETRY_DELAYS,
    _async_startup_refresh,
)
from custom_components.insite_energy.const import CACHE_SAVE_DELAY, DOMAIN
from custom_components.insite_energy.coordinator import (
    InsiteEnergyDataUpdateCoordinator,
)

from .conftest import USERNAME

# Every entity the integration creates, as a unique id suffix.
ENTITY_SUFFIXES = [
    "account_balance",
    "account_last_poll",
    "co_reading",
    "co_rate",
    "co_standing_charge",
    "co_reading_date",
    "co_serial",
    "hh_reading",
    "hh_rate",
    "hh_standing_charge",
    "hh_reading_date",
    "hh_serial",
]


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up a config entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_creates_entities(hass, config_entry, mock_client):
    """A clean setup produces the full entity set, keyed on the entry id."""
    await setup_entry(hass, config_entry)

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    assert len(entries) == len(ENTITY_SUFFIXES)

    expected = {f"{config_entry.entry_id}_{suffix}" for suffix in ENTITY_SUFFIXES}
    assert {e.unique_id for e in entries} == expected


async def test_no_unique_id_contains_the_email(hass, config_entry, mock_client):
    """Identifiers must not embed the email, which the options flow can change."""
    await setup_entry(hass, config_entry)

    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, config_entry.entry_id):
        assert USERNAME not in entity.unique_id

    device_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_reg, config_entry.entry_id):
        for _, identifier in device.identifiers:
            assert USERNAME not in identifier


async def test_reload_keeps_identifiers_stable(hass, config_entry, mock_client):
    """Reloading must adopt the existing entities rather than duplicate them."""
    await setup_entry(hass, config_entry)
    registry = er.async_get(hass)
    before = {
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, config_entry.entry_id)
    }

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    after = {
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, config_entry.entry_id)
    }
    assert before == after


async def flush_cache_write(hass: HomeAssistant) -> None:
    """Let the debounced Store write land."""
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=CACHE_SAVE_DELAY + 1)
    )
    await hass.async_block_till_done()


async def test_first_setup_without_cache_needs_the_site(hass, config_entry, mock_client):
    """With nothing cached there's no choice but to wait, and fail if it's down."""
    mock_client.async_get_data.side_effect = InsiteApiError("site down")
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def restart_with_slow_site(hass, config_entry, mock_client) -> asyncio.Event:
    """Reload with the site hanging. The cache must already be seeded."""
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    # A login that never returns, standing in for the 30s+ worst case.
    started = asyncio.Event()

    async def never_returns(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    mock_client.async_get_data.side_effect = never_returns

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return started


async def test_startup_is_not_blocked_by_a_slow_site(
    hass, config_entry, mock_client, hass_storage
):
    """Setup completes on cached data while the refresh is still in flight.

    This is the startup fix: previously setup awaited the login, so a slow
    site held up the whole entry. Contrast with the no-cache test above.
    """
    await setup_entry(hass, config_entry)
    await flush_cache_write(hass)
    assert any(DOMAIN in key for key in hass_storage), "cache was never written"

    started = await restart_with_slow_site(hass, config_entry, mock_client)

    # Setup returned even though the refresh has not finished...
    assert started.is_set()
    # ...and the entities are already serving the previous run's values.
    assert hass.states.get("sensor.account_details_balance").state == "-47.53"
    assert hass.states.get("sensor.heating_hot_water_11291934_rate").state == "0.1711"
    assert hass.states.get("sensor.cooling_1566tbc22_meter_reading").state == "504.6"

    # Cancel the in-flight background task before the test ends.
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_cache_round_trips_the_poll_timestamp(hass, config_entry, mock_client):
    """The poll time is a datetime; JSON flattens it and it must come back."""
    await setup_entry(hass, config_entry)
    await flush_cache_write(hass)
    await restart_with_slow_site(hass, config_entry, mock_client)

    state = hass.states.get("sensor.account_details_last_poll_time")
    assert state is not None
    # A TIMESTAMP sensor rejects a plain string, so this proves it reparsed.
    assert dt_util.parse_datetime(state.state) is not None

    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_cache_from_another_account_is_ignored(hass, config_entry, mock_client):
    """A changed email must not serve the previous account's data as current.

    The cache is keyed on the entry id, which survives the change, so without
    the account check the balance and readings of the old account would come
    back up looking like fresh values.
    """
    await setup_entry(hass, config_entry)
    await flush_cache_write(hass)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    # The same account still gets the fast path.
    coordinator = InsiteEnergyDataUpdateCoordinator(hass, config_entry)
    assert await coordinator.async_load_cache()

    # Point the entry at a different account, as the options flow does.
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_USERNAME: "other@example.com"},
    )

    coordinator = InsiteEnergyDataUpdateCoordinator(hass, config_entry)
    assert not await coordinator.async_load_cache()
    assert coordinator.data is None


async def test_failed_refresh_marks_entities_unavailable(
    hass, config_entry, mock_client
):
    """Stale cached values must not be presented as current when polls fail."""
    await setup_entry(hass, config_entry)
    assert hass.states.get("sensor.account_details_balance").state == "-47.53"

    mock_client.async_get_data.side_effect = InsiteApiError("site down")
    await hass.services.async_call(DOMAIN, "refresh_data", blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.account_details_balance").state == "unavailable"


async def test_refresh_data_service(hass, config_entry, mock_client):
    """The service is registered once and refreshes the coordinator."""
    await setup_entry(hass, config_entry)
    assert hass.services.has_service(DOMAIN, "refresh_data")

    before = mock_client.async_get_data.await_count
    await hass.services.async_call(DOMAIN, "refresh_data", blocking=True)
    await hass.async_block_till_done()
    assert mock_client.async_get_data.await_count > before


async def test_unload_entry(hass, config_entry, mock_client):
    """Unloading cleans up without leaving the domain data behind."""
    await setup_entry(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_duplicate_short_name_is_logged(hass, config_entry, mock_client, caplog):
    """A shared ShortName drops a meter, so at least make it diagnosable."""
    mock_client.async_get_data.return_value = {
        "CreditAccountBalance": "-47.53",
        "UtilityDetails": [
            {"Name": "Heating A", "ShortName": "HH", "LastMeterReading": "1"},
            {"Name": "Heating B", "ShortName": "HH", "LastMeterReading": "2"},
        ],
    }
    await setup_entry(hass, config_entry)

    assert "Heating B" in caplog.text
    assert "shares the identifier" in caplog.text


async def test_startup_refresh_stops_after_auth_failure():
    """Bad credentials must not be retried; reauth is already raised."""
    coordinator = MagicMock()
    coordinator.async_refresh = AsyncMock()
    coordinator.last_update_success = False
    coordinator.last_exception = ConfigEntryAuthFailed("bad password")

    # Patch sleep even though a passing run never reaches it: without it a
    # regression would sleep out the whole 21-minute ladder instead of failing.
    with patch("custom_components.insite_energy.asyncio.sleep", AsyncMock()) as sleep:
        await _async_startup_refresh(coordinator)

    assert coordinator.async_refresh.await_count == 1
    sleep.assert_not_awaited()


async def test_startup_refresh_retries_other_failures():
    """A transient failure still gets the full retry ladder."""
    coordinator = MagicMock()
    coordinator.async_refresh = AsyncMock()
    coordinator.last_update_success = False
    coordinator.last_exception = InsiteApiError("site down")

    with patch("custom_components.insite_energy.asyncio.sleep", AsyncMock()):
        await _async_startup_refresh(coordinator)

    assert coordinator.async_refresh.await_count == 1 + len(INITIAL_RETRY_DELAYS)


async def test_price_sensors_are_recorded_as_measurements(hass, config_entry, mock_client):
    """Rate and standing charge must produce long-term statistics.

    They previously had device_class MONETARY and no state class, so the
    recorder skipped them entirely. MONETARY permits only state_class TOTAL,
    which would record a nonsensical running sum of a unit price, so the
    device class is deliberately absent.
    """
    await setup_entry(hass, config_entry)

    for entity_id, unit in (
        ("sensor.heating_hot_water_11291934_rate", "GBP/kWh"),
        ("sensor.heating_hot_water_11291934_standing_charge", "GBP/day"),
    ):
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        state_class = state.attributes.get("state_class")
        assert state_class is SensorStateClass.MEASUREMENT, entity_id
        assert state.attributes.get("unit_of_measurement") == unit
        # MONETARY would make MEASUREMENT invalid and log a warning.
        assert state.attributes.get("device_class") is None, entity_id
        # The recorder only compiles statistics for state classes it knows.
        assert state_class in DEFAULT_STATISTICS, entity_id


async def test_balance_keeps_its_monetary_total(hass, config_entry, mock_client):
    """The balance is a real monetary total and must stay one."""
    await setup_entry(hass, config_entry)

    state = hass.states.get("sensor.account_details_balance")
    assert state.attributes.get("device_class") == SensorDeviceClass.MONETARY
    assert state.attributes.get("state_class") is SensorStateClass.TOTAL
    assert state.attributes.get("state_class") in DEFAULT_STATISTICS


async def test_no_invalid_state_class_warnings(hass, config_entry, mock_client, caplog):
    """Every device_class/state_class pairing must be one HA considers valid."""
    await setup_entry(hass, config_entry)
    assert "impossible considering device class" not in caplog.text
