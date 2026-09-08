"""DataUpdateCoordinator for Insite Energy."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import InsiteApiError, InsiteAuthError, InsiteClient, strict_cookie_jar
from .const import (
    CACHE_ACCOUNT_KEY,
    CONF_COOKIES,
    CONF_UPDATE_INTERVAL,
    CONF_WEIGHTS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    LAST_POLL_KEY,
    STORAGE_VERSION,
)
from .statistics import async_publish_spread_statistics

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
        self._reannounce = False
        # Use a dedicated session to avoid cookie cross-contamination with
        # HA's shared session. The client relies on the cookies persisting
        # between polls to skip the slow login. Created during entry setup, so
        # HA detaches it on unload; closing it here is forbidden.
        self.client = InsiteClient(
            async_create_clientsession(hass, cookie_jar=strict_cookie_jar()),
            self.username,
            entry.data[CONF_PASSWORD],
        )
        # The verified-browser cookie is worth more than the session one: it is
        # what stops a restart asking the user for a fresh emailed code.
        self.client.load_cookies(entry.data.get(CONF_COOKIES) or {})
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

    @property
    def reannouncing(self) -> bool:
        """Whether entities should write state even where nothing changed."""
        return self._reannounce

    @callback
    def async_reannounce(self) -> None:
        """Re-write every entity's state, unchanged values included.

        The energy dashboard's cost sensor accrues nothing on the first meter
        event it sees - it takes that reading as a baseline - and it only
        initialises from a state_changed event. Announcing an unchanged reading
        gives it something harmless to baseline on, instead of it swallowing a
        real day's consumption later.

        Home Assistant collapses a write whose state and attributes are both
        unchanged into a state_reported event, which the cost sensor does not
        listen for, so InsiteUtilityReadingSensor reports force_update while
        this runs. The flag is only ever true for the duration of this call,
        which is why setting it lives here beside the thing it guards rather
        than at the caller.
        """
        self._reannounce = True
        try:
            # Synchronous, and callback listeners run inside bus.async_fire, so
            # the cost sensors have taken their baseline by the time it returns.
            self.async_update_listeners()
        finally:
            self._reannounce = False

    async def async_load_cache(self) -> bool:
        """Seed data from the last successful poll stored on disk.

        Logging in takes tens of seconds, so at startup we come up on the
        cached snapshot and refresh in the background instead of blocking
        setup. Returns True if a usable snapshot was loaded.
        """
        try:
            cached = await self._store.async_load()
        # Deliberately blind: a corrupt or unreadable cache must never block
        # setup, since the whole point of it is to be optional.
        except Exception:
            _LOGGER.exception("Failed to load cached data, will fetch instead")
            return False

        if not cached:
            return False

        # The cache is keyed on the entry id, which survives an email change in
        # the options flow. Serving the previous account's balance and readings
        # as current would look like real data, so anything we can't attribute
        # to the configured account is discarded.
        if cached.get(CACHE_ACCOUNT_KEY) != self.username:
            _LOGGER.debug("Cached data belongs to another account, ignoring it")
            return False

        # JSON has no datetime type, so the poll timestamp comes back as a string.
        last_poll = cached.get(LAST_POLL_KEY)
        if isinstance(last_poll, str):
            cached[LAST_POLL_KEY] = dt_util.parse_datetime(last_poll)

        self.data = cached
        return True

    @callback
    def _async_store_cookies(self) -> None:
        """Keep the jar in the entry, so the next run starts already verified.

        There is no update listener on the entry, so writing here reloads
        nothing; the poll that triggered it carries on.
        """
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_COOKIES: self.client.dump_cookies(),
            },
        )
        self.client.cookies_changed = False

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

        if self.client.cookies_changed:
            self._async_store_cookies()

        view_model[LAST_POLL_KEY] = dt_util.utcnow()
        view_model[CACHE_ACCOUNT_KEY] = self.username
        # Written now rather than on a debounce. A delayed write outlives the
        # thing that scheduled it: deleting the entry removes the file and the
        # pending write puts it straight back, orphaned, and a reload inside the
        # delay leaves the new coordinator reading the previous poll off disk -
        # which then derives a window overlapping one already published, and
        # gets refused. Polls are hours apart, so there is nothing to debounce.
        await self._store.async_save(view_model)

        # self.data is still the previous snapshot here, which is what tells us
        # the period this reading covers. Failures are handled in there, so the
        # poll cannot be lost to them.
        await async_publish_spread_statistics(
            self.hass,
            self.data,
            view_model,
            self.config_entry.options.get(CONF_WEIGHTS) or {},
        )

        return view_model
