#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高低价预测模块 v1.0
基于GradientBoostingRegressor预测次日高/低价
模型: model_high.pkl / model_low.pkl
特征(23维): open, high, low, close, volume, ma5/10/20/60, RSI14, MACD, ATR14, bb_position等

使用:
  from predict_hl import predict_hl
  pred_high, pred_low, meta, latest_row = predict_hl(corn_df)
"""

import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from constants import MODEL_HIGH, MODEL_LOW

MODEL_DIR = Path(__file__).parent

FEATURE_COLS = [
    "open", "high", "low", "close", "volume",
    "ma5", "ma10", "ma20", "ma60",
    "price_vs_ma5", "price_vs_ma20",
    "bb_position", "rsi14", "atr14",
    "macd_hist", "macd_signal",
    "vol_ratio", "daily_range", "daily_range_pct",
    "chg_pct", "open_close_spread", "range_momentum",
    "month",
]

# ─────────────────────────────────────────
# 模型加载
# ─────────────────────────────────────────
_model_h = None
_model_l = None

def _load_models():
    global _model_h, _model_l
    if _model_h is None:
        with open(MODEL_HIGH, "rb") as f:
            _model_h = pickle.load(f)
        with open(MODEL_LOW, "rb") as f:
            _model_l = pickle.load(f)
    return _model_h, _model_l

# ─────────────────────────────────────────
# 特征工程（与train_hl_predictor.py完全一致）
# ─────────────────────────────────────────
def compute_features(df):
    """
    从DCE日线DataFrame计算23维特征
    df: akshare futures_zh_daily_sina(symbol="C0") 的排序结果（date升序）
    """
    df = df.copy().sort_values("date").reset_index(drop=True)
    close = df["close"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    open_ = df["open"].values.astype(float)
    volume = df["volume"].values.astype(float)

    # 移动平均
    for w in [5, 10, 20, 60]:
        df[f"ma{w}"] = pd.Series(close).rolling(w).mean()

    # 均线相对位置
    df["price_vs_ma5"] = (close - df["ma5"].values) / (df["ma5"].values + 1e-9)
    df["price_vs_ma20"] = (close - df["ma20"].values) / (df["ma20"].values + 1e-9)

    # 布林带
    bb_std20 = pd.Series(close).rolling(20).std()
    df["bb_upper"] = df["ma20"] + 2 * bb_std20
    df["bb_lower"] = df["ma20"] - 2 * bb_std20
    bb_range = df["bb_upper"].values - df["bb_lower"].values + 1e-9
    df["bb_position"] = np.clip((close - df["bb_lower"].values) / bb_range, 0, 1) * 100

    # RSI(14)
    delta = pd.Series(close).diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - (100 / (1 + gain / (loss + 1e-9)))

    # ATR(14)
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close),
                               np.abs(low - prev_close)))
    df["atr14"] = pd.Series(tr).rolling(14).mean()

    # MACD
    ema12 = pd.Series(close).ewm(span=12, adjust=False).mean()
    ema26 = pd.Series(close).ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    df["macd_hist"] = macd - signal
    df["macd_signal"] = signal

    # 成交量比率
    vol_ma5 = pd.Series(volume).rolling(5).mean()
    df["vol_ratio"] = volume / (vol_ma5.values + 1)

    # 当日波动
    df["daily_range"] = high - low
    df["daily_range_pct"] = df["daily_range"] / close * 100

    # 价格变化率
    df["chg_pct"] = df["close"].pct_change() * 100

    # 开收价差（必须与train_hl_predictor.py一致：绝对值 open - close）
    df["open_close_spread"] = open_ - close

    # 区间动量（波幅变化）
    df["range_momentum"] = df["daily_range"].diff()

    # 月份
    df["month"] = pd.to_datetime(df["date"]).dt.month

    return df

# ─────────────────────────────────────────
# 主预测函数
# ─────────────────────────────────────────
def predict_hl(corn_df=None):
    """
    预测次日高低价

    参数:
      corn_df: akshare DCE日线DataFrame（已按date升序）

    返回:
      pred_high: 预测最高价（元/吨）
      pred_low:  预测最低价（元/吨）
      meta:      附加信息字典
      latest_row: 最新一行的DataFrame（含所有特征）
    """
    if corn_df is None:
        import akshare as ak
        corn_df = ak.futures_zh_daily_sina(symbol="C0")
        corn_df["date"] = corn_df["date"].astype(str)

    model_h, model_l = _load_models()
    feat_df = compute_features(corn_df)

    # 取最新一行
    latest = feat_df.iloc[-1:].copy()

    # 提取特征（处理缺失值）
    X = latest[FEATURE_COLS].fillna(0).values

    # 预测
    pred_h = float(model_h.predict(X)[0])
    pred_l = float(model_l.predict(X)[0])

    # 确保高价>=低价
    if pred_l > pred_h:
        mid = (pred_h + pred_l) / 2
        pred_h = mid + 1
        pred_l = mid - 1

    # 附加信息
    latest_close = float(latest["close"].values[0])
    meta = {
        "date": str(latest["date"].values[0]),
        "close": latest_close,
        "high": float(latest["high"].values[0]),
        "low": float(latest["low"].values[0]),
        "bb_position": float(latest["bb_position"].values[0]),
        "rsi14": float(latest["rsi14"].values[0]),
        "atr14": float(latest["atr14"].values[0]),
        "vol_ratio": float(latest["vol_ratio"].values[0]),
        "daily_range": float(latest["daily_range"].values[0]),
        "range_pct": float(latest["daily_range_pct"].values[0]),
    }

    return pred_h, pred_l, meta, latest


# ─────────────────────────────────────────
# CLI 测试
# ─────────────────────────────────────────
if __name__ == "__main__":
    import akshare as ak

    corn_df = ak.futures_zh_daily_sina(symbol="C0")
    corn_df["date"] = corn_df["date"].astype(str)

    pred_h, pred_l, meta, latest = predict_hl(corn_df)

    print("=" * 50)
    print("高低价预测 — CLI测试")
    print("=" * 50)
    print(f"日期: {meta['date']}")
    print(f"最新收盘: {meta['close']:.0f}  (高:{meta['high']:.0f} 低:{meta['low']:.0f})")
    print(f"预测次高: {pred_h:.0f}  预测次低: {pred_l:.0f}")
    print(f"预测区间: {pred_l:.0f} ~ {pred_h:.0f}  (波幅:{pred_h-pred_l:.0f}元)")
    print(f"BB位置: {meta['bb_position']:.0f}%  RSI14: {meta['rsi14']:.0f}  ATR14: {meta['atr14']:.0f}")
    print(f"成交量比: {meta['vol_ratio']:.2f}x  当日波幅: {meta['daily_range']:.0f}元({meta['range_pct']:.1f}%)")

