"""Tests for spreading a meter reading across the period it covers.

The property that matters throughout: weighting changes *where* the energy
lands, never how much of it there is. Every test that touches weighting also
checks the rows still sum to the meter delta.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise
import re
from unittest.mock import patch
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.insite_energy.const import DOMAIN
from custom_components.insite_energy.statistics import (
    WEIGHT_HISTORY_LOOKBACK,
    async_publish_spread_statistics,
    statistic_id,
)
from custom_components.insite_energy.util import parse_reading_date

# The real cooling case: a month between readings, 231.3 kWh in one lump.
JULY = "2026/07/04 00:00"
AUGUST = "2026/08/04 00:00"
SEPTEMBER = "2026/09/04 00:00"
HOURS_IN_WINDOW = 744


@pytest.fixture(autouse=True)
def _pin_now(freezer):
    """Pin the clock past every reading date used below.

    The windows here are fixed calendar dates, and a reading dated in the future
    is refused outright, so without this the file quietly starts failing once
    real time walks past them - as a wrong-guard-fired error, nowhere near the
    real cause. Must stay ahead of the latest date used anywhere below, which is
    the October DST window rather than SEPTEMBER.
    """
    freezer.move_to("2026-12-01 00:00:00+00:00")


def window_start() -> datetime:
    """First hour of the spread window, in UTC.

    Derived the same way the code derives it, so these tests do not care what
    timezone the test instance happens to run in.
    """
    return dt_util.as_utc(parse_reading_date(JULY, dt_util.DEFAULT_TIME_ZONE))


def payload(reading: str, date: str, rate: str = "14.67p") -> dict:
    """A viewModel with a single cooling utility."""
    return {
        "UtilityDetails": [
            {
                "Name": "Cooling",
                "ShortName": "CO",
                "MeterReadingDate": date,
                "MeterSerialNumber": "1566TBC22",
                "LastMeterReading": reading,
                "Rates": rate,
                "StandingChargeValue": "24.53p",
            }
        ]
    }


class FakeRecorder:
    """Stands in for the recorder instance, running its jobs inline.

    The real one needs a database; everything under test here is arithmetic on
    what comes back from it, so running the lookups synchronously keeps these
    tests fast and lets each one state its own data.
    """

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def patch_recorder(series=None, last_sums=None):
    """Patch out every recorder touchpoint in the statistics module."""

    def _stats(_hass, start, end, ids, period, units, types):
        return {sid: (series or {}).get(sid, []) for sid in ids}

    def _last(_hass, _count, stat_id, _convert, _types):
        entry = (last_sums or {}).get(stat_id)
        if entry is None:
            return {}
        # Either a bare sum, or (sum, last published start) for the idempotency
        # guard - recorder returns both from the sum-only path.
        total, start = entry if isinstance(entry, tuple) else (entry, None)
        return {stat_id: [{"sum": total, "start": start}]}

    return (
        patch(
            "custom_components.insite_energy.statistics.get_instance",
            lambda _hass: FakeRecorder(),
        ),
        patch(
            "custom_components.insite_energy.statistics.statistics_during_period", _stats
        ),
        patch("custom_components.insite_energy.statistics.get_last_statistics", _last),
    )


async def publish(
    hass: HomeAssistant, previous, current, weights=None, series=None, last_sums=None
):
    """Run the publisher, capturing what it would have written.

    `series` maps a statistic id to the rows a weight lookup should return.
    """
    captured: list[tuple[dict, list]] = []
    # The publisher skips instances with no recorder; the fake one stands in for
    # it here, so tell hass it is present.
    hass.config.components.add("recorder")
    recorder = patch_recorder(series, last_sums)

    with patch(
        "custom_components.insite_energy.statistics.async_add_external_statistics",
        lambda _h, metadata, rows: captured.append((metadata, rows)),
    ), recorder[0], recorder[1], recorder[2]:
        await async_publish_spread_statistics(hass, previous, current, weights or {})

    return {m["statistic_id"]: rows for m, rows in captured}


def assert_no_swallowed_failure(caplog) -> None:
    """Every failure in this module is caught and logged, never raised.

    So an empty result means "a guard declined to publish" *or* "it blew up and
    the handler ate it" - and a test asserting `== {}` cannot tell the two
    apart. Check the log to make the distinction real.
    """
    assert "Failed to publish" not in caplog.text, caplog.text


def amounts(rows: list) -> list[float]:
    """The per-hour amounts, which are the `state` on each row."""
    return [r["state"] for r in rows]


def deltas(rows: list, base: float = 0.0) -> list[float]:
    """The per-hour rises in `sum` - what the energy dashboard actually plots.

    `state` is stored and never differenced; the dashboard reads `change`, which
    recorder derives from `sum`. Asserting only on `state` leaves the field that
    matters unpinned, which is how the millisecond bug survived a green suite.
    """
    out, prev = [], base
    for row in rows:
        out.append(row["sum"] - prev)
        prev = row["sum"]
    return out


def hourly(stat_id: str, start: datetime, values: list[float], cumulative=False):
    """Build weight-source rows as recorder returns them.

    A measurement statistic carries `mean`; a cumulative one carries `change`,
    which recorder derives from the sums itself - including the baseline before
    the window - so `values` are simply the per-hour rises either way.
    """
    key = "change" if cumulative else "mean"
    return {
        stat_id: [
            # Epoch seconds, as recorder emits them. This fixture agreeing with
            # the code is not evidence the unit is right - see
            # test_real_recorder_weights_are_read_in_the_right_unit.
            {"start": (start + timedelta(hours=i)).timestamp(), key: value}
            for i, value in enumerate(values)
        ]
    }


# --- spreading ---------------------------------------------------------------


async def test_reading_is_spread_evenly_when_unweighted(hass):
    """A month's reading becomes a flat band, not a single spike."""
    rows = await publish(hass, payload("273.3", JULY), payload("504.6", AUGUST))

    energy = rows[statistic_id("co", "energy")]
    assert len(energy) == HOURS_IN_WINDOW
    assert amounts(energy) == pytest.approx([231.3 / HOURS_IN_WINDOW] * HOURS_IN_WINDOW)
    assert energy[-1]["sum"] == pytest.approx(231.3)
    # Cumulative and ascending, an hour apart.
    assert energy[1]["start"] - energy[0]["start"] == timedelta(hours=1)


