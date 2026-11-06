"""云端首次启动时从公开接口重建数据库；已有完整历史数据时不重复请求。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from risk_engine import detect_events, load_rules, save_events
from weather_pipeline import (
    fetch_current,
    fetch_history,
    fetch_hourly,
    init_database,
    load_config,
    save_current,
    save_hourly,
)

EXPECTED_HISTORY_ROWS = 26_304


def history_rows(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM hourly_weather WHERE data_kind='history' AND model_name='era5'"
        ).fetchone()[0])
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def ensure_database(db_path: Path) -> bool:
    """数据库完整则返回False；首次重建完成则返回True。"""
    if history_rows(db_path) == EXPECTED_HISTORY_ROWS:
        return False

    config = load_config()
    conn = init_database(db_path)
    try:
        history_config = dict(config)
        history_config["source_name"] = "Open-Meteo Historical Weather API"
        for year in (2023, 2024, 2025):
            payload = fetch_history(history_config, year, "era5")
            save_hourly(conn, payload, history_config, data_kind="history", on_duplicate="update")

        save_current(conn, fetch_current(config), config)
        save_hourly(conn, fetch_hourly(config), config, data_kind="forecast", on_duplicate="update")

        history = pd.read_sql_query(
            "SELECT * FROM hourly_weather WHERE data_kind='history' AND model_name='era5'",
            conn,
        )
        if len(history) != EXPECTED_HISTORY_ROWS:
            raise RuntimeError(f"历史数据重建后应为{EXPECTED_HISTORY_ROWS}条，实际为{len(history)}条")
        rules = load_rules()
        save_events(conn, detect_events(history, rules), rules)
    finally:
        conn.close()
    return True
