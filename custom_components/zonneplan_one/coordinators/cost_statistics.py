"""
Daily *actual cost* statistics (EUR), as shown in the Zonneplan app.

The kWh statistics in ``statistics.py`` are hourly. The money values Zonneplan bills are only
published per local day (``electricity-delivered/charts/hours?date=D`` for the P1 connection,
``home_battery_installation/charts/days`` for the Powerplay battery), so these statistics have
one row per local day, placed at the hour start of local midnight, with a running ``sum``.

Design rules:

* One row is always (re)written as a whole, with ``sum = baseline + value`` where the baseline is
  the stored ``sum`` of the last row *before* that day. Re-importing a day is therefore idempotent
  and keeps the running sum continuous.
* Every day is committed on its own, so an interrupted backfill resumes from the last stored day.
* Money in the API is in 1e-7 EUR; ``money()`` is the only place that scales it.
* Positive ``net_cost`` = you paid; ``production_cost`` is the amount *earned* for feed-in (positive).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import homeassistant.util.dt as dt_util
from homeassistant.components.recorder import get_instance
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

from ..api import ZonneplanApiError, ZonneplanRateLimitError
from ..const import DOMAIN, ZONNEPLAN_API_TIME_ZONE

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MONEY_FACTOR = 1e-7
ENERGY_FACTOR = 0.001
# Zonneplan history starts in August 2025; earlier days are null.
COST_HISTORY_START = date(2025, 8, 1)
# Days re-imported on every daily refresh, because Zonneplan corrects recent data.
REFRESH_DAYS = 3
# Longest gap the unattended refresh fills by itself; older gaps need the service.
MAX_AUTO_GAP_DAYS = 30
# Pause between per-day API calls of a backfill (~2.5 requests/min stays well under the rate limit).
BACKFILL_PACE_SECONDS = 1.5
BACKFILL_MAX_RETRIES = 3
YESTERDAY_REFRESH_CUTOFF = time(hour=0, minute=20)

CURRENCY = "EUR"


def money(raw: float | None) -> float | None:
    """Convert an API money value (1e-7 EUR) to EUR."""
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return round(raw * MONEY_FACTOR, 7)


def energy_kwh(raw: float | None) -> float | None:
    """Convert an API energy value (Wh) to kWh."""
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return round(raw * ENERGY_FACTOR, 6)


def local_midnight(day: date) -> datetime:
    """Local (Europe/Amsterdam) midnight of ``day``, as a UTC datetime (hour-aligned)."""
    return datetime.combine(day, time.min, tzinfo=ZONNEPLAN_API_TIME_ZONE).astimezone(dt_util.UTC)


def local_day_of(value: datetime | str) -> date:
    """Local calendar day of an API instant."""
    if isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            msg = f"Cannot parse datetime {value!r}"
            raise ValueError(msg)
        value = parsed
    return dt_util.as_utc(value).astimezone(ZONNEPLAN_API_TIME_ZONE).date()


def local_today() -> date:
    return dt_util.now(ZONNEPLAN_API_TIME_ZONE).date()


@dataclass(frozen=True)
class DayCosts:
    """Actual electricity money for one local day, in EUR (tax incl. and excl.)."""

    day: date
    delivery_incl: float
    delivery_excl: float
    production_incl: float
    production_excl: float
    delivered_kwh: float | None = None
    produced_kwh: float | None = None

    @property
    def net_incl(self) -> float:
        return round(self.delivery_incl - self.production_incl, 7)

    @property
    def net_excl(self) -> float:
        return round(self.delivery_excl - self.production_excl, 7)

    def as_values(self) -> dict[str, float]:
        return {
            "delivery_cost": self.delivery_incl,
            "production_cost": self.production_incl,
            "net_cost": self.net_incl,
            "delivery_cost_excl_tax": self.delivery_excl,
            "production_cost_excl_tax": self.production_excl,
            "net_cost_excl_tax": self.net_excl,
        }

    def as_attributes(self) -> dict[str, float | None]:
        return {
            "delivery_costs_incl_tax": self.delivery_incl,
            "delivery_costs_excl_tax": self.delivery_excl,
            "production_costs_incl_tax": self.production_incl,
            "production_costs_excl_tax": self.production_excl,
            "net_cost_excl_tax": self.net_excl,
            "delivered_kwh": self.delivered_kwh,
            "produced_kwh": self.produced_kwh,
        }


def costs_from_meta(meta: dict[str, Any] | None, day: date) -> DayCosts | None:
    """
    Build ``DayCosts`` from a group ``meta`` holding the day's totals.

    NOTE: ``meta.result_tax_included`` of a group is delivery + production, *not* the net result,
    so the net is always computed here from delivery and production.
    """
    if not meta:
        return None
    delivery_incl = money(meta.get("delivery_costs_incl_tax"))
    production_incl = money(meta.get("production_costs_incl_tax"))
    delivery_excl = money(meta.get("delivery_costs_excl_tax"))
    production_excl = money(meta.get("production_costs_excl_tax"))
    if delivery_incl is None or production_incl is None:
        return None
    return DayCosts(
        day=day,
        delivery_incl=delivery_incl,
        delivery_excl=delivery_excl if delivery_excl is not None else 0.0,
        production_incl=production_incl,
        production_excl=production_excl if production_excl is not None else 0.0,
        delivered_kwh=energy_kwh(meta.get("energy_delivered_sum")),
        produced_kwh=energy_kwh(meta.get("energy_produced_sum")),
    )


def costs_from_hours_payload(payload: dict[str, Any] | None, day: date) -> DayCosts | None:
    """Day costs from an ``electricity-delivered/charts/hours?date=D`` payload."""
    for group in (payload or {}).get("measurement_groups") or []:
        if group.get("type") != "hours":
            continue
        group_day = local_day_of(group["date"]) if group.get("date") else day
        if group_day != day:
            _LOGGER.warning("Hours payload for %s holds day %s, ignoring", day, group_day)
            return None
        return costs_from_meta(group.get("meta"), day)
    return None


def costs_from_live_payload(payload: dict[str, Any] | None) -> DayCosts | None:
    """Today's costs so far from the bare ``/electricity-delivered`` payload (group 0 / ``live``)."""
    groups = (payload or {}).get("measurement_groups") or []
    group = next((g for g in groups if g.get("type") == "live"), groups[0] if groups else None)
    if not group or not group.get("date"):
        return None
    return costs_from_meta(group.get("meta"), local_day_of(group["date"]))


