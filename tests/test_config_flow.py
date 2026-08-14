"""Tests for the config, options and reauth flows."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import StatisticSelector

from custom_components.insite_energy import config_flow
from custom_components.insite_energy.api import InsiteApiError, InsiteAuthError
from custom_components.insite_energy.const import (
    CONF_UPDATE_INTERVAL,
    CONF_WEIGHTS,
    DOMAIN,
)

from .conftest import PASSWORD, USERNAME

NEW_USERNAME = "new@example.com"


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


async def test_validation_releases_its_session(hass, mock_client, caplog):
    """The throwaway session must actually be released.

    HA replaces close() on its sessions with a stub that only warns, so an
    awaited close() leaks the session (and logs) on every attempt.
    """
    sessions = []
    create_session = config_flow.async_create_clientsession

    def track(*args, **kwargs):
        session = create_session(*args, **kwargs)
        sessions.append(session)
        return session

    with patch.object(config_flow, "async_create_clientsession", track):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(sessions) == 1
    assert sessions[0].closed
    assert "closes the Home Assistant aiohttp session" not in caplog.text


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


async def test_options_flow_moves_the_unique_id_with_the_email(
    hass, config_entry, mock_client
):
    """The unique id and title are the email, so they have to follow it."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_USERNAME: NEW_USERNAME, CONF_UPDATE_INTERVAL: 12},
    )
    await hass.async_block_till_done()

    assert config_entry.data[CONF_USERNAME] == NEW_USERNAME
    # Left behind, these would still identify the entry by the old account, so
    # re-adding that account would be refused as a duplicate.
    assert config_entry.unique_id == NEW_USERNAME
    assert config_entry.title == NEW_USERNAME


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InsiteAuthError("bad"), "invalid_auth"),
        (InsiteApiError("down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_options_flow_validates_credentials(
    hass, config_entry, mock_client, error, expected
):
    """Credentials that don't work must not be stored.

    Unvalidated, a typo only surfaces later as a reauth prompt from a failing
    poll, with the working credentials already overwritten.
    """
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    mock_client.async_get_data.side_effect = error
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: NEW_USERNAME,
            CONF_PASSWORD: "typo",
            CONF_UPDATE_INTERVAL: 6,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert config_entry.data[CONF_USERNAME] == USERNAME
    assert config_entry.data[CONF_PASSWORD] == PASSWORD
    assert config_entry.options.get(CONF_UPDATE_INTERVAL) != 6


async def test_options_flow_interval_change_skips_the_login(
    hass, config_entry, mock_client
):
    """Unchanged credentials need no check.

    A login takes tens of seconds and fails outright when the site is down,
    neither of which should stand between the user and the poll interval.
    """
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    with patch.object(config_flow, "validate_input", AsyncMock()) as validate:
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_USERNAME: USERNAME, CONF_PASSWORD: "", CONF_UPDATE_INTERVAL: 6},
        )
        await hass.async_block_till_done()

    validate.assert_not_awaited()
    assert config_entry.options == {CONF_UPDATE_INTERVAL: 6}


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


# --- Weight sources ----------------------------------------------------------


def weight_field(result, key: str):
    """Pull one utility's weight field out of the rendered options schema."""
    for marker in result["data_schema"].schema:
        if str(marker) == f"weight_{key}":
            return result["data_schema"].schema[marker]
    return None


async def test_weight_fields_use_the_statistic_picker(hass, config_entry, mock_client):
    """The list has to come from recorder metadata, not from loaded entities.

    A weight is read back weeks later, so what matters is whether recorder holds
    statistics for it - which outlives the entity, and respects the recorder
    filters that a state-class check would ignore.
    """
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert isinstance(weight_field(result, "co"), StatisticSelector)


async def test_an_existing_weight_is_offered_back(hass, config_entry, mock_client):
    """A saved statistic id must survive a trip through the form untouched."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={CONF_UPDATE_INTERVAL: 6, CONF_WEIGHTS: {"co": "sensor.cooling_activity"}},
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    for marker in result["data_schema"].schema:
        if str(marker) == "weight_co":
            assert marker.description["suggested_value"] == "sensor.cooling_activity"
            break
    else:
        pytest.fail("weight_co was not offered")


async def test_weight_selection_is_stored_per_utility(hass, config_entry, mock_client):
    """Each utility gets its own weight, and blank ones are left out."""
    hass.states.async_set("sensor.cooling_activity", "0", {"state_class": "measurement"})

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: USERNAME,
            CONF_UPDATE_INTERVAL: 6,
            "weight_co": "sensor.cooling_activity",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Heating was left blank, so only cooling is stored.
    assert config_entry.options[CONF_WEIGHTS] == {"co": "sensor.cooling_activity"}


async def test_a_weight_survives_its_utility_missing_from_a_poll(
    hass, config_entry, mock_client, view_model
):
    """A degraded scrape must not permanently delete a utility's weight.

    The form only renders fields for utilities in the current payload, so a
    utility that drops out of one poll has no field - and rebuilding options
    from the form alone would silently discard its saved weight.
    """
    import copy

    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={
            CONF_UPDATE_INTERVAL: 6,
            CONF_WEIGHTS: {"co": "sensor.cooling_zones", "hh": "sensor.heating_demand"},
        },
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Cooling drops out of the payload; heating is all the form will render.
    degraded = copy.deepcopy(view_model)
    degraded["UtilityDetails"] = [
        u for u in degraded["UtilityDetails"] if u["ShortName"] == "HH"
    ]
    hass.data[DOMAIN][config_entry.entry_id].data = degraded

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: USERNAME,
            CONF_UPDATE_INTERVAL: 6,
            "weight_hh": "sensor.heating_demand",
        },
    )
    await hass.async_block_till_done()

    assert config_entry.options[CONF_WEIGHTS] == {
        "co": "sensor.cooling_zones",
        "hh": "sensor.heating_demand",
    }


async def test_clearing_a_rendered_field_still_removes_its_weight(
    hass, config_entry, mock_client
):
    """Carrying absent utilities through must not make a weight unclearable."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={CONF_UPDATE_INTERVAL: 6, CONF_WEIGHTS: {"co": "sensor.cooling_zones"}},
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USERNAME, CONF_UPDATE_INTERVAL: 6},
    )
    await hass.async_block_till_done()

    assert CONF_WEIGHTS not in config_entry.options
