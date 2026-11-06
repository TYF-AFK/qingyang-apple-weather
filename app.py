"""庆阳苹果气象监测与风险提示系统（本地 Streamlit 应用）。"""
import json
import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from analysis import historical_same_period, prepare_history
from auto_update import update_due
from bootstrap import ensure_database
from dashboard_data import csv_bytes, hourly_kind, load_table, recent_hours, units_from_row
from risk_engine import annual_summary, load_rules

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("WEATHER_DB_PATH", str(ROOT / "data" / "weather.db")))
RULES_PATH = ROOT / "risk_rules.json"
GREEN = "#17634A"
BLUE = "#47798E"
BROWN = "#866D46"
INK = "#1E2B26"
MUTED = "#5D6B64"
PAPER = "#F3F6F4"
YEAR_COLORS = {2023: BLUE, 2024: BROWN, 2025: GREEN}

st.set_page_config(page_title="庆阳苹果气象监测", layout="wide", initial_sidebar_state="expanded")
bootstrap_error = None
if os.environ.get("WEATHER_DISABLE_AUTO_REFRESH") != "1":
    try:
        with st.spinner("首次启动正在重建公开气象数据，请稍候……"):
            ensure_database(DB_PATH)
    except Exception as exc:
        bootstrap_error = f"历史数据尚未重建完成：{exc}"

st.markdown(
    f"""
    <style>
    .stApp {{ background: {PAPER}; color: {INK}; font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif; }}
    [data-testid="stSidebar"] {{ background: #E8EFEB; }}
    .block-container {{ max-width: 1380px; padding-top: 2rem; padding-bottom: 2rem; }}
    h1, h2, h3 {{ color: {INK}; letter-spacing: .01em; }}
    p, label, div {{ line-height: 1.6; }}
    .hero {{ border-top: 4px solid {GREEN}; padding: 18px 0 8px; margin-bottom: 18px; }}
    .hero h1 {{ font-size: 2rem; margin: 0 0 8px; font-weight: 700; }}
    .hero p {{ color: {MUTED}; margin: 0; font-size: 1rem; }}
    .boundary {{ background: #E4EEE8; border-radius: 8px; padding: 12px 16px; color: #294B3B; margin: 8px 0 18px; }}
    .small-note {{ color: {MUTED}; font-size: .9rem; }}
    [data-testid="stMetric"] {{ background: #FBFCFB; border: 1px solid #D9E2DC; border-radius: 8px; padding: 14px 16px; }}
    .stButton button, .stDownloadButton button {{ min-height: 44px; }}
    .stButton button:focus-visible, .stDownloadButton button:focus-visible {{ outline: 3px solid #79A88F; outline-offset: 2px; }}
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="hero">
      <h1>庆阳苹果气象监测与风险提示</h1>
      <p>西峰区城市／网格级代表点 · 公开气象数据监测、历史同期分析与规则回测</p>
    </div>
    <div class="boundary">数据边界：本系统使用城市／网格级公开气象数据，不是果园传感器实测；风险事件仅表示规则命中，不代表实际农业灾害发生。</div>
    """,
    unsafe_allow_html=True,
)
if bootstrap_error:
    st.error(bootstrap_error)