def _period_costs(group: dict[str, Any] | None, day: date) -> DayCosts | None:
    return costs_from_meta(group.get("meta"), day) if group else None


def net_cost_sensor_data(payload: dict[str, Any] | None) -> dict[str, Any]:
    """
    Today / this month / this year net-cost block, injected into the coordinator data.

    Mirrors the groups the existing delivery/production cost sensors read (live, days, months).
    """
    groups = (payload or {}).get("measurement_groups") or []
    by_type = {g.get("type"): g for g in groups}
    today = local_today()
    result: dict[str, Any] = {}
    for key, group in (
        ("today", by_type.get("live") or (groups[0] if groups else None)),
        ("this_month", by_type.get("days") or (groups[2] if len(groups) > 2 else None)),  # noqa: PLR2004
        ("this_year", by_type.get("months") or (groups[3] if len(groups) > 3 else None)),  # noqa: PLR2004
    ):
        costs = _period_costs(group, today)
        if costs is None:
            continue
        result[key] = {
            "net_incl_tax": costs.net_incl,
            "date": group.get("date"),
            **costs.as_attributes(),
        }
    return result


def battery_days_from_chart(chart_data: Any) -> dict[date, float]:
    """Net earnings (EUR) per local day from a battery ``days`` chart (``data`` list)."""
    if not chart_data:
        return {}
    group = chart_data[0] if isinstance(chart_data, list) else chart_data
    days: dict[date, float] = {}
    for measurement in (group or {}).get("measurements") or []:
        measured_at = measurement.get("measured_at")
        value = money(measurement.get("value"))
        if not measured_at or value is None:
            continue
        days[local_day_of(measured_at)] = value
    return days


# --- statistic channels ---------------------------------------------------------------------


@dataclass(frozen=True)
class CostChannel:
    key: str
    statistic_id: str
    name: str


