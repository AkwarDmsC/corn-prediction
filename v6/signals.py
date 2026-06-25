"""
v6 信号计算层
职责：从 DataFrame 计算技术指标/信号快照。只做计算，不做预测。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import CORE_WEIGHTS, ELIMINATED_SIGNALS, VERSION


def prepare_corn_df(corn_df: pd.DataFrame) -> pd.DataFrame:
    """标准化 DCE 玉米 DataFrame：排序、类型转换、去空"""
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(corn_df.columns)
    if missing:
        raise ValueError(f"corn_df missing required columns: {sorted(missing)}")

    df = corn_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    return df


def _as_float_series(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(df[col], errors="coerce").astype(float)


def sma(values: Iterable[float], window: int) -> Optional[float]:
    """简单移动平均（最后一个窗口）"""
    arr = list(values)
    if len(arr) < window:
        return None
    return float(np.mean(arr[-window:]))


def rsi(values: Iterable[float], period: int = 14) -> Optional[float]:
    """相对强弱指标"""
    s = pd.Series(list(values), dtype=float)
    if len(s) < period + 1:
        return None
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    val = 100 - 100 / (1 + gain / (loss + 1e-9))
    return float(val.iloc[-1])


def macd_hist(values: Iterable[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """MACD 柱状图及双线值"""
    s = pd.Series(list(values), dtype=float)
    if len(s) < 35:
        return None, None, None
    ema12 = s.ewm(span=12, adjust=False).mean()
    ema26 = s.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return float(macd.iloc[-1]), float(signal.iloc[-1]), float(hist.iloc[-1])


def _bb_position(close: float, ma20: Optional[float], sd20: Optional[float]) -> float:
    """布林带位置（0~100），与训练口径一致"""
    if ma20 is None or sd20 is None or sd20 <= 0:
        return 50.0
    upper = ma20 + 2 * sd20
    lower = ma20 - 2 * sd20
    return float(np.clip((close - lower) / (upper - lower) * 100, 0, 100))


def next_trading_day(now: Optional[datetime] = None) -> datetime:
    """获取下一交易日（跳过周末）"""
    now = now or datetime.now()
    d = now + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _direction_label(score: float, trend_note: str = "") -> str:
    """方向文字标签"""
    if score > 0.05:
        return "↗ 震荡偏强" + trend_note
    if score < -0.05:
        return "↘ 震荡偏弱" + trend_note
    return "↔ 震荡整理" + trend_note


def _confidence(consistency: float, active: int, conflicts: List[str]) -> Tuple[str, str]:
    """置信度等级与百分比"""
    pct = int(round(abs(consistency) * 100))
    if conflicts:
        pct = int(pct * 0.85)
    if pct >= 70 and active >= 5:
        return "🟢高", f"{pct}%"
    if pct >= 40:
        return "🟡中", f"{pct}%"
    return "🔴低", f"{pct}%"


def compute_signal_snapshot(
    corn_df: pd.DataFrame,
    *,
    soy_df: Optional[pd.DataFrame] = None,
    cbot_chg: Optional[float] = None,
    weather_score: float = 0.0,
    policy_signal: float = 0.0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    计算完整的信号快照（结构化 dict，与 analysis_v6 兼容）。

    Args:
        corn_df: DCE 玉米日线 DataFrame（date, open, high, low, close, volume）
        soy_df: 可选，DCE 豆粕日线 DataFrame（计算共振信号）
        cbot_chg: 可选，CBOT 玉米隔夜涨跌幅（%）
        weather_score: 可选，产区天气评分（-2~+2）
        policy_signal: 可选，政策事件信号（-1~+1）
        now: 可选，用于确定下一交易日

    Returns:
        兼容 dict，包含 indicators / signals / effective_signals / 置信度等
    """
    df = prepare_corn_df(corn_df)
    closes = df["close"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    vols = df["volume"].tolist()
    latest = df.iloc[-1]
    close = float(latest["close"])

    # ── 均线 ──
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    ma60_prev = sma(closes[:-20], 60) if len(closes) >= 80 else None
    ma_dir = 1 if (ma5 and ma10 and ma20 and ma5 > ma10 > ma20) else -1 if (ma5 and ma10 and ma20 and ma5 < ma10 < ma20) else 0
    ma60_trend = 0
    if ma60 and ma60_prev:
        ma60_trend = 1 if ma60 > ma60_prev * 1.002 else -1 if ma60 < ma60_prev * 0.998 else 0

    # ── RSI ──
    rsi_val = rsi(closes)
    rsi_sig = 1 if (rsi_val is not None and rsi_val < 40) else -1 if (rsi_val is not None and rsi_val > 65) else 0

    # ── 布林带 ──
    sd20 = float(pd.Series(closes).rolling(20).std().iloc[-1]) if len(closes) >= 20 else None
    bb_pos = _bb_position(close, ma20, sd20)
    bb_sig = 1 if bb_pos < 20 else -1 if bb_pos > 80 else 0

    # ── MACD ──
    _, _, hist = macd_hist(closes)
    macd_sig = 1 if (hist is not None and hist > 0) else -1 if (hist is not None and hist < 0) else 0

    # ── 成交量 ──
    vol_ma5 = float(np.mean(vols[-5:])) if len(vols) >= 5 else float(vols[-1] or 1)
    vol_ratio = float(vols[-1] / (vol_ma5 + 1e-9))
    vol_sig = 1.0 if vol_ratio > 1.2 else -0.5 if vol_ratio < 0.8 else 0.0

    # ── 大豆共振 ──
    soy_sig = 0.0
    soy_corr = None
    if soy_df is not None and len(soy_df) >= 20 and "close" in soy_df.columns:
        soy = soy_df.copy()
        soy["date"] = pd.to_datetime(soy["date"]) if "date" in soy.columns else pd.RangeIndex(len(soy))
        soy = soy.sort_values("date").reset_index(drop=True)
        soy_close = pd.to_numeric(soy["close"], errors="coerce").astype(float)
        n_align = min(len(df), len(soy_close))
        aligned = pd.DataFrame({
            "corn": df["close"].tail(n_align).to_numpy(),
            "soy": soy_close.tail(n_align).to_numpy(),
        }).dropna()
        if len(aligned) >= 20:
            soy_corr = float(aligned["corn"].tail(60).corr(aligned["soy"].tail(60)))
            soy_ma5 = float(soy_close.rolling(5).mean().iloc[-1])
            soy_ma20 = float(soy_close.rolling(20).mean().iloc[-1])
            soy_trend = 1 if soy_ma5 > soy_ma20 else -1 if soy_ma5 < soy_ma20 else 0
            soy_sig = soy_corr * soy_trend if soy_corr > 0.5 else 0.0

    # ── CBOT / 政策 / 天气 ──
    cbot_sig = 1 if (cbot_chg is not None and cbot_chg > 0) else -1 if (cbot_chg is not None and cbot_chg < 0) else 0
    weather_w = CORE_WEIGHTS["WEATHER_EXTREME"] if abs(weather_score or 0) > 0.3 else CORE_WEIGHTS["WEATHER_NORMAL"]

    # ── 原始加权信号 ──
    raw_weighted = [
        ("MA", ma_dir, CORE_WEIGHTS["MA"]),
        ("RSI", rsi_sig, CORE_WEIGHTS["RSI"]),
        ("BB", bb_sig, CORE_WEIGHTS["BB"]),
        ("SOY", soy_sig, CORE_WEIGHTS["SOY"]),
        ("POLICY", policy_signal, CORE_WEIGHTS["POLICY"] if abs(policy_signal) > 0 else 0.0),
        ("CBOT", cbot_sig, CORE_WEIGHTS["CBOT"]),
        ("MACD", macd_sig, CORE_WEIGHTS["MACD"]),
        ("VOLUME", vol_sig, CORE_WEIGHTS["VOLUME"]),
        ("WEATHER", weather_score, weather_w),
    ]

    # ── MA60 趋势过滤 ──
    filtered_weighted = []
    disabled_by_trend = []
    for name, sig, weight in raw_weighted:
        eff_sig = float(sig)
        if ma60_trend and weight and eff_sig * ma60_trend < 0:
            eff_sig = 0.0
            disabled_by_trend.append(name)
        filtered_weighted.append((name, eff_sig, weight))

    total_w = sum(abs(w) for _, _, w in raw_weighted if w)
    raw_weighted_sum = sum(float(sig) * float(w) for _, sig, w in raw_weighted)
    weighted_sum = sum(float(sig) * float(w) for _, sig, w in filtered_weighted)
    consistency = raw_weighted_sum / total_w if total_w else 0.0
    filtered_consistency = weighted_sum / total_w if total_w else 0.0

    if disabled_by_trend:
        trend_filter = "counter_trend_blocked:" + ",".join(disabled_by_trend)
    elif ma60_trend:
        trend_filter = "aligned_or_neutral"
    else:
        trend_filter = "none"

    # ── 冲突检测 ──
    conflicts = []
    if ma_dir > 0 and rsi_val is not None and rsi_val > 65 and vol_ratio < 0.8:
        conflicts.append("均线多头+RSI超买+缩量")
    if ma_dir < 0 and rsi_val is not None and rsi_val < 35 and vol_ratio < 0.8:
        conflicts.append("均线空头+RSI超卖+缩量")
    if ma_dir and bb_sig and ma_dir * bb_sig < 0:
        conflicts.append("趋势与布林超买超卖反向")

    active = sum(1 for _, sig, w in filtered_weighted if w and abs(float(sig)) > 0.05)
    confidence, confidence_pct = _confidence(filtered_consistency, active, conflicts)

    # ── 布林上下轨（给预测用） ──
    upper = ma20 + 2 * sd20 if ma20 is not None and sd20 is not None else None
    lower = ma20 - 2 * sd20 if ma20 is not None and sd20 is not None else None
    recent_vol = float(pd.Series(closes).tail(20).std()) if len(closes) >= 20 else 20.0

    return {
        "version": VERSION,
        "date": str(latest["date"].date()),
        "close": close,
        "change": close - float(df.iloc[-2]["close"]) if len(df) >= 2 else 0.0,
        "indicators": {
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
            "ma_dir": ma_dir, "ma60_trend": ma60_trend,
            "rsi14": rsi_val, "bb_position": bb_pos,
            "bb_upper": upper, "bb_lower": lower,
            "macd_hist": hist, "vol_ratio": vol_ratio,
            "recent_vol": recent_vol, "soy_corr": soy_corr,
        },
        "signals": {name: {"value": sig, "weight": w} for name, sig, w in raw_weighted},
        "effective_signals": {name: {"value": sig, "weight": w} for name, sig, w in filtered_weighted},
        "disabled_by_trend": disabled_by_trend,
        "raw_weighted_sum": raw_weighted_sum,
        "weighted_sum": weighted_sum,
        "consistency": consistency,
        "filtered_consistency": filtered_consistency,
        "trend_filter": trend_filter,
        "confidence": confidence,
        "confidence_pct": confidence_pct,
        "conflicts": conflicts,
        "eliminated_signals": ELIMINATED_SIGNALS,
        "next_trading_day": next_trading_day(now).strftime("%Y-%m-%d"),
    }


__all__ = [
    "prepare_corn_df", "sma", "rsi", "macd_hist",
    "compute_signal_snapshot", "next_trading_day",
    "_direction_label", "_confidence",
]
