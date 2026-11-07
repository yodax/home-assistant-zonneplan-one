import logging
from datetime import datetime, timedelta
from http import HTTPStatus

from aiohttp.client_exceptions import ClientResponseError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer

from ..api import AsyncConfigEntryAuth
from ..const import DOMAIN, ZONNEPLAN_API_TIME_ZONE
from ..zonneplan_api.types import ZonneplanContract
from .cost_statistics import ElectricityCostStatistics, net_cost_sensor_data
from .statistics import ElectricityStatisticsService
from .zonneplan_data_update_coordinator import ZonneplanDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class ElectricityDataUpdateCoordinator(ZonneplanDataUpdateCoordinator):
    """Zonneplan p1 electricity data update coordinator."""

    hass: HomeAssistant
    api: AsyncConfigEntryAuth
    address_uuid: str
    connection_uuid: str
    contracts: list[ZonneplanContract]
    _statistics_service: ElectricityStatisticsService

    def __init__(
        self,
        hass: HomeAssistant,
        api: AsyncConfigEntryAuth,
        address_uuid: str,
        connection_uuid: str,
        contracts: list[ZonneplanContract],
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=300),
            request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=60, immediate=False),
        )

        self.api: AsyncConfigEntryAuth = api
        self.address_uuid = address_uuid
        self.connection_uuid = connection_uuid
        self.contracts = contracts
        self._statistics_service = ElectricityStatisticsService(
            hass=self.hass,
            api=self.api,
            connection_uuid=self.connection_uuid,
            delivered_id=self.electricity_delivered_id,
            produced_id=self.electricity_produced_id,
        )
        self._cost_statistics = ElectricityCostStatistics(hass, api, connection_uuid)

    async def _async_update_data(self) -> dict:
        """Fetch the latest status."""
        try:
            electricity = await self.api.async_get(self.connection_uuid, "/electricity-delivered")

        except ClientResponseError as e:
            if e.status == HTTPStatus.UNAUTHORIZED:
                raise ConfigEntryAuthFailed from e
            raise
        else:
            _LOGGER.debug("Update electricity data: %s", electricity)
            if electricity:
                _LOGGER.debug("Process stats for %s", [self.electricity_delivered_id, self.electricity_produced_id])
                await self._statistics_service.process_payload(electricity)

            if electricity:
                try:
                    await self._cost_statistics.process_live(electricity)
                except Exception:  # cost statistics must never break the sensors
                    _LOGGER.warning("Updating cost statistics failed, retrying next update", exc_info=True)
                electricity["net_costs"] = net_cost_sensor_data(electricity)

            return electricity or self.data

    @property
    def electricity_delivered_id(self) -> str:
        return f"{DOMAIN}:electricity_delivered_{self.connection_uuid.replace('-', '_')}"

    @property
    def electricity_produced_id(self) -> str:
        return f"{DOMAIN}:electricity_produced_{self.connection_uuid.replace('-', '_')}"

    async def async_backfill_statistics(self, start_date: datetime) -> None:
        """Backfill statistics from start_date until now."""
        await self._statistics_service.async_backfill_from(start_date)

    async def async_backfill_cost_statistics(self, start_date: datetime | None) -> int:
        """Backfill the daily cost statistics (resume from the last stored day when start_date is None)."""
        start = start_date.astimezone(ZONNEPLAN_API_TIME_ZONE).date() if start_date else None
        return await self._cost_statistics.backfill(start)
