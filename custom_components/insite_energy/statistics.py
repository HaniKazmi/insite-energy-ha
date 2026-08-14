"""Publish meter readings as statistics spread across the period they cover.

Insite reports a meter reading long after the fact, covering everything since
the previous reading - a month at a time for some utilities. Home Assistant
attributes the whole delta to the hour the reading happened to arrive, so the
Energy dashboard shows a month of usage as one spike.

`MeterReadingDate` tells us which period a reading actually covers, so we can
publish that consumption spread across the hours it belongs to. That has to be
external statistics rather than the entity's own: the period is already over by
the time we learn of it, so there is no live value to report, and the recorder
would overwrite anything we wrote to an entity-backed statistic.

Where a utility has an activity signal configured - an air conditioner running,
water being drawn - the spread is weighted by it, so energy lands on the hours
it was really used rather than smeared flat.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
import re
from typing import NamedTuple

from homeassistant.components.recorder import DOMAIN as RECORDER_DOMAIN, get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STAT_COST_SUFFIX, STAT_ENERGY_SUFFIX
from .util import parse_pence, parse_reading_date, utility_key

_LOGGER = logging.getLogger(__name__)

# Readings arrive at most daily, so a window longer than this is a sign the
# cache is stale or a reading date is wrong rather than a genuine gap. Emitting
# a year of hourly rows on a bad date would be slow and hard to undo.
MAX_WINDOW_HOURS = 24 * 400

# How far before a window to look for any sign the weight source already had
# statistics. Only its presence is read, never its values, so this only has to
# outlast a plausible gap - Home Assistant down for a few days - rather than
# reach back to the source's first ever row.
WEIGHT_HISTORY_LOOKBACK = timedelta(days=7)


def statistic_id(key: str, suffix: str) -> str:
    """Return the external statistic id for a utility.

    Recorder enforces `[\\da-z_]+:[\\da-z_]+` with no leading, trailing or
    doubled underscores, and rejects anything else outright. `utility_key` is
    the portal's ShortName lowercased, so a name like "H&C" or "Hot Water"
    would otherwise fail every publish for that utility - visible only as a
    swallowed traceback once per poll.

    Slugging happens here rather than in `utility_key`, because that value is
    also baked into entity unique ids and device identifiers: changing it would
    orphan whatever an existing install has already registered.
    """
    slug = re.sub(r"_+", "_", re.sub(r"[^a-z\d]", "_", key.lower())).strip("_")
    return f"{DOMAIN}:{slug or 'unknown'}_{suffix}"


def _floor_hour(value: datetime) -> datetime:
    """Return the start of the hour `value` falls in."""
    return value.replace(minute=0, second=0, microsecond=0)


def _hours_between(start: datetime, end: datetime) -> list[datetime]:
    """Return every hour boundary in [start, end), both ends snapped to the hour.

    Both ends are floored, not just the start. An hour has to belong to exactly
    one window: flooring only the start makes the bucket containing `end` the
    last row of this window *and* the first row of the next one, whenever a
    reading's UTC instant is not hour-aligned - either because the portal
    reported a real clock time rather than midnight, or because the local zone's
    offset is not a whole number of hours. The already-published guard below
    reads that shared row as a partial overlap and refuses the whole window, so
    every other reading is silently dropped and never retried.

    Snapping both keeps consecutive windows contiguous with no shared row and no
    gap. The cost is that the first and last partial hours of a window are
    attributed to the hour they start in, which is at most 59 minutes of skew on
    a window that is usually weeks long.
    """
    hours: list[datetime] = []
    current = _floor_hour(dt_util.as_utc(start))
    limit = _floor_hour(dt_util.as_utc(end))
    while current < limit:
        hours.append(current)
        current += timedelta(hours=1)
    return hours


def _suspect_hour(rows: list[dict], window_start: datetime) -> datetime | None:
    """The hour whose `change` is a lifetime total, not an hourly rise - if any.

    Recorder derives `change` by differencing sums against the last row strictly
    before the window. When there is no such row it seeds from zero, so that
    first `change` is the source's whole running total. An externally imported
    meter reading tens of thousands high would weigh its first hour by that,
    against neighbours weighing single digits, and collapse the entire spread
    onto it.

    The obvious test - `change == sum` - is not that condition. Recorder does
    `prev_sum = prev_sums.get(statistic_id) or 0`, so "no earlier row" and "an
    earlier row whose sum is exactly 0.0" produce byte-identical output. A
    cumulative meter that simply had not moved yet - away for the summer, a
    utility_meter just after its cycle reset - sits at sum 0.0 for every row, so
    the first hour it *does* move satisfies `change == sum` and would be thrown
    away. That is the one hour that mattered, and if it was the only active hour
    the whole window silently falls back to a flat spread.

    Widening the query does not disambiguate it either: recorder looks the
    baseline up separately from the requested window, so it already had that row.
    What does work is asking the question directly - `rows` now reaches back
    before the window (see WEIGHT_HISTORY_LOOKBACK), so an earlier row being
    present is proof recorder had something real to difference against, whatever
    its sum happened to be.
    """
    if any(dt_util.utc_from_timestamp(r["start"]) < window_start for r in rows):
        return None

    # No history within the lookback, so a first row equal to its own sum really
    # is a running total rather than an hour's worth.
    earliest = min(
        (r for r in rows if r.get("change") is not None),
        key=lambda r: r["start"],
        default=None,
    )
    if (
        earliest is None
        or earliest.get("sum") is None
        or float(earliest["change"]) != float(earliest["sum"])
    ):
        return None

    suspect = _floor_hour(dt_util.utc_from_timestamp(earliest["start"]))
    _LOGGER.debug(
        "Weight source has no history before %s; ignoring that hour rather than "
        "treating a lifetime total as an hourly rise",
        suspect.isoformat(),
    )
    return suspect


def _series_to_weights(rows: list[dict], hours: list[datetime]) -> list[float]:
    """Reduce one statistic's rows to a per-hour weight.

    A `measurement` statistic carries a mean - the average of whatever it
    measures over that hour, which is already a rate. A cumulative one carries
    `change`, the rise across the hour, which recorder works out for us; that is
    what lets a water meter be used directly, more accurately than its flow
    sensor.

    Hours with no row weigh nothing. That is deliberate: an hour we have no
    activity record for should not claim a share of the reading.
    """
    by_hour: dict[datetime, dict] = {}
    for row in rows:
        # Epoch *seconds*: recorder emits "end": start_ts + table_duration_seconds,
        # which fixes the unit. Only the websocket API scales to milliseconds, for
        # the frontend - reading these as ms lands every hour in 1970, matches
        # nothing, and silently zeroes every weight.
        by_hour[_floor_hour(dt_util.utc_from_timestamp(row["start"]))] = row

    if any(row.get("mean") is not None for row in rows):
        return [max(0.0, float((by_hour.get(h) or {}).get("mean") or 0.0)) for h in hours]

    suspect = _suspect_hour(rows, hours[0])

    return [
        0.0 if h == suspect
        else max(0.0, float((by_hour.get(h) or {}).get("change") or 0.0))
        for h in hours
    ]


def _is_own_output(hass: HomeAssistant, source: str) -> bool:
    """Whether a weight source is something this integration published.

    Two spellings have to be caught, and the prefix only covers one. The
    external statistics written below are `insite_energy:...`, but the meter
    reading entity is TOTAL_INCREASING, so it has long-term statistics of its
    own under `sensor....` - which the picker also offers. Weighting a utility
    by its own meter reading is the worst case of all: that series is flat
    except for the single hour the reading landed, so the whole delta collapses
    onto that hour - reproducing exactly the spike this module exists to remove,
    while reporting itself as "weighted by activity".
    """
    if source.startswith(f"{DOMAIN}:"):
        return True
    entity = er.async_get(hass).async_get(source)
    return entity is not None and entity.platform == DOMAIN


async def _async_weights(
    hass: HomeAssistant,
    source: str | None,
    hours: list[datetime],
) -> list[float]:
    """Read the configured weight source into a per-hour series.

    One source per utility. Anything more complicated - blending hot water with
    space heating, say - belongs in a template sensor, which can express it far
    better than a list of coefficients could.
    """
    if not source:
        return [0.0] * len(hours)

    if _is_own_output(hass, source):
        # Weighting a utility by its own published output would make each spread
        # a copy of the last one's shape, drifting further from reality every
        # time and never saying so. The statistic picker cannot exclude ids, so
        # refuse here instead.
        _LOGGER.warning(
            "Weight source %s is this integration's own output; ignoring it and "
            "spreading evenly instead",
            source,
        )
        return [0.0] * len(hours)

    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        hours[0] - WEIGHT_HISTORY_LOOKBACK,
        hours[-1] + timedelta(hours=1),
        {source},
        "hour",
        None,
        # The *end* is never widened: an extra hour of activity there would pull
        # real weight into the split. The start is, and only to see whether any
        # row exists before the window - that is the one thing that separates
        # "recorder had no baseline" from "the baseline was legitimately zero".
        # It does not change the `change` values within the window, because
        # recorder looks its baseline up independently of what we ask for.
        {"mean", "change", "sum"},
    )
    series = _series_to_weights(rows.get(source, []), hours)
    if not any(series):
        _LOGGER.debug("Weight source %s contributed nothing over the window", source)
    return series


class _LastPublished(NamedTuple):
    """Where a statistic got to: its running total, and the hour it reached."""

    total: float
    start: float | None


def _last_sums(hass: HomeAssistant, ids: tuple[str, ...]) -> dict[str, _LastPublished]:
    """Return where each statistic got to last time.

    Read back from the recorder rather than tracked ourselves, so a database
    restore or a manual correction is picked up rather than fought with. Runs in
    the executor, so all the lookups share one trip. `start` comes back for free
    - recorder's sum-only path emits it alongside the sum - and is what lets us
    notice a window we have already written.
    """
    results: dict[str, _LastPublished] = {}
    for stat_id in ids:
        rows = get_last_statistics(hass, 1, stat_id, True, {"sum"}).get(stat_id) or []
        if rows:
            results[stat_id] = _LastPublished(
                float(rows[0].get("sum") or 0.0), rows[0].get("start")
            )
        else:
            results[stat_id] = _LastPublished(0.0, None)
    return results


def _rows(hours: list[datetime], values: list[float], base: float) -> list[StatisticData]:
    """Turn per-hour amounts into cumulative statistic rows.

    Sums are rounded on the way out because binary floats do not add up: 16.5 +
    106.8 lands on 123.30000000000001, and one neighbouring row holding 123.3
    is enough to give a day a change of -1.4e-14, which the energy dashboard
    faithfully renders as "-0 kWh". Rounding to the microunit is far below
    anything a meter resolves, and leaves sums that never go backwards.

    Only the sum is snapped, since only it gets differenced. The running total
    stays unrounded so the rounding cannot accumulate, and `state` is left
    exact - quantising it would lose real precision on small per-hour costs for
    no gain.
    """
    rows: list[StatisticData] = []
    total = base
    for hour, value in zip(hours, values, strict=True):
        total += value
        rows.append(StatisticData(start=hour, state=value, sum=round(total, 6)))
    return rows


def _metadata(
    key: str, suffix: str, name: str, unit: str | None, unit_class: str | None
) -> StatisticMetaData:
    """Describe one of our statistics to the recorder.

    `unit_class` groups units the recorder knows how to convert between, so kWh
    is "energy" while a currency has none. Recorder asks for both it and
    `mean_type` explicitly, and warns when either is missing.

    These are cumulative sums with no average to speak of, hence NONE. The older
    `has_mean` flag it replaces is deprecated and due out in 2026.4, so it is
    left off entirely rather than set alongside.
    """
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id(key, suffix),
        unit_of_measurement=unit,
        unit_class=unit_class,
    )


async def async_publish_spread_statistics(
    hass: HomeAssistant,
    previous: dict | None,
    current: dict,
    weight_config: dict[str, str],
) -> None:
    """Publish spread statistics for every utility whose reading advanced.

    Statistics must never cost us a poll: the entities are the point of this
    integration and they work fine without them. Every failure mode is
    swallowed here - a recorder-less instance, one malformed utility, or
    anything unforeseen - so callers can simply await this.
    """
    try:
        if not previous:
            # Nothing to measure a period against yet.
            return

        if RECORDER_DOMAIN not in hass.config.components:
            _LOGGER.debug("Recorder not set up; skipping spread statistics")
            return

        before = {
            utility_key(u): u
            for u in (previous.get("UtilityDetails") or [])
            if u.get("Name")
        }

        for utility in current.get("UtilityDetails") or []:
            if not utility.get("Name"):
                continue
            key = utility_key(utility)
            if (was := before.get(key)) is None:
                continue
            try:
                await _async_publish_one(hass, key, was, utility, weight_config.get(key))
            # Deliberately blind: one malformed utility must not stop the rest.
            except Exception:
                _LOGGER.exception("Failed to publish spread statistics for %s", key)
    # Deliberately blind: statistics are a bonus, and must never cost a poll.
    except Exception:
        _LOGGER.exception("Failed to publish spread statistics")


async def _async_publish_one(
    hass: HomeAssistant,
    key: str,
    previous: dict,
    current: dict,
    weight_source: str | None,
) -> None:
    """Spread one utility's new reading across the period it covers."""
    tz = dt_util.DEFAULT_TIME_ZONE
    name = str(current.get("Name") or key)

    try:
        reading = float(current["LastMeterReading"])
        was_reading = float(previous["LastMeterReading"])
    except (KeyError, TypeError, ValueError):
        return

    date = parse_reading_date(current.get("MeterReadingDate"), tz)
    was_date = parse_reading_date(previous.get("MeterReadingDate"), tz)
    if date is None or was_date is None:
        return

    if date <= was_date:
        if reading != was_reading:
            _LOGGER.debug(
                "%s reading changed without a new reading date; treating as a "
                "correction and leaving statistics alone",
                name,
            )
        return

    if reading < was_reading:
        _LOGGER.warning(
            "%s meter went backwards (%s to %s); skipping rather than "
            "recording negative consumption",
            name,
            was_reading,
            reading,
        )
        return

    # Checked from the dates, before building anything: strptime happily accepts
    # a year like "0202", and _hours_between would materialise ~16M datetimes -
    # about a gigabyte, and seconds of blocked event loop - before a guard on
    # len(hours) ever got to reject them.
    span_hours = int((date - was_date).total_seconds() // 3600)
    if span_hours > MAX_WINDOW_HOURS:
        _LOGGER.warning(
            "%s reading spans %s hours, which is too long to be real; skipping",
            name,
            span_hours,
        )
        return

    # The span check bounds how long a window is, never where it ends, so a
    # reading dated a year out still passes it. Publishing that would write a
    # year of consumption into hours that have not happened, and every later
    # poll would then be refused - first as an older date, then as an
    # already-published window - leaving the utility silent until real time
    # caught up. Both of those refusals are debug-level, so it would look like
    # nothing was wrong.
    if date > dt_util.utcnow():
        _LOGGER.warning(
            "%s reading is dated %s, which is in the future; skipping rather "
            "than publishing consumption for hours that have not happened",
            name,
            date.isoformat(),
        )
        return

    hours = _hours_between(was_date, date)
    if not hours:
        return

    delta = reading - was_reading
    weights = await _async_weights(hass, weight_source, hours)
    total_weight = sum(weights)
    if total_weight > 0:
        per_hour = [delta * w / total_weight for w in weights]
    else:
        # Either nothing is configured, or nothing was recorded as active all
        # period. The energy still has to go somewhere.
        per_hour = [delta / len(hours)] * len(hours)

    energy_id = statistic_id(key, STAT_ENERGY_SUFFIX)
    cost_id = statistic_id(key, STAT_COST_SUFFIX)

    # Fetched together: the two totals are independent of each other and of the
    # writes, so there is no reason to pay for two trips to the database.
    rate = parse_pence(current.get("Rates"))
    if rate is None:
        _LOGGER.debug("No rate for %s; publishing energy without cost", name)
    wanted = (energy_id,) if rate is None else (energy_id, cost_id)
    bases = await get_instance(hass).async_add_executor_job(
        _last_sums, hass, wanted
    )

    # Republishing a window would count the period twice, which a restored
    # database, a hand correction or a snapshot older than the last publish can
    # all lead to. Energy is the authority; cost may legitimately lag it when a
    # rate was missing on an earlier run.
    last_start = bases[energy_id].start
    if last_start is not None and hours[0].timestamp() <= last_start:
        if hours[-1].timestamp() <= last_start:
            # The ordinary case: an exact replay of a window already written.
            _LOGGER.debug(
                "%s window starting %s has already been published; skipping",
                name,
                hours[0].isoformat(),
            )
            return
        # Partial overlap. Trimming to the uncovered hours and spreading the
        # whole delta over them would count the overlap twice, since the delta
        # spans the published part too - and the two publishes derive from
        # different readings, so there is no honest way to split it here.
        # Skipping loses the tail, which is the safe direction, but say so
        # loudly: it means real consumption is missing from the dashboard.
        _LOGGER.warning(
            "%s reading covers %s to %s, which overlaps statistics already "
            "published up to %s. Skipping to avoid double counting - "
            "consumption after that point is not recorded and needs correcting "
            "by hand if it matters",
            name,
            hours[0].isoformat(),
            hours[-1].isoformat(),
            dt_util.utc_from_timestamp(last_start).isoformat(),
        )
        return

    async_add_external_statistics(
        hass,
        _metadata(
            key, STAT_ENERGY_SUFFIX, f"{name} Energy",
            UnitOfEnergy.KILO_WATT_HOUR, "energy",
        ),
        _rows(hours, per_hour, bases[energy_id].total),
    )

    if rate is not None:
        async_add_external_statistics(
            hass,
            _metadata(key, STAT_COST_SUFFIX, f"{name} Cost", "GBP", None),
            _rows(hours, [v * rate for v in per_hour], bases[cost_id].total),
        )

    _LOGGER.info(
        "Spread %.3f kWh of %s across %d hours (%s to %s)%s",
        delta,
        name,
        len(hours),
        was_date.isoformat(),
        date.isoformat(),
        " weighted by activity" if total_weight > 0 else " evenly",
    )
