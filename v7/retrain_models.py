#!/usr/bin/env python3
"""
重训 v7 ML 模型（HL + Night）
使用截至 2026-06-24 的最新 DCE 玉米日线数据。
输出到 v7/models/（覆盖原文件生成新模型）。
"""

import pickle
import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import akshare as ak

# ── 模型存储路径 ──
V7_DIR = Path(__file__).parent
MODELS_DIR = V7_DIR / "models"
HISTORY_DIR = V7_DIR / "history"

sys.path.insert(0, str(V7_DIR.parent))
from v7.signals import prepare_corn_df
from v7.config import HL_FEATURE_COLS, NIGHT_BASE_COLS, NIGHT_EXT_COLS

RANDOM_STATE = 42


# ════════════════════════════════════════════════════════════
# Part 1: HL 模型重训
# ════════════════════════════════════════════════════════════

def compute_hl_features(df: pd.DataFrame) -> pd.DataFrame:
    """与 v7/decider.py _predict_hl_v7 同口径"""
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
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - 100 / (1 + gain / (loss + 1e-9))

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
    return df


def train_hl(df: pd.DataFrame):
    """训练 HL 模型（GradientBoosting），输出 model_high.pkl 和 model_low.pkl"""
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error

    print("\n═══ HL 模型训练 ═══")
    df_feat = compute_hl_features(df.copy())
    df_feat["next_high"] = df_feat["high"].shift(-1)
    df_feat["next_low"] = df_feat["low"].shift(-1)

    X = df_feat[HL_FEATURE_COLS].ffill().fillna(0).to_numpy(dtype=float)
    y_high = df_feat["next_high"].to_numpy(dtype=float)
    y_low = df_feat["next_low"].to_numpy(dtype=float)
    mask = ~(np.isnan(y_high) | np.isnan(y_low))
    X, y_high, y_low = X[mask], y_high[mask], y_low[mask]

    print(f"  样本: {len(X)} 行")

    mh = GradientBoostingRegressor(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        min_samples_leaf=10, random_state=RANDOM_STATE
    )
    mh.fit(X, y_high)
    pred_high = mh.predict(X)
    mae_h = mean_absolute_error(y_high, pred_high)
    print(f"  model_high: MAE={mae_h:.2f}")

    ml = GradientBoostingRegressor(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        min_samples_leaf=10, random_state=RANDOM_STATE
    )
    ml.fit(X, y_low)
    pred_low = ml.predict(X)
    mae_l = mean_absolute_error(y_low, pred_low)
    print(f"  model_low:  MAE={mae_l:.2f}")

    model_path_h = MODELS_DIR / "model_high.pkl"
    model_path_l = MODELS_DIR / "model_low.pkl"
    with open(model_path_h, "wb") as f:
        pickle.dump(mh, f)
    with open(model_path_l, "wb") as f:
        pickle.dump(ml, f)
    print(f"  ✓ 已保存: {model_path_h}, {model_path_l}")
    return {"high_mae": round(mae_h, 2), "low_mae": round(mae_l, 2), "samples": len(X)}


# ════════════════════════════════════════════════════════════
# Part 2: 夜盘模型重训
# ════════════════════════════════════════════════════════════

def compute_night_features(df: pd.DataFrame) -> pd.DataFrame:
    """与 v7/decider.py _compute_night_features_v7 同口径"""
    close = df["close"].astype(float)
    df["price_chg"] = close - close.shift(1)
    for w in [5, 10, 20, 60]:
        df[f"ma{w}"] = close.rolling(w).mean()

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

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

    month_to_score = {1: 0.0, 2: -0.2, 3: 0.0, 4: 0.25, 5: 0.1, 6: 0.35,
                      7: 0.0, 8: 0.3, 9: 0.25, 10: 0.0, 11: -0.1, 12: 0.2}
    df["month"] = df["date"].dt.month
    df["seasonal"] = df["month"].map(month_to_score)

    df["price_chg_abs"] = df["price_chg"].abs()
    bb_width = bb_upper - bb_lower
    df["bb_width_ratio"] = bb_width / (close + 1e-9) * 100
    df["bb_squeeze"] = df["bb_width_ratio"].rolling(20).max() - df["bb_width_ratio"]
    df["bb_squeeze"] = df["bb_squeeze"] / df["bb_squeeze"].rolling(20).max().replace(0, np.nan)
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


