"""Unit tests for the daily actual-cost statistics (no Home Assistant instance needed)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

from custom_components.zonneplan_one.api import ZonneplanApiError, ZonneplanRateLimitError
from custom_components.zonneplan_one.coordinators import cost_statistics as cs

CONN = "00000000-0000-4000-8000-000000000001"


# --- helpers ---------------------------------------------------------------------------------


class FakeStore:
    """In-memory ``StatisticsStore`` with the recorder's overwrite-by-start semantics."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[float, tuple[float, float]]] = {}
        self.adds = 0

    async def baseline_sum(self, statistic_id: str, before: datetime) -> float:
        earlier = [(s, v) for s, v in self.rows.get(statistic_id, {}).items() if s < before.timestamp()]
        return max(earlier)[1][1] if earlier else 0.0

    async def last_day(self, statistic_id: str) -> date | None:
        rows = self.rows.get(statistic_id)
        if not rows:
            return None
        return datetime.fromtimestamp(max(rows), tz=dt_util.UTC).astimezone(cs.ZONNEPLAN_API_TIME_ZONE).date()

    def add(self, channel: cs.CostChannel, rows: list[Any]) -> None:
        for row in rows:
            self.adds += 1
            self.rows.setdefault(channel.statistic_id, {})[row["start"].timestamp()] = (row["state"], row["sum"])

    def series(self, statistic_id: str) -> list[tuple[date, float, float]]:
        return [
            (datetime.fromtimestamp(s, tz=dt_util.UTC).astimezone(cs.ZONNEPLAN_API_TIME_ZONE).date(), v[0], v[1])
            for s, v in sorted(self.rows.get(statistic_id, {}).items())
        ]


def meta(delivery: int, production: int, d_excl: int | None = None, p_excl: int | None = None) -> dict[str, Any]:
    return {
        "has_supply_contract": True,
        "energy_delivered_sum": 33656,
        "energy_produced_sum": 24948,
        "delivery_costs_incl_tax": delivery,
        "delivery_costs_excl_tax": d_excl if d_excl is not None else delivery // 2,
        "production_costs_incl_tax": production,
        "production_costs_excl_tax": p_excl if p_excl is not None else production // 2,
        # Group-level result_* is delivery + production (NOT net): must never be used as net.
        "result_tax_included": delivery + production,
        "result_tax_excluded": (delivery + production) // 2,
    }


def hours_payload(day: date, delivery: int, production: int) -> dict[str, Any]:
    return {
        "measurement_groups": [
            {"type": "hours", "date": cs.local_midnight(day).isoformat().replace("+00:00", "Z"), "meta": meta(delivery, production), "measurements": []},
        ],
    }


class FakeApi:
    """Serves ``hours?date=D`` payloads; ``fail_on``/``rate_limits`` inject failures."""

    def __init__(self, values: dict[date, tuple[int, int]], fail_on: date | None = None) -> None:
        self.values = values
        self.fail_on = fail_on
        self.calls: list[date] = []
        self.rate_limits_left = 0

    async def async_get(self, connection_uuid: str, path: str, *, ignore_etag: bool = False) -> dict[str, Any] | None:
        assert connection_uuid == CONN
        assert ignore_etag
        day = date.fromisoformat(path.split("date=")[1])
        if self.rate_limits_left:
            self.rate_limits_left -= 1
            raise ZonneplanRateLimitError(request_info=None, history=(), status=429, message="x", retry_after=5)
        if day == self.fail_on:
            raise ZonneplanApiError("boom")
        self.calls.append(day)
        if day not in self.values:
            return {"measurement_groups": [{"type": "hours", "date": cs.local_midnight(day).isoformat(), "meta": {"has_supply_contract": False}}]}
        return hours_payload(day, *self.values[day])


class Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    fixed = date(2025, 8, 10)
    monkeypatch.setattr(cs, "local_today", lambda: fixed)
    return fixed


