"""The one weighting test that goes through a real recorder.

Every other weighting test in `test_statistics.py` asserts against a fixture, so
the fixture and the code can agree on a wrong unit and all of them still pass -
which is exactly what happened. Recorder emits epoch seconds; the code divided by
1000, so every hour landed in 1970, matched nothing, and weighting silently
degraded to an even spread with only a DEBUG line to show for it.

This file is separate because `recorder_mock` refuses to build a database once
`hass` exists, and the autouse fixture in conftest pulls `hass` in first. The
override below shadows it for this module only, so the rest of the suite keeps
running without the cost of a real database.
"""
from __future__ import annotations

from datetime import timedelta
from functools import partial
from unittest.mock import patch

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.insite_energy.statistics import (
    async_publish_spread_statistics,
    statistic_id,
)
from custom_components.insite_energy.util import parse_reading_date

from .test_statistics import payload


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Shadow conftest's fixture so recorder_mock is built before hass."""
    return


async def test_real_recorder_weights_are_read_in_the_right_unit(hass):
    """Read a weight back through the real recorder rather than a fixture.

    This is the only test here that does not take the timestamp unit on faith,
    and it fails with either `/ 1000` or `* 1000` reinstated.
    """
    start = dt_util.as_utc(
        parse_reading_date("2026/07/04 00:00", dt_util.DEFAULT_TIME_ZONE)
    )

    # Outside our own namespace: statistics.py refuses to weight a utility by
    # anything this integration publishes itself.
    async_add_external_statistics(
        hass,
        {
            "has_mean": True,
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
            "name": "Test Activity",
            "source": "test",
            "statistic_id": "test:activity",
            "unit_of_measurement": None,
            "unit_class": None,
        },
        [
            {"start": start + timedelta(hours=i), "mean": mean, "min": mean, "max": mean}
            for i, mean in enumerate([0.0, 0.0, 1.0, 0.0])
        ],
    )
    await async_wait_recording_done(hass)

    captured: list[tuple[dict, list]] = []
    with patch(
        "custom_components.insite_energy.statistics.async_add_external_statistics",
        lambda _h, metadata, rows: captured.append((metadata, rows)),
    ):
        await async_publish_spread_statistics(
            hass,
            payload("100.0", "2026/07/04 00:00"),
            payload("110.0", "2026/07/04 04:00"),
            {"co": "test:activity"},
        )

    rows = dict((m["statistic_id"], r) for m, r in captured)[statistic_id("co", "energy")]
    per_hour = [r["state"] for r in rows]

    assert len(per_hour) == 4
    assert sum(per_hour) == pytest.approx(10.0)
    # All of it on the single active hour. Reading the timestamps as
    # milliseconds gave four equal shares of 2.5 instead.
    assert per_hour == pytest.approx([0.0, 0.0, 10.0, 0.0])


async def test_published_statistics_are_accepted_by_recorder(hass, caplog):
    """Let our own metadata reach the real recorder and read the rows back.

    Every other test patches `async_add_external_statistics` away, so nothing
    else checks that what we describe to the recorder is actually valid. It has
    tightened twice already - `mean_type` replacing the deprecated `has_mean`,
    and `unit_class` for unit conversion - and a custom integration only gets a
    log warning for getting it wrong, so this asserts on the log too.
    """
    await async_publish_spread_statistics(
        hass,
        payload("100.0", "2026/07/04 00:00"),
        payload("110.0", "2026/07/04 04:00"),
        {},
    )
    await async_wait_recording_done(hass)

    energy_id = statistic_id("co", "energy")
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.as_utc(parse_reading_date("2026/07/04 00:00", dt_util.DEFAULT_TIME_ZONE)),
        dt_util.as_utc(parse_reading_date("2026/07/05 00:00", dt_util.DEFAULT_TIME_ZONE)),
        {energy_id},
        "hour",
        None,
        {"sum"},
    )

    assert len(rows[energy_id]) == 4
    assert rows[energy_id][-1]["sum"] == pytest.approx(10.0)
    # Recorder reports incorrect usage rather than raising for a custom
    # integration, so a bad metadata shape would otherwise pass silently.
    assert "doesn't specify" not in caplog.text

    meta = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={energy_id})
    )
    assert meta[energy_id][1]["unit_class"] == "energy"
    assert meta[energy_id][1]["mean_type"] == StatisticMeanType.NONE


async def test_a_weight_source_with_no_prior_history_does_not_hijack_the_spread(hass):
    """A meter whose statistics begin inside the window must not eat it whole.

    Recorder derives `change` by differencing sums and seeds from the last row
    *strictly before* the window; with no such row it seeds from zero, so the
    first row's change is its entire lifetime total. A water meter added
    mid-window would weigh one hour by tens of thousands and collapse the whole
    spread onto it - the exact single-spike artefact this feature removes.
    """
    start = dt_util.as_utc(
        parse_reading_date("2026/07/04 00:00", dt_util.DEFAULT_TIME_ZONE)
    )

    # Cumulative, and it starts *inside* the window carrying a large lifetime
    # total, with small honest hourly rises after that.
    async_add_external_statistics(
        hass,
        {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": "Test Meter",
            "source": "test",
            "statistic_id": "test:meter",
            "unit_of_measurement": "L",
            "unit_class": None,
        },
        [
            {"start": start + timedelta(hours=1), "sum": 12000.0},
            {"start": start + timedelta(hours=2), "sum": 12001.0},
            {"start": start + timedelta(hours=3), "sum": 12003.0},
        ],
    )
    await async_wait_recording_done(hass)

    captured: list[tuple[dict, list]] = []
    with patch(
        "custom_components.insite_energy.statistics.async_add_external_statistics",
        lambda _h, metadata, rows: captured.append((metadata, rows)),
    ):
        await async_publish_spread_statistics(
            hass,
            payload("100.0", "2026/07/04 00:00"),
            payload("110.0", "2026/07/04 04:00"),
            {"co": "test:meter"},
        )

    rows = dict((m["statistic_id"], r) for m, r in captured)[statistic_id("co", "energy")]
    per_hour, prev = [], 0.0
    for row in rows:
        per_hour.append(row["sum"] - prev)
        prev = row["sum"]

    assert sum(per_hour) == pytest.approx(10.0)
    # The unusable hour is dropped, so the split follows the two honest rises
    # of 1 and 2 litres - a third and two thirds.
    assert per_hour == pytest.approx([0.0, 0.0, 10.0 / 3, 20.0 / 3])
    # Before the fix this was [0, 10, ~0, ~0]: everything on the bogus hour.
    assert per_hour[1] < 1.0
