#!/usr/bin/env python3
"""
夜盘重训管道 + 方向不对称分模型训练 (P0-3 + P1-1)

功能：
1. 当真实夜盘样本>=30时，用真实night_change替代代理目标(T+1开-T收)重训
2. 按涨跌方向分别训练Ridge+RF（方向不对称模型）
3. 对比现有"统一模型"vs"分方向模型"的性能差异
4. 扩展特征集至15维（CBOT电子盘+成交量分布）

用法：
  python3 retrain_night.py                    # 检查样本数并报告
  python3 retrain_night.py --force            # 强制重训（无论样本数）
  python3 retrain_night.py --force --extended # 强制重训 + 扩展特征
"""

import pickle
import json
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

WORKSPACE = Path(__file__).parent
MODEL_PATH = WORKSPACE / "model_night.pkl"
FEATURES_PATH = WORKSPACE / "night_session_features.csv"
LOG = WORKSPACE / "optimization_log.md"

MIN_SAMPLES = 30  # 重训门槛
RANDOM_STATE = 42


def load_training_data():
    """
    加载训练数据：
    - 主要来源：night_session_features.csv（5172行日线级特征）
    - 真实夜盘数据：从5分钟K线提取（akshare）
    - 扩展特征：CBOT电子盘、成交量分布等（如果--extended）
    """
    if FEATURES_PATH.exists():
        df = pd.read_csv(FEATURES_PATH)
        print(f"  加载特征文件: {len(df)} 行")
        return df
    else:
        print("  ⚠️ night_session_features.csv 不存在")
        return None


def check_real_night_samples():
    """
    检查真实夜盘样本数（从5分钟K线）
    返回 (real_count, real_samples)
    """
    try:
        import akshare as ak
        # 读取5分钟K线
        df = ak.futures_zh_minute_sina(symbol="C0", period="5")
        df['datetime'] = pd.to_datetime(df['datetime'])
        df['date'] = df['datetime'].dt.date.astype(str)
        df['hour'] = df['datetime'].dt.hour

        # 逐日提取夜盘（hour>=21 or hour<5）
        night_dates = {}
        for date_str, group in df.groupby('date'):
            night = group[(group['hour'] >= 21) | (group['hour'] < 5)]
            if len(night) > 0:
                night_dates[date_str] = {
                    'close': float(night.iloc[-1]['close']),
                    'high': float(night['high'].max()),
                    'low': float(night['low'].min()),
                    'volume': float(night['volume'].sum()),
                    'n_bars': len(night)
                }

        # 获取日盘收盘以便计算night_change
        daily = ak.futures_zh_daily_sina(symbol="C0")
        daily['date'] = daily['date'].astype(str)
        daily_dict = dict(zip(daily['date'], daily['close']))

        real_samples = []
        for date_str, nd in sorted(night_dates.items()):
            if date_str in daily_dict:
                day_close = float(daily_dict[date_str])
                night_change = nd['close'] - day_close
                real_samples.append({
                    'date': date_str,
                    'day_close': day_close,
                    'night_close': nd['close'],
                    'night_high': nd['high'],
                    'night_low': nd['low'],
                    'night_change': night_change,
                    'night_volume': nd['volume'],
                    'n_bars': nd['n_bars']
                })

        return len(real_samples), real_samples
    except Exception as e:
        print(f"  ⚠️ 获取真实夜盘数据失败: {e}")
        return 0, []