async def test_weights_redistribute_without_changing_the_total(hass):
    """The headline property: weighting moves energy, it never creates it."""
    start = window_start()
    # Active for one hour only, right in the middle.
    values = [0.0] * HOURS_IN_WINDOW
    values[400] = 1.0

    rows = await publish(
        hass,
        payload("273.3", JULY),
        payload("504.6", AUGUST),
        weights={"co": "sensor.cooling_activity"},
        series=hourly("sensor.cooling_activity", start, values),
    )

    energy = deltas(rows[statistic_id("co", "energy")])
    assert sum(energy) == pytest.approx(231.3)
    assert energy[400] == pytest.approx(231.3)
    assert sum(energy[:400]) == pytest.approx(0.0)
    assert sum(energy[401:]) == pytest.approx(0.0)
    # state and sum must tell the same story, or the statistics table and the
    # dashboard disagree about the same hour.
    assert amounts(rows[statistic_id("co", "energy")]) == pytest.approx(energy)


async def test_missing_weight_hours_receive_nothing(hass):
    """Hours with no activity record get no energy; their share moves."""
    start = window_start()
    # Only the first three hours have any weight at all.
    rows = await publish(
        hass,
        payload("273.3", JULY),
        payload("504.6", AUGUST),
        weights={"co": "sensor.cooling_activity"},
        series=hourly("sensor.cooling_activity", start, [1.0, 1.0, 2.0]),
    )

    energy = deltas(rows[statistic_id("co", "energy")])
    assert sum(energy) == pytest.approx(231.3)
    quarter = 231.3 / 4
    assert energy[:3] == pytest.approx([quarter, quarter, quarter * 2])
    assert sum(energy[3:]) == pytest.approx(0.0)


