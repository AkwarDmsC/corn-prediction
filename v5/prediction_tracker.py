#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
预测追踪器 v4.0
日夜盘双预测的记录、验证、统计、校准
记录至 predictions.md
"""

import re
import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from constants import PREDICTIONS, ANALYSIS_SCRIPT, TRACKER_STATE as _TS, CALIBRATION as _CL, ROOT

PREDICTIONS_FILE = PREDICTIONS
TRACKER_STATE = _TS
CALIBRATION_FILE = _CL

# ─────────────────────────────────────────
# 状态管理
# ─────────────────────────────────────────
def load_state():
    if TRACKER_STATE.exists():
        return json.loads(TRACKER_STATE.read_text())
    return {"pending_verification": [], "verified": [], "attributions": []}

def save_state(state):
    TRACKER_STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

# ─────────────────────────────────────────
# 数据获取
# ─────────────────────────────────────────
def get_price_N_days_after(date_str, n_days):
    """获取N个交易日后价格"""
    try:
        import akshare as ak
        df = ak.futures_zh_daily_sina(symbol="C0")
        df["date"] = df["date"].astype(str)
        df = df.sort_values("date").reset_index(drop=True)
        idx_list = df[df["date"] == date_str].index.tolist()
        if not idx_list:
            return None, None
        target_idx = idx_list[0] + n_days
        if target_idx < len(df):
            return float(df.iloc[target_idx]["close"]), str(df.iloc[target_idx]["date"])
        return None, None
    except:
        return None, None

def get_actual_price_on_date(date_str):
    """获取指定交易日的收盘价（日盘15:00）"""
    try:
        import akshare as ak
        df = ak.futures_zh_daily_sina(symbol="C0")
        df["date"] = df["date"].astype(str)
        df = df.sort_values("date").reset_index(drop=True)
        row = df[df["date"] == date_str]
        if len(row) > 0:
            return float(row.iloc[0]["close"])
        return None
    except:
        return None

def get_actual_night_close_on_date(date_str):
    """获取指定交易日的夜盘收盘价（23:00左右），从5分钟K线重构"""
    try:
        import akshare as ak
        import pandas as pd
        df = ak.futures_zh_minute_sina(symbol="C0", period="5")
        df['datetime'] = pd.to_datetime(df['datetime'])
        df['date'] = df['datetime'].dt.date.astype(str)
        df['hour'] = df['datetime'].dt.hour
        today_data = df[df['date'] == date_str]
        if len(today_data) == 0:
            return None, None, None
        # 夜盘: hour >= 21 or hour < 5
        night = today_data[(today_data['hour'] >= 21) | (today_data['hour'] < 5)]
        if len(night) == 0:
            return None, None, None
        day_close = today_data[(today_data['hour'] >= 9) & (today_data['hour'] < 15)]
        day_c = float(day_close.iloc[-1]['close']) if len(day_close) > 0 else None
        night_c = float(night.iloc[-1]['close'])
        night_high = float(night['high'].max())
        night_low = float(night['low'].min())
        return night_c, night_high, night_low
    except Exception as e:
        return None, None, None

# ─────────────────────────────────────────
# 解析预测数据
# ─────────────────────────────────────────
def parse_forecast(output):
    data = {}
    # 日期
    date_match = re.search(r'=== .*?每日分析.*? (\d{4}-\d{2}-\d{2})', output)
    if date_match:
        data["date"] = date_match.group(1)
    else:
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', output)
        data["date"] = date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d")

    # 周末运行 → 修正为最新数据日期
    if data["date"] > datetime.now().strftime("%Y-%m-%d"):
        import akshare as ak
        df = ak.futures_zh_daily_sina(symbol="C0")
        df["date"] = df["date"].astype(str)
        data["date"] = df["date"].max()

    # 今日收盘（日盘15:00）
    close_match = re.search(r"今日收盘[：:]\s*(\d+\.?\d*)\s*元/吨", output)
    if close_match:
        data["actual_close"] = float(close_match.group(1))

    # 日夜盘数据（v2.0新增）
    # 格式: 日盘收2354 → 夜盘收2357 (gap +3,+0.13%)
    sess_match = re.search(r"日夜盘[：:]\s*日盘收(\d+)\s*→\s*夜盘收(\d+)\s*\(([^)]+)\)", output)
    if sess_match:
        data["day_close"] = float(sess_match.group(1))
        data["night_close"] = float(sess_match.group(2))

    # 第二日——日盘预测（v2.0新增双预测）
    # 兼容两种格式：
    #   旧格式: ── 日盘(15:00) ──
    #           基准:2354元 → 预测:2353(-0.04%) | 区间:2337~2414
    #           方向:↗ 震荡偏强
    #   新格式(v5.0): ── 日盘(15:00) ──
    #           基准:2328元/吨 → 区间:2292~2409
    #           方向:↘ 震荡偏弱(均线空头)
    day_pred_m = re.search(r"── 日盘\(15:00\) ──.*?基准:(\d+).*?(?:预测:(\d+).*?)?区间:(\d+)[~-](\d+).*?方向:([^\n]+)", output, re.DOTALL)
    if day_pred_m:
        data["day_base"] = float(day_pred_m.group(1))
        data["d2_pred"] = float(day_pred_m.group(2)) if day_pred_m.group(2) else float(day_pred_m.group(1))
        data["d2_low"] = float(day_pred_m.group(3))
        data["d2_high"] = float(day_pred_m.group(4))
        data["d2_direction"] = day_pred_m.group(5).strip()

    # 第二日——夜盘预测（v2.0新增）
    # 兼容三种输出格式：
    #   完整: 基准:2333元 → 预测:2335(+0.1%) | 区间:2325~2395
    #   简约: 基准:2333元/吨 → 区间:2325~2395  (无预测:字段，仅基准→区间→方向)
    #   v5.0: 基准:2325元/吨 → 区间:2317~2333
    #          方向:↘ 偏弱
    night_pred_m = re.search(r"── 夜盘\(23:00\) ──.*?基准:(\d+).*?(?:预测:[^|]+\|)?\s*区间:(\d+)[~-](\d+).*?方向:([^\n]+)", output, re.DOTALL)
    if night_pred_m:
        data["night_base"] = float(night_pred_m.group(1))
        data["night_low"] = float(night_pred_m.group(2))
        data["night_high"] = float(night_pred_m.group(3))
        data["night_direction"] = night_pred_m.group(4).strip()
        # 如果有"预测:"字段则额外解析
        pred_val = re.search(r"预测[：:]([+-]?\d+\.?\d*)", output.split("夜盘\(23:00\)")[-1].split("方向:")[0] if "夜盘" in output and "方向:" in output else "")
        if pred_val:
            data["night_pred"] = float(pred_val.group(1))

    # 旧格式兼容：直接搜索"基准"和"区间"（单预测格式）
    if "d2_pred" not in data:
        d2 = re.search(r"基准[：:]\s*(\d+\.?\d*)\s*元/吨.*?区间[：:]\s*(\d+\.?\d*)\s*[~-]\s*(\d+\.?\d*)", output, re.DOTALL)
        if d2:
            data["d2_pred"] = float(d2.group(1))
            data["d2_low"] = float(d2.group(2))
            data["d2_high"] = float(d2.group(3))

    # 第二日方向
    dir_match = re.search(r"方向[:：]\s*([^\n|]+)", output)
    if dir_match:
        data["d2_direction"] = dir_match.group(1).strip()

    # ML模型预测次高/次低价（新增 v1.15）
    # 格式: 模型预测高价:2388 | 低价:2373 | 波幅:15元(0.6%) ↘收缩
    ml_hl = re.search(r"模型预测高价:(\d+)\s*\|\s*低价:(\d+)", output)
    if ml_hl:
        data["d2_ml_high"] = float(ml_hl.group(1))
        data["d2_ml_low"] = float(ml_hl.group(2))

    # 短期（兼容「短期」和「短期(1-4周)」两种格式）
    st = re.search(r"短期\s*\(?[1-4周]*\)?[\s\S]*?区间[:：]\s*(\d+)\s*[~-]\s*(\d+)", output)
    if st:
        data["st_low"] = float(st.group(1))
        data["st_high"] = float(st.group(2))
    # 备选：直接匹配带元/吨的区间行
    if "st_low" not in data:
        st2 = re.search(r"短期.*?\n.*?(\d{4})\s*[~-]\s*(\d{4})\s*元/吨", output, re.DOTALL)
        if st2:
            data["st_low"] = float(st2.group(1))
            data["st_high"] = float(st2.group(2))

    # 中期
    mt = re.search(r"中期\s*\(?1-3个月\)?[\s\S]*?区间[:：]\s*(\d+)\s*[~-]\s*(\d+)", output)
    if mt:
        data["mt_low"] = float(mt.group(1))
        data["mt_high"] = float(mt.group(2))

    # 长期
    lt = re.search(r"长期\s*\(?3-12个月\)?[\s\S]*?区间[:：]\s*(\d+)\s*[~-]\s*(\d+)", output)
    if lt:
        data["lt_low"] = float(lt.group(1))
        data["lt_high"] = float(lt.group(2))

    # 置信度
    conf = re.search(r"置信度评分[\s\S]*?([🟢🟡🔴])\s*([\u4e00-\u9fa5]+)", output)
    if conf:
        data["confidence"] = conf.group(2).strip()

    # 第二日关键因子（注意/关键因子行）
    factors = re.search(r"注意[:：]([^\n]+)", output)
    if factors:
        data["d2_factors"] = factors.group(1).strip()

    # ── 解析信号快照（验证时用于归因）─────────────
    # 提取当日关键信号状态，用于事后分析偏差来源
    signals = {}

    # 均线状态
    ma_match = re.search(r"MA5=([\d.]+)\s+MA10=([\d.]+)\s+MA20=([\d.]+)\s+MA60=([\d.]+)", output)
    if ma_match:
        signals["ma5"] = float(ma_match.group(1))
        signals["ma10"] = float(ma_match.group(2))
        signals["ma20"] = float(ma_match.group(3))
        signals["ma60"] = float(ma_match.group(4))
        signals["ma_bull"] = signals["ma5"] > signals["ma10"] > signals["ma20"]

    # RSI
    rsi_match = re.search(r"RSI\(14\)[^\d]*([\d.]+)", output)
    if rsi_match:
        signals["rsi"] = float(rsi_match.group(1))
        signals["rsi_overbought"] = signals["rsi"] > 65
        signals["rsi_oversold"] = signals["rsi"] < 35

    # MACD方向
    macd_match = re.search(r"MACD[^\w]*([\u4e00-\u9fa5]+)", output)
    if macd_match:
        macd_str = macd_match.group(1)
        signals["macd_bull"] = "金叉" in macd_str or "看涨" in macd_str or "多头" in macd_str
        signals["macd_bear"] = "死叉" in macd_str or "看跌" in macd_str or "空头" in macd_str

    # 成交量比率
    vol_match = re.search(r"比率(\d+\.?\d*)x", output)
    if vol_match:
        signals["vol_ratio"] = float(vol_match.group(1))
        signals["vol_surge"] = signals["vol_ratio"] > 1.2
        signals["vol_shrink"] = signals["vol_ratio"] < 0.8

    # 布林带位置
    bb_match = re.search(r"位置=(\d+)%", output)
    if bb_match:
        signals["bb_position"] = float(bb_match.group(1))

    # 季节性方向
    season_match = re.search(r"季节性[^\n]*?([偏多偏空中性]+)", output)
    if season_match:
        signals["season_dir"] = season_match.group(1)

    data["signals"] = signals

    # ── 解析新闻信息（v4.1新增）─────────────
    # 格式:
    #   【今日政策/市场新闻】
    #     🟢 轻微利多(综合评分: +0.50)共5条
    #     1. 🟢+1.0分 新闻标题...
    #   或者: 暂无相关政策新闻
    news_block = re.search(r'【今日政策/市场新闻】(.+?)(?=\n\n|\n===)', output, re.DOTALL)
    if news_block:
        news_text = news_block.group(1).strip()
        # 解析综合评分
        score_m = re.search(r'综合评分: ([+-]\d+\.?\d*)', news_text)
        if score_m:
            data["news_score"] = float(score_m.group(1))
        # 解析信号
        signal_m = re.search(r'[🟢🔴⚪]\s*([^\n]+?)(?:\s*\(|$)', news_text)
        if signal_m:
            data["news_signal"] = signal_m.group(1).strip()
        # 解析前4条新闻标题
        titles = re.findall(r'\d+\.\s*[🟢🔴⚪][+-]?\d+\.?\d*分\s*([^\n]+)', news_text)
        data["news_titles"] = titles[:4]
        # 新闻条数
        count_m = re.search(r'共(\d+)条', news_text)
        if count_m:
            data["news_count"] = int(count_m.group(1))
        # 无新闻的情况
        if '暂无' in news_text:
            data["news_signal"] = "无"
            data["news_titles"] = []
            data["news_score"] = 0
            data["news_count"] = 0

    # 解析短中长期方向（如果有）
    for horizon in ["st", "mt", "lt"]:
        pattern = {
            "st": rf"{horizon.upper()}.*?方向[:：]\s*([^\n|]+)",
            "mt": rf"{horizon.upper()}.*?方向[:：]\s*([^\n|]+)",
            "lt": rf"{horizon.upper()}.*?方向[:：]\s*([^\n|]+)",
        }.get(horizon.upper(), "")
        # 中文方向词
        dir_h = re.search(rf"{horizon.upper()}.*?([↗↖↘↙偏多偏空中性↔])", output)
        if dir_h:
            data[f"{horizon}_dir"] = dir_h.group(1)

    return data

# ─────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────
def format_direction(direction_str):
    if not direction_str:
        return "未知"
    d = str(direction_str).strip()
    if any(x in d for x in ["↗", "↖", "偏强", "偏多", "偏上", "震荡偏强", "多头", "看涨"]):
        return "偏强"
    if any(x in d for x in ["↘", "↙", "偏弱", "偏空", "偏下", "震荡偏弱", "空头", "看跌"]):
        return "偏弱"
    if any(x in d for x in ["↔", "震荡", "整理", "中性", "纠缠"]):
        return "中性"
    return d

def std_dir(direction_str):
    """返回标准方向标签"""
    return format_direction(direction_str)

def verify_direction(pred_dir, actual_change):
    """验证方向是否正确"""
    if actual_change > 0 and "偏强" in pred_dir:
        return "✅偏强"
    if actual_change < 0 and "偏弱" in pred_dir:
        return "✅偏弱"
    if actual_change == 0 and "中性" in pred_dir:
        return "✅中性"
    # 方向错了
    if "偏强" in pred_dir:
        return "❌实际偏弱"
    if "偏弱" in pred_dir:
        return "❌实际偏强"
    return "—"

def in_range(actual, low, high):
    if low is None or high is None:
        return False
    return low <= actual <= high

# ─────────────────────────────────────────
# 偏差归因
# ─────────────────────────────────────────
def analyze_attribution(pred_date, pred_signals, actual_close, d2_pred, signals_at_prediction):
    """
    分析预测偏差的来源
    signals_at_prediction: 预测时的信号快照 dict
    返回归因字符串
    """
    dev = actual_close - d2_pred
    dev_pct = dev / d2_pred * 100 if d2_pred else 0
    attribution_parts = []

    if abs(dev_pct) <= 0.3:
        return "✅ 偏差极小(≤0.3%)，模型表现良好"

    # 1. 均线信号 vs 实际走势
    if "ma_bull" in signals_at_prediction and "ma5" in signals_at_prediction:
        ma_bull = signals_at_prediction["ma_bull"]
        actual_bull = actual_close > d2_pred
        if ma_bull != actual_bull:
            attribution_parts.append(f"均线信号({('多头排列' if ma_bull else '空头排列')})与实际走势不符")

    # 2. RSI 超买超卖 vs 实际走势
    if "rsi" in signals_at_prediction:
        rsi = signals_at_prediction["rsi"]
        if rsi > 65 and actual_close < d2_pred:
            attribution_parts.append(f"RSI超买({rsi:.0f})但价格未回调，RSI信号失效")
        elif rsi < 35 and actual_close > d2_pred:
            attribution_parts.append(f"RSI超卖({rsi:.0f})但未反弹，超卖信号失效")

    # 3. 成交量信号
    if "vol_surge" in signals_at_prediction:
        if signals_at_prediction["vol_surge"] and actual_close < d2_pred:
            attribution_parts.append("放量不涨，量价背离")
        if signals_at_prediction["vol_shrink"] and abs(dev_pct) > 0.5:
            attribution_parts.append("缩量趋势不稳定，信号可信度低")

    # 4. 布林带位置
    if "bb_position" in signals_at_prediction:
        bb_pos = signals_at_prediction["bb_position"]
        if bb_pos > 85 and actual_close > d2_pred:
            attribution_parts.append(f"布林上轨压力位({bb_pos:.0f}%)未回调，突破信号强")
        elif bb_pos < 15 and actual_close < d2_pred:
            attribution_parts.append(f"布林下轨支撑位({bb_pos:.0f}%)未反弹，超卖信号失效")

    # 5. 季节性
    if "season_dir" in signals_at_prediction:
        season = signals_at_prediction["season_dir"]
        if season in ["偏多", "偏强"] and actual_close < d2_pred:
            attribution_parts.append(f"季节性({season})偏多但价格下跌，季节性失效")
        elif season in ["偏空", "偏弱"] and actual_close > d2_pred:
            attribution_parts.append(f"季节性({season})偏空但价格上涨，季节性失效")

    if not attribution_parts:
        attribution_parts.append(f"偏差{dev_pct:+.1f}%原因不明确，可能受突发事件/政策影响")

    return "；".join(attribution_parts)

# ─────────────────────────────────────────
# 记录预测
# ─────────────────────────────────────────
def record_prediction(data):
    import re
    pred_date = data["date"]
    signals = data.get("signals", {})

    # v4.0 格式：日夜盘完全分离，每列独立
    entry = f"""## 预测 {pred_date}

