"""Tests for the config, options and reauth flows."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType

from custom_components.insite_energy.api import InsiteApiError, InsiteAuthError
from custom_components.insite_energy.const import CONF_UPDATE_INTERVAL, DOMAIN

from .conftest import PASSWORD, USERNAME


async def test_user_flow_creates_entry(hass, mock_client):
    """The happy path validates credentials and stores them."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InsiteAuthError("bad"), "invalid_auth"),
        (InsiteApiError("down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors(hass, mock_client, error, expected):
    """Each failure maps to its own message; an outage is not 'invalid_auth'."""
    mock_client.async_get_data.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_reauth_updates_the_password(hass, config_entry, mock_client):
    """Reauth replaces the password and keeps the same entry."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new-password"
    assert config_entry.data[CONF_USERNAME] == USERNAME


async def test_reauth_rejects_a_still_wrong_password(hass, config_entry, mock_client):
    """A password that still fails keeps the form open and changes nothing."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)

    mock_client.async_get_data.side_effect = InsiteAuthError()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "still-wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_reauth_outage_does_not_reject_the_password(
    hass, config_entry, mock_client
):
    """An outage during reauth must not be reported as a bad password."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)

    mock_client.async_get_data.side_effect = InsiteApiError()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_auth_failure_triggers_a_reauth_flow(hass, config_entry, mock_client):
    """A failing login while running raises the reauth prompt."""
    config_entry.add_to_hass(hass)
    mock_client.async_get_data.side_effect = InsiteAuthError("bad password")

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"


async def test_options_flow_updates_interval(hass, config_entry, mock_client):
    """Interval changes land in options and survive as an int."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_UPDATE_INTERVAL: 6},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_UPDATE_INTERVAL: 6}
    # Blank password means unchanged.
    assert config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_options_flow_blank_password_keeps_the_old_one(
    hass, config_entry, mock_client
):
    """The password is never sent to the browser, so blank must mean 'keep'."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_PASSWORD: "", CONF_UPDATE_INTERVAL: 12},
    )
    await hass.async_block_till_done()

    assert config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_options_flow_sets_a_new_password(hass, config_entry, mock_client):
    """A supplied password replaces the stored one."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: "rotated",
            CONF_UPDATE_INTERVAL: 12,
        },
    )
    await hass.async_block_till_done()

    assert config_entry.data[CONF_PASSWORD] == "rotated"


async def test_options_flow_reloads_once(hass, config_entry, mock_client):
    """Writing data and options separately used to reload the entry twice."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    with patch(
        "homeassistant.config_entries.ConfigEntries.async_reload",
        wraps=hass.config_entries.async_reload,
    ) as reload:
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: USERNAME,
                CONF_PASSWORD: "changed-too",
                CONF_UPDATE_INTERVAL: 6,
            },
        )
        await hass.async_block_till_done()

    assert reload.call_count == 1