def make(api: FakeApi, store: FakeStore, sleeper: Sleeper | None = None) -> cs.ElectricityCostStatistics:
    return cs.ElectricityCostStatistics(None, api, CONN, store, sleep=sleeper or Sleeper(), pace=1.5)  # type: ignore[arg-type]


NET = f"zonneplan_one:electricity_net_cost_{CONN.replace('-', '_')}"
DELIVERY = f"zonneplan_one:electricity_delivery_cost_{CONN.replace('-', '_')}"


# --- scaling and net ---------------------------------------------------------------------------


def test_money_scaling() -> None:
    assert cs.money(137847545) == 13.7847545
    assert cs.money(100408215.99999999) == 10.0408216
    assert cs.money(0) == 0.0
    assert cs.money(None) is None
    assert cs.money("12") is None
    assert cs.energy_kwh(33656) == 33.656


def test_net_is_delivery_minus_production_not_group_result() -> None:
    costs = cs.costs_from_meta(meta(137847545, 128062600, 100540509, 100408216), date(2026, 11, 5))
    assert costs is not None
    assert costs.net_incl == pytest.approx(0.9784945)  # 13.7847545 - 12.80626
    assert costs.net_excl == pytest.approx(0.0132293)
    # the group's result_tax_included (26.59) is delivery + production and must not leak through
    assert costs.net_incl != cs.money(meta(137847545, 128062600)["result_tax_included"])
    assert costs.as_values()["net_cost"] == costs.net_incl
    assert costs.delivered_kwh == 33.656


def test_negative_net_when_feed_in_exceeds_delivery() -> None:
    costs = cs.costs_from_meta(meta(1_000_000, 4_000_000), date(2026, 6, 1))
    assert costs is not None
    assert costs.net_incl == pytest.approx(-0.3)


def test_null_costs_are_skipped() -> None:
    assert cs.costs_from_meta({"has_supply_contract": False, "delivery_costs_incl_tax": None}, date(2025, 7, 1)) is None
    assert cs.costs_from_meta(None, date(2025, 7, 1)) is None
    assert cs.costs_from_hours_payload({"measurement_groups": []}, date(2025, 7, 1)) is None


def test_hours_payload_for_other_day_is_rejected() -> None:
    assert cs.costs_from_hours_payload(hours_payload(date(2025, 8, 3), 1, 1), date(2025, 8, 4)) is None


def test_live_payload_gives_today_so_far() -> None:
    payload = {"measurement_groups": [{"type": "live", "date": cs.local_midnight(date(2026, 11, 7)).isoformat(), "meta": meta(2966005, 2418069)}]}
    costs = cs.costs_from_live_payload(payload)
    assert costs is not None
    assert costs.day == date(2026, 11, 7)
    assert costs.net_incl == pytest.approx(0.0547936)


def test_net_cost_sensor_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "local_today", lambda: date(2026, 11, 7))
    payload = {
        "measurement_groups": [
            {"type": "live", "date": "2026-11-06T23:00:00Z", "meta": meta(2966005, 2418069)},
            {"type": "hours", "date": "2026-11-06T23:00:00Z", "meta": meta(1, 1)},
            {"type": "days", "date": "2026-10-31T23:00:00Z", "meta": meta(1161731884, 715244473)},
            {"type": "months", "date": "2025-12-31T23:00:00Z", "meta": meta(29086090799, 25559317233)},
        ],
    }
    data = cs.net_cost_sensor_data(payload)
    assert data["today"]["net_incl_tax"] == pytest.approx(0.0547936)
    assert data["this_month"]["net_incl_tax"] == pytest.approx(44.6487411)
    assert data["this_year"]["net_incl_tax"] == pytest.approx(352.6773566)
    assert data["this_month"]["date"] == "2026-10-31T23:00:00Z"
    assert data["today"]["delivery_costs_incl_tax"] == 0.2966005
    assert cs.net_cost_sensor_data(None) == {}


