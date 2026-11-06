"""可配置气象提示规则与连续事件回测。

只计算规则命中事件；没有灾害标签时不计算准确率或召回率。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent
RULES_PATH = ROOT / "risk_rules.json"


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        config = json.load(f)
    if config["event_merge_max_gap_hours"] != 0:
        raise ValueError("当前最小版本仅支持相邻逐小时命中合并（中断0小时）")
    return config


def _month_day(value: pd.Timestamp) -> str:
    return value.strftime("%m-%d")


def in_phenology_window(moment: pd.Timestamp, windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    md = _month_day(moment)
    for window in windows:
        if window["start_month_day"] <= md <= window["end_month_day"]:
            return window
    return None


def _level(value: float, rule: dict[str, Any]) -> str | None:
    if rule["operator"] == "<=":
        if value <= rule["high_threshold"]:
            return "较高"
        if value <= rule["attention_threshold"]:
            return "关注"
    elif rule["operator"] == ">=":
        if value >= rule["high_threshold"]:
            return "较高"
        if value >= rule["attention_threshold"]:
            return "关注"
    else:
        raise ValueError(f"不支持的运算符：{rule['operator']}")
    return None


def detect_events(frame: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    """对三年数据使用同一冻结配置；相邻命中小时合并为一次事件。"""
    required = {"observed_time", "location_name", "model_name"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"回测数据缺少字段：{sorted(missing)}")
    history = frame.copy()
    history["observed_datetime"] = pd.to_datetime(history["observed_time"], errors="coerce")
    history = history.dropna(subset=["observed_datetime"]).sort_values("observed_datetime")
    history = history.loc[history["observed_datetime"].dt.year.isin(config["years"])]
    history = history.loc[history["location_name"] == config["location_name"]]
    history = history.loc[history["model_name"].str.lower() == config["model_name"].lower()]
    rows: list[dict[str, Any]] = []
    now = datetime.now().astimezone().isoformat(timespec="seconds")

    for rule in config["rules"]:
        metric = rule["metric"]
        if metric not in history:
            raise ValueError(f"规则 {rule['rule_code']} 所需字段不存在：{metric}")
        active_group: list[pd.Series] = []
        active_levels: list[str] = []
        active_window: dict[str, Any] | None = None
        previous_time: pd.Timestamp | None = None

        def flush() -> None:
            nonlocal active_group, active_levels, active_window
            if not active_group:
                return
            values = pd.Series([float(item[metric]) for item in active_group])
            extreme = values.min() if rule["aggregation"] == "min" else values.max()
            level = "较高" if "较高" in active_levels else "关注"
            start = pd.Timestamp(active_group[0]["observed_datetime"])
            end = pd.Timestamp(active_group[-1]["observed_datetime"])
            extreme_label = "最低" if rule["aggregation"] == "min" else "最大"
            window_text = active_window["name"] if active_window else "全年"
            summary = (
                f"{extreme_label}{metric}={extreme:.2f}{rule['unit']}；"
                f"命中{len(active_group)}个小时；窗口={window_text}；"
                "事件仅表示规则命中，不代表灾害发生"
            )
            rows.append({
                "location_name": config["location_name"],
                "rule_code": rule["rule_code"],
                "config_version": config["config_version"],
                "risk_type": rule["risk_type"],
                "risk_level": level,
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "duration_hours": float(len(active_group)),
                "trigger_summary": summary,
                "created_at": now,
                "updated_at": now,
                "年份": int(start.year),
                "物候窗口": window_text,
                "极值": float(extreme),
            })
            active_group = []
            active_levels = []
            active_window = None

        for _, item in history.iterrows():
            moment = pd.Timestamp(item["observed_datetime"])
            value = item[metric]
            window = in_phenology_window(moment, config["phenology_windows"])
            eligible = window is not None if rule["phenology_restricted"] else True
            level = _level(float(value), rule) if eligible and pd.notna(value) else None
            continuous = (
                previous_time is not None
                and moment - previous_time == pd.Timedelta(hours=1)
                and (not active_window or not window or active_window["code"] == window["code"])
            )
            if level is None:
                flush()
            else:
                if active_group and not continuous:
                    flush()
                if not active_group:
                    active_window = window
                active_group.append(item)
                active_levels.append(level)
            previous_time = moment
        flush()

    columns = [
        "location_name", "rule_code", "config_version", "risk_type", "risk_level",
        "start_time", "end_time", "duration_hours", "trigger_summary", "created_at",
        "updated_at", "年份", "物候窗口", "极值",
    ]
    return pd.DataFrame(rows, columns=columns).sort_values("start_time").reset_index(drop=True)


def save_events(conn: sqlite3.Connection, events: pd.DataFrame, config: dict[str, Any]) -> int:
    """按配置版本幂等重建本地点事件，不影响其他配置。"""
    db_columns = [
        "location_name", "rule_code", "config_version", "risk_type", "risk_level",
        "start_time", "end_time", "duration_hours", "trigger_summary", "created_at", "updated_at",
    ]
    with conn:
        conn.execute(
            "DELETE FROM risk_events WHERE location_name=? AND config_version=?",
            (config["location_name"], config["config_version"]),
        )
        if not events.empty:
            conn.executemany(
                f"INSERT INTO risk_events ({','.join(db_columns)}) VALUES ({','.join('?' for _ in db_columns)})",
                [tuple(row) for row in events[db_columns].itertuples(index=False, name=None)],
            )
    return len(events)


def annual_summary(events: pd.DataFrame, years: list[int], risk_types: list[str]) -> pd.DataFrame:
    rows = []
    for year in years:
        for risk_type in risk_types:
            part = events.loc[(events["年份"] == year) & (events["risk_type"] == risk_type)] if not events.empty else events
            rows.append({
                "年份": year,
                "风险类型": risk_type,
                "事件数": int(len(part)),
                "累计持续小时": float(part["duration_hours"].sum()) if not part.empty else 0.0,
                "较高等级事件数": int((part["risk_level"] == "较高").sum()) if not part.empty else 0,
                "阶段口径": "2023—2024年规则回测" if year in (2023, 2024) else "2025年独立展示",
            })
    return pd.DataFrame(rows)
