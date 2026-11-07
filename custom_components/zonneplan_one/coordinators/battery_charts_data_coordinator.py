import logging
from datetime import date, datetime, timedelta
from http import HTTPStatus
from typing import Any

import homeassistant.util.dt as dt_util
from aiohttp.client_exceptions import ClientResponseError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer

from ..api import AsyncConfigEntryAuth
from ..const import DOMAIN, ZONNEPLAN_API_TIME_ZONE
from ..zonneplan_api.types import ZonneplanContract
from .cost_statistics import PowerplayEarningsStatistics, battery_days_from_chart, local_midnight
from .zonneplan_data_update_coordinator import ZonneplanDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


def _parse_day_chart(chart_data: Any, month_date: date) -> dict | None:
    group = chart_data[0]
    if not group:
        _LOGGER.warning("No day data in %s", chart_data)
        return None

    measurements = group.get("measurements") or []
    meta = group.get("meta") or {}

    days: dict[str, Any] = {}
    for measurement in measurements:
        measured_at = measurement.get("measured_at")
        dt_value = dt_util.parse_datetime(measured_at) if measured_at else None
        if not dt_value:
            continue

        dt_value = dt_util.as_utc(dt_value).astimezone(ZONNEPLAN_API_TIME_ZONE)

        day_key = dt_value.date().isoformat()
        days[day_key] = {
            "result": (measurement.get("value") or 0) * 0.0000001,
            "delivery_kwh": (measurement.get("meta", {}).get("delivery") or 0) * 0.001,
            "production_kwh": (measurement.get("meta", {}).get("production") or 0) * 0.001,
        }

    return {
        "month": month_date.strftime("%Y-%m"),
        "total_result": (group.get("total") or 0) * 0.0000001,
        "total_delivery_kwh": (meta.get("delivery") or 0) * 0.001,
        "total_production_kwh": (meta.get("production") or 0) * 0.001,
        "days": days,
    }


def _parse_month_chart(chart_data: Any, year: int) -> dict | None:
    group = chart_data[0]
    if not group:
        _LOGGER.warning("No month data in %s", chart_data)
        return None

    measurements = group.get("measurements") or []
    meta = group.get("meta") or {}

    months: dict[str, Any] = {}
    for measurement in measurements:
        measured_at = measurement.get("measured_at")
        dt_value = dt_util.parse_datetime(measured_at) if measured_at else None
        if not dt_value:
            continue

        dt_value = dt_util.as_utc(dt_value).astimezone(ZONNEPLAN_API_TIME_ZONE)

        month_key = dt_value.date().strftime("%Y-%m")
        months[month_key] = {
            "result": (measurement.get("value") or 0) * 0.0000001,
            "delivery_kwh": (measurement.get("meta", {}).get("delivery") or 0) * 0.001,
            "production_kwh": (measurement.get("meta", {}).get("production") or 0) * 0.001,
        }

    return {
        "year": year,
        "total_result": (group.get("total") or 0) * 0.0000001,
        "total_delivery_kwh": (meta.get("delivery") or 0) * 0.001,
        "total_production_kwh": (meta.get("production") or 0) * 0.001,
        "months": months,
    }


def _today_result(this_month: dict | None, today: date) -> dict:
    """Today's Powerplay result, from the current month's day chart (0 until the first measurement)."""
    day = ((this_month or {}).get("days") or {}).get(today.isoformat()) or {}
    return {
        "date": local_midnight(today).isoformat(),
        "result": day.get("result", 0),
        "delivery_kwh": day.get("delivery_kwh", 0),
        "production_kwh": day.get("production_kwh", 0),
    }


class BatteryChartsDataUpdateCoordinator(ZonneplanDataUpdateCoordinator):
    """Zonneplan battery history data update coordinator."""

    hass: HomeAssistant
    api: AsyncConfigEntryAuth
    address_uuid: str
    connection_uuid: str
    contract: ZonneplanContract

    def __init__(
        self,
        hass: HomeAssistant,
        api: AsyncConfigEntryAuth,
        address_uuid: str,
        connection_uuid: str,
        contract: ZonneplanContract,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=15),
            request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=60, immediate=False),
        )

        self.data: dict = {}
        self.api: AsyncConfigEntryAuth = api
        self.address_uuid = address_uuid
        self.connection_uuid = connection_uuid
        self.contract = contract

        self._last_battery_chart_fetch: dict[str, datetime] = {}
        self._last_full_fetch: datetime | None = None
        self._last_month_days: dict[date, float] = {}
        self._this_month_days: dict[date, float] = {}
        self._cost_statistics = PowerplayEarningsStatistics(hass, api, contract["uuid"])

    async def _async_update_data(self) -> dict:
        """Fetch the latest status."""
        try:
            charts = await self._async_get_battery_charts(self.data or {})

        except ClientResponseError as e:
            if e.status == HTTPStatus.UNAUTHORIZED:
                raise ConfigEntryAuthFailed from e
            raise
        else:
            _LOGGER.debug("Update battery charts data: %s", charts)

            return charts or self.data

    async def _async_get_battery_charts(self, charts: dict) -> dict:
        today = dt_util.now(ZONNEPLAN_API_TIME_ZONE).date()
        current_year_date = date(today.year, 1, 1)
        last_year_date = date(today.year - 1, 1, 1)
        current_month_date = today.replace(day=1)
        last_month_date = (current_month_date - timedelta(days=1)).replace(day=1)

        contract_uuid = self.contract.get("uuid")

        # The year/month overviews change slowly: fetch them every 12 hours (or when the month
        # rolled over). The current month's days, which hold today's result, every update.
        now = dt_util.utcnow()
        month_rolled = (charts.get("this_month") or {}).get("month") != current_month_date.strftime("%Y-%m")
        full_fetch = self._last_full_fetch is None or month_rolled or now - self._last_full_fetch >= timedelta(hours=12)

        if full_fetch:
            months_this_year = await self.api.async_get_battery_chart(contract_uuid, "months", current_year_date)
            if months_this_year and (parsed := _parse_month_chart(months_this_year, current_year_date.year)):
                charts["this_year"] = parsed

            months_last_year = await self.api.async_get_battery_chart(contract_uuid, "months", last_year_date)
            if months_last_year and (parsed := _parse_month_chart(months_last_year, last_year_date.year)):
                charts["last_year"] = parsed

            days_last_month = await self.api.async_get_battery_chart(contract_uuid, "days", last_month_date)
            if days_last_month and (parsed := _parse_day_chart(days_last_month, last_month_date)):
                charts["last_month"] = parsed
                self._last_month_days = battery_days_from_chart(days_last_month)

        days_this_month = await self.api.async_get_battery_chart(contract_uuid, "days", current_month_date, ignore_etag=True)
        if days_this_month and (parsed := _parse_day_chart(days_this_month, current_month_date)):
            charts["this_month"] = parsed
            self._this_month_days = battery_days_from_chart(days_this_month)

        if full_fetch:
            self._last_full_fetch = now

        charts["today"] = _today_result(charts.get("this_month"), today)

        try:
            await self._cost_statistics.process_charts(
                {**self._last_month_days, **self._this_month_days},
            )
        except Exception:  # cost statistics must never break the sensors
            _LOGGER.warning("Updating Powerplay earnings statistics failed, retrying next update", exc_info=True)

        return charts

    async def async_backfill_cost_statistics(self, start_date: datetime | None) -> int:
        """Backfill the daily Powerplay earnings statistic (resume from the last stored day when start_date is None)."""
        start = start_date.astimezone(ZONNEPLAN_API_TIME_ZONE).date() if start_date else None
        return await self._cost_statistics.backfill(start)