def test_battery_days_from_chart() -> None:
    chart = [
        {
            "type": "days",
            "total": 186065874,
            "measurements": [
                {"value": 12679650, "measured_at": "2026-10-31T23:00:00.000000Z"},
                {"value": 35427352, "measured_at": "2026-11-01T23:00:00.000000Z"},
                {"value": None, "measured_at": "2026-11-02T23:00:00.000000Z"},
            ],
        },
    ]
    assert cs.battery_days_from_chart(chart) == {date(2026, 11, 1): 1.267965, date(2026, 11, 2): 3.5427352}
    assert cs.battery_days_from_chart(None) == {}


# --- placement across DST ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected_utc"),
    [
        (date(2025, 10, 25), "2025-10-24T22:00:00+00:00"),  # CEST
        (date(2025, 10, 26), "2025-10-25T22:00:00+00:00"),  # fall-back day (25 h): still CEST at midnight
        (date(2025, 10, 27), "2025-10-26T23:00:00+00:00"),  # CET: this day starts 25 h after the previous
        (date(2026, 3, 28), "2026-03-27T23:00:00+00:00"),  # CET
        (date(2026, 3, 29), "2026-03-28T23:00:00+00:00"),  # spring-forward day (23 h): midnight still CET
        (date(2026, 3, 30), "2026-03-29T22:00:00+00:00"),  # CEST
    ],
)
def test_local_midnight_placement(day: date, expected_utc: str) -> None:
    start = cs.local_midnight(day)
    assert start.isoformat() == expected_utc
    assert start.minute == 0 and start.second == 0
    assert cs.local_day_of(start) == day


def test_row_start_matches_api_group_date_on_dst_days() -> None:
    # the API's group date for Sunday 2026-03-29 is the UTC instant of local midnight
    assert cs.local_day_of("2026-03-28T23:00:00.000000Z") == date(2026, 3, 29)
    assert cs.local_day_of("2025-10-25T22:00:00.000000Z") == date(2025, 10, 26)


async def test_rows_land_on_local_midnight_across_dst() -> None:
    store = FakeStore()
    writer = cs.DailySeriesWriter(store, cs.electricity_channels(CONN))  # type: ignore[arg-type]
    for i, day in enumerate([date(2025, 10, 25), date(2025, 10, 26), date(2025, 10, 27)]):
        costs = cs.costs_from_meta(meta(10_000_000 * (i + 1), 0), day)
        await writer.add_day(day, costs.as_values())  # type: ignore[union-attr]
    starts = sorted(store.rows[DELIVERY])
    assert [datetime.fromtimestamp(s, tz=dt_util.UTC).isoformat() for s in starts] == [
        "2025-10-24T22:00:00+00:00",
        "2025-10-25T22:00:00+00:00",
        "2025-10-26T23:00:00+00:00",
    ]
    assert [d for d, _, _ in store.series(DELIVERY)] == [date(2025, 10, 25), date(2025, 10, 26), date(2025, 10, 27)]


# --- sum continuity ----------------------------------------------------------------------------


async def test_sum_is_running_total_with_baseline() -> None:
    store = FakeStore()
    channels = cs.electricity_channels(CONN)
    first = cs.DailySeriesWriter(store, channels)  # type: ignore[arg-type]
    for i in range(1, 6):
        await first.add_day(date(2025, 8, i), cs.costs_from_meta(meta(10_000_000 * i, 4_000_000 * i), date(2025, 8, i)).as_values())  # type: ignore[union-attr]
    assert [round(s, 6) for _, _, s in store.series(DELIVERY)] == [1, 3, 6, 10, 15]
    assert [round(s, 6) for _, _, s in store.series(NET)] == [0.6, 1.8, 3.6, 6.0, 9.0]


