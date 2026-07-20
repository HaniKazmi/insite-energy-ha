"""DataUpdateCoordinator for Insite Energy."""
from __future__ import annotations

from datetime import timedelta, datetime, timezone
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import async_login_and_fetch, InsiteApiError, InsiteAuthError
from .const import DOMAIN, CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class InsiteEnergyDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Insite Energy data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.username = entry.data[CONF_USERNAME]
        self.password = entry.data[CONF_PASSWORD]
        # Use a dedicated session to avoid cookie cross-contamination
        # with HA's shared session.
        self.session = async_create_clientsession(hass)

        interval_hours = int(
            entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=interval_hours),
        )

    async def _async_update_data(self) -> dict:
        """Fetch data from API endpoint."""
        try:
            view_model = await async_login_and_fetch(
                self.session, self.username, self.password
            )
            view_model["_last_poll_time"] = datetime.now(timezone.utc)
            return view_model
        except InsiteAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except InsiteApiError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