def train_night(df: pd.DataFrame, use_base: bool = False):
    """训练夜盘模型（Ridge + RF ensemble）"""
    from sklearn.linear_model import Ridge
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import mean_absolute_error

    print("\n═══ 夜盘模型训练 ═══")
    if not use_base:
        cols = NIGHT_EXT_COLS
        print(f"  使用扩展特征集 ({len(cols)} 维)")
    else:
        cols = NIGHT_BASE_COLS
        print(f"  使用基础特征集 ({len(cols)} 维)")

    df_feat = compute_night_features(df.copy())
    df_feat["night_change_proxy"] = df_feat["open"].shift(-1) - df_feat["close"]

    X_raw = df_feat[cols].ffill().fillna(0).to_numpy(dtype=float)
    y_raw = df_feat["night_change_proxy"].to_numpy(dtype=float)
    mask = ~(np.isnan(X_raw).any(axis=1) | np.isnan(y_raw))
    X, y = X_raw[mask], y_raw[mask]

    print(f"  样本: {len(X)} 行")
    print(f"  y 分布: mean={y.mean():.2f}, std={y.std():.2f}, min={y.min():.2f}, max={y.max():.2f}")

    # 训练/测试分割（按时间，最后 20% 做测试）
    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    print(f"  训练: {len(X_train)}, 测试: {len(X_test)}")

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    ridge = Ridge(alpha=1.0)
    ridge.fit(X_train_s, y_train)
    r_pred = ridge.predict(X_test_s)
    r_mae = mean_absolute_error(y_test, r_pred)
    r_dir = (np.sign(ridge.predict(X_test_s)) == np.sign(y_test)).mean()
    print(f"  Ridge: MAE={r_mae:.2f}, 方向准确率={r_dir:.1%}")

    rf = RandomForestRegressor(
        n_estimators=200, max_depth=6,
        min_samples_leaf=5, random_state=RANDOM_STATE
    )
    rf.fit(X_train, y_train)
    rf_pred = rf.predict(X_test)
    rf_mae = mean_absolute_error(y_test, rf_pred)
    rf_dir = (np.sign(rf.predict(X_test)) == np.sign(y_test)).mean()
    print(f"  RF:    MAE={rf_mae:.2f}, 方向准确率={rf_dir:.1%}")

    # Ensemble: 0.4 ridge + 0.6 rf
    ensemble_pred = 0.4 * ridge.predict(X_test_s) + 0.6 * rf.predict(X_test)
    ensemble_mae = mean_absolute_error(y_test, ensemble_pred)
    ensemble_dir = (np.sign(ensemble_pred) == np.sign(y_test)).mean()
    print(f"  Ensemble (0.4R+0.6RF): MAE={ensemble_mae:.2f}, 方向准确率={ensemble_dir:.1%}")

    # 校准
    direction_mask = (np.sign(y_test) != 0)
    if direction_mask.any():
        ridge_dir_only = ridge.predict(X_test_s[direction_mask])
        r_dir_acc = (np.sign(ridge_dir_only) == np.sign(y_test[direction_mask])).mean()
        print(f"  Ridge 方向性预测（非0时）: {r_dir_acc:.1%}")
    else:
        r_dir_acc = 0

    model_data = {
        "ridge": ridge,
        "rf": rf,
        "scaler": scaler,
        "ensemble_weights": [0.4, 0.6],
        "calibration": {"ridge_direction_only": r_dir_acc >= 0.35, "coeffs": [0, 1]},
        "feature_cols": cols,
        "training_meta": {
            "trained_at": datetime.now().isoformat(),
            "samples": len(X),
            "test_period": f"{len(X_test)} samples",
            "ridge_mae": round(r_mae, 2),
            "rf_mae": round(rf_mae, 2),
            "ensemble_mae": round(ensemble_mae, 2),
            "ridge_dir_acc": round(float(r_dir), 4),
            "rf_dir_acc": round(float(rf_dir), 4),
            "ensemble_dir_acc": round(float(ensemble_dir), 4),
        },
    }

    model_path = MODELS_DIR / "model_night.pkl"
    with open(model_path, "wb") as f:
        pickle.dump(model_data, f)
    print(f"  ✓ 已保存: {model_path}")
    return model_data["training_meta"]


# ════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("🌽 v7 ML 模型重训")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # 1. 加载最新数据
    print("\n[1/3] 加载 DCE 玉米日线数据...")
    df = ak.futures_zh_daily_sina("C0")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df = df[["date", "open", "high", "low", "close", "volume"]]
    print(f"  共 {len(df)} 行 | {df['date'].min().date()} ~ {df['date'].max().date()}")

    # 2. 训练 HL 模型
    print("\n[2/3] 训练 HL 模型...")
    hl_results = train_hl(df)

    # 3. 训练夜盘模型
    print("\n[3/3] 训练夜盘模型...")
    night_results = train_night(df)

    # 4. 保存汇总
    summary = {
        "timestamp": datetime.now().isoformat(),
        "data": {"rows": len(df), "end_date": str(df['date'].max().date())},
        "hl": hl_results,
        "night": night_results,
    }
    summary_path = HISTORY_DIR / "retrain_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n✅ 重训完成！汇总已保存: {summary_path}")


if __name__ == "__main__":
    main()
