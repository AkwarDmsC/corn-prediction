#!/usr/bin/env python3
"""
v7.1 vs v7.0 回测对比

测试方法：对同一历史数据集，用旧权重和新权重分别回测，
比较方向准确率、区间命中率、震荡输出占比等。
"""

import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import akshare as ak

sys.path.insert(0, str(Path(__file__).parent.parent))
from v7.signals import compute_signal_snapshot, prepare_corn_df
from v7.decider import _count_agreement, _should_force_neutral, _scenario_from_score


def backtest_v71(corn_df, *, old_weights=False):
    """
    Walk-forward 回测。
    如果 old_weights=True，临时把 CORE_WEIGHTS 改成 v7.0 版本。
    否则用当前 v7.1 的权重。
    """
    # ── 配置 ──
    MIN_TRAIN = 200  # 最低训练天数
    
    from v7.config import CORE_WEIGHTS, DIRECTION_THRESHOLD, LOW_AGREEMENT_CUTOFF, LOW_CONSISTENCY_CUTOFF

    old_core_weights = {
        "MA": 1.6, "RSI": 2.2, "BB": 1.8,
        "SOY": 0.8, "POLICY": 1.0, "CBOT": 0.5,
        "MACD": 0.5, "VOLUME": 0.3,
        "WEATHER_NORMAL": 1.0, "WEATHER_EXTREME": 2.0,
    }
    
    df = prepare_corn_df(corn_df)
    results = []

    for i in range(MIN_TRAIN, len(df)):
        if i < MIN_TRAIN:
            continue
        train_df = df.iloc[:i+1]

        # 动态修改权重（hacky but works）
        import v7.signals as sig_module
        import v7.decider as dec_module
        old_cw = sig_module.CORE_WEIGHTS.copy()
        old_dt = sig_module.DIRECTION_THRESHOLD if hasattr(sig_module, 'DIRECTION_THRESHOLD') else DIRECTION_THRESHOLD
        
        if old_weights:
            sig_module.CORE_WEIGHTS = old_core_weights
            # 确保做多因子权重为 0
            sig_module.CORE_WEIGHTS["BB_LOWER_TOUCH"] = 0.0
            sig_module.CORE_WEIGHTS["CONSECUTIVE_DOWN"] = 0.0
            sig_module.CORE_WEIGHTS["GAP_DOWN"] = 0.0
        else:
            sig_module.CORE_WEIGHTS = CORE_WEIGHTS

        try:
            signal = compute_signal_snapshot(train_df)
            ind = signal["indicators"]
            close = float(train_df.iloc[-1]["close"])
            agreeing_count, total_active = _count_agreement(signal)
            score = signal["filtered_consistency"]

            day = _scenario_from_score(
                close, score, ind["recent_vol"],
                ind["bb_lower"], ind["bb_upper"],
                agreeing_count=agreeing_count,
                total_active=total_active,
            )
        except Exception as e:
            day = {"direction": "↔ 震荡整理", "low": close-20, "high": close+20, "score": 0}
        finally:
            sig_module.CORE_WEIGHTS = old_cw

        # 实际次日涨跌（实际值）
        if i + 1 < len(df):
            actual_close = float(df.iloc[i + 1]["close"])
            actual_chg_pct = (actual_close - close) / close * 100
            actual_dir = 1 if actual_chg_pct > 0 else -1 if actual_chg_pct < 0 else 0

            # 预测方向
            pred_is_neutral = "震荡" in day["direction"]
            if pred_is_neutral:
                pred_dir = 0
            else:
                pred_dir = 1 if "偏强" in day["direction"] else -1

            correct = (pred_dir == actual_dir) or (pred_is_neutral and abs(actual_chg_pct) < 0.2)

            # 区间命中
            in_range = day["low"] <= actual_close <= day["high"]

            results.append({
                "date": str(df.iloc[i]["date"].date()),
                "next_date": str(df.iloc[i+1]["date"].date()),
                "close": close,
                "actual_close": actual_close,
                "actual_chg_pct": actual_chg_pct,
                "pred_dir": pred_dir,
                "actual_dir": actual_dir,
                "pred_is_neutral": pred_is_neutral,
                "direction": day["direction"],
                "correct": correct,
                "in_range": in_range,
                "low": day["low"],
                "high": day["high"],
                "score": day["score"],
            })

    return pd.DataFrame(results)