with st.sidebar:
    st.markdown("## 数据导航")
    page = st.radio("选择页面", ["数据概览", "近24小时", "历史数据", "历史同期", "风险事件", "规则与说明"])
    auto_enabled = st.toggle("自动更新最新气象", value=True)
    interval_minutes = st.selectbox("自动更新间隔", [5, 10, 30], index=1, format_func=lambda value: f"{value} 分钟")
    manual_update = st.button("立即更新", width="stretch", help="立即请求公开接口并写入最新当前天气和逐小时预报")
    if st.button("刷新显示", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    st.markdown("---")
    st.markdown("**存储与更新状态**")
    st.caption("页面只读；采集程序事务写入")
    st.caption("数据库已找到" if DB_PATH.exists() else "数据库尚未创建")
    st.caption(f"规则配置：{load_rules()['config_version']}")

# 页面定时重跑；只有到期或点击“立即更新”时才请求接口，避免频繁请求。
auto_disabled_for_test = os.environ.get("WEATHER_DISABLE_AUTO_REFRESH") == "1"
if auto_enabled and not auto_disabled_for_test:
    st_autorefresh(interval=interval_minutes * 60 * 1000, key="weather_auto_refresh")
if not auto_disabled_for_test and (manual_update or auto_enabled):
    try:
        with st.spinner("正在获取最新公开气象数据……" if manual_update else "检查数据是否到期……"):
            update_result = update_due(DB_PATH, interval_minutes=interval_minutes, force=manual_update)
        if update_result["updated"]:
            st.cache_data.clear()
            st.session_state["auto_update_message"] = f"更新成功：{update_result['finished_at']}"
        elif manual_update:
            st.session_state["auto_update_message"] = "已是最新数据"
    except Exception as exc:
        # 接口异常不影响已保存数据和历史结果的展示。
        st.session_state["auto_update_message"] = f"本次更新失败，已保留原有数据：{exc}"
if st.session_state.get("auto_update_message"):
    st.sidebar.caption(st.session_state["auto_update_message"])


@st.cache_data(ttl=30, show_spinner=False)
def get_data(db_path: str):
    path = Path(db_path)
    return (
        load_table(path, "current_weather"),
        load_table(path, "hourly_weather"),
        load_table(path, "risk_events"),
    )


@st.cache_data(show_spinner=False)
def get_same_period(history: pd.DataFrame):
    return historical_same_period(history)


def chart_style(fig, y_title, x_title="时间"):
    fig.update_layout(
        font={"family": "Microsoft YaHei, Noto Sans CJK SC, sans-serif", "color": INK},
        paper_bgcolor=PAPER,
        plot_bgcolor="#FBFCFB",
        margin={"l": 20, "r": 20, "t": 55, "b": 20},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.15},
    )
    fig.update_xaxes(title=x_title, showgrid=False)
    fig.update_yaxes(title=y_title, gridcolor="#E1E7E3")
    return fig


def show_download(frame, label, filename):
    if not frame.empty:
        st.download_button(label, csv_bytes(frame), filename, "text/csv", width="content")


def rounded(frame: pd.DataFrame, digits: int = 2) -> pd.DataFrame:
    result = frame.copy()
    numeric = result.select_dtypes(include="number").columns
    result[numeric] = result[numeric].round(digits)
    return result


current, hourly, events = get_data(str(DB_PATH))
forecast = hourly_kind(hourly, "forecast")
history = hourly_kind(hourly, "history")
if not history.empty:
    history = history.loc[history["model_name"].str.lower() == "era5"].copy()

if page == "数据概览":
    st.subheader("最新公开气象状态")
    if current.empty:
        st.info("暂无真实当前天气数据。网络恢复后运行采集程序，页面即可读取并展示。")
    else:
        latest = current.iloc[-1]
        units = units_from_row(latest)
        cols = st.columns(4)
        metrics = [
            ("2米气温", latest["temperature_2m"], units.get("temperature_2m", "°C")),
            ("2米相对湿度", latest["relative_humidity_2m"], units.get("relative_humidity_2m", "%")),
            ("降水量", latest["precipitation"], units.get("precipitation", "mm")),
            ("10米风速", latest["wind_speed_10m"], units.get("wind_speed_10m", "km/h")),
        ]
        for col, (label, value, unit) in zip(cols, metrics):
            col.metric(label, "缺测" if pd.isna(value) else f"{value} {unit}")
        st.markdown(
            f"**观测时间：** {latest['observed_time']}　　**采集时间：** {latest['collected_at']}  \n"
            f"**数据来源：** {latest['source_name']}　　**地点：** {latest['location_name']}"
        )
        with st.expander("查看坐标与数据口径"):
            st.write({
                "请求纬度": latest["request_latitude"],
                "请求经度": latest["request_longitude"],
                "返回网格纬度": latest["grid_latitude"],
                "返回网格经度": latest["grid_longitude"],
                "坐标状态": latest["coordinate_status"],
                "时区": latest["timezone"],
            })
        show_download(current, "下载当前天气 CSV", "current_weather.csv")

    st.subheader("真实数据与计算结果概况")
    counts = pd.DataFrame({
        "数据类别": ["当前天气", "逐小时预报", "2023—2025年历史气象", "规则事件"],
        "记录数": [len(current), len(forecast), len(history), len(events)],
    })
    cols = st.columns(4)
    for col, row in zip(cols, counts.to_dict(orient="records")):
        col.metric(row["数据类别"], f"{row['记录数']:,} 条")
    if not history.empty:
        quality_rows = []
        prepared = prepare_history(history)
        for year in (2023, 2024, 2025):
            part = prepared.loc[prepared["年份"] == year]
            quality_rows.append({
                "年份": year, "记录数": len(part),
                "四项指标缺测数": int(part[["temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m"]].isna().sum().sum()),
                "时区": part.iloc[0]["timezone"] if not part.empty else "—",
            })
        st.dataframe(pd.DataFrame(quality_rows), hide_index=True, width="stretch")
    st.info("2023—2024年用于规则回测，2025年用于独立展示；本系统不计算没有灾害标签支撑的准确率。")