async def test_reimport_is_idempotent_and_keeps_sum_continuous() -> None:
    store = FakeStore()
    channels = cs.electricity_channels(CONN)
    original = {date(2025, 8, i): (10_000_000 * i, 4_000_000 * i) for i in range(1, 6)}
    w = cs.DailySeriesWriter(store, channels)  # type: ignore[arg-type]
    for day, (d, p) in original.items():
        await w.add_day(day, cs.costs_from_meta(meta(d, p), day).as_values())  # type: ignore[union-attr]
    before = store.series(DELIVERY)

    # identical re-import of days 3..5: nothing changes
    w2 = cs.DailySeriesWriter(store, channels)  # type: ignore[arg-type]
    for day in (date(2025, 8, 3), date(2025, 8, 4), date(2025, 8, 5)):
        d, p = original[day]
        await w2.add_day(day, cs.costs_from_meta(meta(d, p), day).as_values())  # type: ignore[union-attr]
    assert store.series(DELIVERY) == before

    # Zonneplan corrects day 4 (+1 EUR): re-import 4..5 fixes day 4 and shifts day 5 by the delta
    w3 = cs.DailySeriesWriter(store, channels)  # type: ignore[arg-type]
    await w3.add_day(date(2025, 8, 4), cs.costs_from_meta(meta(50_000_000, 16_000_000), date(2025, 8, 4)).as_values())  # type: ignore[union-attr]
    await w3.add_day(date(2025, 8, 5), cs.costs_from_meta(meta(50_000_000, 20_000_000), date(2025, 8, 5)).as_values())  # type: ignore[union-attr]
    sums = [round(s, 6) for _, _, s in store.series(DELIVERY)]
    assert sums == [1, 3, 6, 11, 16]  # days 4, 5 corrected; continuity holds (sum_n = sum_{n-1} + value_n)
    series = store.series(DELIVERY)
    for prev, cur in zip(series, series[1:], strict=False):
        assert cur[2] == pytest.approx(prev[2] + cur[1])


async def test_days_must_ascend() -> None:
    w = cs.DailySeriesWriter(FakeStore(), cs.powerplay_channels("c"))  # type: ignore[arg-type]
    await w.add_day(date(2025, 8, 2), {"earnings": 1.0})
    with pytest.raises(ValueError, match="ascend"):
        await w.add_day(date(2025, 8, 2), {"earnings": 1.0})


# --- backfill: pacing, resume, retry -----------------------------------------------------------


async def test_backfill_full_run_paces_and_skips_null_days(today: date) -> None:
    # history starts 2025-08-01; 08-02 is a null day (no contract data) and must not move the sum
    values = {date(2025, 8, d): (10_000_000, 4_000_000) for d in range(1, 11) if d != 2}
    api, store, sleeper = FakeApi(values), FakeStore(), Sleeper()
    written = await make(api, store, sleeper).backfill()
    assert written == 9
    assert api.calls[0] == date(2025, 8, 1) and api.calls[-1] == date(2025, 8, 10)
    assert len(api.calls) == 10
    assert sleeper.delays == [1.5] * 9  # paced between days, not after the last
    assert store.series(DELIVERY)[-1][2] == pytest.approx(9.0)
    assert store.series(NET)[-1][2] == pytest.approx(5.4)
    assert date(2025, 8, 2) not in [d for d, _, _ in store.series(NET)]


async def test_backfill_never_starts_before_history_start(today: date) -> None:
    api, store = FakeApi({}), FakeStore()
    await make(api, store).backfill(date(2025, 1, 1))
    assert api.calls[0] == date(2025, 8, 1)


async def test_backfill_is_resumable(today: date) -> None:
    values = {date(2025, 8, d): (10_000_000 * d, 3_000_000 * d) for d in range(1, 11)}
    # reference: uninterrupted run
    ref_store = FakeStore()
    await make(FakeApi(values), ref_store).backfill()

    store = FakeStore()
    api = FakeApi(values, fail_on=date(2025, 8, 6))
    with pytest.raises(ZonneplanApiError):
        await make(api, store).backfill()
    assert store.series(DELIVERY)[-1][0] == date(2025, 8, 5)  # days 1..5 committed

    api2 = FakeApi(values)
    await make(api2, store).backfill()  # no start: resume
    assert api2.calls[0] == date(2025, 8, 5)  # re-imports the last stored day, then continues
    assert api2.calls[-1] == date(2025, 8, 10)
    for sid in (DELIVERY, NET):
        assert store.series(sid) == ref_store.series(sid)


