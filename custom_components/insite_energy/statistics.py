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
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STAT_COST_SUFFIX, STAT_ENERGY_SUFFIX
from .util import parse_pence, parse_reading_date, utility_key

_LOGGER = logging.getLogger(__name__)

# Readings arrive at most daily, so a window longer than this is a sign the
# cache is stale or a reading date is wrong rather than a genuine gap. Emitting
# a year of hourly rows on a bad date would be slow and hard to undo.
MAX_WINDOW_HOURS = 24 * 400


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


def _hours_between(start: datetime, end: datetime) -> list[datetime]:
    """Return every hour boundary in [start, end)."""
    hours: list[datetime] = []
    current = dt_util.as_utc(start).replace(minute=0, second=0, microsecond=0)
    limit = dt_util.as_utc(end)
    while current < limit:
        hours.append(current)
        current += timedelta(hours=1)
    return hours


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
        start = dt_util.utc_from_timestamp(row["start"])
        by_hour[start.replace(minute=0, second=0, microsecond=0)] = row

    if any(row.get("mean") is not None for row in rows):
        return [max(0.0, float((by_hour.get(h) or {}).get("mean") or 0.0)) for h in hours]

    # Recorder derives `change` by differencing sums, seeding from the last row
    # *strictly before* the window - but when there is no such row it seeds from
    # zero, so the earliest row's change is its whole lifetime total rather than
    # an hour's rise. A meter installed mid-window would otherwise weigh its
    # first hour by tens of thousands and collapse the entire spread onto it.
    # `change == sum` is that signature; discarding one hour is the safe read.
    earliest = min((r for r in rows if r.get("change") is not None),
                   key=lambda r: r["start"], default=None)
    suspect = None
    if earliest is not None and earliest.get("sum") is not None:
        if float(earliest["change"]) == float(earliest["sum"]):
            suspect = dt_util.utc_from_timestamp(earliest["start"]).replace(
                minute=0, second=0, microsecond=0
            )
            _LOGGER.debug(
                "Weight source has no history before %s; ignoring that hour "
                "rather than treating a lifetime total as an hourly rise",
                suspect.isoformat(),
            )

    return [
        0.0 if h == suspect
        else max(0.0, float((by_hour.get(h) or {}).get("change") or 0.0))
        for h in hours
    ]


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

    if source.startswith(f"{DOMAIN}:"):
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
        hours[0],
        hours[-1] + timedelta(hours=1),
        {source},
        "hour",
        None,
        # "change" is recorder differencing a cumulative sum for us, so this
        # window needs no widening. "sum" comes along to spot the one case
        # where recorder had no baseline to difference against - see
        # _series_to_weights.
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
    for hour, value in zip(hours, values):
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
            except Exception:  # noqa: BLE001 - one bad utility must not stop the rest
                _LOGGER.exception("Failed to publish spread statistics for %s", key)
    except Exception:  # noqa: BLE001
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

    # Republishing a window would count the period twice, and the cached
    # snapshot this window is derived from can outlive the publish that used it:
    # a reload within CACHE_SAVE_DELAY of a poll leaves the new coordinator
    # reading the stale reading off disk. Energy is the authority; cost may
    # legitimately lag it when a rate was missing on an earlier run.
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