**日盘收盘(15:00)**: {data.get("day_close", data.get("actual_close", "N/A"))} 元/吨
**夜盘收盘(23:00)**: {data.get("night_close", "—")} 元/吨

### 🌞 日盘(15:00)预测 → 下一交易日日盘

| 项目 | 值 |
|------|-----|
| 预测基准 | {data.get("day_base", data.get("d2_pred", "?"))} 元/吨 |
| 方向 | {format_direction(data.get("d2_direction", ""))} |
| 预测区间 | {data.get("d2_low", "?")} ~ {data.get("d2_high", "?")} 元/吨 |
| ML预测高价 | {data.get("d2_ml_high", "—")} |
| ML预测低价 | {data.get("d2_ml_low", "—")} |
| 置信度 | {data.get("confidence", "N/A")} |

### 🌙 夜盘(23:00)预测 → 下一交易日夜盘

| 项目 | 值 |
|------|-----|
| 预测基准 | {data.get("night_base", "?")} 元/吨 |
| 方向 | {format_direction(data.get("night_direction", data.get("d2_direction", "")))} |
| 预测区间 | {data.get("night_low", data.get("d2_low", "?"))} ~ {data.get("night_high", data.get("d2_high", "?"))} 元/吨 |

### 📆 短中长期预测

| 周期 | 方向 | 区间 |
|------|------|------|
| 短期(1-4周) | {format_direction(data.get("st_dir", "—"))} | {data.get("st_low", "?")}~{data.get("st_high", "?")} |
| 中期(1-3月) | {format_direction(data.get("mt_dir", "—"))} | {data.get("mt_low", "?")}~{data.get("mt_high", "?")} |
| 长期(3-12月) | {format_direction(data.get("lt_dir", "—"))} | {data.get("lt_low", "?")}~{data.get("lt_high", "?")} |

**关键因子**: {data.get("d2_factors", "N/A")}

### 信号快照

