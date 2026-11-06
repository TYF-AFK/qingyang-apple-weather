"""西峰区公开气象数据采集、SQLite 入库与 CSV 导出。

仅处理城市／网格级公开气象数据，不代表果园传感器实测。
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
EXPORT_DIR = ROOT / "exports"
LOG_DIR = ROOT / "logs"
DB_PATH = DATA_DIR / "weather.db"
LOG_PATH = LOG_DIR / "pipeline.log"
METRICS = ("temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m")


class DataQualityError(ValueError):
    """返回结构或关键数据口径不符合要求。"""


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        config = json.load(f)
    lat = float(config["request_latitude"])
    lon = float(config["request_longitude"])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise DataQualityError("请求坐标超出有效范围")
    ZoneInfo(config["timezone"])
    return config


def collected_now(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")


def request_json(url: str, params: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    timeout = config["request_timeout"]
    retry = config["retry"]
    waits = retry["ordinary_wait_seconds"]
    max_attempts = int(retry["max_attempts_after_first"]) + 1
    headers = {"User-Agent": "QingyangAppleWeatherStudentProject/0.1"}

    for attempt in range(1, max_attempts + 1):
        try:
            logging.info("请求气象接口，第 %s/%s 次", attempt, max_attempts)
            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=(timeout["connect_seconds"], timeout["read_seconds"]),
            )
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else None
                if attempt < max_attempts:
                    delay = wait if wait is not None else waits[min(attempt - 1, len(waits) - 1)]
                    logging.warning("接口限流，按等待要求 %.0f 秒后重试", delay)
                    time.sleep(delay)
                    continue
            if response.status_code >= 500 and attempt < max_attempts:
                delay = waits[min(attempt - 1, len(waits) - 1)]
                logging.warning("服务端状态 %s，%s 秒后重试", response.status_code, delay)
                time.sleep(delay)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise DataQualityError("接口返回不是 JSON 对象")
            if payload.get("error"):
                raise DataQualityError(f"接口返回错误：{payload.get('reason', payload)}")
            return payload
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(f"接口连接在 {max_attempts} 次尝试后仍失败：{exc}") from exc
            delay = waits[min(attempt - 1, len(waits) - 1)]
            logging.warning("连接异常：%s；%s 秒后重试", exc, delay)
            time.sleep(delay)
        except requests.RequestException as exc:
            raise RuntimeError(f"接口请求失败：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise DataQualityError(f"JSON 解析失败：{exc}") from exc
    raise RuntimeError("接口请求结束但未获得有效数据")


def as_float(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"字段 {field} 含非数值内容：{value!r}") from exc


def parse_time(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise DataQualityError(f"时间字段无效：{value!r}")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise DataQualityError(f"时间字段无法解析：{value!r}") from exc
    return value


def common_values(payload: dict[str, Any], config: dict[str, Any], collected_at: str) -> dict[str, Any]:
    return {
        "location_name": config["location_name"],
        "source_name": config["source_name"],
        "model_name": str(payload.get("model") or "接口默认（未指定）"),
        "collected_at": collected_at,
        "request_latitude": float(config["request_latitude"]),
        "request_longitude": float(config["request_longitude"]),
        "grid_latitude": as_float(payload.get("latitude"), "latitude"),
        "grid_longitude": as_float(payload.get("longitude"), "longitude"),
        "timezone": str(payload.get("timezone") or config["timezone"]),
        "utc_offset_seconds": int(payload.get("utc_offset_seconds") or 0),
        "coordinate_status": config.get("coordinate_status", "待确认"),
    }


def init_database(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS current_weather (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_name TEXT NOT NULL,
            source_name TEXT NOT NULL,
            model_name TEXT NOT NULL,
            observed_time TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            request_latitude REAL NOT NULL,
            request_longitude REAL NOT NULL,
            grid_latitude REAL,
            grid_longitude REAL,
            timezone TEXT NOT NULL,
            utc_offset_seconds INTEGER NOT NULL,
            coordinate_status TEXT NOT NULL,
            temperature_2m REAL,
            relative_humidity_2m REAL,
            precipitation REAL,
            wind_speed_10m REAL,
            units_json TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(location_name, source_name, observed_time)
        );
        CREATE TABLE IF NOT EXISTS hourly_weather (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_name TEXT NOT NULL,
            source_name TEXT NOT NULL,
            model_name TEXT NOT NULL,
            data_kind TEXT NOT NULL CHECK(data_kind IN ('forecast', 'history')),
            observed_time TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            request_latitude REAL NOT NULL,
            request_longitude REAL NOT NULL,
            grid_latitude REAL,
            grid_longitude REAL,
            timezone TEXT NOT NULL,
            utc_offset_seconds INTEGER NOT NULL,
            coordinate_status TEXT NOT NULL,
            temperature_2m REAL,
            relative_humidity_2m REAL,
            precipitation REAL,
            wind_speed_10m REAL,
            units_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(location_name, source_name, model_name, data_kind, observed_time)
        );
        CREATE TABLE IF NOT EXISTS risk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_name TEXT NOT NULL,
            rule_code TEXT NOT NULL,
            config_version TEXT NOT NULL,
            risk_type TEXT NOT NULL,
            risk_level TEXT,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            duration_hours REAL,
            trigger_summary TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(location_name, rule_code, config_version, start_time)
        );
        """
    )
    return conn


