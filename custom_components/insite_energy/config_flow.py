"""Config flow for Insite Energy integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import InsiteApiError, InsiteAuthError, InsiteClient
from .const import (
    DOMAIN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)

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


class InsiteEnergyOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Insite Energy."""

    def _is_taken(self, username: str) -> bool:
        """Return True if another entry already holds this account."""
        return any(
            entry.entry_id != self.config_entry.entry_id
            and entry.unique_id == username
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        )

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

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            # Blank password means "unchanged", so it never has to be sent to
            # the browser.
            password = (
                user_input.get(CONF_PASSWORD)
                or self.config_entry.data[CONF_PASSWORD]
            )

            if username != current_username and self._is_taken(username):
                errors["base"] = "already_configured"
            elif username != current_username or user_input.get(CONF_PASSWORD):
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
                # Credentials belong in entry.data, not options.
                new_data = {
                    **self.config_entry.data,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                }

                # Write data and options in one go. Two calls would fire the
                # update listener twice and reload the entry twice. The
                # async_create_entry below then finds the options unchanged and
                # doesn't fire again. The unique id and title are the email, so
                # they have to move with it.
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                    options=options,
                    title=username,
                    unique_id=username,
                )
                return self.async_create_entry(title="", data=options)

            # Re-showing the form: keep what was typed, minus the password.
            current_username = username
            current_interval = int(user_input[CONF_UPDATE_INTERVAL])

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=current_username): str,
                    vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
                    vol.Required(
                        CONF_UPDATE_INTERVAL, default=current_interval
                    ): INTERVAL_SELECTOR,
                }
            ),
            errors=errors,
        )