def electricity_channels(connection_uuid: str) -> tuple[CostChannel, ...]:
    suffix = connection_uuid.replace("-", "_")
    return (
        CostChannel("delivery_cost", f"{DOMAIN}:electricity_delivery_cost_{suffix}", "Stroomkosten afname (incl. btw)"),
        CostChannel("production_cost", f"{DOMAIN}:electricity_production_cost_{suffix}", "Terugleververgoeding (incl. btw)"),
        CostChannel("net_cost", f"{DOMAIN}:electricity_net_cost_{suffix}", "Netto stroomkosten (incl. btw)"),
        CostChannel("delivery_cost_excl_tax", f"{DOMAIN}:electricity_delivery_cost_excl_tax_{suffix}", "Stroomkosten afname (excl. btw)"),
        CostChannel("production_cost_excl_tax", f"{DOMAIN}:electricity_production_cost_excl_tax_{suffix}", "Terugleververgoeding (excl. btw)"),
        CostChannel("net_cost_excl_tax", f"{DOMAIN}:electricity_net_cost_excl_tax_{suffix}", "Netto stroomkosten (excl. btw)"),
    )


def powerplay_channels(contract_uuid: str) -> tuple[CostChannel, ...]:
    suffix = contract_uuid.replace("-", "_")
    return (CostChannel("earnings", f"{DOMAIN}:powerplay_earnings_{suffix}", "Powerplay opbrengst"),)


# --- recorder access ------------------------------------------------------------------------


class StatisticsStore(Protocol):
    """What the importer needs from the recorder (faked in tests)."""

    async def baseline_sum(self, statistic_id: str, before: datetime) -> float: ...

    async def last_day(self, statistic_id: str) -> date | None: ...

    def add(self, channel: CostChannel, rows: list[StatisticData]) -> None: ...