async def test_backfill_resume_uses_oldest_channel(today: date) -> None:
    values = {date(2025, 8, d): (10_000_000, 1_000_000) for d in range(1, 11)}
    store = FakeStore()
    await make(FakeApi(values), store).backfill(date(2025, 8, 1), date(2025, 8, 8))
    # simulate one channel lagging (e.g. partially written before a crash)
    lag = f"zonneplan_one:electricity_production_cost_{CONN.replace('-', '_')}"
    del store.rows[lag][max(store.rows[lag])]
    api = FakeApi(values)
    await make(api, store).backfill()
    assert api.calls[0] == date(2025, 8, 7)


async def test_backfill_waits_out_rate_limit_then_continues(today: date) -> None:
    values = {date(2025, 8, d): (10_000_000, 0) for d in range(1, 4)}
    api, store, sleeper = FakeApi(values), FakeStore(), Sleeper()
    api.rate_limits_left = 2
    await make(api, store, sleeper).backfill(date(2025, 8, 1), date(2025, 8, 3))
    assert sleeper.delays[:2] == [25, 50]  # (retry_after + 20) * attempt
    assert len(store.series(DELIVERY)) == 3


async def test_backfill_gives_up_after_three_rate_limits(today: date) -> None:
    api, store = FakeApi({}), FakeStore()
    api.rate_limits_left = 99
    with pytest.raises(ZonneplanRateLimitError):
        await make(api, store).backfill(date(2025, 8, 1), date(2025, 8, 2))
    assert store.adds == 0


def test_refresh_start() -> None:
    today = date(2026, 11, 7)
    assert cs.refresh_start(today, None) == date(2026, 11, 4)  # today-3
    assert cs.refresh_start(today, date(2026, 11, 6)) == date(2026, 11, 4)
    assert cs.refresh_start(today, date(2026, 11, 1)) == date(2026, 11, 1)  # gap filled
    assert cs.refresh_start(today, date(2026, 1, 1)) == date(2026, 10, 8)  # capped at 30 days


# --- live (incremental) update -----------------------------------------------------------------


def live(day: date, delivery: int, production: int) -> dict[str, Any]:
    return {"measurement_groups": [{"type": "live", "date": cs.local_midnight(day).isoformat(), "meta": meta(delivery, production)}]}


async def test_live_update_refreshes_last_3_days_once_then_only_today(monkeypatch: pytest.MonkeyPatch) -> None:
    today = date(2026, 11, 7)
    monkeypatch.setattr(cs, "local_today", lambda: today)
    monkeypatch.setattr(cs.dt_util, "now", lambda tz=None: datetime(2026, 11, 7, 9, 0, tzinfo=cs.ZONNEPLAN_API_TIME_ZONE))
    values = {today - timedelta(days=i): (10_000_000, 2_000_000) for i in range(1, 6)}
    api, store = FakeApi(values), FakeStore()
    stats = make(api, store)

    await stats.process_live(live(today, 5_000_000, 1_000_000))
    assert api.calls == [today - timedelta(days=3), today - timedelta(days=2), today - timedelta(days=1)]
    series = store.series(NET)
    assert [d for d, _, _ in series] == [today - timedelta(days=3), today - timedelta(days=2), today - timedelta(days=1), today]
    assert series[-1][1] == pytest.approx(0.4)
    assert series[-1][2] == pytest.approx(0.8 * 3 + 0.4)

    # same payload again: nothing to do; changed payload: only today is rewritten, no API calls
    api.calls.clear()
    await stats.process_live(live(today, 5_000_000, 1_000_000))
    assert api.calls == [] and store.series(NET) == series
    await stats.process_live(live(today, 6_000_000, 1_000_000))
    assert api.calls == []
    new = store.series(NET)
    assert new[-1][1] == pytest.approx(0.5)
    assert new[-1][2] == pytest.approx(0.8 * 3 + 0.5)
    assert new[:-1] == series[:-1]


