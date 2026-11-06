"""页面数据读取与筛选；只读 SQLite，不创建或修改正式数据。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

TABLES = {"current_weather", "hourly_weather", "risk_events"}


def load_table(db_path: Path, table: str) -> pd.DataFrame:
    if table not in TABLES:
        raise ValueError("不允许读取的表名")
    if not db_path.exists():
        return pd.DataFrame()
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return pd.read_sql_query(f"SELECT * FROM {table} ORDER BY id", conn)
    except sqlite3.OperationalError:
        return pd.DataFrame()
    finally:
        conn.close()


def recent_hours(frame: pd.DataFrame, hours: int = 24) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    if hours <= 0:
        raise ValueError("hours 必须为正整数")
    result = frame.copy()
    result["observed_datetime"] = pd.to_datetime(result["observed_time"], errors="coerce")
    result = result.dropna(subset=["observed_datetime"]).sort_values("observed_datetime")
    if result.empty:
        return result
    latest = result["observed_datetime"].max()
    start = latest - pd.Timedelta(hours=hours - 1)
    return result.loc[result["observed_datetime"] >= start].copy()


def hourly_kind(frame: pd.DataFrame, kind: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    return frame.loc[frame["data_kind"] == kind].copy()


def units_from_row(row: Any) -> dict[str, str]:
    try:
        value = row.get("units_json", "{}") if hasattr(row, "get") else row["units_json"]
        units = json.loads(value or "{}")
        return units if isinstance(units, dict) else {}
    except (json.JSONDecodeError, TypeError, KeyError):
        return {}


def csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8-sig")
