"""DataUpdateCoordinator for Insite Energy."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import InsiteApiError, InsiteAuthError, InsiteClient
from .const import (
    CACHE_SAVE_DELAY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    LAST_POLL_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


def async_get_cache_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Return the Store holding the last successful response for an entry."""
    return Store(
        hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", encoder=JSONEncoder
    )


class InsiteEnergyDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Insite Energy data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.username = entry.data[CONF_USERNAME]
        # Use a dedicated session to avoid cookie cross-contamination with
        # HA's shared session. The client relies on the cookies persisting
        # between polls to skip the slow login.
        self.session = async_create_clientsession(hass)
        self.client = InsiteClient(
            self.session, self.username, entry.data[CONF_PASSWORD]
        )
        self._store = async_get_cache_store(hass, entry.entry_id)

        interval_hours = int(
            entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        )

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(hours=interval_hours),
        )

    async def async_load_cache(self) -> bool:
        """Seed data from the last successful poll stored on disk.

        Logging in takes tens of seconds, so at startup we come up on the
        cached snapshot and refresh in the background instead of blocking
        setup. Returns True if a usable snapshot was loaded.
        """
        try:
            cached = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a bad cache must never block setup
            _LOGGER.exception("Failed to load cached data, will fetch instead")
            return False

        if not cached:
            return False

        # JSON has no datetime type, so the poll timestamp comes back as a string.
        last_poll = cached.get(LAST_POLL_KEY)
        if isinstance(last_poll, str):
            cached[LAST_POLL_KEY] = dt_util.parse_datetime(last_poll)

        self.data = cached
        return True

    async def _async_update_data(self) -> dict:
        """Fetch data from API endpoint."""
        try:
            view_model = await self.client.async_get_data()
        except InsiteAuthError as err:
            # Now that an outage is no longer mistaken for bad credentials,
            # this is safe to surface as a reauth prompt.
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except InsiteApiError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        view_model[LAST_POLL_KEY] = dt_util.utcnow()
        self._store.async_delay_save(lambda: view_model, CACHE_SAVE_DELAY)
        return view_model
