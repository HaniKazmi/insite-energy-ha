"""Tests for setup, caching and the entity identifiers."""
from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.sensor.recorder import DEFAULT_STATISTICS
from homeassistant.const import CONF_USERNAME, EVENT_STATE_CHANGED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.insite_energy import INITIAL_RETRY_DELAYS, _async_startup_refresh
from custom_components.insite_energy.api import InsiteApiError
from custom_components.insite_energy.const import CONF_COOKIES, CONF_WEIGHTS, DOMAIN
from custom_components.insite_energy.coordinator import InsiteEnergyDataUpdateCoordinator

from .conftest import COOKIES, USERNAME

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
    """Settle the cache write.

    The store is written during the poll now rather than on a debounce, so this
    only has to let the poll finish. Kept as a named helper because the tests
    below read better for saying what they are waiting on.
    """
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


async def test_a_poll_keeps_the_cookies_it_was_given(hass, config_entry, mock_client):
    """A login's cookies have to reach the entry to outlive the process.

    The 45 day browser trust is a cookie; left in the session it dies with the
    next restart and the user is asked for another emailed code.
    """
    mock_client.cookies_changed = True
    await setup_entry(hass, config_entry)

    assert config_entry.data.get(CONF_COOKIES) == COOKIES
    # Cleared once written, so a poll that changed nothing does not rewrite it.
    assert mock_client.cookies_changed is False


async def test_a_poll_that_changes_no_cookies_leaves_the_entry_alone(
    hass, config_entry, mock_client
):
    """Only a login writes cookies; a warm poll has nothing new to store."""
    mock_client.cookies_changed = False
    await setup_entry(hass, config_entry)

    assert CONF_COOKIES not in config_entry.data


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

    with patch(
        "custom_components.insite_energy.asyncio.sleep", AsyncMock()
    ) as sleep:
        await _async_startup_refresh(coordinator)

    # Hardcoded, not `1 + len(INITIAL_RETRY_DELAYS)`: deriving the expected
    # count from the very constant the code iterates means emptying that tuple
    # deletes the retry ladder and leaves this test green.
    assert coordinator.async_refresh.await_count == 4
    # And the ladder is the policy, so pin the delays rather than just the count.
    assert [call.args[0] for call in sleep.await_args_list] == [60, 300, 900]
    assert INITIAL_RETRY_DELAYS == (60, 300, 900)


# --- Startup re-announcement -------------------------------------------------
#
# The energy dashboard's cost sensor accrues nothing on the first meter event it
# sees; it takes that reading as its baseline. Because this meter only moves once
# a day, that event is usually a real increment, and the day's cost is lost. We
# re-announce the cached reading once HA is up so it baselines on something
# harmless, and we do it strictly before the refresh so a meter that moved while
# HA was down still arrives as a chargeable delta.

METER = "sensor.heating_hot_water_11291934_meter_reading"
RATE = "sensor.heating_hot_water_11291934_rate"


async def restart_with_reading(
    hass, config_entry, mock_client, view_model, reading: str
) -> list:
    """Restart from cache with the site reporting `reading`, capturing states.

    The cache must already be seeded. HA is put back into `not_running` so the
    re-announcement has to wait for startup, as it does on a real boot.
    """
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    payload = copy.deepcopy(view_model)
    for utility in payload["UtilityDetails"]:
        if utility["ShortName"] == "HH":
            utility["LastMeterReading"] = reading
    # Only `return_value`. Clearing `side_effect` would bypass conftest's
    # per-poll deepcopy, so every poll would hand back the same object - which
    # makes the coordinator's previous snapshot *be* the new payload, and
    # anything comparing the two silently sees no change. conftest's wrapper
    # already honours return_value, so there is nothing to clear.
    mock_client.async_get_data.return_value = payload

    hass.set_state(CoreState.not_running)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.async_start()
    await hass.async_block_till_done()

    return events


def transitions(events: list, entity_id: str) -> list[tuple[str | None, str | None]]:
    """The (old, new) state pairs captured for one entity, in order."""
    return [
        (
            event.data["old_state"].state if event.data["old_state"] else None,
            event.data["new_state"].state if event.data["new_state"] else None,
        )
        for event in events
        if event.data["entity_id"] == entity_id
    ]


async def test_meter_reading_is_reannounced_before_the_startup_refresh(
    hass, config_entry, mock_client, view_model, hass_storage
):
    """A meter that moved while HA was down must still be charged.

    The re-announcement has to land first so the cost sensor baselines on the
    cached 2999; the refresh then delivers 3000 as a real 1 kWh delta. If the
    order flips, the cost sensor baselines on 3000 and the kWh is lost.
    """
    await setup_entry(hass, config_entry)
    await flush_cache_write(hass)

    events = await restart_with_reading(
        hass, config_entry, mock_client, view_model, "3000.000"
    )
    seq = transitions(events, METER)

    assert ("2999.0", "2999.0") in seq, f"reading was never re-announced: {seq}"
    assert ("2999.0", "3000.0") in seq, f"refresh delta never landed: {seq}"
    assert seq.index(("2999.0", "2999.0")) < seq.index(("2999.0", "3000.0")), (
        f"refresh beat the re-announcement, so the kWh was swallowed: {seq}"
    )


