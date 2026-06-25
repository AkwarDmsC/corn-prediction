#!/usr/bin/env python3
"""
v7 信号权重优化 — 历史贡献度回溯

方法：walk-forward 逐日计算每个信号独立方向贡献，
      统计每个信号对正确/错误预测的贡献率，
      校准权重表。

输出：signal_contribution_report.md + 更新建议 config.py
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import akshare as ak

# ── 加载 v7 信号计算模块 ──
sys.path.insert(0, str(Path(__file__).parent.parent))
from v7.signals import prepare_corn_df, sma, rsi, macd_hist


def compute_individual_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    对每行(i>=60)计算 9 个独立信号的方向值，同时记录次日涨跌。
    返回 DataFrame 每行：
      - date, close, next_chg_pct（次日涨跌幅%）
      - 各信号原始值（ma_dir, rsi_sig, bb_sig, macd_sig, vol_sig, soy_corr, cbot, policy, weather）
    """
    closes = df["close"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    vols = df["volume"].tolist()
    rows = []

    for i in range(60, len(df)):
        sub = df.iloc[:i+1]
        row = df.iloc[i]
        close = float(row["close"])
        chg_pct = None
        if i + 1 < len(df):
            next_close = float(df.iloc[i + 1]["close"])
            chg_pct = (next_close - close) / close * 100

        c = closes[:i+1]
        h = highs[:i+1]
        l = lows[:i+1]
        v = vols[:i+1]

        # 均线
        ma5 = sma(c, 5)
        ma10 = sma(c, 10)
        ma20 = sma(c, 20)
        ma60 = sma(c, 60)
        ma60_prev = sma(c[:-20], 60) if len(c) >= 80 else None
        ma_dir = 1 if (ma5 and ma10 and ma20 and ma5 > ma10 > ma20) else -1 if (ma5 and ma10 and ma20 and ma5 < ma10 < ma20) else 0
        ma60_trend = 0
        if ma60 and ma60_prev:
            ma60_trend = 1 if ma60 > ma60_prev * 1.002 else -1 if ma60 < ma60_prev * 0.998 else 0

        # RSI
        rsi_val = rsi(c)
        rsi_sig = 1 if (rsi_val is not None and rsi_val < 40) else -1 if (rsi_val is not None and rsi_val > 65) else 0

        # 布林
        sd20 = float(pd.Series(c).rolling(20).std().iloc[-1]) if len(c) >= 20 else None
        bb_pos = 50.0
        if ma20 and sd20:
            upper = ma20 + 2 * sd20
            lower = ma20 - 2 * sd20
            bb_pos = float(np.clip((close - lower) / (upper - lower) * 100, 0, 100))
        bb_sig = 1 if bb_pos < 20 else -1 if bb_pos > 80 else 0

        # MACD
        _, _, hist = macd_hist(c)
        macd_sig = 1 if (hist is not None and hist > 0) else -1 if (hist is not None and hist < 0) else 0

        # 成交量
        vol_ma5 = float(np.mean(v[-5:])) if len(v) >= 5 else float(v[-1] or 1)
        vol_ratio = float(v[-1] / (vol_ma5 + 1e-9))
        vol_sig = 1.0 if vol_ratio > 1.2 else -0.5 if vol_ratio < 0.8 else 0.0

        rows.append({
            "date": str(row["date"].date()),
            "close": close,
            "next_chg_pct": chg_pct,
            "ma_dir": ma_dir,
            "rsi_sig": rsi_sig,
            "bb_sig": bb_sig,
            "macd_sig": macd_sig,
            "vol_sig": vol_sig,
            "bb_pos": bb_pos,
            "rsi_val": rsi_val,
            "ma60_trend": ma60_trend,
        })
    return pd.DataFrame(rows)


def evaluate_signal(df_sig: pd.DataFrame, col: str, label: str) -> dict:
    """
    评估单个信号的历史方向预测能力。
    信号方向与次日涨跌方向一致 = 正确。
    返回：准确率、样本数、做多时准确率、做空时准确率。
    """
    valid = df_sig.dropna(subset=["next_chg_pct", col])
    valid = valid[valid[col] != 0]

    if len(valid) == 0:
        return {"label": label, "col": col, "total": 0, "accuracy": 0, "bull_accuracy": 0, "bear_accuracy": 0, "n_bull": 0, "n_bear": 0}

    # 信号方向 vs 实际涨跌方向
    correct = ((valid[col] > 0) & (valid["next_chg_pct"] > 0)) | ((valid[col] < 0) & (valid["next_chg_pct"] < 0))
    accuracy = correct.mean()

    bull = valid[valid[col] > 0]
    bear = valid[valid[col] < 0]

    bull_acc = (bull["next_chg_pct"] > 0).mean() if len(bull) > 0 else 0
    bear_acc = (bear["next_chg_pct"] < 0).mean() if len(bear) > 0 else 0

    return {
        "label": label,
        "col": col,
        "total": len(valid),
        "accuracy": round(float(accuracy), 4),
        "n_bull": len(bull),
        "bull_accuracy": round(float(bull_acc), 4),
        "n_bear": len(bear),
        "bear_accuracy": round(float(bear_acc), 4),
    }


def evaluate_weight_optimized(df_sig: pd.DataFrame, steps: int = 8) -> dict:
    """
    两阶段权重优化：先粗搜确定范围，再细搜精调。
    比穷举快几个数量级。
    """
    valid = df_sig.dropna(subset=["next_chg_pct"]).copy()
    signal_cols = ["ma_dir", "rsi_sig", "bb_sig", "macd_sig", "vol_sig"]

    # 先把所有信号的原始值缓存在 numpy array 中
    X = np.column_stack([valid[c].values for c in signal_cols])
    y = valid["next_chg_pct"].values

    def _accuracy(weights: np.ndarray) -> float:
        combined = X @ weights
        direction = np.sign(combined)
        correct = ((direction > 0) & (y > 0)) | ((direction < 0) & (y < 0))
        return float(correct.mean())

    print(f"    粗搜阶段 (步长 {(3.0/steps):.1f}, {steps**5} 组合)...")
    best_w = np.zeros(5)
    best_acc = 0.0

    # Phase 1: 粗搜 [0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0] 共 7 种
    coarse_range = [0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    from itertools import product as iter_product
    for weights in iter_product(coarse_range, repeat=5):
        w = np.array(weights, dtype=float)
        acc = _accuracy(w)
        if acc > best_acc:
            best_acc = acc
            best_w = w.copy()

    print(f"      粗搜最优: {list(best_w)}  acc={best_acc:.4f}")

    # Phase 2: 在粗搜结果的 ±0.5 范围精搜，步长 0.2
    fine_steps = 6  # -0.5 ~ +0.5 共 6 步
    fine_range = np.linspace(-0.5, 0.5, fine_steps)
    improved = True
    iterations = 0
    while improved and iterations < 5:
        iterations += 1
        improved = False
        for dim_idx in range(5):
            base = best_w[dim_idx]
            best_local_acc = best_acc
            best_local_w = base
            for delta in fine_range:
                candidate = max(0, base + delta)
                trial_w = best_w.copy()
                trial_w[dim_idx] = candidate
                acc = _accuracy(trial_w)
                if acc > best_local_acc:
                    best_local_acc = acc
                    best_local_w = candidate
                    improved = True
            if improved:
                best_w[dim_idx] = best_local_w
                best_acc = best_local_acc

    result = {
        "acc": round(float(best_acc), 4),
        "n": int((((X @ best_w) > 0) & (y > 0)).sum() + (((X @ best_w) < 0) & (y < 0)).sum()),
        "total": len(y),
    }
    for i, col in enumerate(signal_cols):
        result[f"w_{col.split('_')[0]}"] = round(float(best_w[i]), 2)
    return result


def main():
    print("=" * 60)
    print("🌽 v7 信号贡献度回溯分析")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # 1. 获取数据
    print("\n[1/4] 加载 DCE 玉米日线...")
    df = ak.futures_zh_daily_sina("C0")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    # 2. 计算信号
    print(f"  共 {len(df)} 行 | {df['date'].min().date()} ~ {df['date'].max().date()}")
    print("\n[2/4] 逐日计算信号回溯...")
    sig_df = compute_individual_signals(df)
    print(f"  有效回溯样本: {len(sig_df)} 天")

    # 3. 单信号评估
    print("\n[3/4] 单信号独立准确率评估...")
    signals_to_test = [
        ("ma_dir", "均线方向"),
        ("rsi_sig", "RSI超买超卖"),
        ("bb_sig", "布林带极值"),
        ("macd_sig", "MACD柱"),
        ("vol_sig", "成交量异动"),
    ]

    results = []
    for col, label in signals_to_test:
        r = evaluate_signal(sig_df, col, label)
        results.append(r)
        print(f"  {label:16s} | 总 {r['total']:5d} | 准 {r['accuracy']:.1%}"
              f" | 看多 {r['n_bull']:4d} -> {r['bull_accuracy']:.1%}"
              f" | 看空 {r['n_bear']:4d} -> {r['bear_accuracy']:.1%}")

    # 4. 权重优化
    print("\n[4/4] 权重 grid-search 优化...")
    # 分段测试
    recent = sig_df[sig_df["date"] >= "2023-01-01"].copy()
    full_set = sig_df[sig_df["date"] >= "2015-01-01"].copy()

    print(f"\n  全样本 (2015-2026): {len(full_set)} 天")
    print(f"\n  全样本 (2015-2026): {len(full_set)} 天")
    best_full = evaluate_weight_optimized(full_set, steps=7)
    print(f"  最优组合: w_ma={best_full['w_ma']}, w_rsi={best_full['w_rsi']}, "
          f"w_bb={best_full['w_bb']}, w_macd={best_full['w_macd']}, w_vol={best_full['w_vol']}")
    print(f"  方向准确率: {best_full['acc']:.1%} ({best_full['n']}/{best_full['total']})")

    print(f"\n  近期 (2023-2026): {len(recent)} 天")
    best_recent = evaluate_weight_optimized(recent, steps=7)
    print(f"  最优组合: w_ma={best_recent['w_ma']}, w_rsi={best_recent['w_rsi']}, "
          f"w_bb={best_recent['w_bb']}, w_macd={best_recent['w_macd']}, w_vol={best_recent['w_vol']}")
    print(f"  方向准确率: {best_recent['acc']:.1%} ({best_recent['n']}/{best_recent['total']})")

    # 5. 震荡筛选效果对比
    print("\n\n── 震荡筛选效果对比 ──")
    # 模拟 v7 的震荡筛选：同向信号<4且一致性<0.2时剔除
    valid = full_set.copy()
    signals_cols = ["ma_dir", "rsi_sig", "bb_sig", "macd_sig", "vol_sig"]
    weights_v7 = [1.6, 2.2, 1.8, 0.5, 0.3]  # 当前 v7 权重
    combined_v7 = sum(valid[c].values * w for c, w in zip(signals_cols, weights_v7))
    total_w = sum(abs(w) for w in weights_v7)
    consistency = combined_v7 / total_w
    direction_v7 = np.sign(combined_v7)
    correct_v7 = ((direction_v7 > 0) & (valid["next_chg_pct"].values > 0)) | ((direction_v7 < 0) & (valid["next_chg_pct"].values < 0))
    # 震荡筛选移除
    agreeing_counts = []
    for i, row in valid.iterrows():
        score_np = combined_v7[valid.index.get_loc(i)] if i in valid.index else 0
        active = sum(1 for c in signals_cols if abs(row[c]) > 0.05)
        agreeing = sum(1 for c in signals_cols if row[c] * score_np > 0)
        agreeing_counts.append((agreeing, active))
    valid = valid.copy()
    valid["agreeing"] = [a[0] for a in agreeing_counts]
    valid["active"] = [a[1] for a in agreeing_counts]
    valid["consistency"] = np.abs(consistency)

    # 未筛选：全部
    no_filter_acc = correct_v7.mean()
    print(f"  未筛选全部样本: {no_filter_acc:.1%} ({int(correct_v7.sum())}/{len(correct_v7)})")

    # v7 震荡筛选
    mask_keep = ~((valid["agreeing"] == 0) & (valid["active"] >= 2))  # 全部分歧
    mask_keep &= ~(valid["active"] <= 1)  # 单个信号
    mask_keep &= ~((valid["agreeing"] < 3) & (valid["consistency"] < 0.2) & (valid["active"] >= 3))  # 低一致+低信号
    filtered_correct = correct_v7[mask_keep.values]
    filtered_acc = filtered_correct.mean() if len(filtered_correct) > 0 else 0
    print(f"  v7 震荡筛选后  : {filtered_acc:.1%} ({int(filtered_correct.sum())}/{len(filtered_correct)}) (保留 {mask_keep.sum()}/{len(mask_keep)} 天)")

    # 6. 生成报告
    print("\n\n── 报告摘要 ──")
    print(f"\n各信号独立准确率:")
    for r in sorted(results, key=lambda x: -x["accuracy"]):
        print(f"  {r['label']:16s}  {r['accuracy']:.1%}  (看多 {r['bull_accuracy']:.1%} / 看空 {r['bear_accuracy']:.1%})")

    print(f"\n最优权重组合 (近期):")
    print(f"  MA={best_recent['w_ma']}, RSI={best_recent['w_rsi']}, BB={best_recent['w_bb']}")
    print(f"  MACD={best_recent['w_macd']}, VOL={best_recent['w_vol']}")
    print(f"  方向准确率: {best_recent['acc']:.1%}")

    # 与现有权重对比
    current_w = {"MA": 1.6, "RSI": 2.2, "BB": 1.8, "MACD": 0.5, "VOL": 0.3}
    current_sum = sum(current_w.values())
    current_combined = sum(valid[c].values * w for c, w in zip(signals_cols, [1.6, 2.2, 1.8, 0.5, 0.3]))
    current_dir = np.sign(current_combined)
    current_correct = ((current_dir > 0) & (valid["next_chg_pct"].values > 0)) | ((current_dir < 0) & (valid["next_chg_pct"].values < 0))
    print(f"\n现有权重 (v7):   {current_w}")
    print(f"  方向准确率: {current_correct.mean():.1%}")

    # 保存结果
    out = {
        "timestamp": datetime.now().isoformat(),
        "total_samples": len(sig_df),
        "individual_signals": {r["col"]: {k: v for k, v in r.items() if k != "col"} for r in results},
        "optimal_weights_full": best_full,
        "optimal_weights_recent": best_recent,
        "current_weights": current_w,
        "current_accuracy": round(float(current_correct.mean()), 4),
        "no_filter_accuracy": round(float(no_filter_acc), 4),
        "v7_filtered_accuracy": round(float(filtered_acc), 4),
    }
    out_path = Path(__file__).parent / "history" / "signal_optimization.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()