class RecorderStatisticsStore:
    """``StatisticsStore`` backed by the HA recorder."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def baseline_sum(self, statistic_id: str, before: datetime) -> float:
        """Return the stored ``sum`` of the last row strictly before ``before`` (0 if none)."""
        instance = get_instance(self.hass)
        rows = await instance.async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.as_utc(datetime(2020, 1, 1, tzinfo=dt_util.UTC)),
            before,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        series = rows.get(statistic_id) or []
        for row in reversed(series):
            if row.get("sum") is not None and row["start"] < before.timestamp():
                return float(row["sum"])
        return 0.0

    async def last_day(self, statistic_id: str) -> date | None:
        instance = get_instance(self.hass)
        last = await instance.async_add_executor_job(get_last_statistics, self.hass, 1, statistic_id, True, {"sum"})
        rows = (last or {}).get(statistic_id)
        if not rows:
            return None
        return datetime.fromtimestamp(rows[0]["start"], tz=dt_util.UTC).astimezone(ZONNEPLAN_API_TIME_ZONE).date()

    def add(self, channel: CostChannel, rows: list[StatisticData]) -> None:
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=channel.name,
            source=DOMAIN,
            statistic_id=channel.statistic_id,
            unit_class=None,
            unit_of_measurement=CURRENCY,
        )
        async_add_external_statistics(self.hass, metadata, rows)


# --- importer -------------------------------------------------------------------------------


class DailySeriesWriter:
    """
    Writes one row per local day for several channels with a correct running ``sum``.

    Days must be added in ascending order. The baseline is read from the store when the first
    day of a run is added, and held in memory afterwards, so a run does not depend on the
    recorder having committed its own earlier rows.
    """

    def __init__(self, store: StatisticsStore, channels: Iterable[CostChannel]) -> None:
        self.store = store
        self.channels = tuple(channels)
        self._sums: dict[str, float] | None = None
        self._last_day: date | None = None
        self.days_written = 0

    async def add_day(self, day: date, values: dict[str, float]) -> None:
        if self._last_day is not None and day <= self._last_day:
            msg = f"Days must ascend: {day} after {self._last_day}"
            raise ValueError(msg)
        start = local_midnight(day)
        if self._sums is None:
            self._sums = {channel.key: await self.store.baseline_sum(channel.statistic_id, start) for channel in self.channels}
        for channel in self.channels:
            value = values.get(channel.key)
            if value is None:
                continue
            self._sums[channel.key] = round(self._sums[channel.key] + value, 7)
            self.store.add(channel, [StatisticData(start=start, state=value, sum=self._sums[channel.key])])
        self._last_day = day
        self.days_written += 1


async def resume_start(store: StatisticsStore, channels: Iterable[CostChannel], floor: date) -> date:
    """Day to resume a backfill from: the oldest 'last stored day' of the channels, else ``floor``."""
    last_days = [await store.last_day(channel.statistic_id) for channel in channels]
    if not last_days or any(day is None for day in last_days):
        return floor
    return max(floor, min(d for d in last_days if d is not None))


def refresh_start(today: date, last_stored: date | None) -> date:
    """First day of the daily refresh: today-3, extended back to the last stored day (max 30 days)."""
    start = today - timedelta(days=REFRESH_DAYS)
    if last_stored is not None and last_stored < start:
        start = max(last_stored, today - timedelta(days=MAX_AUTO_GAP_DAYS))
    return start


async def fetch_with_retry[T](
    fetch: Callable[[], Awaitable[T]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    what: str = "",
) -> T:
    """Call ``fetch``, waiting out API rate limits (same policy as the kWh backfill: retry_after+20 s, x attempt)."""
    failures = 0
    while True:
        try:
            return await fetch()
        except ZonneplanRateLimitError as err:
            failures += 1
            if failures >= BACKFILL_MAX_RETRIES:
                _LOGGER.warning("Rate limit hit %s times fetching %s, giving up (resume later)", failures, what)
                raise
            wait = ((err.retry_after + 20) if err.retry_after is not None else 60) * failures
            _LOGGER.warning("Rate limited fetching %s (attempt %s/%s), waiting %s s", what, failures, BACKFILL_MAX_RETRIES, wait)
            await sleep(wait)


class ElectricityCostStatistics:
    """Daily electricity cost statistics for one P1 connection."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: Any,
        connection_uuid: str,
        store: StatisticsStore | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        pace: float = BACKFILL_PACE_SECONDS,
    ) -> None:
        self.hass = hass
        self.api = api
        self.connection_uuid = connection_uuid
        self.channels = electricity_channels(connection_uuid)
        self.store: StatisticsStore = store or RecorderStatisticsStore(hass)
        self._sleep = sleep
        self._pace = pace
        self._lock = asyncio.Lock()
        self._refreshed_day: date | None = None
        self._last_live: tuple | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def _fetch_day(self, day: date) -> DayCosts | None:
        path = f"/electricity-delivered/charts/hours?date={day.isoformat()}"
        payload = await fetch_with_retry(
            lambda: self.api.async_get(self.connection_uuid, path, ignore_etag=True),
            sleep=self._sleep,
            what=path,
        )
        if not payload:
            msg = f"Empty cost payload for {day}"
            raise ZonneplanApiError(msg)
        return costs_from_hours_payload(payload, day)

    async def backfill(self, start: date | None = None, end: date | None = None) -> int:
        """
        Re-import ``start``..``end`` (default: resume .. today) one API call per day, paced.

        Returns the number of days written. Raises on API failure; days written so far stay,
        and calling again without ``start`` resumes from the last stored day.
        """
        today = local_today()
        end = min(end or today, today)
        async with self._lock:
            if start is None:
                start = await resume_start(self.store, self.channels, COST_HISTORY_START)
            start = max(start, COST_HISTORY_START)
            _LOGGER.info("Cost backfill %s: %s .. %s", self.connection_uuid, start, end)
            writer = DailySeriesWriter(self.store, self.channels)
            day = start
            while day <= end:
                costs = await self._fetch_day(day)
                if costs is not None:
                    await writer.add_day(day, costs.as_values())
                if writer.days_written and writer.days_written % 30 == 0:
                    _LOGGER.info("Cost backfill %s: reached %s", self.connection_uuid, day)
                day += timedelta(days=1)
                if day <= end:
                    await self._sleep(self._pace)
            _LOGGER.info("Cost backfill %s done: %s days written", self.connection_uuid, writer.days_written)
            return writer.days_written

    async def process_live(self, live_payload: dict[str, Any] | None) -> None:
        """
        Handle a coordinator update with the bare ``/electricity-delivered`` payload.

        Writes today-so-far from that payload (no extra API call). Once per day after 00:20 local
        it first re-imports the last 3 days (and any gap up to 30 days), because Zonneplan
        corrects recent data. Skipped while a backfill runs; failures are retried next update.
        """
        today_costs = costs_from_live_payload(live_payload)
        today = local_today()
        if today_costs is None or today_costs.day != today or self._lock.locked():
            return
        now = dt_util.now(ZONNEPLAN_API_TIME_ZONE)
        need_refresh = self._refreshed_day != today and now.time() >= YESTERDAY_REFRESH_CUTOFF
        signature = (today, today_costs.delivery_incl, today_costs.production_incl, today_costs.delivery_excl, today_costs.production_excl)
        if not need_refresh and signature == self._last_live:
            return
        async with self._lock:
            writer = DailySeriesWriter(self.store, self.channels)
            if need_refresh:
                last = await self.store.last_day(self.channels[0].statistic_id)
                day = refresh_start(today, last)
                while day < today:
                    costs = await self._fetch_day(day)
                    if costs is not None:
                        await writer.add_day(day, costs.as_values())
                    day += timedelta(days=1)
                    await self._sleep(self._pace)
            await writer.add_day(today, today_costs.as_values())
            self._last_live = signature
            if need_refresh:
                self._refreshed_day = today


