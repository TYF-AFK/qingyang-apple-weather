"""当前天气与逐小时预报的到期更新；失败时保留数据库原有记录。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from weather_pipeline import (
    CONFIG_PATH, DB_PATH, export_table, fetch_current, fetch_hourly,
    init_database, load_config, save_current, save_hourly,
)


def _last_collected_at(db_path: Path) -> datetime | None:
    if not db_path.exists():
        return None
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            value = conn.execute("SELECT MAX(collected_at) FROM current_weather").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return None
    if not value:
        return None
    return datetime.fromisoformat(value)


def update_due(
    db_path: Path = DB_PATH,
    config_path: Path = CONFIG_PATH,
    interval_minutes: int = 10,
    force: bool = False,
) -> dict[str, Any]:
    """到期才请求接口；返回是否实际更新，供页面清除缓存。"""
    if interval_minutes <= 0:
        raise ValueError("自动更新间隔必须为正整数")
    config = load_config(config_path)
    now = datetime.now(ZoneInfo(config["timezone"]))
    last = _last_collected_at(db_path)
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=ZoneInfo(config["timezone"]))
        elapsed_seconds = (now - last.astimezone(ZoneInfo(config["timezone"]))).total_seconds()
    else:
        elapsed_seconds = None
    if not force and last is not None and elapsed_seconds is not None and elapsed_seconds < interval_minutes * 60:
        return {"updated": False, "reason": "尚未到期", "last_collected_at": last.isoformat(timespec="seconds")}

    conn = init_database(db_path)
    try:
        current_stats = save_current(conn, fetch_current(config), config)
        hourly_stats = save_hourly(conn, fetch_hourly(config), config, data_kind="forecast", on_duplicate="update")
        export_table(conn, "current_weather")
        export_table(conn, "hourly_weather")
    finally:
        conn.close()
    return {
        "updated": True,
        "current": current_stats,
        "hourly": hourly_stats,
        "finished_at": now.isoformat(timespec="seconds"),
    }
