"""
v7 方向决策器

职责：基于信号快照，决定方向、区间和置信度。
核心改进（vs v5）：
1. DIRECTION_THRESHOLD 0.05 → 0.15
2. 震荡市熔断：_should_force_neutral()
3. 日盘/夜盘不同管线
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from v7.config import (
    DIRECTION_THRESHOLD,
    LOW_AGREEMENT_CUTOFF, LOW_CONSISTENCY_CUTOFF,
    NIGHT_CBOT_WEIGHT, VERSION,
)
from v7.signals import (
    prepare_corn_df, compute_signal_snapshot, _direction_label,
)


# ============================================================
# 震荡市检测
# ============================================================

def _count_agreement(signal_snapshot: dict) -> tuple:
    """从信号快照中提取同向信号数量和活跃信号总数"""
    weighted = signal_snapshot.get("effective_signals", {})
    score = signal_snapshot.get("filtered_consistency", 0.0)
    agreeing = 0
    active = 0
    for name, info in weighted.items():
        w = info.get("weight", 0)
        v = info.get("value", 0)
        if w and abs(v) > 0.05:
            active += 1
            if v * score > 0:
                agreeing += 1
    return agreeing, active


def _should_force_neutral(
    score: float,
    agreeing_count: int,
    total_active: int,
) -> bool:
    """
    判断是否应强制输出震荡/中性。
    
    规则（按优先级）：
    1. 绝对阈值：|score| < 0.05 → 震荡
    2. 信号完全分歧：agreeing == 0 且 total_active >= 2 → 震荡
    3. 低信号+低一致：agreeing < 3 且 |score| < 0.15 且 total_active >= 3 → 震荡
    4. 单信号主导：只激活了 1 个信号 → 震荡（单一信号不可靠）
    
    除了以上，都允许做方向判断。
    """
    if abs(score) < 0.03:
        return True
    if agreeing_count == 0 and total_active >= 3:
        return True
    if total_active == 1 and abs(score) < 0.10:
        return True
    if agreeing_count < LOW_AGREEMENT_CUTOFF and abs(score) < LOW_CONSISTENCY_CUTOFF and total_active >= 3:
        return True
    return False


# ============================================================
# 场景预测（单 session 调用）
# ============================================================

def _scenario_from_score(
    base_price: float, score: float, recent_vol: float,
    bb_lower: Optional[float], bb_upper: Optional[float],
    *,
    agreeing_count: int = 0,
    total_active: int = 0,
    session_label: str = "",
) -> Dict[str, Any]:
    """
    从信号一致性评分计算场景预测。
    
    v7 改进：
    - 用 _should_force_neutral 检测震荡市
    - 信号微弱 + 分歧 → 不做方向判断
    - 仅输出"震荡整理(信号分歧)" + 区间
    """
    if _should_force_neutral(score, agreeing_count, total_active):
        direction_score = 0.0
        direction_label = "↔ 震荡整理(信号分歧)"
    elif abs(score) <= DIRECTION_THRESHOLD:
        direction_score = 0.0
        direction_label = "↔ 震荡整理"
    else:
        direction_score = score
        direction_label = _direction_label(score)

    unit_move = max(recent_vol * 0.15, 1.5)
    change = direction_score * unit_move
    pred = round(base_price + change, 0)
    span = max(int(recent_vol * 4), 18)
    low = round(pred - span, 0)
    high = round(pred + span, 0)

    if bb_lower is not None:
        low = max(low, round(bb_lower, 0))
    if bb_upper is not None:
        high = min(high, round(bb_upper, 0))
        pred = min(pred, round(bb_upper, 0))

    return {
        "session": session_label,
        "base": round(base_price, 0),
        "pred": pred,
        "low": low,
        "high": high,
        "change": round(pred - base_price, 1),
        "change_pct": round((pred - base_price) / base_price * 100, 2) if base_price else 0.0,
        "direction": direction_label,
        "score": score,
        "agreeing_count": agreeing_count,
        "total_active": total_active,
    }


# ============================================================
# 夜盘 ML 预测（从 v6 移植，保持独立）
# ============================================================

def _compute_night_features_v7(corn_df: pd.DataFrame) -> pd.DataFrame:
    """夜盘模型特征构建（同 v6 compute_night_features_v6）"""
    df = prepare_corn_df(corn_df)
    close = df["close"].astype(float)
    df["price_chg"] = close - close.shift(1)
    for w in [5, 10, 20, 60]:
        df[f"ma{w}"] = close.rolling(w).mean()

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))

    ma20 = df["ma20"]
    std20 = close.rolling(20).std()
    bb_upper = ma20 + 2 * std20
    bb_lower = ma20 - 2 * std20
    df["bb_position"] = (close - bb_lower) / (bb_upper - bb_lower + 1e-9) * 100

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    df["macd_hist"] = macd - macd_signal
    df["vol_ratio"] = df["volume"] / df["volume"].rolling(20).mean()

    month_to_score = {
        1: 0.0, 2: -0.2, 3: 0.0, 4: 0.25, 5: 0.1, 6: 0.35,
        7: 0.0, 8: 0.3, 9: 0.25, 10: 0.0, 11: -0.1, 12: 0.2,
    }
    df["month"] = df["date"].dt.month
    df["seasonal"] = df["month"].map(month_to_score)

    df["price_chg_abs"] = df["price_chg"].abs()
    bb_width = bb_upper - bb_lower
    df["bb_width_ratio"] = bb_width / (close + 1e-9) * 100
    df["bb_squeeze"] = df["bb_width_ratio"].rolling(20).max() - df["bb_width_ratio"]
    df["bb_squeeze"] = df["bb_squeeze"] / df["bb_squeeze"].rolling(20).max().replace(0, float("nan"))
    df["range"] = df["high"] - df["low"]
    df["range_pct"] = df["range"] / df["open"] * 100
    df["vol_surge"] = (df["vol_ratio"] > 1.5).astype(int) - (df["vol_ratio"] < 0.6).astype(int)
    df["up"] = (df["price_chg"] > 0).astype(int)
    df["down"] = (df["price_chg"] < 0).astype(int)
    df["consecutive_up"] = df["up"].groupby((df["up"] == 0).cumsum()).cumsum()
    df["consecutive_down"] = df["down"].groupby((df["down"] == 0).cumsum()).cumsum()
    df["ma5_slope"] = df["ma5"].diff()
    df["ma20_slope"] = df["ma20"].diff()
    df["candle_body_pct"] = (df["close"] - df["open"]) / df["open"] * 100
    df["upper_shadow"] = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_shadow"] = df[["close", "open"]].min(axis=1) - df["low"]
    return df


def _predict_night_v7(
    corn_df: pd.DataFrame,
    base_price: float,
    *,
    cbot_chg: Optional[float] = None,
    cbot_coef: float = 10.0,
) -> Dict[str, Any]:
    """
    加载夜盘 ML 模型（Ridge+RF ensemble），预测夜盘变化量和方向。
    同 v6 predict_night_v6，路径指向 v7/models/。
    """
    import pickle
    from v7.config import MODEL_NIGHT, NIGHT_BASE_COLS, NIGHT_EXT_COLS

    with open(MODEL_NIGHT, "rb") as f:
        model_data = pickle.load(f)
    ridge = model_data["ridge"]
    rf = model_data["rf"]
    scaler = model_data["scaler"]
    expected = getattr(scaler, "n_features_in_", len(NIGHT_BASE_COLS))
    cols = NIGHT_EXT_COLS if expected > len(NIGHT_BASE_COLS) else NIGHT_BASE_COLS

    feat_df = _compute_night_features_v7(corn_df)
    row = feat_df.iloc[-1]
    X = np.array([[row.get(col, 0.0) for col in cols]], dtype=float)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    X_scaled = scaler.transform(X)
    ridge_pred = float(ridge.predict(X_scaled)[0])
    rf_pred = float(rf.predict(X_scaled)[0])
    ensemble_pred = 0.4 * ridge_pred + 0.6 * rf_pred

    cal = model_data.get("calibration", {})
    if cal.get("ridge_direction_only"):
        coeffs = cal.get("coeffs", [0, 1])
        mag = max(0.0, float(coeffs[0]) + float(coeffs[1]) * abs(ensemble_pred))
        ml_change = float(np.sign(ridge_pred) * mag) if abs(ridge_pred) > 0.3 else ensemble_pred * 0.8
    else:
        ml_change = ensemble_pred

    cbot_adj = (cbot_chg or 0.0) * cbot_coef
    total_change = ml_change + cbot_adj
    pred = round(base_price + total_change, 0)
    span = max(abs(ml_change) * 2, 8.0)
    return {
        "session": "night_ml",
        "base": round(base_price, 0),
        "pred": pred,
        "low": round(pred - span, 0),
        "high": round(pred + span, 0),
        "change": round(total_change, 1),
        "change_pct": round(total_change / base_price * 100, 2) if base_price else 0.0,
        "direction": "↗ 偏强" if total_change > 2 else "↘ 偏弱" if total_change < -2 else "↔ 震荡",
        "confidence": "🟢高" if abs(ridge_pred) >= 12.5 else "🟡中" if abs(ridge_pred) >= 5 else "🔴低",
        "ridge_pred": round(ridge_pred, 2),
        "rf_pred": round(rf_pred, 2),
        "ensemble_pred": round(ensemble_pred, 2),
        "ml_change": round(ml_change, 1),
        "cbot_adj": round(cbot_adj, 1),
    }


# ============================================================
# HL 预测
# ============================================================

def _predict_hl_v7(corn_df: pd.DataFrame) -> Dict[str, Any]:
    """加载 HL 模型，预测次日高价/低价。同 v6 predict_hl_v6。"""
    import pickle
    from v7.config import MODEL_HIGH, MODEL_LOW, HL_FEATURE_COLS

    # 特征构建（同 v6 compute_hl_features_v6）
    df = prepare_corn_df(corn_df)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)
    volume = df["volume"].astype(float)

    for w in [5, 10, 20, 60]:
        df[f"ma{w}"] = close.rolling(w).mean()
    df["price_vs_ma5"] = (close - df["ma5"]) / (df["ma5"] + 1e-9)
    df["price_vs_ma20"] = (close - df["ma20"]) / (df["ma20"] + 1e-9)

    bb_std20 = close.rolling(20).std()
    df["bb_upper"] = df["ma20"] + 2 * bb_std20
    df["bb_lower"] = df["ma20"] - 2 * bb_std20
    bb_range = df["bb_upper"] - df["bb_lower"] + 1e-9
    df["bb_position"] = np.clip((close - df["bb_lower"]) / bb_range, 0, 1) * 100

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss_num = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - 100 / (1 + gain / (loss_num + 1e-9))

    prev_close = close.shift(1).fillna(close)
    tr = np.maximum(
        high - low,
        np.maximum((high - prev_close).abs(), (low - prev_close).abs()),
    )
    df["atr14"] = pd.Series(tr).rolling(14).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()
    df["macd_hist"] = macd - sig
    df["macd_signal"] = sig

    vol_ma5 = volume.rolling(5).mean()
    df["vol_ratio"] = volume / (vol_ma5 + 1)
    df["daily_range"] = high - low
    df["daily_range_pct"] = df["daily_range"] / close * 100
    df["chg_pct"] = close.pct_change() * 100
    df["open_close_spread"] = open_ - close
    df["range_momentum"] = df["daily_range"].rolling(5).mean()
    df["month"] = df["date"].dt.month

    latest = df.iloc[-1:]
    X = latest[HL_FEATURE_COLS].ffill().fillna(0).values

    with open(MODEL_HIGH, "rb") as f:
        mh = pickle.load(f)
    with open(MODEL_LOW, "rb") as f:
        ml = pickle.load(f)
    pred_high = float(mh.predict(X)[0])
    pred_low = float(ml.predict(X)[0])
    if pred_low > pred_high:
        mid = (pred_high + pred_low) / 2
        pred_high, pred_low = mid + 1, mid - 1
    return {
        "pred_high": round(pred_high, 0),
        "pred_low": round(pred_low, 0),
        "range": round(pred_high - pred_low, 0),
    }


# ============================================================
# 主入口：analyze_corn_v7
# ============================================================

def analyze_corn_v7(
    corn_df: pd.DataFrame,
    *,
    soy_df: Optional[pd.DataFrame] = None,
    cbot_chg: Optional[float] = None,
    weather_score: float = 0.0,
    policy_signal: float = 0.0,
    day_session: Optional[Dict[str, float]] = None,
    night_session: Optional[Dict[str, float]] = None,
    now: Optional[datetime] = None,
    run_ml: bool = True,
) -> Dict[str, Any]:
    """
    综合预测入口：信号计算 → 日盘决策 → 夜盘决策 → 汇总。

    v7 改进：
    - 方向阈值 0.05 → 0.15
    - 震荡市熔断（信号分歧时强制中性）
    - 日盘/夜盘不同管线
    - 夜盘 CBOT 权重提升
    """
    df = prepare_corn_df(corn_df)
    signal = compute_signal_snapshot(
        df, soy_df=soy_df, cbot_chg=cbot_chg,
        weather_score=weather_score, policy_signal=policy_signal, now=now,
    )
    ind = signal["indicators"]
    latest_close = float(df.iloc[-1]["close"])
    day_base = float(day_session["close"]) if day_session and "close" in day_session else latest_close
    night_base = float(night_session["close"]) if night_session and "close" in night_session else day_base

    agreeing_count, total_active = _count_agreement(signal)

    # ── 日盘决策 ──
    day = _scenario_from_score(
        day_base, signal["filtered_consistency"], ind["recent_vol"],
        ind["bb_lower"], ind["bb_upper"],
        agreeing_count=agreeing_count,
        total_active=total_active,
        session_label="day",
    )
    # 均线强制方向覆盖（仅当已做方向判断时）
    if "震荡整理" not in day["direction"]:
        if ind.get("ma_dir", 0) < 0 and "偏弱" not in day["direction"]:
            day["direction"] = "↘ 震荡偏弱(均线空头)"
        elif ind.get("ma_dir", 0) > 0 and "偏强" not in day["direction"] and signal["filtered_consistency"] >= 0:
            day["direction"] = "↗ 震荡偏强(均线多头)"

    # ── 夜盘决策（v7: CBOT 权重提升） ──
    night_score = signal["filtered_consistency"]
    if cbot_chg is not None and abs(cbot_chg) > 0.1:
        cbot_boost = (1 if cbot_chg > 0 else -1) * (NIGHT_CBOT_WEIGHT - 0.4)
        if abs(night_score) < abs(cbot_boost) * 0.5:
            night_score += cbot_boost * 0.2

    night = _scenario_from_score(
        night_base, night_score, ind["recent_vol"],
        ind["bb_lower"], ind["bb_upper"],
        agreeing_count=agreeing_count,
        total_active=total_active,
        session_label="night",
    )

    hl = None
    model_errors = {}
    if run_ml:
        try:
            night = _predict_night_v7(df, night_base, cbot_chg=cbot_chg)
        except Exception as exc:
            model_errors["night"] = str(exc)
        try:
            hl = _predict_hl_v7(df)
        except Exception as exc:
            model_errors["hl"] = str(exc)

    full_low = min(day["low"], night["low"])
    full_high = max(day["high"], night["high"])

    return {
        "version": VERSION,
        "generated_at": (now or datetime.now()).strftime("%Y-%m-%d %H:%M"),
        "input_date": signal["date"],
        "next_trading_day": signal["next_trading_day"],
        "signal": signal,
        "day": day,
        "night": night,
        "hl": hl,
        "full_day_range": {"low": full_low, "high": full_high},
        "model_errors": model_errors,
        "agreeing_count": agreeing_count,
        "total_active": total_active,
    }


__all__ = [
    "_count_agreement", "_should_force_neutral",
    "_scenario_from_score",
    "_predict_night_v7", "_predict_hl_v7",
    "analyze_corn_v7",
]