elif page == "近24小时":
    st.subheader("近24小时气象变化")
    recent = recent_hours(forecast, 24)
    if recent.empty:
        st.info("暂无真实逐小时预报数据。本页面不会使用模拟数据补齐图表。")
    else:
        st.caption(f"按数据库最新观测时间向前截取24个小时，共 {len(recent)} 条记录。")
        units = units_from_row(recent.iloc[-1])
        temp = px.line(recent, x="observed_datetime", y="temperature_2m", title="气温变化", color_discrete_sequence=[GREEN])
        temp.update_traces(name="2米气温")
        st.plotly_chart(chart_style(temp, f"气温（{units.get('temperature_2m', '°C')}）"), width="stretch")
        rain = px.bar(recent, x="observed_datetime", y="precipitation", title="逐小时降水", color_discrete_sequence=[BLUE])
        rain.update_traces(name="降水量")
        st.plotly_chart(chart_style(rain, f"降水（{units.get('precipitation', 'mm')}）"), width="stretch")
        wind = px.line(recent, x="observed_datetime", y="wind_speed_10m", title="风速变化", color_discrete_sequence=[BROWN])
        wind.update_traces(name="10米风速")
        st.plotly_chart(chart_style(wind, f"风速（{units.get('wind_speed_10m', 'km/h')}）"), width="stretch")
        show_download(recent.drop(columns=["observed_datetime"]), "下载图中数据 CSV", "recent_24_hours.csv")

elif page == "历史数据":
    st.subheader("2023—2025年历史气象记录")
    if history.empty:
        st.info("暂无真实历史气象数据。历史模型确认且接口可用后，按年份运行采集任务。")
    else:
        prepared = prepare_history(history)
        years = sorted(prepared["年份"].dropna().astype(int).unique().tolist())
        selected = st.multiselect("选择年份", years, default=years)
        filtered = prepared.loc[prepared["年份"].isin(selected)].copy()
        if filtered.empty:
            st.warning("请选择至少一个有记录的年份。")
        else:
            models = sorted(filtered["model_name"].dropna().unique().tolist())
            st.caption(f"地点：{filtered.iloc[-1]['location_name']}；模型：{', '.join(models)}；时区：{filtered.iloc[-1]['timezone']}")
            daily = filtered.set_index("observed_datetime").groupby("年份").resample("D").agg(
                日均温=("temperature_2m", "mean"),
                日降水量=("precipitation", "sum"),
                日最大风速=("wind_speed_10m", "max"),
                有效小时数=("observed_time", "count"),
            ).reset_index()
            metric = st.selectbox("选择日统计指标", ["日均温", "日降水量", "日最大风速", "有效小时数"])
            fig = px.line(daily, x="observed_datetime", y=metric, color="年份", title=f"历史{metric}变化", color_discrete_map=YEAR_COLORS)
            st.plotly_chart(chart_style(fig, metric), width="stretch")
            st.markdown("**日统计结果（每年前5日）**")
            st.dataframe(rounded(daily.groupby("年份").head(5)), hide_index=True, width="stretch")
            show_download(rounded(daily), "下载所选年份日统计 CSV", "history_daily_statistics.csv")
            show_download(filtered.drop(columns=["observed_datetime"]), "下载所选历史逐小时数据 CSV", "history_weather.csv")

elif page == "历史同期":
    st.subheader("历史同期均值与距平")
    if history.empty:
        st.info("暂无真实历史气象数据，无法计算历史同期。")
    else:
        detail, anomalies = get_same_period(history)
        st.caption("参照日：2026年11月6日；分别取2023、2024、2025年11月3—9日；逐小时数据先聚合为日统计，再计算窗口均值与距平。")
        metric = st.selectbox("同期指标", ["日均温", "日降水量", "日最大风速"])
        fig = px.line(detail, x="相对参照日天数", y=metric, color="年份", markers=True,
                      title=f"各年11月3—9日{metric}", color_discrete_map=YEAR_COLORS)
        unit = {"日均温": "℃", "日降水量": "mm", "日最大风速": "km/h"}[metric]
        st.plotly_chart(chart_style(fig, f"{metric}（{unit}）", "相对当年11月6日的天数（日）"), width="stretch")
        display = anomalies.loc[anomalies["指标"] == metric, ["年份", "窗口日数", "当年窗口均值", "三年窗口均值", "距平"]]
        st.dataframe(rounded(display, 3), hide_index=True, width="stretch")
        st.caption("距平＝当年窗口均值－三年窗口均值。正值表示高于三年窗口均值，负值表示低于三年窗口均值。")
        show_download(detail, "下载历史同期日明细 CSV", "historical_same_period_daily.csv")
        show_download(anomalies, "下载历史同期均值与距平 CSV", "historical_same_period_anomalies.csv")