async def test_live_update_failure_writes_nothing_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    today = date(2026, 11, 7)
    monkeypatch.setattr(cs, "local_today", lambda: today)
    monkeypatch.setattr(cs.dt_util, "now", lambda tz=None: datetime(2026, 11, 7, 9, 0, tzinfo=cs.ZONNEPLAN_API_TIME_ZONE))
    values = {today - timedelta(days=i): (10_000_000, 0) for i in range(1, 4)}
    api, store = FakeApi(values, fail_on=today - timedelta(days=2)), FakeStore()
    stats = make(api, store)
    with pytest.raises(ZonneplanApiError):
        await stats.process_live(live(today, 1, 1))
    assert store.rows.get(NET, {}) == {} or store.series(NET)[-1][0] != today
    api.fail_on = None
    await stats.process_live(live(today, 1, 1))
    assert store.series(NET)[-1][0] == today


async def test_live_update_before_cutoff_only_writes_today(monkeypatch: pytest.MonkeyPatch) -> None:
    today = date(2026, 11, 7)
    monkeypatch.setattr(cs, "local_today", lambda: today)
    monkeypatch.setattr(cs.dt_util, "now", lambda tz=None: datetime(2026, 11, 7, 0, 5, tzinfo=cs.ZONNEPLAN_API_TIME_ZONE))
    api, store = FakeApi({}), FakeStore()
    await make(api, store).process_live(live(today, 1_000_000, 0))
    assert api.calls == []
    assert [d for d, _, _ in store.series(NET)] == [today]


async def test_live_update_ignores_stale_payload_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "local_today", lambda: date(2026, 11, 7))
    api, store = FakeApi({}), FakeStore()
    await make(api, store).process_live(live(date(2026, 11, 6), 1_000_000, 0))
    assert store.adds == 0


async def test_live_update_skipped_while_backfill_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    today = date(2026, 11, 7)
    monkeypatch.setattr(cs, "local_today", lambda: today)
    api, store = FakeApi({}), FakeStore()
    stats = make(api, store)
    async with stats._lock:  # noqa: SLF001
        await stats.process_live(live(today, 1_000_000, 0))
    assert store.adds == 0


# --- powerplay -----------------------------------------------------------------------------------


class FakeBatteryApi:
    def __init__(self, months: dict[date, list[tuple[date, int]]]) -> None:
        self.months = months
        self.calls: list[date] = []

    async def async_get_battery_chart(self, contract: str, chart: str, chart_date: date, *, ignore_etag: bool = False) -> Any:
        assert chart == "days" and ignore_etag
        self.calls.append(chart_date)
        return [
            {
                "type": "days",
                "measurements": [{"value": v, "measured_at": cs.local_midnight(d).isoformat()} for d, v in self.months.get(chart_date, [])],
            },
        ]


async def test_powerplay_backfill_one_call_per_month(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "local_today", lambda: date(2025, 10, 3))
    months = {
        date(2025, 8, 1): [(date(2025, 8, 1), 10_000_000), (date(2025, 8, 2), 20_000_000)],
        date(2025, 9, 1): [(date(2025, 9, 1), -5_000_000)],
        date(2025, 10, 1): [(date(2025, 10, 1), 30_000_000), (date(2025, 10, 4), 99_000_000)],  # future day ignored
    }
    api, store, sleeper = FakeBatteryApi(months), FakeStore(), Sleeper()
    stats = cs.PowerplayEarningsStatistics(None, api, "bbbb-cccc", store, sleep=sleeper, pace=1.5)  # type: ignore[arg-type]
    assert await stats.backfill() == 4
    assert api.calls == [date(2025, 8, 1), date(2025, 9, 1), date(2025, 10, 1)]
    sid = "zonneplan_one:powerplay_earnings_bbbb_cccc"
    assert [round(s, 6) for _, _, s in store.series(sid)] == [1.0, 3.0, 2.5, 5.5]
    # resume: nothing lost, last day re-imported, same result
    api.calls.clear()
    await stats.backfill()
    assert api.calls == [date(2025, 10, 1)]
    assert [round(s, 6) for _, _, s in store.series(sid)] == [1.0, 3.0, 2.5, 5.5]