| 信号 | 值 | 状态 |
|------|-----|------|
| MA5/MA10/MA20 | {signals.get('ma5','?')}/{signals.get('ma10','?')}/{signals.get('ma20','?')} | {'多头' if signals.get('ma_bull') else '空头/混乱'} |
| RSI(14) | {signals.get('rsi','?')} | {'超买' if signals.get('rsi_overbought') else '超卖' if signals.get('rsi_oversold') else '正常'} |
| 成交量比率 | {signals.get('vol_ratio','?')}x | {'放量' if signals.get('vol_surge') else '缩量' if signals.get('vol_shrink') else '正常'} |
| 布林带位置 | {signals.get('bb_position','?')}% | {'上轨附近' if signals.get('bb_position',0)>80 else '下轨附近' if signals.get('bb_position',0)<20 else '中轨区间'} |
| MACD | {'多头' if signals.get('macd_bull') else '空头' if signals.get('macd_bear') else '中性'} | — |
| 季节性 | {signals.get('season_dir','?')} | — |

### 📰 当日新闻

| 综合评分 | 动向 | 条数 |
|---------|------|------|
| {f"{data['news_score']:+.2f}" if 'news_score' in data else '—'} | {data.get('news_signal','—')} | {data.get('news_count','—')}条 |

{chr(10).join('- '+t for t in data.get('news_titles', [])) if data.get('news_titles') else '无相关政策新闻'}

### 验证结果

| 周期 | 预测基准 | 实际收盘 | 偏差 | 区间命中 | 方向 |
|------|---------|---------|------|---------|------|
| 🌞 日盘→明天 | {data.get("day_base", data.get("d2_pred", "?"))} | _待填_ | _待填_ | _待填_ | _待填_ |
| 🌙 夜盘→下一交易日夜盘 | {data.get("night_base", "?")} | _待填_ | _待填_ | _待填_ | _待填_ |
| 短期 | {data.get("st_low","?")}~{data.get("st_high","?")} | _待填_ | _待填_ | _待填_ | _待填_ |
| 中期 | {data.get("mt_low","?")}~{data.get("mt_high","?")} | _待填_ | _待填_ | _待填_ | _待填_ |
| 长期 | {data.get("lt_low","?")}~{data.get("lt_high","?")} | _待填_ | _待填_ | _待填_ | _待填_ |

### ML高价/低价验证

| 项目 | 预测 | 实际 | 偏差 | 命中 |
|------|------|------|------|------|
| 🌞 日盘高价(明天) | {data.get("d2_ml_high", "—")} | _待填_ | _待填_ | _待填_ |
| 🌞 日盘低价(明天) | {data.get("d2_ml_low", "—")} | _待填_ | _待填_ | _待填_ |

### 偏差归因

_待归因（验证后自动填写）_

---
"""
    HEADER = """# 🌽 中国玉米每日预测记录 (DCE C0)

> v4.0 日夜盘完全分离
> 日盘预测→下一交易日日盘实际收盘；夜盘预测→下一交易日夜盘23:00实际收盘

