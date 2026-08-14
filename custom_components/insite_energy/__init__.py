"""The Insite Energy integration."""
from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import InsiteEnergyDataUpdateCoordinator, async_get_cache_store

PLATFORMS: list[Platform] = [Platform.SENSOR]

# When the background startup refresh fails we would otherwise sit on stale
# cached data until the next scheduled poll (12h by default), so retry a few
# times first.
INITIAL_RETRY_DELAYS = (60, 300, 900)

SERVICE_REFRESH_DATA = "refresh_data"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-wide service."""

    async def handle_refresh_data(call: ServiceCall) -> None:
        """Refresh every configured account."""
        for coordinator in hass.data.get(DOMAIN, {}).values():
            await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_REFRESH_DATA, handle_refresh_data)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Insite Energy from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = InsiteEnergyDataUpdateCoordinator(hass, entry)

    # Come up immediately on the previous run's data and fetch a newer reading
    # in the background, so a slow login doesn't hold up HA startup.
    served_from_cache = await coordinator.async_load_cache()
    if not served_from_cache:
        # Nothing cached (first run), so we have no choice but to wait.
        await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def _schedule_startup_work(_hass: HomeAssistant) -> None:
        # Kept as a background task on the entry so unload cancels a refresh
        # that is still waiting on a slow login.
        entry.async_create_background_task(
            hass,
            _async_announce_then_refresh(coordinator, refresh=served_from_cache),
            f"{DOMAIN} startup refresh",
        )

    # Registered *after* the platforms, and only ever here: when HA is already
    # running - a reload, or adding the entry - this fires immediately, so the
    # meter entities have to exist by now or the re-announcement finds nothing
    # to announce.
    entry.async_on_unload(async_at_started(hass, _schedule_startup_work))

    # Deliberately no update listener. `async_update_reload_and_abort` in the
    # reauth flow already schedules its own reload, so a listener that also
    # reloads makes every password change cost two full teardowns and two slow
    # logins - and Home Assistant warns that the combination stops working in
    # 2026.12. The options flow reloads explicitly instead, which also means a
    # cosmetic rename no longer forces a reload.
    return True


async def _async_announce_then_refresh(
    coordinator: InsiteEnergyDataUpdateCoordinator,
    refresh: bool,
) -> None:
    """Re-announce the cached reading, then fetch a newer one.

    The energy dashboard's cost sensor accrues nothing on the first meter event
    it sees: it takes that reading as its baseline and returns. It only ever
    initialises from a state_changed event, and our entities are created before
    it registers its listener, so left alone the first event it sees is a real
    meter increment - and that day's cost is silently lost.

    Waiting for startup to finish and re-announcing the cached reading gives it
    a harmless baseline instead. Doing that strictly *before* the refresh is
    what makes the meter moving while HA was down work: the newer reading then
    arrives as a genuine delta and gets charged, rather than being swallowed.
    """
    coordinator.async_reannounce()

    if refresh:
        await _async_startup_refresh(coordinator)


async def _async_startup_refresh(
    coordinator: InsiteEnergyDataUpdateCoordinator,
) -> None:
    """Refresh once HA is up, retrying briefly before the normal interval."""
    await coordinator.async_refresh()

    for delay in INITIAL_RETRY_DELAYS:
        if coordinator.last_update_success:
            return
        if isinstance(coordinator.last_exception, ConfigEntryAuthFailed):
            # Reauth has been raised; retrying just burns known-bad logins
            # against a site that may well lock the account out.
            return
        await asyncio.sleep(delay)
        await coordinator.async_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Discard the cached data when the entry is deleted."""
    await async_get_cache_store(hass, entry.entry_id).async_remove()