def save_current(conn: sqlite3.Connection, payload: dict[str, Any], config: dict[str, Any],
                 collected_at: str | None = None) -> dict[str, int]:
    current = payload.get("current")
    if not isinstance(current, dict):
        raise DataQualityError("缺少 current 对象")
    collected_at = collected_at or collected_now(config["timezone"])
    common = common_values(payload, config, collected_at)
    row = {
        **common,
        "observed_time": parse_time(current.get("time")),
        **{field: as_float(current.get(field), field) for field in METRICS},
        "units_json": json.dumps(payload.get("current_units", {}), ensure_ascii=False),
        "raw_json": json.dumps(current, ensure_ascii=False),
        "created_at": collected_at,
        "updated_at": collected_at,
    }
    columns = list(row)
    update_columns = [c for c in columns if c not in {"location_name", "source_name", "observed_time", "created_at"}]
    sql = f"""
        INSERT INTO current_weather ({','.join(columns)})
        VALUES ({','.join(':' + c for c in columns)})
        ON CONFLICT(location_name, source_name, observed_time) DO UPDATE SET
        {','.join(c + '=excluded.' + c for c in update_columns)}
    """
    with conn:
        conn.execute(sql, row)
    return {"returned": 1, "written": 1, "skipped": 0}


def save_hourly(conn: sqlite3.Connection, payload: dict[str, Any], config: dict[str, Any],
                data_kind: str = "forecast", on_duplicate: str = "update",
                collected_at: str | None = None) -> dict[str, int]:
    if data_kind not in {"forecast", "history"}:
        raise ValueError("data_kind 必须为 forecast 或 history")
    if on_duplicate not in {"update", "skip"}:
        raise ValueError("on_duplicate 必须为 update 或 skip")
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise DataQualityError("缺少 hourly.time 数组")
    times = hourly["time"]
    if not times:
        raise DataQualityError("hourly.time 为空")
    for field in METRICS:
        values = hourly.get(field)
        if not isinstance(values, list) or len(values) != len(times):
            raise DataQualityError(f"hourly.{field} 缺失或数组长度与 time 不一致")

    collected_at = collected_at or collected_now(config["timezone"])
    common = common_values(payload, config, collected_at)
    units_json = json.dumps(payload.get("hourly_units", {}), ensure_ascii=False)
    rows = []
    for i, raw_time in enumerate(times):
        rows.append({
            **common,
            "data_kind": data_kind,
            "observed_time": parse_time(raw_time),
            **{field: as_float(hourly[field][i], field) for field in METRICS},
            "units_json": units_json,
            "created_at": collected_at,
            "updated_at": collected_at,
        })

    columns = list(rows[0])
    conflict = "location_name,source_name,model_name,data_kind,observed_time"
    if on_duplicate == "update":
        update_columns = [c for c in columns if c not in {
            "location_name", "source_name", "model_name", "data_kind", "observed_time", "created_at"
        }]
        action = "DO UPDATE SET " + ",".join(c + "=excluded." + c for c in update_columns)
    else:
        action = "DO NOTHING"
    sql = f"""
        INSERT INTO hourly_weather ({','.join(columns)})
        VALUES ({','.join(':' + c for c in columns)})
        ON CONFLICT({conflict}) {action}
    """
    written = 0
    with conn:
        for row in rows:
            cursor = conn.execute(sql, row)
            written += max(cursor.rowcount, 0)
    return {"returned": len(rows), "written": written, "skipped": len(rows) - written}


def export_table(conn: sqlite3.Connection, table: str, output_dir: Path = EXPORT_DIR) -> Path:
    if table not in {"current_weather", "hourly_weather", "risk_events"}:
        raise ValueError("不允许导出的表名")
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    path = output_dir / f"{table}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if rows:
            writer.writerow(rows[0].keys())
            writer.writerows([tuple(row) for row in rows])
        else:
            writer.writerow([d[1] for d in conn.execute(f"PRAGMA table_info({table})")])
    return path


def fetch_current(config: dict[str, Any]) -> dict[str, Any]:
    params = {
        "latitude": config["request_latitude"],
        "longitude": config["request_longitude"],
        "current": ",".join(config["current_fields"]),
        "timezone": config["timezone"],
    }
    return request_json(config["forecast_api"], params, config)