elif page == "风险事件":
    st.subheader("风险提示规则回测与独立展示")
    st.caption("规则回测与案例核验，不是模型精度评估。没有真实灾害标签时，不计算准确率或召回率。")
    if events.empty:
        st.info("暂无风险事件。运行 generate_results.py 后，页面将显示真实规则回测结果。")
    else:
        events = events.copy()
        events["年份"] = pd.to_datetime(events["start_time"], errors="coerce").dt.year
        years = sorted(events["年份"].dropna().astype(int).unique().tolist())
        types = sorted(events["risk_type"].dropna().unique().tolist())
        col1, col2 = st.columns(2)
        selected_years = col1.multiselect("筛选年份", years, default=years)
        selected_types = col2.multiselect("筛选风险类型", types, default=types)
        filtered = events.loc[events["年份"].isin(selected_years) & events["risk_type"].isin(selected_types)].copy()
        st.markdown("**年度汇总（包含零事件类别）**")
        summary = annual_summary(events, [2023, 2024, 2025], ["春季低温", "强降水", "大风"])
        summary = summary.loc[summary["年份"].isin(selected_years) & summary["风险类型"].isin(selected_types)]
        st.dataframe(summary, hide_index=True, width="stretch")
        if filtered.empty:
            st.warning("当前筛选条件下没有事件；这不代表该年份没有气象数据。")
        else:
            chart_summary = summary.copy()
            chart_summary["年份"] = chart_summary["年份"].astype(str)
            fig = px.bar(chart_summary, x="风险类型", y="事件数", color="年份", barmode="group", title="筛选范围内事件数", category_orders={"年份": ["2023", "2024", "2025"]}, color_discrete_map={str(k): v for k, v in YEAR_COLORS.items()})
            st.plotly_chart(chart_style(fig, "事件数（次）", "规则类型"), width="stretch")
            st.markdown("**事件明细**")
            st.dataframe(filtered.sort_values("start_time"), hide_index=True, width="stretch")
            show_download(filtered, "下载筛选事件 CSV", "risk_events_filtered.csv")

else:
    st.subheader("规则配置与使用边界")
    rules = load_rules(RULES_PATH)
    st.markdown(f"**配置版本：** `{rules['config_version']}`　　**事件合并：** 相邻逐小时命中，允许中断0小时")
    rule_rows = []
    for rule in rules["rules"]:
        if rule["operator"] == "<=":
            threshold = f"关注≤{rule['attention_threshold']}{rule['unit']}；较高≤{rule['high_threshold']}{rule['unit']}"
        else:
            threshold = f"关注≥{rule['attention_threshold']}{rule['unit']}；较高≥{rule['high_threshold']}{rule['unit']}"
        rule_rows.append({"规则": rule["risk_type"], "字段": rule["metric"], "阈值": threshold, "适用范围": "春季物候窗口" if rule["phenology_restricted"] else "全年", "说明": rule["description"]})
    st.dataframe(pd.DataFrame(rule_rows), hide_index=True, width="stretch")
    st.markdown("**春季物候工程窗口**")
    st.dataframe(pd.DataFrame(rules["phenology_windows"])[["name", "start_month_day", "end_month_day", "basis"]].rename(columns={"name": "窗口", "start_month_day": "开始", "end_month_day": "结束", "basis": "依据与边界"}), hide_index=True, width="stretch")
    st.warning(rules["status_note"])
    st.markdown(
        """
        **可复现流程**  
        1. `weather_pipeline.py` 获取公开接口数据，以自然键写入 SQLite，重复运行不会生成重复自然键。  
        2. `generate_results.py` 用同一配置计算三年规则事件，并生成同期分析、CSV 和图表。  
        3. 2023—2024年为规则回测，2025年为独立展示；未使用灾害标签，不将命中数解释为准确率。  
        4. 本系统是课程竞赛用途的本地原型，不替代气象部门正式预报预警或果园现场管理。
        """
    )
    with st.expander("查看完整规则 JSON"):
        st.code(json.dumps(rules, ensure_ascii=False, indent=2), language="json")

st.markdown("---")
st.markdown('<div class="small-note">本系统用于公开气象数据的组织、监测和规则提示，不替代气象部门正式预报预警或农业技术人员现场判断。</div>', unsafe_allow_html=True)