async def test_relative_weights_set_the_split(hass):
    """Hours split in proportion to their weight, whatever the scale."""
    rows = await publish(
        hass,
        payload("273.3", JULY),
        payload("504.6", AUGUST),
        weights={"co": "sensor.cooling_activity"},
        series=hourly("sensor.cooling_activity", window_start(), [3.5, 1.0]),
    )

    energy = deltas(rows[statistic_id("co", "energy")])
    assert sum(energy) == pytest.approx(231.3)
    assert energy[0] / energy[1] == pytest.approx(3.5)


async def test_cumulative_sources_use_the_per_hour_change(hass):
    """A cumulative source (the water meter) weighs by its rise, not its total.

    Recorder supplies `change` per row; reading the raw cumulative `sum` instead
    would make every later hour outweigh every earlier one regardless of use.
    """
    start = window_start()
    rows = await publish(
        hass,
        payload("273.3", JULY),
        payload("504.6", AUGUST),
        weights={"co": "sensor.water"},
        series=hourly("sensor.water", start, [10.0, 30.0], cumulative=True),
    )

    energy = deltas(rows[statistic_id("co", "energy")])
    assert energy[0] / energy[1] == pytest.approx(1 / 3)


async def test_the_window_end_is_not_widened_for_cumulative_sources(hass):
    """The end must cover exactly our hours; the start deliberately reaches back.

    Widening the end would pull an extra hour of real activity into the split.
    Widening the start cannot: those rows are only read to see whether the
    source had any history before the window, which is what separates "recorder
    had no baseline" from "the baseline was legitimately zero".
    """
    seen: dict[str, datetime] = {}

    def _stats(_hass, start, end, ids, period, units, types):
        seen.update(start=start, end=end, period=period, types=types)
        return {}

    with patch(
        "custom_components.insite_energy.statistics.async_add_external_statistics",
        lambda *a: None,
    ), patch(
        "custom_components.insite_energy.statistics.get_instance",
        lambda _hass: FakeRecorder(),
    ), patch(
        "custom_components.insite_energy.statistics.statistics_during_period", _stats
    ), patch(
        "custom_components.insite_energy.statistics.get_last_statistics",
        lambda *a: {},
    ):
        hass.config.components.add("recorder")
        await async_publish_spread_statistics(
            hass, payload("273.3", JULY), payload("504.6", AUGUST),
            {"co": "sensor.water"},
        )

    # The end is the real subject: widening it pulls an extra hour of activity
    # into the weights and shifts the split.
    assert seen["end"] == window_start() + timedelta(hours=HOURS_IN_WINDOW)
    assert seen["start"] == window_start() - WEIGHT_HISTORY_LOOKBACK
    assert seen["period"] == "hour"
    assert "change" in seen["types"]


async def test_a_utilitys_own_output_is_refused_as_its_weight(hass):
    """Weighting a utility by what we published for it would compound forever.

    The statistic picker cannot exclude ids, so this has to be caught here.
    """
    start = window_start()
    rows = await publish(
        hass,
        payload("273.3", JULY),
        payload("504.6", AUGUST),
        weights={"co": statistic_id("co", "energy")},
        series=hourly(statistic_id("co", "energy"), start, [1.0, 0.0]),
    )

    # Ignored, so it falls back to spreading evenly rather than copying itself.
    energy = deltas(rows[statistic_id("co", "energy")])
    # sums are rounded to the microunit, so per-hour deltas jitter there.
    assert energy == pytest.approx([231.3 / HOURS_IN_WINDOW] * HOURS_IN_WINDOW, abs=2e-6)