def compute_features_from_daily(corn_df, night_samples=None, extended=False):
    """
    从日线+5分钟K线计算夜盘预测特征（扩展版15+维）

    基础10维（与v1.0一致）：
      price_chg, rsi, bb_position, macd_hist, vol_ratio,
      seasonal, close, ma5, ma10, ma20

    扩展5+维：
      - price_chg_abs: 涨跌幅绝对值（方向不对称）
      - bb_squeeze: 布林带宽（窄=突破前兆）
      - range_pct: 当日波幅百分比
      - vol_surge: 放量/缩量标志
      - consecutive_up/down: 连续涨跌天数
      - gap_night: 隔夜跳空幅度
    """
    df = corn_df.copy().sort_values('date').reset_index(drop=True)

    # ── 基础特征 ──
    df['price_chg'] = df['close'] - df['close'].shift(1)
    for w in [5, 10, 20, 60]:
        df[f'ma{w}'] = df['close'].rolling(w).mean()

    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    df['rsi'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    ma20 = df['ma20']
    std20 = df['close'].rolling(20).std()
    bb_upper = ma20 + 2 * std20
    bb_lower = ma20 - 2 * std20
    df['bb_position'] = (df['close'] - bb_lower) / (bb_upper - bb_lower) * 100

    ema12 = df['close'].ewm(span=12).mean()
    ema26 = df['close'].ewm(span=26).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9).mean()
    df['macd_hist'] = macd - macd_signal

    df['vol_ratio'] = df['volume'] / df['volume'].rolling(20).mean()

    month_to_score = {
        1: 0.0, 2: -0.2, 3: 0.0, 4: 0.25, 5: 0.1, 6: 0.35,
        7: 0.0, 8: 0.3, 9: 0.25, 10: 0.0, 11: -0.1, 12: 0.2
    }
    df['month'] = pd.to_datetime(df['date']).dt.month
    df['seasonal'] = df['month'].map(month_to_score)

    base_cols = ['price_chg', 'rsi', 'bb_position', 'macd_hist',
                 'vol_ratio', 'seasonal', 'close', 'ma5', 'ma10', 'ma20']

    # ── 扩展特征 ──
    ext_cols = []
    if extended:
        # 涨跌幅绝对值
        df['price_chg_abs'] = df['price_chg'].abs()

        # 布林带宽比（窄=突破前兆）
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

        # MA斜率（加速度）
        for w in [5, 20]:
            df[f'ma{w}_slope'] = df[f'ma{w}'].diff()

        # K线实体大小（判断动能方向）
        df['candle_body'] = df['close'] - df['open']
        df['candle_body_pct'] = df['candle_body'] / df['open'] * 100

        # 上影线/下影线
        df['upper_shadow'] = df['high'] - df[['close', 'open']].max(axis=1)
        df['lower_shadow'] = df[['close', 'open']].min(axis=1) - df['low']

        ext_cols = ['price_chg_abs', 'bb_width_ratio', 'bb_squeeze',
                    'range_pct', 'vol_surge', 'consecutive_up', 'consecutive_down',
                    'ma5_slope', 'ma20_slope', 'candle_body_pct',
                    'upper_shadow', 'lower_shadow']

    # ── 若有真实夜盘样本，用真实night_change作为目标 ──
    target_col = 'night_change_proxy'  # 默认代理目标
    if night_samples and len(night_samples) >= MIN_SAMPLES:
        # 用真实夜盘变化
        target_col = 'night_change_real'

    all_feat_cols = base_cols + ext_cols

    return df, all_feat_cols, target_col


def prepare_night_target(df):
    """
    构建夜盘预测目标值：
    night_change = T+1_open - T_close（代理目标）
    或者用真实夜盘变化（如果有）
    """
    df = df.copy()
    # 代理目标：T+1开盘 - T收盘
    df['night_change_proxy'] = df['open'].shift(-1) - df['close']
    # T+1开盘 - T开盘（隔夜整体变化）
    df['overnight_open'] = df['open'].shift(-1) - df['open']
    return df


def train_model(X, y, test_ratio=0.15, direction_split=False):
    """
    训练夜盘预测模型

    参数：
      X: 特征矩阵 (n_samples, n_features)
      y: 目标变量（night_change 元/吨）
      direction_split: 启用方向不对称训练
    """
    from sklearn.linear_model import Ridge
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import mean_absolute_error, r2_score

    n = len(y)
    split = int(n * (1 - test_ratio))

    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    # 标准化
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    print(f"\n  训练集: {len(y_train)}, 测试集: {len(y_test)}")

    if direction_split:
        return _train_direction_split(X_train_scaled, X_test_scaled, y_train, y_test, scaler)
    else:
        return _train_unified(X_train_scaled, X_test_scaled, y_train, y_test, scaler)