async def test_reannouncement_fires_even_when_the_reading_is_unchanged(
    hass, config_entry, mock_client, view_model, hass_storage
):
    """The common case: nothing moved, but the cost sensor still needs an event.

    HA collapses a write whose state and attributes are unchanged into a
    state_reported event, which the cost sensor does not listen for. This only
    passes because the sensor sets force_update for that one write.
    """
    await setup_entry(hass, config_entry)
    await flush_cache_write(hass)

    events = await restart_with_reading(
        hass, config_entry, mock_client, view_model, "2999.000"
    )

    assert ("2999.0", "2999.0") in transitions(events, METER)


async def test_reannouncement_is_limited_to_the_meter_reading(
    hass, config_entry, mock_client, view_model, hass_storage
):
    """Only the energy entity needs forcing; the cost sensor reads the rate directly."""
    await setup_entry(hass, config_entry)
    await flush_cache_write(hass)

    events = await restart_with_reading(
        hass, config_entry, mock_client, view_model, "2999.000"
    )

    unchanged = [(old, new) for old, new in transitions(events, RATE) if old == new]
    assert not unchanged, f"rate was force-written too: {unchanged}"


async def test_force_update_is_cleared_after_the_reannouncement(
    hass, config_entry, mock_client, view_model, hass_storage
):
    """Ordinary coordinator writes must keep collapsing, or the recorder bloats."""
    await setup_entry(hass, config_entry)
    await flush_cache_write(hass)

    await restart_with_reading(
        hass, config_entry, mock_client, view_model, "2999.000"
    )

    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    assert coordinator.reannouncing is False


async def test_price_sensors_are_recorded_as_measurements(
    hass, config_entry, mock_client
):
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


async def test_the_coordinator_publishes_spread_statistics(
    hass, config_entry, mock_client, view_model
):
    """The coordinator must actually reach the statistics module, with the
    previous snapshot and the configured weights.

    Nothing covered this wiring: deleting the call outright, passing the new
    payload as both previous and current, or reading weights from the wrong
    options key all left the suite green.
    """
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_WEIGHTS: {"co": "sensor.cooling_activity"}}
    )

    calls = []
    with patch(
        "custom_components.insite_energy.coordinator.async_publish_spread_statistics",
        side_effect=lambda hass, previous, current, weights: calls.append(
            (previous, current, weights)
        ),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        # A second poll, so there is a previous snapshot to compare against.
        moved = copy.deepcopy(view_model)
        for utility in moved["UtilityDetails"]:
            if utility["ShortName"] == "CO":
                utility["LastMeterReading"] = "600.000"
                utility["MeterReadingDate"] = "2026/09/04 00:00"
        mock_client.async_get_data.return_value = moved
        await hass.data[DOMAIN][config_entry.entry_id].async_refresh()
        await hass.async_block_till_done()

    assert calls, "the coordinator never reached the statistics module"
    previous, current, weights = calls[-1]

    # The weights must arrive keyed the way the options flow stores them.
    assert weights == {"co": "sensor.cooling_activity"}

    # previous and current must be different snapshots, or no window exists.
    def cooling(payload):
        return next(u for u in payload["UtilityDetails"] if u["ShortName"] == "CO")

    assert previous is not None
    assert cooling(previous)["LastMeterReading"] != cooling(current)["LastMeterReading"]
    assert cooling(current)["LastMeterReading"] == "600.000"


async def test_deleting_the_entry_leaves_no_cache_behind(
    hass, config_entry, mock_client, hass_storage
):
    """A debounced write outlived the removal that was meant to clean it up.

    The cache holds the account holder's name, email, balance and meter serials,
    so a file left behind after the integration is deleted is account data with
    nothing left to ever remove it - and it lands in every backup.
    """
    await setup_entry(hass, config_entry)
    await flush_cache_write(hass)
    assert any(DOMAIN in key for key in hass_storage), "cache was never written"

    assert await hass.config_entries.async_remove(config_entry.entry_id) == {
        "require_restart": False
    }
    await hass.async_block_till_done()

    # And nothing arrives late to undo it.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()

    leftover = {k: v for k, v in hass_storage.items() if DOMAIN in k}
    assert not leftover, f"cache survived removal: {leftover}"


async def test_the_cache_is_on_disk_before_the_poll_returns(
    hass, config_entry, mock_client, hass_storage
):
    """No window between a poll and its cache landing.

    A reload inside that window came up on the previous poll's reading, which
    then derived a statistics window overlapping one already published - and
    that gets refused outright, losing the consumption for good.
    """
    await setup_entry(hass, config_entry)

    # No time travel, no extra block_till_done beyond setup.
    assert any(DOMAIN in key for key in hass_storage)