async def test_zero_total_weight_falls_back_to_flat(hass):
    """Nothing recorded as active all month; the energy still has to land."""
    start = window_start()
    rows = await publish(
        hass,
        payload("273.3", JULY),
        payload("504.6", AUGUST),
        weights={"co": "sensor.cooling_activity"},
        series=hourly("sensor.cooling_activity", start, [0.0] * 24),
    )

    energy = deltas(rows[statistic_id("co", "energy")])
    # sums are rounded to the microunit, so per-hour deltas jitter there.
    assert energy == pytest.approx([231.3 / HOURS_IN_WINDOW] * HOURS_IN_WINDOW, abs=2e-6)


async def test_published_sums_never_go_backwards(hass):
    """A sum that dips by 1e-14 renders as "-0 kWh" on the energy dashboard.

    Binary floats do not add up - 16.5 + 106.8 is 123.30000000000001 - so an
    unrounded running total drifts against neighbouring rows. This is the shape
    of a real artefact found in the cooling statistics.
    """
    rows = await publish(
        hass,
        payload("273.3", JULY),
        payload("504.6", AUGUST),
        last_sums={statistic_id("co", "energy"): 16.5},
    )

    assert rows, "nothing was published, so this would assert nothing"
    for stat_id, published in rows.items():
        sums = [r["sum"] for r in published]
        assert all(b >= a for a, b in pairwise(sums)), f"{stat_id} sum decreased"
        # And nothing carrying float noise below what a meter could resolve.
        assert all(s == round(s, 6) for s in sums), f"{stat_id} has unrounded sums"


async def test_cost_rows_mirror_energy_rows(hass):
    """Cost is energy at the current rate, hour for hour."""
    rows = await publish(hass, payload("273.3", JULY), payload("504.6", AUGUST))

    energy = rows[statistic_id("co", "energy")]
    cost = rows[statistic_id("co", "cost")]
    assert len(cost) == len(energy)
    assert [r["start"] for r in cost] == [r["start"] for r in energy]
    assert amounts(cost) == pytest.approx([v * 0.1467 for v in amounts(energy)])
    assert cost[-1]["sum"] == pytest.approx(231.3 * 0.1467)


# --- guards ------------------------------------------------------------------


async def test_first_run_seeds_without_emitting(hass, caplog):
    """With no previous snapshot there is no period to spread across."""
    assert await publish(hass, None, payload("504.6", AUGUST)) == {}
    assert_no_swallowed_failure(caplog)


async def test_meter_reset_is_skipped(hass, caplog):
    """A meter swap must never record negative consumption."""
    assert await publish(hass, payload("504.6", JULY), payload("10.0", AUGUST)) == {}
    assert_no_swallowed_failure(caplog)


async def test_unchanged_reading_date_is_skipped(hass, caplog):
    """A correction, not a new period - and a zero-length window divides by zero."""
    assert await publish(hass, payload("273.3", AUGUST), payload("280.0", AUGUST)) == {}
    assert_no_swallowed_failure(caplog)


async def test_missing_rate_emits_energy_only(hass):
    """Better a gap in cost than a confidently wrong number."""
    rows = await publish(
        hass, payload("273.3", JULY), payload("504.6", AUGUST, rate="n/a")
    )
    assert statistic_id("co", "energy") in rows
    assert statistic_id("co", "cost") not in rows


async def test_absurd_window_is_skipped(hass):
    """A wrong reading date must not emit years of hourly rows."""
    assert (
        await publish(
            hass, payload("273.3", "2000/01/01 00:00"), payload("504.6", AUGUST)
        )
        == {}
    )


async def test_a_broken_utility_does_not_stop_the_others(hass):
    """One malformed utility must not cost us the other's statistics."""
    previous = payload("273.3", JULY)
    current = payload("504.6", AUGUST)
    for doc in (previous, current):
        doc["UtilityDetails"].append(
            {"Name": "Broken", "ShortName": "BR", "LastMeterReading": "not a number"}
        )

    rows = await publish(hass, previous, current)
    assert statistic_id("co", "energy") in rows
    assert statistic_id("br", "energy") not in rows


