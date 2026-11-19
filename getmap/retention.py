"""Keep the database inside one machine's disk.

1. Retention: drop weekly partitions older than their RETAIN_*_DAYS, and
   delete old shapes / alerts / weather (small tables, row by row).
2. Disk guard: while the database is over MAX_DB_GB or the disk has less than
   MIN_FREE_DISK_GB free, drop the oldest remaining week, in this order:
   raw answers, traffic, then searches + routes together. Never the current
   week. Raw answers stop being stored while the disk is short.

Runs every RETENTION_EVERY_MINUTES inside get.map, or by hand:

    python -m getmap.retention            # show what would go
    python -m getmap.retention --apply
"""
import argparse
import asyncio
import logging
import shutil
from datetime import date, datetime, timedelta, timezone

from getmap import config

log = logging.getLogger("getmap.retention")
GB = 1024 ** 3

# disk-guard order: cheapest to lose first; search_events and routes go together
GUARD_ORDER = (("raw_responses",), ("traffic_observations",), ("search_events", "routes"))
RETAIN = {
    "raw_responses": lambda: config.RETAIN_RAW_DAYS,
    "traffic_observations": lambda: config.RETAIN_TRAFFIC_DAYS,
    "search_events": lambda: config.RETAIN_EVENTS_DAYS,
    "routes": lambda: config.RETAIN_EVENTS_DAYS,
}


def disk_free_gb(path=None):
    return shutil.disk_usage(path or config.DISK_PATH).free / GB


def plan_retention(partitions, today):
    """Partitions past their retention: [(table, partition)]. A week goes when
    it ENDS before the cutoff, so nothing younger than the setting is lost."""
    out = []
    for table, name, week_start, _ in partitions:
        days = RETAIN.get(table, lambda: 0)()
        if days and week_start + timedelta(days=7) <= today - timedelta(days=days):
            out.append((table, name))
    return out


def plan_guard(partitions, today, over_bytes):
    """The oldest weeks to drop to free `over_bytes`, in GUARD_ORDER, never the
    current week. Returns [(table, partition, bytes)]."""
    current = today - timedelta(days=today.weekday())
    by_table = {}
    for table, name, week_start, size in partitions:
        if week_start < current:
            by_table.setdefault(table, []).append((week_start, name, size))
    out, freed = [], 0
    for group in GUARD_ORDER:
        weeks = sorted({w for t in group for w, _, _ in by_table.get(t, [])})
        for wk in weeks:
            if freed >= over_bytes:
                return out
            for t in group:
                for w, name, size in by_table.get(t, []):
                    if w == wk:
                        out.append((t, name, size))
                        freed += size
    return out


async def _partitions(cur):
    await cur.execute("select table_name, partition_name, week_start, bytes from map.partitions order by week_start")
    return await cur.fetchall()


async def run_once(dsn, apply=True, pipeline=None, today=None, disk_free=None):
    """One pass. Returns a report dict."""
    import psycopg
    today = today or datetime.now(timezone.utc).date()
    report = {"dropped": [], "deleted": {}, "db_gb": None, "free_gb": None, "guard": False}
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.cursor() as cur:
            parts = await _partitions(cur)
            for table, name in plan_retention(parts, today):
                report["dropped"].append(name)
                if apply:
                    await cur.execute(f'drop table if exists map."{name}"')

            small = (
                ("route_shapes", "last_seen_at", config.RETAIN_SHAPES_DAYS),
                ("road_alerts", "last_seen_at", config.RETAIN_ALERTS_DAYS),
                ("weather_observations", "observed_at", config.RETAIN_WEATHER_DAYS),
            )
            for table, col, days in small:
                if not days:
                    continue
                if apply:
                    await cur.execute(f"delete from map.{table} where {col} < now() - make_interval(days => %s)", (days,))
                    report["deleted"][table] = cur.rowcount
                else:
                    await cur.execute(f"select count(*) from map.{table} where {col} < now() - make_interval(days => %s)", (days,))
                    report["deleted"][table] = (await cur.fetchone())[0]

            await cur.execute("select pg_database_size(current_database())")
            db_bytes = (await cur.fetchone())[0]
            free = disk_free_gb() if disk_free is None else disk_free
            report["db_gb"], report["free_gb"] = round(db_bytes / GB, 2), round(free, 2)
            over = max(db_bytes - config.MAX_DB_GB * GB, (config.MIN_FREE_DISK_GB - free) * GB, 0)
            report["guard"] = over > 0
            if pipeline is not None:
                pipeline.raw_paused = free < config.MIN_FREE_DISK_GB * 1.25
            if over > 0:
                parts = [p for p in await _partitions(cur) if p[1] not in report["dropped"]]
                for table, name, size in plan_guard(parts, today, over):
                    report["dropped"].append(name)
                    log.warning("disk guard: dropping %s (%.1f GB)", name, size / GB)
                    if apply:
                        await cur.execute(f'drop table if exists map."{name}"')
    if report["dropped"]:
        log.info("retention dropped %s", ", ".join(report["dropped"]))
    return report


async def loop(dsn, pipeline=None):
    while True:
        try:
            await run_once(dsn, apply=True, pipeline=pipeline)
        except Exception:
            log.exception("retention failed")
        await asyncio.sleep(config.RETENTION_EVERY_MINUTES * 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="really drop / delete (default: only show)")
    args = ap.parse_args()
    if not config.DATABASE_URL:
        raise SystemExit("Set DATABASE_URL.")
    report = asyncio.run(run_once(config.DATABASE_URL, apply=args.apply))
    for k, v in report.items():
        print(f"{k}: {v}")
    if not args.apply:
        print("(nothing changed; add --apply)")


if __name__ == "__main__":
    main()