"""

    SEPARATOR = "\n\n---\n\n"

    existing = PREDICTIONS_FILE.read_text() if PREDICTIONS_FILE.exists() else ""

    # 检查今天是否已经记录过
    if f"## 预测 {pred_date}" in existing:
        # 已存在，替换旧条目（用新数据更新）
        # 用正则替换整个预测块
        old_entry_pattern = re.compile(
            rf'## 预测 {re.escape(pred_date)}.*?(?=\n## 预测 |\n## 准确率统计|\n## 权重校准|\Z)',
            re.DOTALL
        )
        updated = old_entry_pattern.sub(entry.strip(), existing)
        PREDICTIONS_FILE.write_text(updated)
        print(f"[Tracker] 预测已更新: {pred_date}")
    else:
        # 不存在，追加到文件末尾（准确率统计之前）
        stats_match = re.search(r'\n## 准确率统计.*', existing, re.DOTALL)
        if stats_match:
            # 在准确率统计块之前插入
            before_stats = existing[:stats_match.start()]
            stats_section = existing[stats_match.start():]
            new_content = before_stats.rstrip() + SEPARATOR + entry.strip() + SEPARATOR + stats_section.strip() + "\n"
        elif existing.strip():
            # 有内容但没有准确率统计块，直接追加
            new_content = existing.rstrip() + SEPARATOR + entry.strip() + SEPARATOR
        else:
            # 全新文件
            new_content = HEADER.strip() + SEPARATOR + entry.strip() + SEPARATOR
        
        PREDICTIONS_FILE.write_text(new_content)
        print(f"[Tracker] 预测已追加: {pred_date}")

# ─────────────────────────────────────────
# 验证已有记录（第二日 + 短中长期）
# ─────────────────────────────────────────
def verify_pending():
    """验证所有待填记录，包括第二日和短中长期"""
    if not PREDICTIONS_FILE.exists():
        return

    content = PREDICTIONS_FILE.read_text()

    try:
        import akshare as ak
        verify_df = ak.futures_zh_daily_sina(symbol="C0")
        verify_df["date"] = verify_df["date"].astype(str)
        verify_df = verify_df.sort_values("date").reset_index(drop=True)
    except Exception as e:
        print(f"[verify_pending] 数据加载失败: {e}")
        verify_df = None

    pending_dates = re.findall(r"## 预测 (\d{4}-\d{2}-\d{2})", content)
    updated_content = content
    new_attributions = []

    for pred_date in pending_dates:
        block_pattern = f"## 预测 {pred_date}\\b.*?(?=\\n## 预测 \\d|\\n## 准确率统计\\b|$)"
        block_match = re.search(block_pattern, updated_content, re.DOTALL)
        if not block_match:
            continue
        block = block_match.group(0)

        # ══ v4.0 格式检测：日夜盘分离验证 ══
        v4_day_match   = re.search(r"\| 🌞 日盘[^|]*\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|", block)
        v4_night_match = re.search(r"\| 🌙 夜盘[^|]*\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|", block)

        if v4_day_match or v4_night_match:
            # v4.0 格式：统一处理日夜盘验证
            updated_block = _verify_day_night_v4(block, pred_date, verify_df)
            if updated_block:
                updated_content = updated_content.replace(block, updated_block, 1)
        else:
            # ══ 第二日·日盘验证（旧格式v3.0）════
            if re.search(r"\| 第二日·日盘 \|.*?_待填_", block):
                updated_block, attr = _verify_d2(block, pred_date, verify_df)
                if updated_block:
                    updated_content = updated_content.replace(block, updated_block, 1)
                    if attr:
                        new_attributions.append(attr)
            # ══ 旧格式第二日验证（兼容v3.0之前记录）════
            elif re.search(r"\| 第二日 \|.*?_待填_", block):
                updated_block, attr = _verify_d2_legacy(block, pred_date, verify_df)
                if updated_block:
                    updated_content = updated_content.replace(block, updated_block, 1)
                    if attr:
                        new_attributions.append(attr)

            # ══ 第二日·夜盘验证（旧格式v2.0）════
            if re.search(r"\| 第二日·夜盘 \|.*?_待填_", block):
                updated_block = _verify_night(block, pred_date)
                if updated_block:
                    updated_content = updated_content.replace(block, updated_block, 1)

        # ══ 短中长期验证（v4.0格式也走这里） ══
        if verify_df is not None:
            updated_block = _verify_horizons(block, pred_date, verify_df)
            if updated_block:
                updated_content = updated_content.replace(block, updated_block, 1)

    PREDICTIONS_FILE.write_text(updated_content)

    # 写归因
    if new_attributions:
        _append_attributions(new_attributions)

    update_stats()
    run_calibration()  # 每次验证后尝试跑校准

def _verify_d2(block, pred_date, verify_df):
    """验证第二日预测"""
    # 获取T+1收盘
    d2_actual, d2_date = None, None
    if verify_df is not None:
        future = verify_df[verify_df["date"] > pred_date]
        if len(future) > 0:
            d2_actual = float(future.iloc[0]["close"])
            d2_date = str(future.iloc[0]["date"])

    # 提取预测信息
    # 验证结果表格列: |时间维度|预测基准|实际收盘|偏差|区间命中|方向|
    # "预测内容"表格列: |时间维度|方向|区间|置信度|
    # 分开搜索两个表格，避免列对齐混乱
    # 1) 从"验证结果"section取 d2_pred（列2=预测基准）
    vsection = re.search(r'### 验证结果\n(.*?)(?=\n###|\n---)', block, re.DOTALL)
    vs = vsection.group(1) if vsection else block
    m_verify = re.search(r"\| 第二日·日盘 \|([^|]+)\|([^|]+)\|", vs)
    if m_verify:
        col2 = m_verify.group(1).strip()
        d2_pred = float(col2) if col2 not in ("_待填_", "?") else None
    else:
        d2_pred = None

    # 2) 从"预测内容"section取方向和区间（避免误匹配验证结果行）
    pcsection = re.search(r'### 预测内容\n(.*?)(?=\n###|\n验证结果)', block, re.DOTALL)
    pcs = pcsection.group(1) if pcsection else ''
    range_m = re.search(r"\|\s*第二日\s*\|([^|]+)\|([^|]+)\|([^|]+)?\|", pcs)
    if range_m:
        direction = range_m.group(1).strip()
        range_str = range_m.group(2).strip()
        range_parts = range_str.split("~")
        d2_low = float(range_parts[0]) if range_parts[0] not in ("?", "_待填_") else None
        d2_high = float(range_parts[1]) if len(range_parts) > 1 and range_parts[1] not in ("?", "_待填_") else None
    else:
        direction, d2_low, d2_high = "", None, None




    # 提取预测时信号快照（用于归因）
    # v3.0新增；v2.0 block无此section，跳过
    signals = {}
    snap = re.search(r'### 预测时信号快照\n(.*?)(?=\n###|\n---)', block, re.DOTALL)
    if not snap:
        pass  # v2.0格式，无信号快照
    else:
        snap_text = snap.group(1)
        ma_m = re.search(r"MA5/MA10/MA20[^\|]*\|([^\|]+)\|", snap_text)
        if ma_m:
            ma_vals = re.findall(r"[\d.]+", ma_m.group(1))
            if len(ma_vals) >= 3:
                signals["ma_bull"] = ma_vals[0] > ma_vals[1] > ma_vals[2]
                signals["ma5"], signals["ma10"], signals["ma20"] = [float(v) for v in ma_vals[:3]]
        rsi_m = re.search(r"RSI\(14\)[^\|]*\|([\d.]+)", snap_text)
        if rsi_m and rsi_m.group(1):
            try:
                signals["rsi"] = float(rsi_m.group(1))
                signals["rsi_overbought"] = signals["rsi"] > 65
                signals["rsi_oversold"] = signals["rsi"] < 35
            except (ValueError, TypeError):
                pass
        vol_m = re.search(r"成交量比率[^\|]*\|([\d.]+)x", snap_text)
        if vol_m and vol_m.group(1):
            try:
                signals["vol_ratio"] = float(vol_m.group(1))
                signals["vol_surge"] = signals["vol_ratio"] > 1.2
                signals["vol_shrink"] = signals["vol_ratio"] < 0.8
            except (ValueError, TypeError):
                pass
        bb_m = re.search(r"布林带位置[^\|]*\|([\d.]+)%", snap_text)
        if bb_m and bb_m.group(1):
            try:
                signals["bb_position"] = float(bb_m.group(1))
            except (ValueError, TypeError):
                pass
        season_m = re.search(r"季节性[^\|]*\|([^\|]+)", snap_text)
        if season_m and season_m.group(1):
            signals["season_dir"] = season_m.group(1).strip()

    if d2_actual is None or d2_pred is None:
        return None, None

    dev = d2_actual - d2_pred
    dev_pct = dev / d2_pred * 100
    range_ok = "✅" if in_range(d2_actual, d2_low, d2_high) else "❌"
    dir_label = verify_direction(direction, dev)

    # 获取T+1实际高低价
    d2_actual_high, d2_actual_low = None, None
    if verify_df is not None:
        future = verify_df[verify_df["date"] > pred_date]
        if len(future) > 0:
            d2_actual_high = float(future.iloc[0]["high"])
            d2_actual_low = float(future.iloc[0]["low"])

    # 判断当前行是6列旧格式还是12列新格式
    # 用严格行级匹配（每行以|开头，列数据行不含header/分隔行关键字）
    def get_row_line(block_text):
        """提取第二日验证数据行（以|开头的非分隔行）"""
        lines = block_text.split('\n')
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('|') and '第二日·日盘' in stripped and '---' not in stripped and stripped.count('|') >= 6:
                return stripped, i
        return None, -1

    row_line, row_idx = get_row_line(block)
    n_cols = row_line.count('|') if row_line else 0

    if n_cols >= 12:
        # 新格式12列：|维度|基准|实际|偏差|区间命中|方向|预测高价|预测低价|实际高价|实际低价|高价命中|低价命中|
        cols = [c.strip() for c in row_line.split('|')]
        ml_h = float(cols[7]) if cols[7] not in ('?', '—', '_待填_') else None
        ml_l = float(cols[8]) if cols[8] not in ('?', '—', '_待填_') else None

        if ml_h is not None and d2_actual_high is not None:
            hit_h = '✅' if abs(ml_h - d2_actual_high) / d2_actual_high < 0.005 else '❌'
        else:
            hit_h = '—'
        if ml_l is not None and d2_actual_low is not None:
            hit_l = '✅' if abs(ml_l - d2_actual_low) / d2_actual_low < 0.005 else '❌'
        else:
            hit_l = '—'

        actual_h_str = f'{d2_actual_high:.0f}' if d2_actual_high else '_待填_'
        actual_l_str = f'{d2_actual_low:.0f}' if d2_actual_low else '_待填_'

        new_row = (f'| 第二日·日盘 | {d2_pred:.0f} | {d2_actual:.0f} ({d2_date}) '
                   f'| {dev:+.0f}({dev_pct:+.1f}%) | {range_ok} | {dir_label} | '
                   f'{cols[7]} | {cols[8]} | {actual_h_str} | {actual_l_str} | {hit_h} | {hit_l} |')
        # 替换整行（保持其他行不变）
        block_lines = block.split('\n')
        block_lines[row_idx] = new_row
        new_block = '\n'.join(block_lines)
    elif n_cols >= 6:
        # 旧格式6列：直接升级为12列
        _ah = f'{d2_actual_high:.0f}' if d2_actual_high else '_'
        _al = f'{d2_actual_low:.0f}' if d2_actual_low else '_'
        new_row = (f'| 第二日·日盘 | {d2_pred:.0f} | {d2_actual:.0f} ({d2_date}) '
                   f'| {dev:+.0f}({dev_pct:+.1f}%) | {range_ok} | {dir_label} | '
                   f'— | — | {_ah} | {_al} | — | — |')
        block_lines = block.split('\n')
        block_lines[row_idx] = new_row
        new_block = '\n'.join(block_lines)
    else:
        new_block = block

    # 偏差归因
    attribution = None
    if abs(dev_pct) >= 0.3:
        attribution = analyze_attribution(pred_date, None, d2_actual, d2_pred, signals)
        attr_row = f"**偏差归因**: {attribution}"
        # 先尝试替换已有归因 section 里的占位符
        new_block = re.sub(
            r"### 偏差归因\n\n_[^_]*_",
            f"### 偏差归因\n\n{attr_row}",
            new_block, count=1
        )
        # 如果 block 里根本没有归因 section（在旧格式里），则在最后的 --- 分隔符前插入
        if "### 偏差归因" not in new_block:
            insert_point = new_block.rfind("\n---\n")
            if insert_point == -1:
                insert_point = len(new_block)
            attr_section = f"\n### 偏差归因\n\n{attr_row}\n"
            new_block = new_block[:insert_point] + attr_section + new_block[insert_point:]

    return new_block, {"date": pred_date, "attribution": attribution, "dev_pct": dev_pct}

def _verify_d2_legacy(block, pred_date, verify_df):
    """验证旧格式第二日预测（v3.0之前，"第二日"非"第二日·日盘"）"""
    from prediction_tracker import in_range, format_direction, verify_direction, analyze_attribution
    import re

    d2_actual, d2_date = None, None
    if verify_df is not None:
        future = verify_df[verify_df["date"] > pred_date]
        if len(future) > 0:
            d2_actual = float(future.iloc[0]["close"])
            d2_date = str(future.iloc[0]["date"])
            d2_actual_high = float(future.iloc[0]["high"])
            d2_actual_low = float(future.iloc[0]["low"])

    vsection = re.search(r'### 验证结果\n(.*?)(?=\n###|\n---)', block, re.DOTALL)
    vs = vsection.group(1) if vsection else block
    m_verify = re.search(r"\| 第二日 \|([^|]+)\|([^|]+)\|", vs)
    if m_verify:
        col2 = m_verify.group(1).strip()
        d2_pred = float(col2) if col2 not in ("_待填_", "?") else None
    else:
        d2_pred = None

    pcsection = re.search(r'### 预测内容\n(.*?)(?=\n###|\n验证结果)', block, re.DOTALL)
    pcs = pcsection.group(1) if pcsection else ""
    range_m = re.search(r"\|\s*第二日\s*\|([^|]+)\|([^|]+)\|([^|]+)?\|", pcs)
    if range_m:
        direction = range_m.group(1).strip()
        range_str = range_m.group(2).strip()
        range_parts = range_str.split("~")
        d2_low = float(range_parts[0]) if range_parts and range_parts[0] not in ("?", "_待填_") else None
        d2_high = float(range_parts[1]) if len(range_parts) > 1 and range_parts[1] not in ("?", "_待填_") else None
    else:
        direction, d2_low, d2_high = "", None, None

    signals = {}
    snap = re.search(r'### 预测时信号快照\n(.*?)(?=\n###|\n---)', block, re.DOTALL)
    if snap:
        snap_text = snap.group(1)
        ma_m = re.search(r"MA5/MA10/MA20[^|]*\|([^|]+)\|", snap_text)
        if ma_m:
            ma_vals = re.findall(r"[\d.]+", ma_m.group(1))
            if len(ma_vals) >= 3:
                signals["ma_bull"] = ma_vals[0] > ma_vals[1] > ma_vals[2]
                signals["ma5"], signals["ma10"], signals["ma20"] = [float(v) for v in ma_vals[:3]]
        rsi_m = re.search(r"RSI\(14\)[^|]*\|([\d.]+)", snap_text)
        if rsi_m and rsi_m.group(1):
            try:
                signals["rsi"] = float(rsi_m.group(1))
                signals["rsi_overbought"] = signals["rsi"] > 65
                signals["rsi_oversold"] = signals["rsi"] < 35
            except:
                pass
        vol_m = re.search(r"成交量比率[^|]*\|([^|]+)\|", snap_text)
        if vol_m:
            try:
                signals["vol_ratio"] = float(re.findall(r"[\d.]+", vol_m.group(1))[0])
                signals["vol_surge"] = signals["vol_ratio"] > 1.2
                signals["vol_shrink"] = signals["vol_ratio"] < 0.8
            except:
                pass
        bb_m = re.search(r"布林带位置[^|]*\|([\d.]+)", snap_text)
        if bb_m:
            try:
                signals["bb_position"] = float(bb_m.group(1))
            except:
                pass
        macd_m = re.search(r"MACD[^|]*\|([^|]+)\|", snap_text)
        if macd_m:
            macd_str = macd_m.group(1)
            signals["macd_bull"] = "多头" in macd_str
            signals["macd_bear"] = "空头" in macd_str
        season_m = re.search(r"季节性[^|]*\|([^|]+)\|", snap_text)
        if season_m:
            signals["season_dir"] = season_m.group(1).strip()

    if d2_actual is None or d2_pred is None:
        return block, None

    dev = d2_actual - d2_pred
    dev_pct = dev / d2_pred * 100
    range_ok = "✅" if d2_low and d2_high and in_range(d2_actual, d2_low, d2_high) else "❌"
    dir_label = verify_direction(direction, dev)

    def get_row_line(block_text):
        lines = block_text.split('\n')
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('|') and '第二日' in stripped and '---' not in stripped and stripped.count('|') >= 6:
                return stripped, i
        return None, -1

    row_line, row_idx = get_row_line(block)
    n_cols = row_line.count('|') if row_line else 0

    if n_cols >= 12:
        cols = [c.strip() for c in row_line.split('|')]
        ml_h = float(cols[7]) if cols[7] not in ("?", "—", "_待填_") else None
        ml_l = float(cols[8]) if cols[8] not in ("?", "—", "_待填_") else None
        if ml_h is not None and d2_actual_high is not None:
            hit_h = "✅" if abs(ml_h - d2_actual_high) / d2_actual_high < 0.005 else "❌"
        else:
            hit_h = "—"
        if ml_l is not None and d2_actual_low is not None:
            hit_l = "✅" if abs(ml_l - d2_actual_low) / d2_actual_low < 0.005 else "❌"
        else:
            hit_l = "—"
        actual_h_str = f"{d2_actual_high:.0f}" if d2_actual_high else "_待填_"
        actual_l_str = f"{d2_actual_low:.0f}" if d2_actual_low else "_待填_"
        new_row = (f'| 第二日 | {d2_pred:.0f} | {d2_actual:.0f} ({d2_date}) '
                  f'| {dev:+.0f}({dev_pct:+.1f}%) | {range_ok} | {dir_label} | '
                  f'{cols[7]} | {cols[8]} | {actual_h_str} | {actual_l_str} | {hit_h} | {hit_l} |')
        block_lines = block.split('\n')
        block_lines[row_idx] = new_row
        new_block = '\n'.join(block_lines)
    elif n_cols >= 6:
        _ah = f"{d2_actual_high:.0f}" if d2_actual_high else "_"
        _al = f"{d2_actual_low:.0f}" if d2_actual_low else "_"
        new_row = (f'| 第二日 | {d2_pred:.0f} | {d2_actual:.0f} ({d2_date}) '
                  f'| {dev:+.0f}({dev_pct:+.1f}%) | {range_ok} | {dir_label} | '
                  f'— | — | {_ah} | {_al} | — | — |')
        block_lines = block.split('\n')
        block_lines[row_idx] = new_row
        new_block = '\n'.join(block_lines)
    else:
        new_block = block

    attribution = None
    if abs(dev_pct) >= 0.3:
        attribution = analyze_attribution(pred_date, None, d2_actual, d2_pred, signals)
        attr_row = f"**偏差归因**: {attribution}"
        insert_point = len(new_block)
        attr_section = f"\n### 偏差归因\n\n{attr_row}\n"
        new_block = new_block[:insert_point] + attr_section + new_block[insert_point:]

    return new_block, {"date": pred_date, "attribution": attribution, "dev_pct": dev_pct}

def _verify_night(block, pred_date):
    """验证第二日·夜盘预测（v2.0新增）"""
    from prediction_tracker import get_actual_night_close_on_date

    # 获取夜盘实际数据
    night_actual, night_actual_high, night_actual_low = get_actual_night_close_on_date(pred_date)

    # 从"预测内容"表格提取夜盘预测数据
    pcsection = re.search(r'### 预测内容\n(.*?)(?=\n###|\n验证结果)', block, re.DOTALL)
    pcs = pcsection.group(1) if pcsection else ''
    night_pred_m = re.search(r"\| 第二日·夜盘 \|([^|]+)\|([^|]+)\|([^|]+)\|", pcs)

    if not night_pred_m:
        return block  # 无夜盘预测数据，跳过

    night_base = float(night_pred_m.group(1).strip()) if night_pred_m.group(1).strip() not in ("?", "_待填_") else None
    direction = night_pred_m.group(2).strip()
    range_str = night_pred_m.group(3).strip()
    range_parts = range_str.split("~")
    night_low = float(range_parts[0]) if range_parts and range_parts[0] not in ("?", "_待填_") else None
    night_high = float(range_parts[1]) if len(range_parts) > 1 and range_parts[1] not in ("?", "_待填_") else None

    # 实际值获取失败则跳过
    if night_actual is None:
        return block

    # 计算偏差
    if night_base:
        dev = night_actual - night_base
        dev_pct = dev / night_base * 100
        dev_str = f'{dev:+.0f}({dev_pct:+.1f}%)'
        range_ok = '✅' if night_low and night_high and night_low <= night_actual <= night_high else '❌'
        # 方向验证（基于预测方向）
        dir_str = format_direction(direction)
        actual_dir = '偏强' if dev > 0 else '偏弱' if dev < 0 else '中性'
        dir_ok = '✅' if dir_str == actual_dir else f'❌实际{actual_dir}'
    else:
        dev_str = '—'
        range_ok = '—'
        dir_ok = '—'

    actual_h_str = f'{night_actual_high:.0f}' if night_actual_high else '_待填_'
    actual_l_str = f'{night_actual_low:.0f}' if night_actual_low else '_待填_'
    high_ok = '—'
    low_ok = '—'
    if night_actual_high and night_high:
        high_ok = '✅' if abs(night_actual_high - night_high) / night_actual_high < 0.005 else '❌'
    if night_actual_low and night_low:
        low_ok = '✅' if abs(night_actual_low - night_low) / night_actual_low < 0.005 else '❌'

    new_night_row = (f'| 第二日·夜盘 | {night_base if night_base else "?"} | {night_actual:.0f} '
                     f'| {dev_str} | {range_ok} | {dir_ok} | — | — | {actual_h_str} | {actual_l_str} | {high_ok} | {low_ok} |')

    # 替换夜盘行
    def replace_night_row(block_text):
        lines = block_text.split('\n')
        for i, line in enumerate(lines):
            if '| 第二日·夜盘 |' in line and '---' not in line:
                lines[i] = new_night_row
                return '\n'.join(lines), True
        return block_text, False

    new_block, changed = replace_night_row(block)
    return new_block


def _verify_day_night_v4(block, pred_date, verify_df):
    """
    v4.0 日夜盘分离验证：
    - 日盘预测 → 下一交易日日盘收盘（从 verify_df 获取）
    - 夜盘预测 → 下一交易日夜盘23:00收盘（从5分钟K线获取）
    """
    from prediction_tracker import get_actual_night_close_on_date

    # ── 获取实际收盘数据 ──
    d2_actual, d2_date = None, None
    d2_actual_high, d2_actual_low = None, None
    if verify_df is not None:
        future = verify_df[verify_df["date"] > pred_date]
        if len(future) > 0:
            d2_actual = float(future.iloc[0]["close"])
            d2_date   = str(future.iloc[0]["date"])
            d2_actual_high = float(future.iloc[0]["high"])
            d2_actual_low = float(future.iloc[0]["low"])

    # 下一交易日（用于夜盘5分钟K线查询）
    ntd = None
    if verify_df is not None:
        future2 = verify_df[verify_df["date"] > pred_date]
        if len(future2) > 0:
            ntd = str(future2.iloc[0]["date"])
    night_actual, night_high, night_low = None, None, None
    if ntd:
        night_actual, night_high, night_low = get_actual_night_close_on_date(ntd)

    lines = block.split("\n")
    new_lines = []
    changed = False

    # 用于归因的信号快照
    signals = {}
    snap = re.search(r"### 信号快照\n(.*?)(?=\n###|\n---)", block, re.DOTALL)
    if snap:
        snap_text = snap.group(1)
        ma_m = re.search(r"MA5/MA10/MA20[^|]*\|([^|]+)\|", snap_text)
        if ma_m:
            ma_vals = re.findall(r"[\d.]+", ma_m.group(1))
            if len(ma_vals) >= 3:
                signals["ma_bull"] = ma_vals[0] > ma_vals[1] > ma_vals[2]
                signals["ma5"] = float(ma_vals[0])
        rsi_m = re.search(r"RSI\(14\)[^|]*\|([\d.]+)", snap_text)
        if rsi_m:
            try: signals["rsi"] = float(rsi_m.group(1))
            except: pass
        vol_m = re.search(r"成交量比率[^|]*\|([\d.]+)x", snap_text)
        if vol_m:
            try: signals["vol_ratio"] = float(vol_m.group(1))
            except: pass
        bb_m = re.search(r"布林带位置[^|]*\|([\d.]+)%", snap_text)
        if bb_m:
            try: signals["bb_position"] = float(bb_m.group(1))
            except: pass

    # 从预测内容提取方向（通用：支持日盘和夜盘）
    def get_pred_direction(block, session_label):
        # session_label: "日盘" 或 "夜盘"
        pat = rf"### .+?{session_label}.+?\n\n\| 项目 \| 值 \|\n\|------\|----\|\n\| 预测基准 \| ([^|]+)"
        m = re.search(pat, block)
        if not m:
            return "?"
        base_val = m.group(1).strip()
        dir_m = re.search(rf"方向 \| ([^|]+)", block)
        if dir_m:
            return format_direction(dir_m.group(1).strip())
        return "?"

    for line in lines:
        stripped = line.strip()

        # ── ML日盘高价/低价行：必须先于通用日盘行处理 ──
        if re.search(r"\| [^|]*日盘高价[^|]*\|", stripped) and "_待填_" in stripped:
            cols = [c.strip() for c in stripped.split("|")]
            cols = [c for c in cols if c]
            if d2_actual_high is not None and len(cols) >= 2:
                try:
                    pred_high = float(cols[1]) if cols[1] not in ("?", "—", "_待填_") else None
                except:
                    pred_high = None
                if pred_high:
                    dev = d2_actual_high - pred_high
                    hit = "✅" if abs(dev / pred_high) < 0.005 else "❌"
                    cols = [cols[0], f"{pred_high:.0f}", f"{d2_actual_high:.0f}", f"{dev:+.0f}", hit]
                    new_lines.append("| " + " | ".join(cols) + " |")
                    changed = True
                    continue
            new_lines.append(line)

        elif re.search(r"\| [^|]*日盘低价[^|]*\|", stripped) and "_待填_" in stripped:
            cols = [c.strip() for c in stripped.split("|")]
            cols = [c for c in cols if c]
            if d2_actual_low is not None and len(cols) >= 2:
                try:
                    pred_low = float(cols[1]) if cols[1] not in ("?", "—", "_待填_") else None
                except:
                    pred_low = None
                if pred_low:
                    dev = d2_actual_low - pred_low
                    hit = "✅" if abs(dev / pred_low) < 0.005 else "❌"
                    cols = [cols[0], f"{pred_low:.0f}", f"{d2_actual_low:.0f}", f"{dev:+.0f}", hit]
                    new_lines.append("| " + " | ".join(cols) + " |")
                    changed = True
                    continue
            new_lines.append(line)

        # ── 日盘行 ──
        elif re.search(r"\| [^|]*日盘[^|]*\|", stripped) and "_待填_" in stripped:
            cols = [c.strip() for c in stripped.split("|")]
            cols = [c for c in cols if c]
            if d2_actual is not None and len(cols) >= 2:
                try:
                    base = float(cols[1]) if cols[1] not in ("?", "_待填_") else None
                except:
                    base = None
                if base:
                    dev     = d2_actual - base
                    dev_pct = dev / base * 100
                    dev_str = f"{dev:+.0f}({dev_pct:+.1f}%)"
                    direction = get_pred_direction(block, "日盘")
                    dir_ok = "✅" if ((dev > 0 and "偏强" in direction) or
                                      (dev < 0 and "偏弱" in direction)) else f"❌实际{'偏强' if dev > 0 else '偏弱'}"
                    try:
                        day_low = float(cols[2]) if len(cols) > 2 and cols[2] not in ("?", "_待填_") else None
                        day_high = float(cols[3]) if len(cols) > 3 and cols[3] not in ("?", "_待填_") else None
                    except:
                        day_low, day_high = None, None
                    range_ok = "✅" if (day_low is not None and day_high is not None and day_low <= d2_actual <= day_high) else "❌"
                    cols = [cols[0], f"{base:.0f}", f"{d2_actual:.0f} ({d2_date})",
                        dev_str, range_ok, dir_ok]
                    new_lines.append("| " + " | ".join(cols) + " |")
                    changed = True
                    continue
            new_lines.append(line)

        # ── 夜盘行 ──
        elif re.search(r"\| [^|]*夜盘[^|]*\|", stripped) and "_待填_" in stripped:
            cols = [c.strip() for c in stripped.split("|")]
            cols = [c for c in cols if c]
            if night_actual is not None and len(cols) >= 2:
                try:
                    base = float(cols[1]) if cols[1] not in ("?", "_待填_") else None
                except:
                    base = None
                if base:
                    dev     = night_actual - base
                    dev_pct = dev / base * 100
                    dev_str = f"{dev:+.0f}({dev_pct:+.1f}%)"
                    direction = get_pred_direction(block, "夜盘")
                    dir_ok = "✅" if ((dev > 0 and "偏强" in direction) or
                                      (dev < 0 and "偏弱" in direction)) else f"❌实际{'偏强' if dev > 0 else '偏弱'}"
                    try:
                        night_low = float(cols[2]) if len(cols) > 2 and cols[2] not in ("?", "_待填_") else None
                        night_high = float(cols[3]) if len(cols) > 3 and cols[3] not in ("?", "_待填_") else None
                    except:
                        night_low, night_high = None, None
                    night_range_ok = "✅" if (night_low is not None and night_high is not None and night_low <= night_actual <= night_high) else "❌"
                    cols = [cols[0], f"{base:.0f}", f"{night_actual:.0f}",
                        dev_str, night_range_ok, dir_ok]
                    new_lines.append("| " + " | ".join(cols) + " |")
                    changed = True
                    continue
            new_lines.append(line)
        else:
            new_lines.append(line)

    if not changed:
        return None

    new_block = "\n".join(new_lines)

    # 偏差归因（日盘，只有在base被定义时才执行）
    if d2_actual is not None and 'base' in dir() and base is not None:
        dev_pct = (d2_actual - base) / base * 100
        if abs(dev_pct) >= 0.3:
            attr_text = analyze_attribution(pred_date, None, d2_actual, base, signals)
            new_block = re.sub(
                r"_待归因（验证后自动填写）_",
                f"**日盘偏差归因**: {attr_text}",
                new_block, count=1
            )

    return new_block


def _verify_horizons(block, pred_date, verify_df):
    """验证短期(20日)、中期(60日)、长期(250日)"""
    new_block = block
    changed = False

    horizons = [
        ("短期(1-4周)", 20, "st"),
        ("中期(1-3月)", 60, "mt"),
        ("长期(3-12月)", 250, "lt"),
    ]

    for label, n_days, key in horizons:
        # 检查该行是否还是待填状态
        row_pattern = rf"\| {re.escape(label)} \|[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|"
        if not re.search(row_pattern, new_block):
            continue
        row_match = re.search(row_pattern, new_block)
        if not row_match:
            continue

        row = row_match.group(0)
        if "_待填_" not in row:
            continue

        # 获取预测区间
        m_low = re.search(rf"\|\s*{re.escape(label)}\s*\|[^|]*\|\s*([\d.]+)\s*[~-]", row)
        m_high = re.search(r"[~-]\s*([\d.]+)\s*元/吨", row)
        if not m_high:
            m_high = re.search(r"[~-]\s*([\d.]+)\s*\|", row)
        low = float(m_low.group(1)) if m_low else None
        high = float(m_high.group(1)) if m_high else None

        # 提取方向
        dir_m = re.search(rf"\|\s*{re.escape(label)}\s*\|\s*(\S+)", row)
        direction = dir_m.group(1) if dir_m else ""

        # 获取N日后收盘
        actual, actual_date = get_price_N_days_after(pred_date, n_days)

        if actual is None or low is None:
            # 数据还不够老，跳过
            continue

        dev = actual - low
        range_ok = "✅" if in_range(actual, low, high) else "❌"
        dir_label = verify_direction(direction, dev)

        new_row = (
            f"| {label} | {low:.0f}~{high:.0f} | "
            f"{actual:.0f} ({actual_date}) | {dev:+.0f}({dev/low*100:+.1f}%) | {range_ok} | {dir_label} |"
        )

        tmp = re.sub(row_pattern, new_row, new_block, count=1)
        if tmp != new_block:
            new_block = tmp
            changed = True

    return new_block if changed else None

def _append_attributions(attributions):
    """将归因结果追加到tracker状态文件（按日期去重，保留最新归因）"""
    state = load_state()
    existing = state.setdefault("attributions", [])
    # 按日期去重：新归因覆盖同日旧归因
    existing_by_date = {}
    for attr in existing:
        if isinstance(attr, dict) and "date" in attr:
            existing_by_date[attr["date"]] = attr
    for attr in attributions:
        if isinstance(attr, dict) and "date" in attr:
            existing_by_date[attr["date"]] = attr
    # 重新构建列表，只保留最近30条
    merged = sorted(existing_by_date.values(), key=lambda x: x.get("date", ""), reverse=True)
    state["attributions"] = merged[:30]
    save_state(state)

# ─────────────────────────────────────────
# 统计更新
# ─────────────────────────────────────────
def update_stats():
    """v4.0: 分别统计日盘/夜盘/短中长期，独立准确率"""
    if not PREDICTIONS_FILE.exists():
        return

    content = PREDICTIONS_FILE.read_text()

    def parse_verified_rows():
        """
        解析验证结果表，区分:
        - day_rows: 🌞 日盘验证行
        - night_rows: 🌙 夜盘验证行
        - st_rows: 短期验证行
        - mt_rows: 中期验证行
        - lt_rows: 长期验证行（v4.0新增）
        """
        def parse_row(line):
            cols = [c.strip() for c in line.split('|')]
            cols = [c for c in cols if c]
            if len(cols) < 5:
                return None
            try:
                # 列2=预测基准（可能是 "2354" 或 "2340~2360" 格式）
                base_str = cols[1]
                if '~' in base_str:
                    # 区间格式（短期/中期/长期），不含方向列
                    parts = base_str.split('~')
                    low, high = float(parts[0]), float(parts[1])
                    actual_m = re.search(r'([\d.]+)', cols[2])
                    actual = float(actual_m.group(1)) if actual_m else None
                    dev_pct = re.search(r'([+-]?[\d.]+)%', cols[3])
                    dev = float(dev_pct.group(1)) if dev_pct else 0.0
                    range_hit = '✅' in cols[4] if len(cols) > 4 else False
                    return {'base': low, 'actual': actual, 'dev': dev,
                            'range_hit': range_hit, 'low': low, 'high': high,
                            'dir_ok': False, 'hit_h': False, 'hit_l': False}
                else:
                    # 单值格式（日/夜盘）
                    pred = float(base_str)
                    actual_m = re.search(r'([\d.]+)', cols[2])
                    if not actual_m:
                        return None
                    actual = float(actual_m.group(1))
                    dev_m = re.search(r'([+-]?[\d.]+)%', cols[3])
                    dev_pct = float(dev_m.group(1)) if dev_m else 0.0
                    # 也提取绝对偏差（元）
                    dev_abs = actual - pred
                    range_hit = '✅' in cols[4] if len(cols) > 4 else False
                    dir_ok = '✅' in cols[5] if len(cols) > 5 else False
                    hit_h = '✅' in cols[10] if len(cols) > 10 else False
                    hit_l = '✅' in cols[11] if len(cols) > 11 else False
                    return {'pred': pred, 'actual': actual, 'dev': dev_pct,
                            'dev_abs': dev_abs,
                            'range_hit': range_hit, 'dir_ok': dir_ok,
                            'hit_h': hit_h, 'hit_l': hit_l}
            except:
                return None

        def _parse_hl_hits(block):
            m = re.search(r'### ML高价/低价验证\n(.*?)(?=\n### |\n---\n|$)', block, flags=re.DOTALL)
            if not m:
                return None, None

            hit_h, hit_l = None, None
            for line in m.group(1).split('\n'):
                if '|' not in line or '_待填_' in line:
                    continue
                cols = [c.strip() for c in line.split('|')]
                cols = [c for c in cols if c]
                if len(cols) < 5:
                    continue
                if '🌞 日盘高价' in cols[0]:
                    hit_h = '✅' in cols[4]
                elif '🌞 日盘低价' in cols[0]:
                    hit_l = '✅' in cols[4]
            return hit_h, hit_l

        day_rows, night_rows, st_rows, mt_rows, lt_rows = [], [], [], [], []
        blocks = re.split(r'(?=\n## 预测 )', content)
        for block in blocks:
            block_hit_h, block_hit_l = _parse_hl_hits(block)
            for line in block.split('\n'):
                if '|' not in line:
                    continue
                if re.search(r'\| 🌞 日盘\(|\| 🌞 日盘→', line):
                    if '_待填_' not in line:
                        p = parse_row(line)
                        if p:
                            if block_hit_h is not None:
                                p['hit_h'] = block_hit_h
                            if block_hit_l is not None:
                                p['hit_l'] = block_hit_l
                            day_rows.append(p)
                elif re.search(r'\| 🌙 夜盘\(|\| 🌙 夜盘→', line):
                    if '_待填_' not in line:
                        p = parse_row(line)
                        if p: night_rows.append(p)
                elif re.search(r'\| 短期 \|', line):
                    if re.search(r'\(\d{4}-\d{2}-\d{2}\)', line):
                        p = parse_row(line)
                        if p: st_rows.append(p)
                elif re.search(r'\| 中期 \|', line):
                    if re.search(r'\(\d{4}-\d{2}-\d{2}\)', line):
                        p = parse_row(line)
                        if p: mt_rows.append(p)
                elif re.search(r'\| 长期 \|', line):
                    if re.search(r'\(\d{4}-\d{2}-\d{2}\)', line):
                        p = parse_row(line)
                        if p: lt_rows.append(p)
        return day_rows, night_rows, st_rows, mt_rows, lt_rows

    day_rows, night_rows, st_rows, mt_rows, lt_rows = parse_verified_rows()

    def stats_table_v4(rows, label):
        n = len(rows)
        if n == 0:
            return f"| {label} | 0 | — | — | — |", 0.0, 0.0
        avg_dev_abs = statistics.mean([abs(r.get("dev_abs", r["dev"] * r.get("pred", 2300) / 100)) for r in rows])
        range_hit_rate = sum(1 for r in rows if r["range_hit"]) / n * 100
        dir_ok_rate = sum(1 for r in rows if r["dir_ok"]) / n * 100 if rows and 'dir_ok' in rows[0] else 0.0
        hit_h_rate = sum(1 for r in rows if r.get("hit_h", False)) / n * 100
        hit_l_rate = sum(1 for r in rows if r.get("hit_l", False)) / n * 100
        hh = sum(1 for r in rows if r.get("hit_h", False))
        hl = sum(1 for r in rows if r.get("hit_l", False))
        rh = sum(1 for r in rows if r["range_hit"])
        if 'dir_ok' in rows[0]:
            return (f"| {label} | {n} | {range_hit_rate:.0f}%({rh}/{n}) "
                    f"| {dir_ok_rate:.0f}% | {avg_dev_abs:.0f}元/吨 "
                    f"| {hit_h_rate:.0f}%({hh}/{n})/{hit_l_rate:.0f}%({hl}/{n}) |"), avg_dev_abs, dir_ok_rate
        else:
            return (f"| {label} | {n} | {range_hit_rate:.0f}%({rh}/{n}) "
                    f"| — | {avg_dev_abs:.0f}元/吨 | — |"), avg_dev_abs, 0.0

    day_stat, day_dev, day_dir = stats_table_v4(day_rows, "🌞 日盘")
    night_stat, night_dev, night_dir = stats_table_v4(night_rows, "🌙 夜盘")
    st_stat, _, _ = stats_table_v4(st_rows, "短期(1-4周)")
    mt_stat, _, _ = stats_table_v4(mt_rows, "中期(1-3月)")

    # 合并第二日统计（日盘+夜盘）
    all_d2_rows = day_rows + night_rows
    n_all = len(all_d2_rows)
    if n_all > 0:
        combined_dev = statistics.mean([abs(r.get("dev_abs", r.get("pred", 2300) * r["dev"] / 100)) for r in all_d2_rows])
        combined_rh = sum(1 for r in all_d2_rows if r["range_hit"]) / n_all * 100
        combined_dir = sum(1 for r in all_d2_rows if r["dir_ok"]) / n_all * 100
        rh_all = sum(1 for r in all_d2_rows if r["range_hit"])
        d2_combined = (f"| 📊 第二日合计 | {n_all} | {combined_rh:.0f}%({rh_all}/{n_all}) "
                       f"| {combined_dir:.0f}% | {combined_dev:.0f}元/吨 | — |")
    else:
        d2_combined = "| 📊 第二日合计 | 0 | — | — | — | — |"

    stats = f"""## 准确率统计（自动更新）