async def test_cumulative_sum_continues_from_the_last_statistic(hass):
    """Published sums must continue the series, not restart it."""
    rows = await publish(
        hass,
        payload("273.3", JULY),
        payload("504.6", AUGUST),
        last_sums={statistic_id("co", "energy"): 1000.0},
    )

    energy = rows[statistic_id("co", "energy")]
    assert energy[-1]["sum"] == pytest.approx(1000.0 + 231.3)
    assert energy[0]["sum"] > 1000.0
    # Cost has no prior sum, so it starts from zero independently.
    assert rows[statistic_id("co", "cost")][-1]["sum"] == pytest.approx(231.3 * 0.1467)


# --- Not publishing the same window twice ------------------------------------


async def test_an_already_published_window_is_skipped(hass):
    """Republishing a window would count the period twice on the dashboard.

    The cached snapshot a window is derived from can outlive the publish that
    used it - an entry reload within CACHE_SAVE_DELAY of a poll leaves the new
    coordinator reading the stale reading off disk and spreading it again.
    """
    first = await publish(hass, payload("273.3", JULY), payload("504.6", AUGUST))
    energy = first[statistic_id("co", "energy")]
    last_start = energy[-1]["start"].timestamp()

    # Same window again, with the recorder now reporting what the first run wrote.
    second = await publish(
        hass,
        payload("273.3", JULY),
        payload("504.6", AUGUST),
        last_sums={
            statistic_id("co", "energy"): (231.3, last_start),
            statistic_id("co", "cost"): (33.93, last_start),
        },
    )
    assert second == {}