def fetch_hourly(config: dict[str, Any]) -> dict[str, Any]:
    params = {
        "latitude": config["request_latitude"],
        "longitude": config["request_longitude"],
        "hourly": ",".join(config["hourly_fields"]),
        "timezone": config["timezone"],
        "past_days": 1,
        "forecast_days": 2,
    }
    return request_json(config["forecast_api"], params, config)


def fetch_history(config: dict[str, Any], year: int, model: str) -> dict[str, Any]:
    if year not in {2023, 2024, 2025}:
        raise ValueError("本项目历史任务仅允许 2023、2024 或 2025 年")
    if not model.strip():
        raise ValueError("历史任务必须明确指定 --model，不能使用未确认的默认模型")
    params = {
        "latitude": config["request_latitude"],
        "longitude": config["request_longitude"],
        "start_date": f"{year}-01-01",
        "end_date": f"{year}-12-31",
        "hourly": ",".join(config["hourly_fields"]),
        "timezone": config["timezone"],
        "models": model,
    }
    payload = request_json(config["historical_api"], params, config)
    payload["model"] = model
    return payload


def import_payload(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise DataQualityError("导入文件必须包含一个 JSON 对象")
    return payload


def print_summary(conn: sqlite3.Connection) -> None:
    for table in ("current_weather", "hourly_weather", "risk_events"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {count} 条")
    latest = conn.execute("SELECT MAX(observed_time) FROM current_weather").fetchone()[0]
    if latest:
        print(f"当前天气最新观测时间：{latest}")
    bounds = conn.execute("SELECT MIN(observed_time), MAX(observed_time) FROM hourly_weather").fetchone()
    if bounds[0]:
        print(f"逐小时数据范围：{bounds[0]} 至 {bounds[1]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="西峰区公开气象数据最小采集闭环")
    parser.add_argument("task", choices=("current", "hourly", "history", "all", "import", "export", "summary"))
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--year", type=int, help="历史年份：2023、2024 或 2025")
    parser.add_argument("--model", help="历史模型标识；运行历史任务时必须明确提供")
    parser.add_argument("--input", type=Path, help="import 任务使用的原始 JSON 文件")
    parser.add_argument("--kind", choices=("current", "hourly", "history"), help="导入数据类别")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    EXPORT_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )
    config = load_config(args.config)
    conn = init_database(args.db)
    failures = []
    try:
        for task in ("current", "hourly") if args.task == "all" else (args.task,):
            try:
                if task == "current":
                    stats = save_current(conn, fetch_current(config), config)
                    export_table(conn, "current_weather")
                    print("当前天气处理统计：", stats)
                elif task == "hourly":
                    stats = save_hourly(conn, fetch_hourly(config), config, data_kind="forecast", on_duplicate="update")
                    export_table(conn, "hourly_weather")
                    print("逐小时天气处理统计：", stats)
                elif task == "history":
                    if args.year is None or not args.model:
                        raise ValueError("history 任务必须同时提供 --year 和 --model")
                    history_config = dict(config)
                    history_config["source_name"] = "Open-Meteo Historical Weather API"
                    payload = fetch_history(history_config, args.year, args.model)
                    stats = save_hourly(conn, payload, history_config, data_kind="history", on_duplicate="skip")
                    export_table(conn, "hourly_weather")
                    print(f"{args.year} 年历史天气处理统计：", stats)
                elif task == "import":
                    if not args.input or not args.kind:
                        raise ValueError("import 任务必须同时提供 --input 和 --kind")
                    payload = import_payload(args.input)
                    if args.kind == "current":
                        stats = save_current(conn, payload, config)
                        export_table(conn, "current_weather")
                    else:
                        import_config = dict(config)
                        if args.kind == "history":
                            if not args.model:
                                raise ValueError("导入历史数据必须用 --model 标明模型")
                            import_config["source_name"] = "Open-Meteo Historical Weather API"
                            payload["model"] = args.model
                        stats = save_hourly(
                            conn, payload, import_config, data_kind=args.kind,
                            on_duplicate="skip" if args.kind == "history" else "update",
                        )
                        export_table(conn, "hourly_weather")
                    print(f"本地 JSON 导入统计：", stats)
                elif task == "export":
                    for table in ("current_weather", "hourly_weather", "risk_events"):
                        print("已导出：", export_table(conn, table))
            except Exception as exc:
                if args.task != "all":
                    raise
                failures.append(task)
                logging.error("%s 任务失败，继续执行其他独立任务：%s", task, exc)
        print_summary(conn)
        if failures:
            raise SystemExit(f"以下任务失败：{', '.join(failures)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