| 周期 | 样本 | 区间命中率 | 方向准确率 | 平均偏差 | 高价/低价命中率 |
|------|------|----------|----------|---------|-------------|
{day_stat}
{night_stat}
{d2_combined}
{st_stat}
{mt_stat}

- 更新于: {datetime.now().strftime("%Y-%m-%d %H:%M")}
"""

    if "## 准确率统计" in content:
        content = re.sub(r"## 准确率统计.*?(?=\n---\n|$)", stats, content, flags=re.DOTALL)
    else:
        header_end = content.find("\n---\n") + 4
        content = content[:header_end] + "\n" + stats + content[header_end:]

    # ── 交易视角统计（v5.1升级：改为真实sim_trade模拟交易） ──
    # 从 sim_trade.md 的交易记录表读取真实模拟交易数据
    sim_trade_path = ROOT / "sim_trade.md"
    trades = []
    if sim_trade_path.exists():
        st_content = sim_trade_path.read_text()
        for line in st_content.split('\n'):
            if not line.startswith('| '):
                continue
            cols = [c.strip() for c in line.split('|')]
            cols = [c for c in cols if c]
            if len(cols) >= 9 and cols[0].isdigit():
                try:
                    pnl = float(cols[7].replace(',', ''))
                    trades.append(pnl)
                except (ValueError, IndexError):
                    continue

    sim_msg = "暂无交易（信号方向未触发开仓条件）"
    if len(trades) >= 1:
        total_trades = len(trades)
        wins_list = [p for p in trades if p > 0]
        losses_list = [p for p in trades if p < 0]
        wins = len(wins_list)
        losses = len(losses_list)
        wr = wins / total_trades * 100
        total_pnl = sum(trades)
        avg_win = statistics.mean(wins_list) if wins else 0
        avg_loss = abs(statistics.mean(losses_list)) if losses else 0
        profit_factor = sum(wins_list) / abs(sum(losses_list)) if losses and sum(losses_list) != 0 else float('inf')
        INITIAL_CAPITAL = 100000
        total_return_pct = total_pnl / INITIAL_CAPITAL * 100
        sim_msg = f"{total_trades}次交易 | 胜率{wr:.0f}% | 累计{total_pnl:+.0f}元" + \
                  f" ({total_return_pct:+.2f}%)"

    trade_stats = f"""
