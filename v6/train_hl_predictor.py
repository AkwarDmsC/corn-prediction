#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v6 高低价模型训练脚本。

用法:
  python3 v6/train_hl_predictor.py
"""
from __future__ import annotations

import json
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

from config import HISTORY_DIR, HL_FEATURE_COLS, MODEL_HIGH, MODEL_LOW, MODELS_DIR
from data import fetch_dce_corn
from predictor import compute_hl_features_v6

TRAINING_SUMMARY = HISTORY_DIR / "model_training_summary.json"
FALLBACK_DATA = Path(__file__).resolve().parent.parent / "dce_data_full.json"
RANDOM_STATE = 42


def load_training_data() -> pd.DataFrame:
    """Load DCE corn data through v6/data.py, falling back to the local archive if offline."""
    df, meta = fetch_dce_corn(days=6000)
    if not df.empty:
        print(f"  v6/data.py: {len(df)} rows from {meta.get('source')}")
        return df

    if not FALLBACK_DATA.exists():
        raise RuntimeError(f"v6/data.py returned no rows and fallback is missing: {FALLBACK_DATA}")

    raw = json.loads(FALLBACK_DATA.read_text(encoding="utf-8"))
    df = pd.DataFrame(raw["corn"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    print(f"  v6/data.py returned no rows; using local archive: {len(df)} rows")
    return df[["date", "open", "high", "low", "close", "volume"]]


def add_targets(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["next_high"] = out["high"].shift(-1)
    out["next_low"] = out["low"].shift(-1)
    return out


def prepare_xy(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    feat_df = df[HL_FEATURE_COLS].copy().ffill().fillna(0)
    y_high = df["next_high"].to_numpy(dtype=float)
    y_low = df["next_low"].to_numpy(dtype=float)
    mask = ~(np.isnan(y_high) | np.isnan(y_low))
    return feat_df.to_numpy(dtype=float)[mask], y_high[mask], y_low[mask]


def train_and_eval(X: np.ndarray, y_high: np.ndarray, y_low: np.ndarray, train_ratio: float = 0.8):
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

    n = len(y_high)
    split = int(n * train_ratio)
    X_train, X_test = X[:split], X[split:]
    y_h_train, y_h_test = y_high[:split], y_high[split:]
    y_l_train, y_l_test = y_low[:split], y_low[split:]

    params = {
        "n_estimators": 200,
        "max_depth": 4,
        "learning_rate": 0.05,
        "min_samples_leaf": 10,
        "random_state": RANDOM_STATE,
    }
    model_h = GradientBoostingRegressor(**params).fit(X_train, y_h_train)
    model_l = GradientBoostingRegressor(**params).fit(X_train, y_l_train)
    pred_h = model_h.predict(X_test)
    pred_l = model_l.predict(X_test)
    n_test = len(y_h_test)
    range_hits = int(np.sum((pred_h >= y_h_test) & (pred_l <= y_l_test)))

    metrics = {
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "model_type": "GradientBoostingRegressor",
        "params": params,
        "feature_cols": HL_FEATURE_COLS,
        "total_samples": int(n),
        "train_samples": int(split),
        "test_samples": int(n_test),
        "high_mae": round(float(mean_absolute_error(y_h_test, pred_h)), 2),
        "low_mae": round(float(mean_absolute_error(y_l_test, pred_l)), 2),
        "high_rmse": round(float(np.sqrt(mean_squared_error(y_h_test, pred_h))), 2),
        "low_rmse": round(float(np.sqrt(mean_squared_error(y_l_test, pred_l))), 2),
        "high_r2": round(float(r2_score(y_h_test, pred_h)), 4),
        "low_r2": round(float(r2_score(y_l_test, pred_l)), 4),
        "range_hit_pct": round(range_hits / n_test * 100, 1) if n_test else 0.0,
    }
    return model_h, model_l, metrics


def save_summary(section: str, data: Dict[str, Any]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    if TRAINING_SUMMARY.exists():
        summary = json.loads(TRAINING_SUMMARY.read_text(encoding="utf-8"))
    else:
        summary = {}
    summary[section] = data
    TRAINING_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    print("=== v6 DCE玉米 高低价预测模型训练 ===")
    print(f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"输出: {MODEL_HIGH} / {MODEL_LOW}")

    df = load_training_data()
    print(f"  数据区间: {df['date'].min().date()} ~ {df['date'].max().date()}")
    feat_df = add_targets(compute_hl_features_v6(df))
    X, y_high, y_low = prepare_xy(feat_df)
    print(f"  有效样本: {len(X)}, 特征维度: {len(HL_FEATURE_COLS)}")

    model_h, model_l, metrics = train_and_eval(X, y_high, y_low)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with open(MODEL_HIGH, "wb") as f:
        pickle.dump(model_h, f)
    with open(MODEL_LOW, "wb") as f:
        pickle.dump(model_l, f)

    save_summary("hl", metrics)
    print(f"  high MAE={metrics['high_mae']}, low MAE={metrics['low_mae']}, range hit={metrics['range_hit_pct']}%")
    print("✅ v6 高低价模型已保存")


if __name__ == "__main__":
    main()
