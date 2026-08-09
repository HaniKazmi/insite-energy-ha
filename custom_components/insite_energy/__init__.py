"""The Insite Energy integration."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import (
    InsiteEnergyDataUpdateCoordinator,
    async_get_cache_store,
)
from .util import legacy_utility_slug, utility_key

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

    await _async_migrate_identifiers(hass, entry, coordinator)

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def _async_migrate_identifiers(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: InsiteEnergyDataUpdateCoordinator,
) -> None:
    """Move entity and device IDs off the email address.

    v1 keyed everything on the account email and the utility's display name,
    so changing the email in the options flow (or a rename upstream) orphaned
    every entity and device. v2 uses the config entry id plus the portal's own
    ShortName. Runs on every setup and is a no-op once migrated.
    """
    old_prefix = f"{entry.data[CONF_USERNAME]}_"
    utilities = (coordinator.data or {}).get("UtilityDetails") or []

    # Entity IDs used a slug of the name; device IDs used the raw name.
    slug_to_key = {}
    name_to_key = {}
    for utility in utilities:
        if name := utility.get("Name"):
            key = utility_key(utility)
            slug_to_key[legacy_utility_slug(str(name))] = key
            name_to_key[str(name)] = key

    # Longest first, so "cooling_meter_2" isn't captured by "cooling".
    slugs_by_length = sorted(slug_to_key, key=len, reverse=True)

    def _rekey(remainder: str) -> str:
        """Swap a legacy utility slug for its stable key, if one leads."""
        for slug in slugs_by_length:
            if remainder.startswith(f"{slug}_"):
                return f"{slug_to_key[slug]}_{remainder[len(slug) + 1:]}"
        return remainder

    @callback
    def _migrate_entity(reg_entry: er.RegistryEntry) -> dict[str, str] | None:
        if not reg_entry.unique_id.startswith(old_prefix):
            return None
        remainder = _rekey(reg_entry.unique_id[len(old_prefix):])
        return {"new_unique_id": f"{entry.entry_id}_{remainder}"}

    # A registry that can't be migrated (say a half-migrated one from an
    # interrupted setup) must not take the whole integration down with it.
    try:
        await er.async_migrate_entries(hass, entry.entry_id, _migrate_entity)
    except (HomeAssistantError, ValueError):
        _LOGGER.exception("Could not migrate entity identifiers")

    device_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
        new_identifiers = set()
        changed = False
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier.startswith(old_prefix):
                remainder = identifier[len(old_prefix):]
                # Devices are "<email>_account" or "<email>_<raw utility name>".
                remainder = name_to_key.get(remainder, remainder)
                new_identifiers.add((domain, f"{entry.entry_id}_{remainder}"))
                changed = True
            else:
                new_identifiers.add((domain, identifier))

        if not changed:
            continue

        try:
            device_reg.async_update_device(device.id, new_identifiers=new_identifiers)
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning(
                "Could not migrate device %s: %s", device.name or device.id, err
            )


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