### 💰 交易视角（基于sim_trade模拟交易，含手续费和滑点）

**{sim_msg}**
"""
    if len(trades) >= 1:
        total_trades = len(trades)
        wins_list = [p for p in trades if p > 0]
        losses_list = [p for p in trades if p < 0]
        wins = len(wins_list)
        losses = len(losses_list)
        wr = wins / total_trades * 100
        total_pnl = sum(trades)
        avg_win = statistics.mean(wins_list) if wins else 0
        avg_loss = abs(statistics.mean(losses_list)) if losses else 0
        profit_factor = sum(wins_list) / abs(sum(losses_list)) if losses and sum(losses_list) != 0 else float('inf')
        INITIAL_CAPITAL = 100000
        total_return_pct = total_pnl / INITIAL_CAPITAL * 100
        trade_stats += f"""
| 指标 | 值 |
|------|-----|
| 总交易次数 | {total_trades} |
| 胜率 | {wr:.1f}% ({wins}/{total_trades}) |
| 累计盈亏 | {total_pnl:+.0f} 元 ({total_return_pct:+.2f}%) |
| 平均盈利 | +{avg_win:.0f} 元 |
| 平均亏损 | -{avg_loss:.0f} 元 |
| 盈亏比 | {profit_factor:.2f} |
"""
    if "### 💰 交易视角" in content:
        content = re.sub(r"### 💰 交易视角.*?(?=\n---\n|$)", trade_stats.strip(), content, flags=re.DOTALL)
    else:
        content += "\n" + trade_stats

    PREDICTIONS_FILE.write_text(content)

# ─────────────────────────────────────────
# 后验权重校准（v3.0 新增）
# ─────────────────────────────────────────
def run_calibration():
    """
    基于已验证数据，生成权重校准报告
    分析哪些指标对预测方向有贡献、哪些在引入误差
    每积累10个第二日样本后触发
    """
    if not PREDICTIONS_FILE.exists():
        return

    state = load_state()
    n_d2 = state.get("_d2_sample_count", 0)

    content = PREDICTIONS_FILE.read_text()

    # 先更新统计表（每次校准都刷新）
    update_stats()

    # 提取所有已验证的第二日行，按日盘/夜盘分离
    def parse_calib_rows():
        day_rows, night_rows = [], []
        for line in content.split('\n'):
            if '~' in line:
                continue  # 区间格式（短中长期）跳过
            if re.search(r'\| 🌞 日盘 \|', line):
                if not re.search(r'\(\d{4}-\d{2}-\d{2}\)', line):
                    continue
                cols = [c.strip() for c in line.split('|')]
                cols = [c for c in cols if c]
                if len(cols) < 12:
                    continue
                try:
                    pred = float(cols[1])
                    actual_m = re.search(r'([\d.]+)', cols[2])
                    if not actual_m: continue
                    actual = float(actual_m.group(1))
                    dev_m = re.search(r'([+-]?[\d.]+)%', cols[3])
                    if not dev_m: continue
                    dev = float(dev_m.group(1))
                    range_hit = '✅' in cols[4]
                    dir_ok = '✅' in cols[5]
                    hit_h = '✅' in cols[10]
                    hit_l = '✅' in cols[11]
                    day_rows.append({'pred': pred, 'actual': actual, 'dev': dev,
                                     'range_hit': range_hit, 'dir_ok': dir_ok,
                                     'hit_h': hit_h, 'hit_l': hit_l})
                except: pass
            elif re.search(r'\| 🌙 夜盘 \|', line):
                if not re.search(r'\(\d{4}-\d{2}-\d{2}\)', line):
                    continue
                cols = [c.strip() for c in line.split('|')]
                cols = [c for c in cols if c]
                if len(cols) < 12:
                    continue
                try:
                    pred = float(cols[1])
                    actual_m = re.search(r'([\d.]+)', cols[2])
                    if not actual_m: continue
                    actual = float(actual_m.group(1))
                    dev_m = re.search(r'([+-]?[\d.]+)%', cols[3])
                    if not dev_m: continue
                    dev = float(dev_m.group(1))
                    range_hit = '✅' in cols[4]
                    dir_ok = '✅' in cols[5]
                    night_rows.append({'pred': pred, 'actual': actual, 'dev': dev,
                                       'range_hit': range_hit, 'dir_ok': dir_ok,
                                       'hit_h': hit_h, 'hit_l': hit_l})
                except: pass
        return day_rows, night_rows

    day_rows_cal, night_rows_cal = parse_calib_rows()
    verified_rows = day_rows_cal + night_rows_cal

    n_verified = len(verified_rows)
    if n_verified < 5:
        return  # 样本不足不校准

    # 更新样本计数
    state["_d2_sample_count"] = n_verified
    save_state(state)

    def calc_stats(rows):
        """计算一组验证行的统计指标"""
        if not rows:
            return None
        devs = [r['dev'] for r in rows]
        dir_oks = [r['dir_ok'] for r in rows]
        range_hits = [r['range_hit'] for r in rows]
        hit_h_list = [r.get('hit_h', False) for r in rows]
        hit_l_list = [r.get('hit_l', False) for r in rows]
        n = len(rows)
        avg_dev = statistics.mean([abs(d) for d in devs])
        dir_rate = sum(dir_oks) / n * 100
        range_rate = sum(range_hits) / n * 100
        hit_h_rate = sum(hit_h_list) / n * 100
        hit_l_rate = sum(hit_l_list) / n * 100
        pos_devs = sum(1 for d in devs if d > 0)
        neg_devs = sum(1 for d in devs if d < 0)
        return {
            'n': n, 'avg_dev': avg_dev, 'dir_rate': dir_rate,
            'range_rate': range_rate, 'hit_h_rate': hit_h_rate,
            'hit_l_rate': hit_l_rate,
            'pos_devs': pos_devs, 'neg_devs': neg_devs
        }

    def eval_stats(s, label):
        """生成单行评价字符串"""
        if s is None:
            return f"{label}: 暂无数据"
        parts = []
        parts.append(f"均值偏差 {'✅' if s['avg_dev'] <= 8 else '⚠️' if s['avg_dev'] <= 15 else '🔴'}{s['avg_dev']:.2f}元/吨")
        parts.append(f"方向 {'✅' if s['dir_rate'] >= 55 else '⚠️' if s['dir_rate'] >= 45 else '🔴'}{s['dir_rate']:.0f}%")
        parts.append(f"区间命中 {'✅' if s['range_rate'] >= 60 else '⚠️'}{s['range_rate']:.0f}%")
        parts.append(f"高价命中 {s['hit_h_rate']:.0f}% 低价命中 {s['hit_l_rate']:.0f}%")
        bias = '偏>实' if s['pos_devs'] > s['neg_devs'] * 2 else ('偏<实' if s['neg_devs'] > s['pos_devs'] * 2 else '无偏')
        parts.append(f"系统偏差:{bias}")
        return f"**{label}**（{s['n']}样本）: " + ' | '.join(parts)

    # 分别计算日盘/夜盘/合计统计
    day_stats = calc_stats(day_rows_cal)
    night_stats = calc_stats(night_rows_cal)
    all_stats = calc_stats(verified_rows)

    # 校准建议
    suggestions = []
    if all_stats:
        pos_devs = all_stats['pos_devs']
        neg_devs = all_stats['neg_devs']
        if pos_devs > neg_devs * 2:
            suggestions.append("⚠️ 系统性偏低于实际，建议基准预测上调")
        elif neg_devs > pos_devs * 2:
            suggestions.append("⚠️ 系统性偏高于实际，建议基准预测下调")
        if all_stats['range_rate'] < 40:
            suggestions.append("⚠️ 区间命中率偏低，预测区间应适当扩大")
        elif all_stats['range_rate'] > 70:
            suggestions.append("✅ 区间命中率良好")
        if all_stats['hit_h_rate'] < 40:
            suggestions.append(f"⚠️ 高价命中率偏低({all_stats['hit_h_rate']:.0f}%)")
        if all_stats['hit_l_rate'] < 40:
            suggestions.append(f"⚠️ 低价命中率偏低({all_stats['hit_l_rate']:.0f}%)")
        if all_stats['dir_rate'] < 45:
            suggestions.append("⚠️ 方向准确率偏低，建议增加MA/MACD权重")
        elif all_stats['dir_rate'] >= 55:
            suggestions.append("✅ 方向准确率良好")
        if all_stats['avg_dev'] > 15:
            suggestions.append(f"⚠️ 平均偏差较大({all_stats['avg_dev']:.1f}元/吨)")
        elif all_stats['avg_dev'] <= 8:
            suggestions.append(f"✅ 平均偏差较小({all_stats['avg_dev']:.1f}元/吨)")

    if suggestions:
        suggestions_lines = ''.join(f'{i+1}. {s}\n' for i, s in enumerate(suggestions))
    else:
        suggestions_lines = "样本不足，校准建议待积累更多数据后生成。"

    report = f"""## 权重校准报告（自动生成）