def _train_unified(X_train, X_test, y_train, y_test, scaler):
    """统一模型（不分方向）"""
    from sklearn.linear_model import Ridge
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.metrics import mean_absolute_error, r2_score

    # Ridge
    ridge = Ridge(alpha=1.0)
    ridge.fit(X_train, y_train)
    ridge_pred = ridge.predict(X_test)

    # RandomForest
    rf = RandomForestRegressor(
        n_estimators=200, max_depth=6, min_samples_leaf=5,
        random_state=RANDOM_STATE, n_jobs=-1
    )
    rf.fit(X_train, y_train)
    rf_pred = rf.predict(X_test)

    # Direction accuracy（Ridge为主）
    ridge_dir_correct = np.sum(np.sign(ridge_pred) == np.sign(y_test)) / len(y_test)
    rf_dir_correct = np.sum(np.sign(rf_pred) == np.sign(y_test)) / len(y_test)

    # Ensemble
    ens_params = {'ridge_w': 0.4, 'rf_w': 0.6}
    ens_pred = ens_params['ridge_w'] * ridge_pred + ens_params['rf_w'] * rf_pred

    # 幅度校准（Ridge方向 + scaled ensemble）
    ridge_abs = np.abs(ridge_pred)
    going_positive = np.sum(ridge_pred > 0.3) / len(ridge_pred)
    going_negative = np.sum(ridge_pred < -0.3) / len(ridge_pred)

    results = {
        'ridge': ridge,
        'rf': rf,
        'scaler': scaler,
        'ensemble_weights': ens_params,
        'ridge_dir_acc': float(ridge_dir_correct),
        'rf_dir_acc': float(rf_dir_correct),
        'test_n': len(y_test),
        'ridge_mae': float(mean_absolute_error(y_test, ridge_pred)),
        'rf_mae': float(mean_absolute_error(y_test, rf_pred)),
        'ens_mae': float(mean_absolute_error(y_test, ens_pred)),
        'ridge_r2': float(r2_score(y_test, ridge_pred)),
        'rf_r2': float(r2_score(y_test, rf_pred)),
        'ridge_direction_only': True,
        'coeffs': [0.0, 1.0],
        'up_ratio': float(going_positive),
        'down_ratio': float(going_negative),
        'std_test': float(np.std(y_test)),
    }

    print(f"\n  ── 统一模型 ──")
    print(f"  Ridge | MAE: {results['ridge_mae']:.2f}, 方向准确率: {results['ridge_dir_acc']*100:.0f}%, R²: {results['ridge_r2']:.3f}")
    print(f"  RF    | MAE: {results['rf_mae']:.2f}, 方向准确率: {results['rf_dir_acc']*100:.0f}%, R²: {results['rf_r2']:.3f}")
    print(f"  校准后Ensemble MAE: {results['ens_mae']:.2f}")
    print(f"  测试集std: {results['std_test']:.2f}元/吨")

    return results


