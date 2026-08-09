"""Tests for setup, caching and the identifier migration."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.insite_energy.api import InsiteApiError
from custom_components.insite_energy.const import CACHE_SAVE_DELAY, DOMAIN

from .conftest import USERNAME

# Every entity the integration creates, as (v1 suffix, v2 suffix).
ENTITY_SUFFIXES = [
    ("account_balance", "account_balance"),
    ("account_last_poll", "account_last_poll"),
    ("cooling_reading", "co_reading"),
    ("cooling_rate", "co_rate"),
    ("cooling_standing_charge", "co_standing_charge"),
    ("cooling_reading_date", "co_reading_date"),
    ("cooling_serial", "co_serial"),
    ("heating_hot_water_reading", "hh_reading"),
    ("heating_hot_water_rate", "hh_rate"),
    ("heating_hot_water_standing_charge", "hh_standing_charge"),
    ("heating_hot_water_reading_date", "hh_reading_date"),
    ("heating_hot_water_serial", "hh_serial"),
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

    expected = {f"{config_entry.entry_id}_{new}" for _, new in ENTITY_SUFFIXES}
    assert {e.unique_id for e in entries} == expected


async def test_no_unique_id_contains_the_email(hass, config_entry, mock_client):
    """The whole point of the migration: identifiers must not embed the email."""
    await setup_entry(hass, config_entry)

    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, config_entry.entry_id):
        assert USERNAME not in entity.unique_id

    device_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_reg, config_entry.entry_id):
        for _, identifier in device.identifiers:
            assert USERNAME not in identifier


async def test_migrates_v1_entity_unique_ids(hass, config_entry, mock_client):
    """Pre-existing entities are rekeyed in place, preserving their history."""
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)

    for old, _ in ENTITY_SUFFIXES:
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{USERNAME}_{old}",
            config_entry=config_entry,
            suggested_object_id=old,
        )
    original_ids = {e.entity_id for e in er.async_entries_for_config_entry(registry, config_entry.entry_id)}

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    # No duplicates: the migrated entities are the ones the platform adopted.
    assert len(entries) == len(ENTITY_SUFFIXES)
    assert {e.unique_id for e in entries} == {
        f"{config_entry.entry_id}_{new}" for _, new in ENTITY_SUFFIXES
    }
    # Entity IDs are untouched, so dashboards and history survive.
    assert {e.entity_id for e in entries} == original_ids


async def test_migrates_v1_device_identifiers(hass, config_entry, mock_client):
    """Devices are rekeyed too, so no empty duplicates are left behind."""
    config_entry.add_to_hass(hass)
    device_reg = dr.async_get(hass)

    for old in ("account", "Cooling", "Heating & Hot Water"):
        device_reg.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={(DOMAIN, f"{USERNAME}_{old}")},
        )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    devices = dr.async_entries_for_config_entry(device_reg, config_entry.entry_id)
    identifiers = {i for d in devices for _, i in d.identifiers}
    assert identifiers == {
        f"{config_entry.entry_id}_account",
        f"{config_entry.entry_id}_co",
        f"{config_entry.entry_id}_hh",
    }
    assert len(devices) == 3


async def test_migration_is_idempotent(hass, config_entry, mock_client):
    """Reloading doesn't rewrite already-migrated identifiers."""
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
