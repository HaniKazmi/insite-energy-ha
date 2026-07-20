"""Config flow for Insite Energy integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import async_login_and_fetch, InsiteApiError, InsiteAuthError
from .const import DOMAIN, CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Raises InsiteApiError or InsiteAuthError on failure.
    """
    session = async_get_clientsession(hass)
    await async_login_and_fetch(session, data[CONF_USERNAME], data[CONF_PASSWORD])
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

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return InsiteEnergyOptionsFlowHandler()


class InsiteEnergyOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Insite Energy."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # Validate interval is a positive number
            try:
                interval = int(user_input[CONF_UPDATE_INTERVAL])
                if interval < 1:
                    errors["base"] = "invalid_interval"
                else:
                    # Credentials belong in entry.data, not options
                    new_data = {
                        **self.config_entry.data,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    }
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, data=new_data
                    )
                    # Only the interval goes into options
                    return self.async_create_entry(
                        title="",
                        data={CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL]},
                    )
            except ValueError:
                errors["base"] = "invalid_interval"

        current_interval = str(
            self.config_entry.options.get(
                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
            )
        )
        current_username = self.config_entry.data.get(CONF_USERNAME)
        current_password = self.config_entry.data.get(CONF_PASSWORD)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=current_username): str,
                    vol.Required(CONF_PASSWORD, default=current_password): str,
                    vol.Optional(
                        CONF_UPDATE_INTERVAL, default=current_interval
                    ): str,
                }
            ),
            errors=errors,
        )