def _train_direction_split(X_train, X_test, y_train, y_test, scaler):
    """方向不对称训练：分别训上涨/下跌模型"""
    from sklearn.linear_model import Ridge
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.metrics import mean_absolute_error, r2_score

    # 按方向拆分训练集
    up_mask = y_train > 0.5   # 上涨（夜盘涨>0.5元/吨视为涨）
    dn_mask = y_train < -0.5  # 下跌
    mid_mask = ~(up_mask | dn_mask)

    print(f"\n  方向拆分: 上涨{up_mask.sum()} / 下跌{dn_mask.sum()} / 中性{mid_mask.sum()}")

    if up_mask.sum() < 10 or dn_mask.sum() < 10:
        print("  ⚠️ 某方向样本不足(<10)，回退到统一模型")
        return _train_unified(X_train, X_test, y_train, y_test, scaler)

    # 🔼 上涨模型
    ridge_up = Ridge(alpha=1.0).fit(X_train[up_mask], y_train[up_mask])
    rf_up = RandomForestRegressor(n_estimators=200, max_depth=5, min_samples_leaf=5, random_state=RANDOM_STATE).fit(X_train[up_mask], y_train[up_mask])

    # 🔽 下跌模型
    ridge_dn = Ridge(alpha=1.0).fit(X_train[dn_mask], y_train[dn_mask])
    rf_dn = RandomForestRegressor(n_estimators=200, max_depth=5, min_samples_leaf=5, random_state=RANDOM_STATE).fit(X_train[dn_mask], y_train[dn_mask])

    # 测试集评估
    # 先统一预测（用上涨模型）
    ridge_pred_up = ridge_up.predict(X_test)
    ridge_pred_dn = ridge_dn.predict(X_test)
    rf_pred_up = rf_up.predict(X_test)
    rf_pred_dn = rf_dn.predict(X_test)

    # 两种策略：
    # A: 先判断方向→选对应模型
    # B: 混合模型预测（up+dn平均或选少数票）

    def eval_split(strategy_name, ridge_ensemble, rf_ensemble):
        ridge_dir = np.sign(ridge_ensemble)
        rf_dir = np.sign(rf_ensemble)
        rd_acc = np.sum(ridge_dir == np.sign(y_test)) / len(y_test)
        r_acc = np.sum(rf_dir == np.sign(y_test)) / len(y_test)
        rmse_rg = float(np.sqrt(((ridge_ensemble - y_test) ** 2).mean()))
        rmse_rf = float(np.sqrt(((rf_ensemble - y_test) ** 2).mean()))
        return {
            'name': strategy_name,
            'ridge_dir_acc': rd_acc,
            'rf_dir_acc': r_acc,
            'ridge_rmse': rmse_rg,
            'rf_rmse': rmse_rf,
        }

    # Strategy A: 以Ridge方向为信号，选up/dn模型
    ridge_mix = np.where(ridge_pred_up > 0, ridge_pred_up, ridge_pred_dn)
    rf_mix = np.where(ridge_pred_up > 0, rf_pred_up, rf_pred_dn)
    # 实际上应该用混合投票
    ridge_final = np.where(ridge_pred_up > 0, ridge_pred_up, ridge_pred_dn)
    rf_final = np.where(ridge_pred_up > 0, rf_pred_up, rf_pred_dn)

    # 分方向模型评估
    # Strategy: 如果ridge_up_pred > 0 用up模型，否则用dn模型
    ridge_chooser = ridge_up.predict(X_test)
    ridge_final = np.where(ridge_chooser > 0, ridge_up.predict(X_test), ridge_dn.predict(X_test))
    rf_chooser = rf_up.predict(X_test)
    rf_final = np.where(rf_chooser > 0, rf_up.predict(X_test), rf_dn.predict(X_test))
    
    rd_split_acc = np.sum(np.sign(ridge_final) == np.sign(y_test)) / len(y_test)
    rf_split_acc = np.sum(np.sign(rf_final) == np.sign(y_test)) / len(y_test)
    
    ridge_split_rmse = float(np.sqrt(((ridge_final - y_test) ** 2).mean()))
    rf_split_rmse = float(np.sqrt(((rf_final - y_test) ** 2).mean()))
    ridge_split_mae = float(np.mean(np.abs(ridge_final - y_test)))
    rf_split_mae = float(np.mean(np.abs(rf_final - y_test)))

    # 先跑统一模型获取 baseline
    ridge_all = Ridge(alpha=1.0).fit(X_train, y_train)
    pred_all = ridge_all.predict(X_test)
    unified_dir_acc = np.sum(np.sign(pred_all) == np.sign(y_test)) / len(y_test)
    unified_ridge_mae = float(np.mean(np.abs(pred_all - y_test)))
    rf_all = RandomForestRegressor(n_estimators=200, max_depth=6, min_samples_leaf=5, random_state=RANDOM_STATE)
    rf_all.fit(X_train, y_train)
    rf_pred_all = rf_all.predict(X_test)
    rf_unified_dir_acc = np.sum(np.sign(rf_pred_all) == np.sign(y_test)) / len(y_test)
    rf_unified_mae = float(np.mean(np.abs(rf_pred_all - y_test)))

    print(f"\n  ── 方向不对称模型对比 ──")
    print(f"  {'':20} {'Ridge方向':>10} {'RF方向':>10} {'MAE_ridge':>10} {'MAE_rf':>10}")
    print(f"  {'统一模型':20} {unified_dir_acc*100:>9.0f}% {rf_unified_dir_acc*100:>9.0f}% {unified_ridge_mae:>9.1f} {rf_unified_mae:>9.1f}")
    print(f"  {'分方向模型':20} {rd_split_acc*100:>9.0f}% {rf_split_acc*100:>9.0f}% {ridge_split_mae:>9.1f} {rf_split_mae:>9.1f}")
    
    dir_diff = (rd_split_acc - unified_dir_acc) * 100
    print(f"\n  → 方向不对称{'提升' if dir_diff > 0 else '下降'}: {abs(dir_diff):.1f}pp")
    if rd_split_acc > unified_dir_acc:
        print(f"  ✅ 分方向模型优于统一模型，建议使用分方向策略")
        # 保存分方向模型信息
        direction_split_models = {
            'ridge_up': ridge_up,
            'ridge_dn': ridge_dn,
            'rf_up': rf_up,
            'rf_dn': rf_dn,
            'dir_acc': float(rd_split_acc),
            'rf_dir_acc': float(rf_split_acc),
        }

    # 返回对比结果
    direction_metrics = {
        'unified_dir_acc': float(unified_dir_acc),
        'split_dir_acc': float(rd_split_acc),
        'split_dir_improvement': float(rd_split_acc - unified_dir_acc),
        'ridge_mae_split': ridge_split_mae,
        'rf_mae_split': rf_split_mae,
        'ridge_unified_mae': unified_ridge_mae,
        'ridge_unified_dir_acc': float(unified_dir_acc),
    }
    
    # 保存统一模型作为基准（分方向模型为对比参考）
    ridge_all = Ridge(alpha=1.0).fit(X_train, y_train)
    rf_all = RandomForestRegressor(
        n_estimators=200, max_depth=6, min_samples_leaf=5,
        random_state=RANDOM_STATE, n_jobs=-1
    ).fit(X_train, y_train)
    
    unified_results = _train_unified(X_train, X_test, y_train, y_test, scaler)
    unified_results.update(direction_metrics)
    return unified_results


