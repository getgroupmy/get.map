import asyncio
from datetime import date, datetime, timedelta, timezone

from conftest import load
from getmap import config, retention
from getmap.pipeline import Pipeline
from getmap.store import PostgresStore, SpoolStore

GB = 1024 ** 3
TODAY = date(2026, 11, 19)          # a Thursday; its week starts Monday 16 Nov


def parts(table, weeks, size=GB):
    return [(table, f"{table}_w{w:%Y%m%d}", w, size) for w in weeks]


def test_plan_retention_keeps_whole_weeks():
    weeks = [date(2026, 11, 2), date(2026, 11, 9), date(2026, 11, 16)]
    config_days = config.RETAIN_RAW_DAYS
    try:
        config.RETAIN_RAW_DAYS = 7
        out = retention.plan_retention(parts("raw_responses", weeks), TODAY)
    finally:
        config.RETAIN_RAW_DAYS = config_days
    # the week of 2 Nov ended 9 Nov, before the 12 Nov cutoff; the week of 9 Nov ends 16 Nov: kept
    assert out == [("raw_responses", "raw_responses_w20261102")]


def test_plan_guard_order_and_current_week():
    weeks = [date(2026, 11, 2), date(2026, 11, 9), date(2026, 11, 16)]
    p = (parts("search_events", weeks, 3 * GB) + parts("routes", weeks, GB)
         + parts("raw_responses", weeks, 2 * GB) + parts("traffic_observations", weeks, GB))
    out = retention.plan_guard(p, TODAY, over_bytes=5 * GB)
    names = [n for _, n, _ in out]
    # raw first (2 + 2 GB, current week never), then traffic's oldest week
    assert names == ["raw_responses_w20261102", "raw_responses_w20261109", "traffic_observations_w20261102"]
    out = retention.plan_guard(p, TODAY, over_bytes=100 * GB)
    names = [n for _, n, _ in out]
    assert not any(n.endswith("20261116") for n in names)
    # searches and routes of a week go together
    assert names.index("search_events_w20261102") + 1 == names.index("routes_w20261102")


def _store_event(db_url, tmp_path, waze_routes, when):
    async def run():
        store = PostgresStore(db_url, SpoolStore(tmp_path))
        ev = {"source": "get_route", "endpoint": "routes_search", "request": {}, "response": waze_routes,
              "status_code": 200, "captured_at": when}
        await Pipeline(store, None).process(ev)
        await store.close()
        assert store.spooled == 0, store.last_error
    asyncio.run(run())


def test_run_once_drops_old_weeks(db_url, tmp_path, waze_routes, monkeypatch):
    import psycopg
    _store_event(db_url, tmp_path, waze_routes, "2025-06-03T10:00:00Z")    # old (backfill-style)
    _store_event(db_url, tmp_path, waze_routes, "2026-11-18T10:00:00Z")    # this week
    monkeypatch.setattr(config, "RETAIN_EVENTS_DAYS", 365)
    dry = asyncio.run(retention.run_once(db_url, apply=False, today=TODAY, disk_free=500))
    assert "search_events_w20250602" in dry["dropped"] and "routes_w20250602" in dry["dropped"]
    with psycopg.connect(db_url) as c:
        assert c.execute("select count(*) from map.search_events").fetchone()[0] == 2   # dry run changed nothing
    rep = asyncio.run(retention.run_once(db_url, apply=True, today=TODAY, disk_free=500))
    assert rep["guard"] is False
    with psycopg.connect(db_url) as c:
        assert c.execute("select count(*) from map.search_events").fetchone()[0] == 1
        assert c.execute("select count(*) from map.routes").fetchone()[0] == 3
        left = {r[0] for r in c.execute("select partition_name from map.partitions")}
        old = {n for n in left if "20250602" in n}
        assert old == {"traffic_observations_w20250602"}     # traffic is kept 800 days; raw, searches, routes went


def test_disk_guard_drops_oldest_and_pauses_raw(db_url, tmp_path, waze_routes, monkeypatch):
    import psycopg
    _store_event(db_url, tmp_path, waze_routes, "2026-10-27T10:00:00Z")
    _store_event(db_url, tmp_path, waze_routes, "2026-11-18T10:00:00Z")
    monkeypatch.setattr(config, "MIN_FREE_DISK_GB", 80)
    pipe = Pipeline(SpoolStore(tmp_path), None)
    rep = asyncio.run(retention.run_once(db_url, apply=True, pipeline=pipe, today=TODAY, disk_free=79.9))
    assert rep["guard"] is True and pipe.raw_paused is True
    assert "search_events_w20261026" in rep["dropped"]
    with psycopg.connect(db_url) as c:
        # the current week is never dropped
        assert c.execute("select count(*) from map.search_events").fetchone()[0] == 1
    rep = asyncio.run(retention.run_once(db_url, apply=True, pipeline=pipe, today=TODAY, disk_free=200))
    assert pipe.raw_paused is False
