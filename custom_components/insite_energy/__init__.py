"""The Insite Energy integration."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import (
    InsiteEnergyDataUpdateCoordinator,
    async_get_cache_store,
)

_LOGGER = logging.getLogger(__name__)

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

    if await coordinator.async_load_cache():
        # Come up immediately on the previous run's data and refresh in the
        # background, so a slow login doesn't hold up HA startup.
        entry.async_create_background_task(
            hass,
            _async_startup_refresh(coordinator),
            f"{DOMAIN} startup refresh",
        )
    else:
        # Nothing cached (first run), so we have no choice but to wait.
        await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


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


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