class PowerplayEarningsStatistics:
    """Daily Powerplay (home battery) net earnings statistic for one battery contract."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: Any,
        contract_uuid: str,
        store: StatisticsStore | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        pace: float = BACKFILL_PACE_SECONDS,
    ) -> None:
        self.hass = hass
        self.api = api
        self.contract_uuid = contract_uuid
        self.channels = powerplay_channels(contract_uuid)
        self.store: StatisticsStore = store or RecorderStatisticsStore(hass)
        self._sleep = sleep
        self._pace = pace
        self._lock = asyncio.Lock()
        self._last_written: tuple | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def _write(self, days: dict[date, float], start: date, end: date) -> int:
        writer = DailySeriesWriter(self.store, self.channels)
        for day in sorted(days):
            if start <= day <= end:
                await writer.add_day(day, {"earnings": days[day]})
        return writer.days_written

    async def backfill(self, start: date | None = None, end: date | None = None) -> int:
        """Re-import ``start``..``end`` (default: resume .. today), one ``days`` chart call per month."""
        today = local_today()
        end = min(end or today, today)
        async with self._lock:
            if start is None:
                start = await resume_start(self.store, self.channels, COST_HISTORY_START)
            start = max(start, COST_HISTORY_START)
            writer = DailySeriesWriter(self.store, self.channels)
            month = start.replace(day=1)
            while month <= end:
                chart = await fetch_with_retry(
                    lambda m=month: self.api.async_get_battery_chart(self.contract_uuid, "days", m, ignore_etag=True),
                    sleep=self._sleep,
                    what=f"battery days {month}",
                )
                if chart is None:
                    msg = f"Empty battery chart for {month}"
                    raise ZonneplanApiError(msg)
                days = battery_days_from_chart(chart)
                for day in sorted(days):
                    if start <= day <= end:
                        await writer.add_day(day, {"earnings": days[day]})
                month = (month + timedelta(days=32)).replace(day=1)
                if month <= end:
                    await self._sleep(self._pace)
            return writer.days_written

    async def process_charts(self, days: dict[date, float]) -> None:
        """Re-import the last 3 days (and any gap up to 30 days) from already fetched day charts."""
        if not days or self._lock.locked():
            return
        today = local_today()
        window = {d: v for d, v in days.items() if d >= today - timedelta(days=MAX_AUTO_GAP_DAYS)}
        signature = tuple(sorted((d.isoformat(), v) for d, v in window.items() if d >= today - timedelta(days=REFRESH_DAYS)))
        if signature == self._last_written:
            return
        async with self._lock:
            last = await self.store.last_day(self.channels[0].statistic_id)
            start = refresh_start(today, last)
            await self._write(window, start, today)
            self._last_written = signature
