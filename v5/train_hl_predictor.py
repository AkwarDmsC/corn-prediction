#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高低价模型训练 v1.0
训练 GradientBoosting 双模型 (high/low)
输出: model_high.pkl, model_low.pkl
特征: OHLC + 技术指标(MA/RSI/MACD/BB/ATR) + 量价特征
"""

import json, pandas as pd, numpy as np
from pathlib import Path
from datetime import datetime
from constants import DCE_DATA, MODEL_HIGH, MODEL_LOW, MODEL_BASELINE

# ============================================================
# 1. 加载数据
# ============================================================
DATA_FILE = DCE_DATA

def load_data():
    with open(DATA_FILE) as f:
        raw = json.load(f)
    df = pd.DataFrame(raw["corn"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df

# ============================================================
# 2. 特征工程
# ============================================================
def compute_features(df):
    df = df.copy()
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
    df["rsi14"] = (100 - 100 / (1 + gain / (loss + 1e-9)))

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

    # 涨跌
    df["chg_pct"] = pd.Series(close).pct_change() * 100

    # 开盘-收盘差
    df["open_close_spread"] = open_ - close

    # 波幅动量
    df["range_momentum"] = df["daily_range"].rolling(5).mean()

    # 季节性
    df["month"] = df["date"].dt.month

    return df

# ============================================================
# 3. 目标变量
# ============================================================
def add_targets(df):
    df = df.copy()
    df["next_high"] = df["high"].shift(-1)
    df["next_low"] = df["low"].shift(-1)
    return df

# ============================================================
# 4. 训练/评估
# ============================================================
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

def prepare_xy(df):
    feat_df = df[FEATURE_COLS].copy()
    feat_df = feat_df.ffill().fillna(0)
    target_h = df["next_high"].values
    target_l = df["next_low"].values
    mask = ~(np.isnan(target_h) | np.isnan(target_l))
    return feat_df.values[mask], target_h[mask], target_l[mask]

def train_and_eval(X, y_high, y_low, train_ratio=0.8):
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

    n = len(y_high)
    split = int(n * train_ratio)
    X_train, X_test = X[:split], X[split:]
    y_h_train, y_h_test = y_high[:split], y_high[split:]
    y_l_train, y_l_test = y_low[:split], y_low[split:]

    print(f"  训练集: {split} 样本, 测试集: {n - split} 样本")

    # 高价模型
    model_h = GradientBoostingRegressor(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        min_samples_leaf=10, random_state=42
    )
    model_h.fit(X_train, y_h_train)
    pred_h = model_h.predict(X_test)

    # 低价模型
    model_l = GradientBoostingRegressor(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        min_samples_leaf=10, random_state=42
    )
    model_l.fit(X_train, y_l_train)
    pred_l = model_l.predict(X_test)

    def eval_metrics(name, y_true, y_pred):
        mae = mean_absolute_error(y_true, y_pred)
        rmse = np.sqrt(mean_squared_error(y_true, y_pred))
        r2 = r2_score(y_true, y_pred)
        mape = np.mean(np.abs((y_true - y_pred) / y_true)) * 100
        print(f"  {name}: MAE={mae:.1f}元, RMSE={rmse:.1f}元, R²={r2:.3f}, MAPE={mape:.2f}%")
        return mae

    print("\n=== 测试集评估 ===")
    mae_h = eval_metrics("次高预测", y_h_test, pred_h)
    mae_l = eval_metrics("次低预测", y_l_test, pred_l)

    # 区间命中（预测高≥实际高 且 预测低≤实际低）
    hits = np.sum((pred_h >= y_h_test) & (pred_l <= y_l_test))
    # 宽松：预测区间包含实际区间
    n_test = len(y_h_test)
    print(f"  区间命中(含): {hits/n_test*100:.1f}%")

    # 边界命中率
    tol_pct = 0.5
    hit_h = np.sum(np.abs(pred_h - y_h_test) / y_h_test * 100 < tol_pct)
    hit_l = np.sum(np.abs(pred_l - y_l_test) / y_l_test * 100 < tol_pct)
    print(f"  高价精确命中(±{tol_pct}%): {hit_h/n_test*100:.1f}%")
    print(f"  低价精确命中(±{tol_pct}%): {hit_l/n_test*100:.1f}%")

    # 特征重要性
    print("\n=== 高价模型 Top6 特征 ===")
    imp = pd.Series(model_h.feature_importances_, index=FEATURE_COLS).sort_values(ascending=False)
    print(imp.head(6).to_string())

    print("\n=== 低价模型 Top6 特征 ===")
    imp_l = pd.Series(model_l.feature_importances_, index=FEATURE_COLS).sort_values(ascending=False)
    print(imp_l.head(6).to_string())

    metrics = {
        "high_mae": round(mae_h, 1),
        "low_mae": round(mae_l, 1),
        "high_rmse": round(np.sqrt(mean_squared_error(y_h_test, pred_h)), 1),
        "low_rmse": round(np.sqrt(mean_squared_error(y_l_test, pred_l)), 1),
        "high_r2": round(r2_score(y_h_test, pred_h), 4),
        "low_r2": round(r2_score(y_l_test, pred_l), 4),
        "high_mape": round(np.mean(np.abs((y_h_test - pred_h) / y_h_test)) * 100, 2),
        "low_mape": round(np.mean(np.abs((y_l_test - pred_l) / y_l_test)) * 100, 2),
        "range_hit_pct": round(hits/n_test*100, 1),
        "high_accuracy_pct": round(hit_h/n_test*100, 1),
        "low_accuracy_pct": round(hit_l/n_test*100, 1),
        "test_samples": n_test,
        "total_samples": len(X),
        "random_state": 42,
        "high_top1_feat": imp.index[0],
        "high_top1_imp": round(float(imp.iloc[0]), 4),
        "low_top1_feat": imp_l.index[0],
        "low_top1_imp": round(float(imp_l.iloc[0]), 4),
    }

    return model_h, model_l, metrics

def save_models(model_h, model_l, metrics=None):
    import pickle
    with open(MODEL_HIGH, "wb") as f:
        pickle.dump(model_h, f)
    with open(MODEL_LOW, "wb") as f:
        pickle.dump(model_l, f)
    print(f"\n模型已保存至 {MODEL_HIGH.name}, {MODEL_LOW.name}")
    # 同时保存训练基线 metrics
    if metrics:
        BL_FILE = MODEL_BASELINE
        try:
            metrics["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(BL_FILE, "w") as f:
                json.dump(metrics, f, indent=2)
            print(f"  训练基线已保存至 {BL_FILE}")
        except Exception as e:
            print(f"  ⚠️ 保存基线失败: {e}")

# ============================================================
# 5. 主程序
# ============================================================
def main():
    print("=== DCE玉米 高低价预测模型训练 ===")
    print(f"时间: {datetime.now():%Y-%m-%d %H:%M}\n")

    print("[1/4] 加载数据...")
    df = load_data()
    print(f"  {len(df)} 行, {df['date'].min().date()} ~ {df['date'].max().date()}")

    print("\n[2/4] 特征工程...")
    df = compute_features(df)
    df = add_targets(df)
    X, y_high, y_low = prepare_xy(df)
    print(f"  有效样本: {len(X)}")

    print("\n[3/4] 训练与评估...")
    model_h, model_l, metrics = train_and_eval(X, y_high, y_low)

    print("\n[4/4] 保存模型...")
    save_models(model_h, model_l, metrics=metrics)

    # 打印最近5天预测 vs 实际
    print("\n=== 近期预测 vs 实际（最新5条）===")
    recent = df.dropna(subset=["next_high"]).tail(5)
    X_r = recent[FEATURE_COLS].fillna(0).values
    ph = model_h.predict(X_r)
    pl = model_l.predict(X_r)
    print(f"{'日期':<12} {'实际高':>8} {'预测高':>8} {'高差':>8} {'实际低':>8} {'预测低':>8} {'低差':>8}")
    for i, (_, row) in enumerate(recent.iterrows()):
        dh = row["next_high"] - ph[i]
        dl = row["next_low"] - pl[i]
        print(f"{str(row['date'].date()):<12} {row['next_high']:8.0f} {ph[i]:8.0f} {dh:+8.0f} {row['next_low']:8.0f} {pl[i]:8.0f} {dl:+8.0f}")

if __name__ == "__main__":
    main()

