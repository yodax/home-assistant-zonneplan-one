"""Zonneplan integration services."""

import logging
from collections.abc import Coroutine
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError

from .const import (
    DOMAIN,
    ELECTRICITY,
    GAS,
    ZONNEPLAN_API_TIME_ZONE,
)

SERVICE_FETCH_STATISTICS = "fetch_statistics"
SERVICE_FETCH_COST_STATISTICS = "fetch_cost_statistics"
_ATTR_ENDPOINT = "endpoint"
_ATTR_START_DATE = "start_date"
_ATTR_CONNECTION_UUID = "connection_uuid"
_ATTR_COST_ENDPOINT = "endpoint"
_COST_ENDPOINTS = ("all", "electricity", "battery")
_DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d")

SERVICE_FETCH_COST_STATISTICS_SCHEMA = vol.Schema(
    {
        vol.Optional(_ATTR_START_DATE): str,
        vol.Optional(_ATTR_COST_ENDPOINT, default="all"): vol.In(_COST_ENDPOINTS),
        vol.Optional(_ATTR_CONNECTION_UUID): str,
    }
)

SERVICE_FETCH_STATISTICS_SCHEMA = vol.Schema(
    {
        vol.Required(_ATTR_ENDPOINT): vol.In([ELECTRICITY, GAS]),
        vol.Required(_ATTR_START_DATE): str,
        vol.Optional(_ATTR_CONNECTION_UUID): str,
    }
)

_LOGGER = logging.getLogger(__name__)


def parse_start_date(value: str) -> datetime:
    """Parse YYYYMMDD / YYYY-MM-DD into local-midnight (API time zone)."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=ZONNEPLAN_API_TIME_ZONE)
        except ValueError:
            continue
    msg = f"Invalid start_date '{value}'. Use YYYYMMDD or YYYY-MM-DD."
    raise ServiceValidationError(msg)


async def _run_cost_backfill(label: str, coro: Coroutine[Any, Any, int]) -> None:
    try:
        days = await coro
    except Exception:
        _LOGGER.exception("Cost statistics backfill %s failed; call fetch_cost_statistics without start_date to resume", label)
    else:
        _LOGGER.info("Cost statistics backfill %s finished, %s days written", label, days)


@callback
def async_setup_fetch_statistics_service(hass: HomeAssistant) -> None:
    """Register the fetch_statistics service if not already registered."""
    if hass.services.has_service(DOMAIN, SERVICE_FETCH_STATISTICS):
        return

    async def handle_fetch_statistics(call: ServiceCall) -> ServiceResponse:
        """Handle the fetch_statistics service call."""
        endpoint: str = call.data[_ATTR_ENDPOINT]
        start_date_str: str = call.data[_ATTR_START_DATE]
        connection_uuid_filter: str | None = call.data.get(_ATTR_CONNECTION_UUID)

        start_date: datetime | None = None
        for fmt in _DATE_FORMATS:
            try:
                start_date = datetime.strptime(start_date_str, fmt).replace(tzinfo=ZONNEPLAN_API_TIME_ZONE)
                break
            except ValueError:
                continue

        if start_date is None:
            msg = f"Invalid start_date '{start_date_str}'. Use YYYYMMDD or YYYY-MM-DD."
            raise ServiceValidationError(msg)

        if connection_uuid_filter:
            connection_uuid_filter = connection_uuid_filter.replace("_", "-")

        _LOGGER.info(
            "Fetch_statistics called: endpoint=%s, start_date=%s, connection_uuid=%s",
            endpoint,
            start_date,
            connection_uuid_filter,
        )

        for loaded_entry in hass.config_entries.async_entries(DOMAIN):
            if loaded_entry.state is not ConfigEntryState.LOADED:
                continue
            account_coordinator = loaded_entry.runtime_data
            for uuid, conn_coordinators in account_coordinator.coordinators.items():
                if connection_uuid_filter and uuid != connection_uuid_filter:
                    continue
                if endpoint == ELECTRICITY and conn_coordinators.p1_electricity is not None:
                    hass.async_create_task(conn_coordinators.p1_electricity.async_backfill_statistics(start_date))
                elif endpoint == GAS and conn_coordinators.p1_gas is not None:
                    hass.async_create_task(conn_coordinators.p1_gas.async_backfill_statistics(start_date))

        return {"result": "Check notifications for progress."}

    hass.services.async_register(
        DOMAIN,
        SERVICE_FETCH_STATISTICS,
        handle_fetch_statistics,
        schema=SERVICE_FETCH_STATISTICS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


@callback
def async_setup_fetch_cost_statistics_service(hass: HomeAssistant) -> None:
    """Register fetch_cost_statistics (daily actual-cost statistics backfill)."""
    if hass.services.has_service(DOMAIN, SERVICE_FETCH_COST_STATISTICS):
        return

    async def handle_fetch_cost_statistics(call: ServiceCall) -> ServiceResponse:
        start_date_str: str | None = call.data.get(_ATTR_START_DATE)
        endpoint: str = call.data[_ATTR_COST_ENDPOINT]
        connection_uuid_filter: str | None = call.data.get(_ATTR_CONNECTION_UUID)
        start_date = parse_start_date(start_date_str) if start_date_str else None
        if connection_uuid_filter:
            connection_uuid_filter = connection_uuid_filter.replace("_", "-")

        started: list[str] = []
        for loaded_entry in hass.config_entries.async_entries(DOMAIN):
            if loaded_entry.state is not ConfigEntryState.LOADED:
                continue
            for uuid, conn in loaded_entry.runtime_data.coordinators.items():
                if connection_uuid_filter and uuid != connection_uuid_filter:
                    continue
                if endpoint in ("all", "electricity") and conn.p1_electricity is not None:
                    if conn.p1_electricity._cost_statistics.busy:  # noqa: SLF001
                        started.append(f"electricity {uuid}: already running")
                    else:
                        hass.async_create_task(
                            _run_cost_backfill(f"electricity {uuid}", conn.p1_electricity.async_backfill_cost_statistics(start_date)),
                        )
                        started.append(f"electricity {uuid}")
                if endpoint in ("all", "battery") and conn.battery_charts is not None:
                    if conn.battery_charts._cost_statistics.busy:  # noqa: SLF001
                        started.append(f"battery {uuid}: already running")
                    else:
                        hass.async_create_task(
                            _run_cost_backfill(f"battery {uuid}", conn.battery_charts.async_backfill_cost_statistics(start_date)),
                        )
                        started.append(f"battery {uuid}")

        _LOGGER.info("fetch_cost_statistics: start_date=%s endpoint=%s started=%s", start_date, endpoint, started)
        return {"started": started, "start_date": start_date.date().isoformat() if start_date else "resume"}

    hass.services.async_register(
        DOMAIN,
        SERVICE_FETCH_COST_STATISTICS,
        handle_fetch_cost_statistics,
        schema=SERVICE_FETCH_COST_STATISTICS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