async def test_a_partially_overlapping_window_is_refused_loudly(hass, caplog):
    """A window straddling the published boundary must not pass quietly.

    It can happen when the on-disk snapshot outlives the publish that used it -
    a hard kill inside the cache-write delay, then days of downtime. The delta
    then spans both published and unpublished hours, and the two publishes come
    from different readings, so there is no honest way to split it. Skipping is
    the safe direction, but it drops real consumption, so it has to be visible
    rather than a debug line.
    """
    first = await publish(hass, payload("273.3", JULY), payload("504.6", AUGUST))
    energy = first[statistic_id("co", "energy")]
    # Halfway through the window that was just written.
    midway = energy[HOURS_IN_WINDOW // 2]["start"].timestamp()

    caplog.clear()
    second = await publish(
        hass,
        payload("273.3", JULY),
        payload("600.0", SEPTEMBER),
        last_sums={statistic_id("co", "energy"): (231.3, midway)},
    )

    assert second == {}
    assert "overlaps statistics already published" in caplog.text
    assert "WARNING" in caplog.text


async def test_the_next_window_still_publishes(hass):
    """The guard must not block windows that merely abut the last one.

    A publish covering [was_date, date) ends its last row at date - 1h, and the
    next window starts at date - strictly later, so this never fires falsely.
    """
    first = await publish(hass, payload("273.3", JULY), payload("504.6", AUGUST))
    last_start = first[statistic_id("co", "energy")][-1]["start"].timestamp()

    second = await publish(
        hass,
        payload("504.6", AUGUST),
        payload("510.0", "2026/08/05 00:00"),
        last_sums={statistic_id("co", "energy"): (231.3, last_start)},
    )
    assert statistic_id("co", "energy") in second
    assert sum(amounts(second[statistic_id("co", "energy")])) == pytest.approx(5.4)


async def test_a_corrupt_reading_date_is_rejected_before_building_hours(hass):
    """strptime accepts "0202/01/01", which is ~16M hours - a gigabyte of them.

    The span has to be judged from the dates, not from a materialised list.
    """
    with patch(
        "custom_components.insite_energy.statistics._hours_between"
    ) as hours_between:
        rows = await publish(
            hass, payload("273.3", "0202/01/01 00:00"), payload("504.6", AUGUST)
        )

    assert rows == {}
    hours_between.assert_not_called()


async def test_the_published_statistic_ids_are_stable(hass):
    """These ids are a user-facing contract, not an implementation detail.

    Changing the shape orphans every existing install: the Energy dashboard
    keeps pointing at the old id, silently loses all history, and a duplicate
    series starts alongside. Nothing else in this file would notice, because
    every other expectation is built by calling statistic_id() itself.
    """
    assert statistic_id("co", "energy") == "insite_energy:co_energy"
    assert statistic_id("hh", "cost") == "insite_energy:hh_cost"


async def test_a_key_that_is_not_a_slug_still_yields_a_valid_id(hass):
    """Recorder rejects anything outside [0-9a-z_], and rejects it per publish.

    The key is the portal's ShortName lowercased, so "H&C" or "Hot Water" would
    otherwise fail every publish for that utility, visible only as a swallowed
    traceback once a poll.
    """
    valid = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$")
    for key in ("H&C", "Hot Water", "HW-1", "hw_", "Wärme", "", "___"):
        for suffix in ("energy", "cost"):
            assert valid.match(statistic_id(key, suffix)), (key, suffix)


async def test_cost_follows_the_weighted_shape_not_a_flat_one(hass):
    """Cost must track energy hour for hour, including when weighted.

    The existing mirror test only runs unweighted, so a flat cost curve beside a
    weighted energy curve - two diverging lines on the dashboard - went unseen.
    """
    rows = await publish(
        hass,
        payload("273.3", JULY),
        payload("504.6", AUGUST),
        weights={"co": "sensor.cooling_activity"},
        series=hourly("sensor.cooling_activity", window_start(), [3.0, 1.0]),
    )

    energy = deltas(rows[statistic_id("co", "energy")])
    cost = deltas(rows[statistic_id("co", "cost")])
    assert cost == pytest.approx([v * 0.1467 for v in energy])
    # And the weighting really is present on both, not flat.
    assert cost[0] / cost[1] == pytest.approx(3.0)


# --- Window alignment --------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "third"),
    [
        # Minutes the portal could plausibly start reporting.
        ("2026/06/04 00:30", "2026/07/04 00:30", "2026/08/04 00:30"),
        # Midnight, but a second that pushes the instant off the hour.
        ("2026/06/04 00:00", "2026/07/04 00:00", "2026/08/04 00:00"),
    ],
)
async def test_consecutive_windows_never_share_an_hour(hass, first, second, third):
    """Window N's last row must not also be window N+1's first row.

    Flooring only the start of a window made the bucket containing its end
    belong to both it and its successor, whenever the reading's UTC instant was
    not hour-aligned. The already-published guard reads that shared row as a
    partial overlap and refuses the whole window, so every other reading was
    dropped - permanently, and with a warning blaming double counting.
    """
    window_a = await publish(hass, payload("100.0", first), payload("200.0", second))
    rows_a = window_a[statistic_id("co", "energy")]
    last_start = rows_a[-1]["start"].timestamp()

    window_b = await publish(
        hass,
        payload("200.0", second),
        payload("300.0", third),
        last_sums={statistic_id("co", "energy"): (100.0, last_start)},
    )

    rows_b = window_b[statistic_id("co", "energy")]
    assert rows_b, "the second window was refused as an overlap"
    assert rows_b[0]["start"] > rows_a[-1]["start"]
    assert rows_b[0]["start"] - rows_a[-1]["start"] == timedelta(hours=1)
    # And the delta still lands in full.
    assert sum(r["state"] for r in rows_b) == pytest.approx(100.0)


