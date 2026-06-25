#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v6 夜盘模型训练脚本。

用法:
  python3 v6/train_night_predictor.py
  python3 v6/train_night_predictor.py --base
"""
from __future__ import annotations

import json
import pickle
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from config import HISTORY_DIR, MODEL_NIGHT, MODELS_DIR, NIGHT_BASE_COLS, NIGHT_EXT_COLS
from data import fetch_dce_corn
from predictor import compute_night_features_v6

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


def prepare_target(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["night_change_proxy"] = out["open"].shift(-1) - out["close"]
    return out


def prepare_xy(df: pd.DataFrame, feature_cols: List[str]) -> Tuple[np.ndarray, np.ndarray]:
    X_raw = df[feature_cols].copy().ffill().fillna(0).to_numpy(dtype=float)
    y_raw = df["night_change_proxy"].to_numpy(dtype=float)
    mask = ~(np.isnan(X_raw).any(axis=1) | np.isnan(y_raw))
    return X_raw[mask], y_raw[mask]


def train_model(X: np.ndarray, y: np.ndarray, test_ratio: float = 0.15) -> Dict[str, Any]:
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.linear_model import Ridge
    from sklearn.metrics import mean_absolute_error, r2_score
    from sklearn.preprocessing import StandardScaler

    split = int(len(y) * (1 - test_ratio))
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    ridge = Ridge(alpha=1.0).fit(X_train_scaled, y_train)
    rf = RandomForestRegressor(
        n_estimators=200,
        max_depth=6,
        min_samples_leaf=5,
        random_state=RANDOM_STATE,
        n_jobs=-1,
    ).fit(X_train_scaled, y_train)

    ridge_pred = ridge.predict(X_test_scaled)
    rf_pred = rf.predict(X_test_scaled)
    ens_pred = 0.4 * ridge_pred + 0.6 * rf_pred
    sign_y = np.sign(y_test)
    metrics = {
        "samples": int(len(y)),
        "train_samples": int(len(y_train)),
        "test_samples": int(len(y_test)),
        "ridge_mae": float(mean_absolute_error(y_test, ridge_pred)),
        "rf_mae": float(mean_absolute_error(y_test, rf_pred)),
        "ens_mae": float(mean_absolute_error(y_test, ens_pred)),
        "ridge_r2": float(r2_score(y_test, ridge_pred)),
        "rf_r2": float(r2_score(y_test, rf_pred)),
        "ridge_dir_acc": float(np.mean(np.sign(ridge_pred) == sign_y)),
        "rf_dir_acc": float(np.mean(np.sign(rf_pred) == sign_y)),
    }
    return {"ridge": ridge, "rf": rf, "scaler": scaler, "metrics": metrics}


def save_summary(section: str, data: Dict[str, Any]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    if TRAINING_SUMMARY.exists():
        summary = json.loads(TRAINING_SUMMARY.read_text(encoding="utf-8"))
    else:
        summary = {}
    summary[section] = data
    TRAINING_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    use_extended = "--base" not in sys.argv
    feature_cols = NIGHT_EXT_COLS if use_extended else NIGHT_BASE_COLS

    print("=== v6 DCE玉米 夜盘预测模型训练 ===")
    print(f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"输出: {MODEL_NIGHT}")
    print(f"特征: {'extended' if use_extended else 'base'} ({len(feature_cols)}维)")

    df = load_training_data()
    print(f"  数据区间: {df['date'].min().date()} ~ {df['date'].max().date()}")
    feat_df = prepare_target(compute_night_features_v6(df))
    X, y = prepare_xy(feat_df, feature_cols)
    print(f"  有效样本: {len(X)}, 特征维度: {len(feature_cols)}")

    result = train_model(X, y)
    metrics = result["metrics"]
    trained_at = datetime.now().isoformat(timespec="seconds")
    model_data = {
        "ridge": result["ridge"],
        "rf": result["rf"],
        "scaler": result["scaler"],
        "ensemble_weights": {"ridge_w": 0.4, "rf_w": 0.6},
        "calibration": {"ridge_direction_only": True, "coeffs": [0.0, 1.0]},
        "feature_cols": feature_cols,
        "training_meta": {
            "samples": metrics["samples"],
            "features": len(feature_cols),
            "real_night_samples": 0,
            "extended": use_extended,
            "direction_split": False,
            "trained_at": trained_at,
            "ridge_mae": metrics["ridge_mae"],
            "ridge_dir_acc": metrics["ridge_dir_acc"],
            "rf_dir_acc": metrics["rf_dir_acc"],
        },
    }

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with open(MODEL_NIGHT, "wb") as f:
        pickle.dump(model_data, f)

    save_summary("night", {**model_data["training_meta"], "metrics": metrics, "feature_cols": feature_cols})
    print(f"  Ridge MAE={metrics['ridge_mae']:.2f}, RF MAE={metrics['rf_mae']:.2f}, Ensemble MAE={metrics['ens_mae']:.2f}")
    print(f"  Ridge方向准确率={metrics['ridge_dir_acc']*100:.1f}%, RF方向准确率={metrics['rf_dir_acc']*100:.1f}%")
    print("✅ v6 夜盘模型已保存")


if __name__ == "__main__":
    main()