**生成时间**: {datetime.now().strftime("%Y-%m-%d %H:%M")}
**样本数**: {n_verified}个第二日预测（日盘{len(day_rows_cal)}个 + 夜盘{len(night_rows_cal)}个）

### 分项表现

| 指标 | 🌞 日盘 | 🌙 夜盘 | 📊 合计 |
|------|--------|--------|--------|
| 样本数 | {day_stats['n'] if day_stats else 0} | {night_stats['n'] if night_stats else 0} | {n_verified} |
| 平均偏差 | {day_stats['avg_dev']:.2f}元/吨{' ⚠️' if day_stats and day_stats['avg_dev'] > 8 else '' if day_stats else '—'} | {night_stats['avg_dev']:.2f}元/吨{' ⚠️' if night_stats and night_stats['avg_dev'] > 8 else '' if night_stats else '—'} | {all_stats['avg_dev']:.2f}元/吨 |
| 方向准确率 | {day_stats['dir_rate']:.0f}%{' ✅' if day_stats and day_stats['dir_rate'] >= 55 else ' ⚠️' if day_stats and day_stats['dir_rate'] >= 45 else '' if day_stats else ''} | {night_stats['dir_rate']:.0f}%{' ✅' if night_stats and night_stats['dir_rate'] >= 55 else ' ⚠️' if night_stats and night_stats['dir_rate'] >= 45 else '' if night_stats else ''} | {all_stats['dir_rate']:.0f}% |
| 区间命中率 | {day_stats['range_rate']:.0f}%{' ✅' if day_stats and day_stats['range_rate'] >= 60 else '' if day_stats else ''} | {night_stats['range_rate']:.0f}%{' ✅' if night_stats and night_stats['range_rate'] >= 60 else '' if night_stats else ''} | {all_stats['range_rate']:.0f}% |
| 高价命中率 | {day_stats['hit_h_rate']:.0f}% | {night_stats['hit_h_rate']:.0f}% | {all_stats['hit_h_rate']:.0f}% |
| 低价命中率 | {day_stats['hit_l_rate']:.0f}% | {night_stats['hit_l_rate']:.0f}% | {all_stats['hit_l_rate']:.0f}% |