def main():
    print("=" * 60)
    print("🌽 v7.1 vs v7.0 回测对比")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # 1. 获取数据
    print("\n[1/2] 加载 DCE 玉米日线...")
    df = ak.futures_zh_daily_sina("C0")

    # 2. 回测 v7.0 (旧权重)
    print("\n[2/2] 运行回测...")
    print("\n  回测 v7.0（旧权重）...")
    bt_old = backtest_v71(df, old_weights=True)
    print(f"    样本: {len(bt_old)}")

    print("  回测 v7.1（优化权重+做多因子）...")
    bt_new = backtest_v71(df, old_weights=False)
    print(f"    样本: {len(bt_new)}")

    # 3. 对比结果
    print("\n\n" + "=" * 60)
    print(" 📊 对比报告")
    print("=" * 60)

    for label, bt in [("v7.0（旧权重+MACD+VOL）", bt_old), ("v7.1（优化权重+做多因子）", bt_new)]:
        total = len(bt)
        correct_count = bt["correct"].sum()
        in_range_count = bt["in_range"].sum()
        neutral_count = bt["pred_is_neutral"].sum()
        non_neutral = bt[~bt["pred_is_neutral"]]
        neutral_ok = bt[bt["pred_is_neutral"] & (bt["actual_chg_pct"].abs() < 0.2)]
        non_neutral_correct = non_neutral["correct"].sum() if len(non_neutral) > 0 else 0

        print(f"\n  {label}")
        print(f"  {'─' * 40}")
        print(f"    总样本: {total}")
        print(f"    方向准确率（含震荡算正确）: {correct_count/total:.1%} ({int(correct_count)}/{total})")
        print(f"    区间命中率: {in_range_count/total:.1%} ({int(in_range_count)}/{total})")
        print(f"    震荡输出占比: {neutral_count/total:.1%} ({int(neutral_count)}/{total})")
        if len(non_neutral) > 0:
            print(f"    非震荡方向准确率: {non_neutral_correct/len(non_neutral):.1%} ({int(non_neutral_correct)}/{len(non_neutral)})")
            print(f"    | 做多准确率: {non_neutral[non_neutral['pred_dir']>0]['correct'].mean():.1%}")
            print(f"    | 做空准确率: {non_neutral[non_neutral['pred_dir']<0]['correct'].mean():.1%}")

    # 按年份细分
    print("\n\n  ── 按年份细分 (v7.1) ──")
    bt_new["year"] = bt_new["date"].str[:4]
    for year in sorted(bt_new["year"].unique()):
        sub = bt_new[bt_new["year"] == year]
        if len(sub) < 20:
            continue
        acc = sub["correct"].mean()
        range_acc = sub["in_range"].mean()
        neutral = sub["pred_is_neutral"].mean()
        nf = sub[~sub["pred_is_neutral"]]
        nf_acc = nf["correct"].mean() if len(nf) > 0 else 0
        print(f"    {year}: 总{len(sub):4d} 方向{acc:.1%} 区间{range_acc:.1%} 震荡{neutral:.1%} 非震荡{nf_acc:.1%}")

    # 保存结果
    print("\n\n  ── 保存结果 ──")
    out = {
        "timestamp": datetime.now().isoformat(),
        "v70": {
            "total": len(bt_old),
            "accuracy": round(float(bt_old["correct"].mean()), 4),
            "range_accuracy": round(float(bt_old["in_range"].mean()), 4),
            "neutral_pct": round(float(bt_old["pred_is_neutral"].mean()), 4),
            "non_neutral_accuracy": round(float(bt_old[~bt_old["pred_is_neutral"]]["correct"].mean()), 4) if len(bt_old[~bt_old["pred_is_neutral"]]) > 0 else 0,
        },
        "v71": {
            "total": len(bt_new),
            "accuracy": round(float(bt_new["correct"].mean()), 4),
            "range_accuracy": round(float(bt_new["in_range"].mean()), 4),
            "neutral_pct": round(float(bt_new["pred_is_neutral"].mean()), 4),
            "non_neutral_accuracy": round(float(bt_new[~bt_new["pred_is_neutral"]]["correct"].mean()), 4) if len(bt_new[~bt_new["pred_is_neutral"]]) > 0 else 0,
        },
    }

    out_path = Path(__file__).parent / "history" / "v71_vs_v70_backtest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"    已保存: {out_path}")


if __name__ == "__main__":
    import json as _mod_json
    if _mod_json is None:
        pass
    main()