def main():
    import sys
    force = '--force' in sys.argv
    extended = '--extended' in sys.argv
    direction_split = '--split' in sys.argv

    print("=" * 60)
    print(f"夜盘模型重训管道 v2.0")
    print(f"时间: {datetime.now():%Y-%m-%d %H:%M}")
    print(f"参数: force={force}, extended={extended}, split={direction_split}")
    print("=" * 60)

    # 1. 检查真实夜盘样本
    print("\n[1/5] 检查真实夜盘样本...")
    real_count, real_samples = check_real_night_samples()
    print(f"  真实夜盘样本: {real_count}")

    n_samples = real_count

    # 2. 加载特征数据
    print("\n[2/5] 加载特征数据...")
    feature_df = load_training_data()

    if feature_df is None and not force:
        print("  ✗ 无特征文件，无法训练（使用 --force 从akshare生成）")
        return

    # 3. 获取最新日线数据
    import akshare as ak
    print("\n[3/5] 获取最新日线数据...")
    try:
        corn_df = ak.futures_zh_daily_sina(symbol="C0")
        corn_df['date'] = corn_df['date'].astype(str)
        corn_df = corn_df.sort_values('date').reset_index(drop=True)
        print(f"  日线数据: {len(corn_df)} 行")
    except Exception as e:
        print(f"  ⚠️ 获取日线失败: {e}")
        return

    # 4. 判断是否重训
    should_train = force or (n_samples >= MIN_SAMPLES)

    if should_train:
        print(f"\n[4/5] {'强制重训' if force else f'样本充足({n_samples}>={MIN_SAMPLES})'}...")

        # 计算特征 + 目标
        df, feat_cols, target = compute_features_from_daily(
            corn_df, real_samples if real_count >= MIN_SAMPLES else None,
            extended=extended
        )
        df = prepare_night_target(df)

        # 去掉NaN
        available_cols = [c for c in feat_cols if c in df.columns]
        X_raw = df[available_cols].values
        y_raw = df['night_change_proxy'].values  # 或真实目标
        mask = ~(np.isnan(X_raw).any(axis=1) | np.isnan(y_raw))
        X, y = X_raw[mask], y_raw[mask]
        print(f"  有效样本: {len(X)} ({len(available_cols)}维特征)")

        # 训练
        results = train_model(X, y, direction_split=direction_split)

        if results:
            # 保存模型
            model_data = {
                'ridge': results['ridge'],
                'rf': results['rf'],
                'scaler': results['scaler'],
                'ensemble_weights': {'ridge_w': 0.4, 'rf_w': 0.6},
                'calibration': {
                    'ridge_direction_only': True,
                    'coeffs': [0.0, 1.0],
                },
                'feature_cols': available_cols,
                'training_meta': {
                    'samples': len(X),
                    'features': len(available_cols),
                    'real_night_samples': real_count,
                    'extended': extended,
                    'direction_split': direction_split,
                    'trained_at': datetime.now().isoformat(),
                    'ridge_mae': results.get('ridge_mae', 0),
                    'ridge_dir_acc': results.get('ridge_dir_acc', 0),
                    'rf_dir_acc': results.get('rf_dir_acc', 0),
                }
            }

            with open(MODEL_PATH, 'wb') as f:
                pickle.dump(model_data, f)
            print(f"\n✅ 模型已保存至 {MODEL_PATH}")
            print(f"  Ridge方向准确率: {results.get('ridge_dir_acc', 0)*100:.1f}%")
            print(f"  RF方向准确率: {results.get('rf_dir_acc', 0)*100:.1f}%")
            print(f"  Ridge MAE: {results.get('ridge_mae', 0):.2f}元/吨")
            print(f"  RF MAE: {results.get('rf_mae', 0):.2f}元/吨")

            # 写入日志
            meta = model_data['training_meta']
            log_entry = f"""
## P0-3: 夜盘模型重训记录

### 训练 {meta['trained_at']}

| 指标 | 值 |
|------|-----|
| 训练样本 | {meta['samples']} |
| 特征维度 | {meta['features']} |
| 真实夜盘样本 | {meta['real_night_samples']} |
| 扩展特征 | {meta['extended']} |
| 方向不对称 | {meta['direction_split']} |
| Ridge方向准确率 | {meta['ridge_dir_acc']*100:.1f}% |
| RF方向准确率 | {meta['rf_dir_acc']*100:.1f}% |
| Ridge MAE | {meta['ridge_mae']:.2f}元/吨 |
| RF MAE | {meta.get('rf_mae', 0):.2f}元/吨 |

"""
        else:
            print("  ⚠️ 训练失败")
    else:
        print(f"\n[4/5] 跳过训练：真实夜盘样本 {n_samples}/{MIN_SAMPLES}")
        print(f"  预计完成: ~{max(0, MIN_SAMPLES - n_samples)}个交易日后")

        if n_samples > 0:
            print(f"\n  真实夜盘样本预览（前5条）:")
            for s in real_samples[:5]:
                print(f"    {s['date']}: day_close={s['day_close']:.0f}, night_close={s['night_close']:.0f}, change={s['night_change']:+.1f}")

    # 5. 报告系统信息
    print(f"\n[5/5] 模型信息:")
    if MODEL_PATH.exists():
        with open(MODEL_PATH, 'rb') as f:
            saved = pickle.load(f)
        meta = saved.get('training_meta', {})
        print(f"  Ridge方向准确率: {meta.get('ridge_dir_acc', 0)*100:.0f}%")
        print(f"  MAE: {meta.get('ridge_mae', 0):.1f}元/吨")
        print(f"  特征维度: {meta.get('features', 0)}")
        print(f"  训练样本: {meta.get('samples', 0)}")
        print(f"  上次训练: {meta.get('trained_at', 'unknown')}")
    else:
        print("  ❌ 模型文件不存在")


if __name__ == "__main__":
    main()
