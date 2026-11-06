"""历史同期日统计、三年均值和距平计算。"""
from __future__ import annotations

from datetime import date
from typing import Iterable

import pandas as pd

METRIC_COLUMNS = {
    "日均温": "temperature_2m",
    "日降水量": "precipitation",
    "日最大风速": "wind_speed_10m",
}


def prepare_history(frame: pd.DataFrame) -> pd.DataFrame:
    """校验并整理历史逐小时数据，不填补缺测。"""
    required = {"observed_time", *METRIC_COLUMNS.values()}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"历史数据缺少字段：{sorted(missing)}")
    result = frame.copy()
    result["observed_datetime"] = pd.to_datetime(result["observed_time"], errors="coerce")
    result = result.dropna(subset=["observed_datetime"]).sort_values("observed_datetime")
    if result["observed_datetime"].dt.tz is not None:
        result["observed_datetime"] = result["observed_datetime"].dt.tz_localize(None)
    result["年份"] = result["observed_datetime"].dt.year
    return result


def daily_statistics(frame: pd.DataFrame) -> pd.DataFrame:
    """按自然日聚合；有效小时数用于识别不完整日期。"""
    history = prepare_history(frame)
    if history.empty:
        return pd.DataFrame(columns=["日期", "年份", *METRIC_COLUMNS, "有效小时数"])
    daily = history.groupby(history["observed_datetime"].dt.date).agg(
        日均温=("temperature_2m", "mean"),
        日降水量=("precipitation", "sum"),
        日最大风速=("wind_speed_10m", "max"),
        有效小时数=("observed_time", "count"),
    ).reset_index(names="日期")
    daily["日期"] = pd.to_datetime(daily["日期"])
    daily["年份"] = daily["日期"].dt.year
    return daily[["日期", "年份", *METRIC_COLUMNS, "有效小时数"]]


def historical_same_period(
    frame: pd.DataFrame,
    reference_date: date | str = date(2026, 11, 6),
    years: Iterable[int] = (2023, 2024, 2025),
    window_days: int = 3,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回逐年日明细与距平表；同日窗口按各年本地日期对齐。"""
    if window_days < 0:
        raise ValueError("window_days 不能为负数")
    reference = pd.Timestamp(reference_date)
    daily = daily_statistics(frame)
    selected = []
    for year in years:
        center = pd.Timestamp(year=year, month=reference.month, day=reference.day)
        start = center - pd.Timedelta(days=window_days)
        end = center + pd.Timedelta(days=window_days)
        part = daily.loc[(daily["日期"] >= start) & (daily["日期"] <= end)].copy()
        part["相对参照日天数"] = (part["日期"] - center).dt.days
        selected.append(part)
    columns = ["日期", "年份", "相对参照日天数", *METRIC_COLUMNS, "有效小时数"]
    detail = pd.concat(selected, ignore_index=True) if selected else pd.DataFrame(columns=columns)
    detail = detail.sort_values(["相对参照日天数", "年份"]).reset_index(drop=True)

    rows = []
    for metric in METRIC_COLUMNS:
        baseline = detail[metric].mean() if not detail.empty else float("nan")
        for year in years:
            values = detail.loc[detail["年份"] == year, metric]
            year_mean = values.mean() if not values.empty else float("nan")
            rows.append({
                "年份": year,
                "指标": metric,
                "窗口日数": int(values.count()),
                "当年窗口均值": year_mean,
                "三年窗口均值": baseline,
                "距平": year_mean - baseline if pd.notna(year_mean) and pd.notna(baseline) else float("nan"),
                "口径": f"{year}年{reference.month}月{reference.day}日前后±{window_days}天，先日统计再对窗口取均值",
            })
    return detail, pd.DataFrame(rows)
