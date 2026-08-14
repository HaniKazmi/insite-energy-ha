"""Config flow for Insite Energy integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    StatisticSelector,
    StatisticSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import InsiteApiError, InsiteAuthError, InsiteClient
from .const import (
    CONF_UPDATE_INTERVAL,
    CONF_WEIGHTS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)
from .util import utility_key

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)

# Bounds are enforced by the selector, so there's no invalid value to handle.
INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_UPDATE_INTERVAL,
        max=MAX_UPDATE_INTERVAL,
        step=1,
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="hours",
    )
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Raises InsiteApiError or InsiteAuthError on failure.
    """
    # A throwaway session, so the login cookies never reach HA's shared one.
    session = async_create_clientsession(hass, auto_cleanup=False)
    try:
        client = InsiteClient(session, data[CONF_USERNAME], data[CONF_PASSWORD])
        await client.async_get_data()
    finally:
        # HA replaces close() with a warning stub that closes nothing, since
        # the connector is shared. detach() is how you release a session HA
        # isn't cleaning up for us; close() would leak it on every attempt.
        session.detach()
    return {"title": data[CONF_USERNAME]}


class InsiteEnergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Insite Energy."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # Check if already configured
            await self.async_set_unique_id(user_input[CONF_USERNAME])
            self._abort_if_unique_id_configured()

            try:
                info = await validate_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data=user_input)
            except InsiteAuthError:
                errors["base"] = "invalid_auth"
            except InsiteApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Handle re-authentication after the password stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Prompt for a new password for the existing account."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        username = entry.data[CONF_USERNAME]

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await validate_input(
                    self.hass,
                    {
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
            except InsiteAuthError:
                errors["base"] = "invalid_auth"
            except InsiteApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"username": username},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return InsiteEnergyOptionsFlowHandler()


WEIGHT_FIELD_PREFIX = "weight_"

# Lists what recorder actually holds statistics for, which is the real question
# - a weight is read back weeks later, so anything without them is useless. It
# cannot be filtered, so it also offers this integration's own output;
# statistics.py refuses that rather than trusting the picker.
WEIGHT_SELECTOR = StatisticSelector(StatisticSelectorConfig())


class InsiteEnergyOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Insite Energy."""

    def _weights_from_input(
        self, user_input: dict[str, Any], utilities: dict[str, str]
    ) -> dict[str, str]:
        """Pull the per-utility weights out of submitted form data.

        Cleared fields come back absent, and are dropped rather than stored as
        empty, so an unweighted utility looks the same as one never configured.
        """
        return {
            key: value
            for key in utilities
            if (value := user_input.get(f"{WEIGHT_FIELD_PREFIX}{key}"))
        }

    def _known_utilities(self) -> dict[str, str]:
        """Return {utility_key: display name} from the last poll.

        Utilities are discovered rather than configured, so the weighting
        fields can only be offered for the ones we have actually seen.
        """
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        data = getattr(coordinator, "data", None) or {}
        return {
            utility_key(u): str(u.get("Name"))
            for u in (data.get("UtilityDetails") or [])
            if u.get("Name")
        }

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        current_username = self.config_entry.data[CONF_USERNAME]
        current_interval = int(
            self.config_entry.options.get(
                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
            )
        )
        utilities = self._known_utilities()
        current_weights = dict(self.config_entry.options.get(CONF_WEIGHTS) or {})

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            # Blank password means "unchanged", so it never has to be sent to
            # the browser.
            password = (
                user_input.get(CONF_PASSWORD)
                or self.config_entry.data[CONF_PASSWORD]
            )

            if username != current_username or user_input.get(CONF_PASSWORD):
                # New credentials are checked for the same reason as in the
                # user and reauth steps: an unvalidated change otherwise only
                # surfaces later, as a reauth prompt from a failing poll.
                # Unchanged ones are not, so that an interval change doesn't
                # sit through a login, or get refused while the site is down.
                try:
                    await validate_input(
                        self.hass,
                        {CONF_USERNAME: username, CONF_PASSWORD: password},
                    )
                except InsiteAuthError:
                    errors["base"] = "invalid_auth"
                except InsiteApiError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"

            if not errors:
                options = {
                    CONF_UPDATE_INTERVAL: int(user_input[CONF_UPDATE_INTERVAL])
                }
                # Only the utilities this form actually rendered are
                # authoritative. A cleared field means "unweighted", but a
                # utility missing from the current poll - a degraded scrape, or
                # an entry that has not finished setting up - has no field here
                # at all, and must keep the weight it already had rather than
                # lose it silently. Read from the entry rather than
                # current_weights, which the error-re-show path reassigns.
                saved = self.config_entry.options.get(CONF_WEIGHTS) or {}
                weights = {
                    **{k: v for k, v in saved.items() if k not in utilities},
                    **self._weights_from_input(user_input, utilities),
                }
                # The key is omitted entirely when nothing is weighted, so
                # options stay empty until they are actually used.
                if weights:
                    options[CONF_WEIGHTS] = weights
                # Credentials belong in entry.data, not options.
                new_data = {
                    **self.config_entry.data,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                }

                # Write data and options in one go, so nothing observes a
                # half-updated entry. The unique id and title are the email, so
                # they have to move with it.
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                    options=options,
                    title=username,
                    unique_id=username,
                )
                # Reloaded here rather than by an update listener. The listener
                # fired on *any* entry change, which made a reauth reload twice
                # over and a rename reload for nothing; and OptionsFlowWithReload
                # would not help, because it only reloads when its own options
                # write changes something, which the call above has already
                # done. Scheduling it explicitly is the one way to get exactly
                # one reload for both a credential change and an options change.
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
                return self.async_create_entry(title="", data=options)

            # Re-showing the form: keep what was typed, minus the password.
            current_username = username
            current_interval = int(user_input[CONF_UPDATE_INTERVAL])
            current_weights = self._weights_from_input(user_input, utilities)

        schema: dict[Any, Any] = {
            vol.Required(CONF_USERNAME, default=current_username): str,
            vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
            vol.Required(
                CONF_UPDATE_INTERVAL, default=current_interval
            ): INTERVAL_SELECTOR,
        }
        # One weighting field per utility. Left empty, that utility's readings
        # are spread evenly across the period they cover.
        for key in utilities:
            schema[
                vol.Optional(
                    f"{WEIGHT_FIELD_PREFIX}{key}",
                    description={"suggested_value": current_weights.get(key)},
                )
            ] = WEIGHT_SELECTOR

        # Field names are built from whatever ShortName the portal returns, so
        # they cannot all have translation keys - anything beyond the handful
        # spelled out in strings.json renders with its raw key as the label.
        # Naming each field against its utility here means an unlabelled
        # `weight_el` is still identifiable from the text directly above it.
        legend = ", ".join(
            f"{WEIGHT_FIELD_PREFIX}{key} = {name}" for key, name in utilities.items()
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "utilities": legend or "none discovered yet"
            },
            errors=errors,
        )
