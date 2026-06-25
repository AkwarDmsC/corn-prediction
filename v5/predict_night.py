#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夜盘预测模块 v1.0
基于日线特征预测次日夜盘收盘变化量 (Ridge + RandomForest ensemble)
模型: model_night.pkl
CV MAE: ~6.5元/吨 (夜盘波动率std=17.8元/吨)

使用:
  from predict_night import predict_night_change
  change_pred, direction = predict_night_change(df_features)

注意: 夜盘预测噪音大，建议与CBOT隔夜信号叠加使用
"""

import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from constants import MODEL_NIGHT, NIGHT_FEATURES

MODEL_PATH = MODEL_NIGHT
DATA_PATH  = NIGHT_FEATURES

# ─────────────────────────────────────────
# 加载模型
# ─────────────────────────────────────────
def _load_model():
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"模型文件不存在: {MODEL_PATH}")
    with open(MODEL_PATH, 'rb') as f:
        return pickle.load(f)

_model = None
def _get_model():
    global _model
    if _model is None:
        _model = _load_model()
    return _model

# ─────────────────────────────────────────
# 特征工程（与训练时完全一致）
# ─────────────────────────────────────────
FEAT_COLS = ['price_chg', 'rsi', 'bb_position', 'macd_hist',
             'vol_ratio', 'seasonal',
             'close', 'ma5', 'ma10', 'ma20']
# 扩展特征（retrain_night.py --extended 训练时使用）
FEAT_COLS_EXT = FEAT_COLS + [
    'price_chg_abs', 'bb_width_ratio', 'bb_squeeze',
    'range_pct', 'vol_surge', 'consecutive_up', 'consecutive_down',
    'ma5_slope', 'ma20_slope', 'candle_body_pct',
    'upper_shadow', 'lower_shadow'
]

def _compute_features(corn_df):
    """
    从DCE玉米日线DataFrame计算夜盘预测所需的特征
    corn_df: akshare futures_zh_daily_sina(symbol="C0") 的排序结果（date升序）
    返回特征字典
    """
    df = corn_df.copy()
    df = df.sort_values('date').reset_index(drop=True)

    # 日涨跌
    df['price_chg'] = df['close'] - df['close'].shift(1)

    # MA
    for w in [5, 10, 20, 60]:
        df[f'ma{w}'] = df['close'].rolling(w).mean()

    # RSI(14)
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))

    # 布林带
    ma20 = df['ma20']
    std20 = df['close'].rolling(20).std()
    bb_upper = ma20 + 2 * std20
    bb_lower = ma20 - 2 * std20
    df['bb_position'] = (df['close'] - bb_lower) / (bb_upper - bb_lower)

    # MACD(12,26,9)
    ema12 = df['close'].ewm(span=12).mean()
    ema26 = df['close'].ewm(span=26).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9).mean()
    df['macd_hist'] = macd - macd_signal

    # 成交量比率
    df['vol_ratio'] = df['volume'] / df['volume'].rolling(20).mean()

    # 季节性
    month_to_score = {
        1: 0.0, 2: -0.2, 3: 0.0, 4: 0.25, 5: 0.1, 6: 0.35,
        7: 0.0, 8: 0.3, 9: 0.25, 10: 0.0, 11: -0.1, 12: 0.2
    }
    df['month'] = pd.to_datetime(df['date']).dt.month
    df['seasonal'] = df['month'].map(month_to_score)

    # ── 扩展特征（对齐 retrain_night.py --extended） ──
    # 涨跌幅绝对值
    df['price_chg_abs'] = df['price_chg'].abs()

    # 布林带宽比
    bb_width = bb_upper - bb_lower
    df['bb_width_ratio'] = bb_width / (df['close'] + 1e-9) * 100
    df['bb_squeeze'] = df['bb_width_ratio'].rolling(20).max() - df['bb_width_ratio']
    df['bb_squeeze'] = df['bb_squeeze'] / df['bb_squeeze'].rolling(20).max().replace(0, np.nan)

    # 当日波幅
    df['range'] = df['high'] - df['low']
    df['range_pct'] = df['range'] / df['open'] * 100

    # 放量/缩量标记
    df['vol_surge'] = (df['vol_ratio'] > 1.5).astype(int) - (df['vol_ratio'] < 0.6).astype(int)

    # 连续涨跌天数
    df['up'] = (df['price_chg'] > 0).astype(int)
    df['down'] = (df['price_chg'] < 0).astype(int)
    df['consecutive_up'] = df['up'].groupby((df['up'] == 0).cumsum()).cumsum()
    df['consecutive_down'] = df['down'].groupby((df['down'] == 0).cumsum()).cumsum()

    # MA斜率
    for w in [5, 20]:
        df[f'ma{w}_slope'] = df[f'ma{w}'].diff()

    # K线实体
    df['candle_body'] = df['close'] - df['open']
    df['candle_body_pct'] = df['candle_body'] / df['open'] * 100

    # 上下影线
    df['upper_shadow'] = df['high'] - df[['close', 'open']].max(axis=1)
    df['lower_shadow'] = df[['close', 'open']].min(axis=1) - df['low']

    return df

# ─────────────────────────────────────────
# 主预测函数
# ─────────────────────────────────────────
def predict_night_change(corn_df=None, features_dict=None):
    """
    预测次日夜盘收盘相对当日收盘的变化量

    参数（二选一）:
      corn_df: akshare日线DataFrame（未排序或已排序均可）
      features_dict: 直接传入特征字典（绕过重算）

    返回:
      {
        'change_pred': float,   # 预测变化量（元/吨，正值=夜盘上涨）
        'direction': str,      # "↗偏强" / "↘偏弱" / "↔震荡"
        'confidence': str,     # "🟢高" / "🟡中" / "🔴低"
        'confidence_pct': float,  # 置信度百分比
        'model': str,          # "ridge" / "rf" / "ensemble"
        'ridge_pred': float,
        'rf_pred': float,
      }

    示例:
      change_pred, result = predict_night_change(corn_df)
      print(f"夜盘预测: {result['direction']}, 变化{result['change_pred']:+.0f}元/吨")
    """
    model_data = _get_model()
    rf = model_data['rf']
    ridge = model_data['ridge']
    scaler = model_data['scaler']

    # 计算或使用已有特征
    # v5.2: 自动检测模型使用的是基础(10)还是扩展(22)特征
    if features_dict is None:
        if corn_df is None:
            raise ValueError("必须提供 corn_df 或 features_dict")
        feat_df = _compute_features(corn_df)
        row = feat_df.iloc[-1]  # 最新一行
        # 根据scaler期望的特征数选择列
        expected_feats = scaler.n_features_in_ if hasattr(scaler, 'n_features_in_') else len(FEAT_COLS)
        if expected_feats > len(FEAT_COLS):
            feat_cols = FEAT_COLS_EXT
        else:
            feat_cols = FEAT_COLS
        features_dict = {col: row[col] for col in feat_cols}
    else:
        # 同样检测
        if hasattr(scaler, 'n_features_in_'):
            if scaler.n_features_in_ > len(FEAT_COLS):
                feat_cols = FEAT_COLS_EXT
            else:
                feat_cols = FEAT_COLS
        else:
            feat_cols = FEAT_COLS

    # 组装特征向量
    X = np.array([[features_dict.get(col, 0) for col in feat_cols]])

    # 处理NaN
    X = np.nan_to_num(X, nan=0.0)

    # 标准化
    X_scaled = scaler.transform(X)

    # 双模型预测
    ridge_pred = float(ridge.predict(X_scaled)[0])
    rf_pred = float(rf.predict(X_scaled)[0])

    # Ensemble: 平均（RF倾向极端，Ridge更稳定）
    ensemble_pred = 0.4 * ridge_pred + 0.6 * rf_pred

    # Ridge方向为主（交叉验证方向准确率69%），ensemble提供幅度
    # 校准逻辑：Ridge方向 + ensemble幅度的80%（避免过度放大）
    cal = model_data.get('calibration', {})
    if cal.get('ridge_direction_only'):
        coeffs = cal.get('coeffs', [0, 1])
        ens_abs = np.abs(ensemble_pred)
        mag = max(0.0, coeffs[0] + coeffs[1] * ens_abs)
        calibrated_pred = float(np.sign(ridge_pred) * mag) if abs(ridge_pred) > 0.3 else ensemble_pred * 0.8
    else:
        calibrated_pred = ensemble_pred

    # 方向（以Ridge为主）
    threshold = 1.5  # 元/吨，超过才视为有明确方向
    if abs(ridge_pred) < threshold:
        direction = "↔ 震荡"
        conf_pct = 50
    elif ridge_pred > 0:
        direction = "↗ 偏强"
        conf_pct = min(80, 50 + abs(ridge_pred) / 0.5)
    else:
        direction = "↘ 偏弱"
        conf_pct = min(80, 50 + abs(ridge_pred) / 0.5)

    confidence = "🟡中" if 60 <= conf_pct < 75 else ("🟢高" if conf_pct >= 75 else "🔴低")

    return calibrated_pred, {
        'change_pred': calibrated_pred,
        'ridge_pred': ridge_pred,
        'rf_pred': rf_pred,
        'ridge_direction': direction,
        'ensemble_pred': ensemble_pred,
        'calibrated': bool(cal),
        'confidence': confidence,
        'confidence_pct': conf_pct,
        'model': 'ridge(r)&ens(mag) calibrated',
        'features': features_dict,
    }


def predict_night_session(night_base_price, corn_df=None, features_dict=None,
                           cbot_chg=None, cbot_coef=10.0):
    """
    预测次日夜盘收盘价（完整版本）

    参数:
      night_base_price: 夜盘基准价（元/吨），即当日夜盘收盘价
      corn_df: akshare日线DataFrame
      features_dict: 特征字典（可选）
      cbot_chg: CBOT当日涨跌幅（%，可选）
      cbot_coef: CBOT 1% 对应DCE的调整系数（默认10元/吨）

    返回:
      {
        'pred': 预测收盘价,
        'pred_high': 乐观情景,
        'pred_low': 悲观情景,
        'chg': 预测变化（元/吨）,
        'chg_pct': 预测变化百分比,
        'direction': 方向描述,
        'night_model_info': 夜盘ML模型信息,
        'cbot_adj': CBOT调整量,
        'confidence': 置信度标签,
      }
    """
    import statistics

    # ML模型预测变化量
    change_pred, model_info = predict_night_change(corn_df, features_dict)

    # CBOT传导调整
    cbot_adj = 0.0
    if cbot_chg is not None:
        cbot_adj = cbot_chg * cbot_coef

    # 合并调整
    total_chg = change_pred + cbot_adj

    # 预测收盘价
    pred = night_base_price + total_chg

    # 区间（基于夜盘波动率）
    recent_vol = abs(change_pred) * 2 if abs(change_pred) > 1 else 5.0
    recent_vol = max(recent_vol, 8.0)  # 最小8元/吨
    pred_high = pred + recent_vol
    pred_low = pred - recent_vol

    # 方向综合
    if total_chg > 2:
        direction = "↗ 偏强"
    elif total_chg < -2:
        direction = "↘ 偏弱"
    else:
        direction = "↔ 震荡"

    return {
        'pred': round(pred, 0),
        'pred_high': round(pred_high, 0),
        'pred_low': round(pred_low, 0),
        'chg': round(total_chg, 1),
        'chg_pct': round(total_chg / night_base_price * 100, 2),
        'direction': direction,
        'night_model_info': model_info,
        'cbot_adj': round(cbot_adj, 1),
        'confidence': model_info['confidence'],
    }


# ─────────────────────────────────────────
# CLI 测试
# ─────────────────────────────────────────
if __name__ == "__main__":
    import akshare as ak

    print("=" * 60)
    print("夜盘预测模块 v1.0 — CLI 测试")
    print("=" * 60)

    # 加载数据
    corn_df = ak.futures_zh_daily_sina(symbol="C0")
    corn_df['date'] = corn_df['date'].astype(str)
    corn_df = corn_df.sort_values('date').reset_index(drop=True)

    latest = corn_df.iloc[-1]
    print(f"\nDCE最新数据: {latest['date']} 收盘={latest['close']}")

    # 模拟夜盘基准（当日收盘作为近似）
    night_base = float(latest['close'])

    result = predict_night_session(
        night_base_price=night_base,
        corn_df=corn_df,
        cbot_chg=None
    )

    print(f"\n夜盘预测结果（基准={night_base}）:")
    print(f"  预测收盘: {result['pred']:.0f} 元/吨")
    print(f"  预测区间: {result['pred_low']:.0f} ~ {result['pred_high']:.0f}")
    print(f"  预测变化: {result['chg']:+.1f}元/吨 ({result['chg_pct']:+.2f}%)")
    print(f"  方向: {result['direction']}")
    print(f"  置信度: {result['confidence']}")
    print(f"  CBOT调整: {result['cbot_adj']:+.1f}元/吨")
    print(f"  模型信息: {result['night_model_info']['model']}")
    print(f"  Ridge预测: {result['night_model_info']['ridge_pred']:+.2f}")
    print(f"  RF预测: {result['night_model_info']['rf_pred']:+.2f}")