async def test_powerplay_process_charts_reimports_last_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "local_today", lambda: date(2025, 10, 5))
    store = FakeStore()
    stats = cs.PowerplayEarningsStatistics(None, None, "bbbb", store)  # type: ignore[arg-type]
    days = {date(2025, 10, d): float(d) for d in range(1, 6)}
    await stats.process_charts(days)
    sid = "zonneplan_one:powerplay_earnings_bbbb"
    assert [d.day for d, _, _ in store.series(sid)] == [2, 3, 4, 5]  # today-3 .. today
    days[date(2025, 10, 4)] = 10.0  # correction
    await stats.process_charts(days)
    assert [round(s, 6) for _, _, s in store.series(sid)] == [2, 5, 15, 20]


def test_channel_ids_follow_existing_convention() -> None:
    ids = [c.statistic_id for c in cs.electricity_channels(CONN)]
    assert f"zonneplan_one:electricity_net_cost_{CONN.replace('-', '_')}" in ids
    assert all(i.startswith("zonneplan_one:") and "-" not in i for i in ids)
    assert len(ids) == len(set(ids)) == 6


# --- sensor wiring -----------------------------------------------------------------------------


def test_new_sensor_keys_resolve_in_coordinator_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new descriptions read the dicts the coordinators produce (real get_data_value path)."""
    from custom_components.zonneplan_one.const import BATTERY_CHARTS, P1_ELECTRICITY, SENSOR_TYPES  # noqa: PLC0415
    from custom_components.zonneplan_one.coordinators import battery_charts_data_coordinator as bc  # noqa: PLC0415
    from custom_components.zonneplan_one.coordinators.zonneplan_data_update_coordinator import ZonneplanDataUpdateCoordinator  # noqa: PLC0415

    monkeypatch.setattr(cs, "local_today", lambda: date(2026, 11, 7))
    payload = {"measurement_groups": [{"type": "live", "date": "2026-11-06T23:00:00Z", "meta": meta(2966005, 2418069)}]}
    holder = ZonneplanDataUpdateCoordinator.__new__(ZonneplanDataUpdateCoordinator)
    holder.data = {**payload, "net_costs": cs.net_cost_sensor_data(payload)}
    desc = SENSOR_TYPES[P1_ELECTRICITY]["totals"]["electricity_net_cost_today"]
    assert holder.get_data_value(desc.key) == pytest.approx(0.0547936)
    assert holder.get_data_value(desc.last_reset_key) == "2026-11-06T23:00:00Z"
    for attribute in desc.attributes:
        assert holder.get_data_value(attribute.key) is not None
    assert desc.native_unit_of_measurement == "EUR"

    month = {"days": {"2026-11-07": {"result": 1.5, "delivery_kwh": 2.0, "production_kwh": 1.0}}}
    holder.data = {"this_month": month, "today": bc._today_result(month, date(2026, 11, 7))}  # noqa: SLF001
    today_desc = SENSOR_TYPES[BATTERY_CHARTS]["result_today"]
    assert holder.get_data_value(today_desc.key) == 1.5
    assert holder.get_data_value(today_desc.last_reset_key) == "2026-11-06T23:00:00+00:00"
    assert bc._today_result(month, date(2026, 11, 8))["result"] == 0  # noqa: SLF001  # no measurement yet