### 评价摘要

- {eval_stats(day_stats, '🌞 日盘')}
- {eval_stats(night_stats, '🌙 夜盘')}
- {eval_stats(all_stats, '📊 合计')}

### 校准建议

{suggestions_lines}

### 待优化项

- [ ] 根据上方分项建议分别调整日盘/夜盘权重
- [ ] 考虑日夜盘使用不同的CBOT传导系数（日盘弱传导，夜盘强传导）
- [ ] 低价预测持续偏低，考虑扩大预测低价区间

---
"""

    # 追加到预测文件末尾
    if "## 权重校准报告" in content:
        content = re.sub(r"## 权重校准报告.*?(?=\n---\n|$)", report.strip(), content, flags=re.DOTALL)
    else:
        content = content.rstrip() + "\n\n" + report

    PREDICTIONS_FILE.write_text(content)
    print(f"[Tracker] 校准报告已更新（{n_verified}样本: 日{len(day_rows_cal)} + 夜{len(night_rows_cal)}）")

# ─────────────────────────────────────────
# 主程序
# ─────────────────────────────────────────
def main():
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--verify-only":
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 验证模式：检查待填记录...")
        verify_pending()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--calibrate":
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 校准模式...")
        run_calibration()
        return

    # 优先从文件读取分析结果（cron job传入）
    output = None
    if len(sys.argv) > 1:
        output_file = sys.argv[1]
        try:
            with open(output_file, "r", encoding="utf-8", errors="replace") as f:
                output = f.read()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 从文件读取分析结果: {len(output)} 字符")
        except Exception as e:
            print(f"[Tracker] 读取分析结果文件失败: {e}")

    if not output:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 无分析结果输入，仅执行验证...")
        verify_pending()
        return

    data = parse_forecast(output)
    if not data:
        print("[Tracker] 解析失败，输出末尾：")
        print(output[-1000:] if len(output) > 1000 else output)
        verify_pending()
        return

    record_prediction(data)
    verify_pending()
    print("[Tracker] Done!")

if __name__ == "__main__":
    main()