async def test_a_reading_dated_in_the_future_is_refused(hass, caplog):
    """A future date would write consumption for hours that have not happened.

    Worse than the phantom rows: every later poll is then refused, first as an
    older date and then as an already-published window, both at debug level - so
    the utility goes silent with nothing to show why.
    """
    result = await publish(
        hass, payload("100.0", AUGUST), payload("200.0", "2026/12/25 00:00")
    )

    assert result == {}
    assert "is in the future" in caplog.text
    assert_no_swallowed_failure(caplog)


async def test_our_own_meter_reading_entity_is_refused_as_a_weight(hass, caplog):
    """The picker offers our meter reading sensor, and it is the worst choice.

    That series is flat except for the hour a reading landed, so weighting by it
    would collapse the whole month onto that hour - exactly the spike this
    module exists to remove.
    """
    from homeassistant.helpers import entity_registry as er

    entity = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "co_reading", suggested_object_id="cooling_meter_reading"
    )

    result = await publish(
        hass,
        payload("273.3", JULY),
        payload("504.6", AUGUST),
        weights={"co": entity.entity_id},
        # All of it on one hour, which is what the real series looks like.
        series={entity.entity_id: [{"start": window_start().timestamp(), "mean": 500.0}]},
    )

    rows = result[statistic_id("co", "energy")]
    assert "this integration's own output" in caplog.text
    # Refused, so spread evenly rather than collapsed onto hour zero.
    spread = amounts(rows)
    assert spread[0] == pytest.approx(spread[-1])
    assert sum(spread) == pytest.approx(231.3)


# --- Daylight saving ---------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "expected", "label"),
    [
        # BST starts 29 March 2026: the local day is 23 hours long.
        ("2026/03/28 00:00", "2026/03/30 00:00", 47, "spring forward"),
        # BST ends 25 October 2026: 25 hours.
        ("2026/10/24 00:00", "2026/10/26 00:00", 49, "fall back"),
        ("2026/05/04 00:00", "2026/05/06 00:00", 48, "no transition"),
    ],
)
async def test_a_window_covers_the_hours_that_really_elapsed(
    hass, first, second, expected, label
):
    """Two local midnights are not always 48 hours apart.

    The spread has to follow real elapsed time, not wall-clock arithmetic, or a
    DST window either invents an hour of consumption or loses one. This is a
    UK-only integration, so Europe/London is the case that matters - and the
    suite otherwise runs under a zone whose transitions fall on different dates,
    with fixtures that straddle none of them.
    """
    tz = ZoneInfo("Europe/London")
    with patch.object(dt_util, "DEFAULT_TIME_ZONE", tz):
        rows = await publish(
            hass, payload("100.0", first), payload("200.0", second)
        )

    energy = rows[statistic_id("co", "energy")]
    assert len(energy) == expected, label
    # Every row an hour apart in real time, across the transition too.
    for earlier, later in pairwise(energy):
        assert later["start"] - earlier["start"] == timedelta(hours=1)
    # And the delta is neither inflated nor lost by the missing/extra hour.
    assert sum(amounts(energy)) == pytest.approx(100.0)
    assert energy[-1]["sum"] == pytest.approx(100.0)


async def test_dst_windows_still_do_not_share_an_hour(hass):
    """The window either side of a transition must abut, not overlap."""
    tz = ZoneInfo("Europe/London")
    with patch.object(dt_util, "DEFAULT_TIME_ZONE", tz):
        before = await publish(
            hass,
            payload("100.0", "2026/10/24 00:00"),
            payload("200.0", "2026/10/26 00:00"),
        )
        rows_before = before[statistic_id("co", "energy")]
        after = await publish(
            hass,
            payload("200.0", "2026/10/26 00:00"),
            payload("300.0", "2026/10/28 00:00"),
            last_sums={
                statistic_id("co", "energy"): (
                    100.0,
                    rows_before[-1]["start"].timestamp(),
                )
            },
        )

    rows_after = after[statistic_id("co", "energy")]
    assert rows_after, "the window after the transition was refused as an overlap"
    assert rows_after[0]["start"] - rows_before[-1]["start"] == timedelta(hours=1)
