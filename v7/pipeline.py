#!/usr/bin/env python3
"""
v7 管线调度 — cron 入口
用法：
  python3 -m v7.pipeline [--run-ml] [--save]

依赖：
  v7/ 下所有模块，模型文件软链到 v7/models/
  数据获取来自 data.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import pandas as pd

from v7.config import PREDICTIONS_JSON, VERSION
from v7.decider import analyze_corn_v7
from v7.validate import append_report, verify_all


def run_v7(
    corn_df,
    *,
    soy_df=None,
    cbot_chg=None,
    weather_score=0.0,
    policy_signal=0.0,
    day_session=None,
    night_session=None,
    run_ml=True,
):
    """执行完整 v7 预测管线"""
    return analyze_corn_v7(
        corn_df,
        soy_df=soy_df,
        cbot_chg=cbot_chg,
        weather_score=weather_score,
        policy_signal=policy_signal,
        day_session=day_session,
        night_session=night_session,
        run_ml=run_ml,
    )


def format_v51_output(result: dict) -> str:
    """格式化输出 — v7.1 清晰版，微信端友好"""
    sig = result["signal"]
    day = result["day"]
    night = result["night"]
    hl = result.get("hl")
    ind = sig.get("indicators", {})
    
    close = f"{sig['close']:.0f}"
    ntd = sig['next_trading_day']
    
    # 日盘方向指示
    day_dir = day['direction']
    day_icon = "↗️" if "偏强" in day_dir else "↘️" if "偏弱" in day_dir else "➡️"
    night_dir = night['direction']
    night_icon = "↗️" if "偏强" in night_dir else "↘️" if "偏弱" in night_dir else "➡️"
    
    lines = []
    lines.append(f"🌽 DCE玉米日报 | {result.get('generated_at', '')[:10]}")
    lines.append("")
    
    # 1. 行情概览
    ma_state = "多头" if ind.get('ma_dir',0) > 0 else "空头" if ind.get('ma_dir',0) < 0 else "震荡"
    rsi_v = f"{ind['rsi14']:.0f}" if ind.get('rsi14') is not None else "?"
    bb_v = f"{ind['bb_position']:.0f}%"
    lines.append(f"📊 {close} 元/吨 · 均线{ma_state} · RSI {rsi_v} · 布林{bb_v}")
    lines.append("")
    
    # 2. 日盘预测
    day_range = f"{day['low']:.0f}–{day['high']:.0f}"
    day_strip = day_dir.replace("↗ ", "").replace("↘ ", "").replace("↔ ", "")
    day_agree = f"{day.get('agreeing_count',0)}/{day.get('total_active',0)}" if day.get('agreeing_count',0) > 0 else "—"
    lines.append(f"🌞 日盘 → {ntd}")
    lines.append(f"   {day_icon} {day_strip}")
    lines.append(f"   区间 {day_range}")
    if hl:
        lines.append(f"   ML {hl['pred_low']:.0f}–{hl['pred_high']:.0f}")
    lines.append(f"   信号 {day_agree}")
    
    # 3. 夜盘预测
    night_range = f"{night['low']:.0f}–{night['high']:.0f}"
    night_strip = night_dir.replace("↗ ", "").replace("↘ ", "").replace("↔ ", "")
    ml_info = ""
    if night.get("ml_change") is not None:
        ml_info = f" (ML {night['ml_change']:+.0f}元)"
    elif night.get("change") and "ml" not in str(night.get("session","")):
        ml_info = ""
    lines.append(f"🌙 夜盘")
    lines.append(f"   {night_icon} {night_strip}{ml_info}")
    lines.append(f"   区间 {night_range}")
    lines.append("")
    
    # 4. 置信度
    conf = f"{sig['confidence']} {sig['confidence_pct']}" if sig.get('confidence_pct') else "—"
    lines.append(f"📈 置信度 {conf}")
    lines.append("")
    
    # 5. 版本脚注
    lines.append(f"{result.get('version','7')} · {'ML已加载' if hl else '规则引擎'} · 🐴")
    return "\n".join(lines)


def _json_dedup_key(result: dict) -> str:
    """JSON 去重键: date + session"""
    gt = result.get("generated_at", "")
    date_label = gt[:10] if gt else datetime.now().strftime("%Y-%m-%d")
    # 判断 session
    night = result.get("night", {})
    if night.get("ml_change") is not None or night.get("cbot_adj") is not None:
        session = "night"
    else:
        session = "day"
    return f"{date_label}-{session}"


def append_json_record(result: dict) -> None:
    """追加预测记录 + 易读日报 + 自动验证已到期的记录（自动去重）"""
    PREDICTIONS_JSON.parent.mkdir(parents=True, exist_ok=True)
    if PREDICTIONS_JSON.exists():
        history = json.loads(PREDICTIONS_JSON.read_text(encoding="utf-8"))
    else:
        history = []
    # ── 去重 ──
    dk = _json_dedup_key(result)
    for existing in history:
        if _json_dedup_key(existing) == dk:
            # 已有同 session 记录，跳过
            return
    history.append(result)
    PREDICTIONS_JSON.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # 易读日报
    append_report(result)
    # 自动验证尚未验证的历史记录（分钟K线优先）
    v_results = verify_all()
    if v_results:
        print(f"[验证] 本次验证了 {len(v_results)} 条记录")
        for vr in v_results:
            tick = "✅" if vr.direction_correct else "❌"
            print(f"  {vr.input_date} {vr.session}: 预测{vr.pred_close:.0f}→实际{vr.actual_close:.0f} 偏差{vr.deviation:+.0f} {tick}")


def main():
    parser = argparse.ArgumentParser(description="v7 玉米预测管线")
    parser.add_argument("--run-ml", action="store_true", default=False)
    parser.add_argument("--save", action="store_true", default=False)
    args = parser.parse_args()

    from v7.data import (
        fetch_dce_corn,
        fetch_dce_soymeal,
        fetch_cbot_corn,
        fetch_weather,
        fetch_policy_news,
    )

    # 数据获取（容错：任何外部数据源失败都不影响核心玉米分析）
    corn_df, corn_meta = fetch_dce_corn()
    if corn_df.empty:
        print(f"v7 pipeline {VERSION} | 数据获取失败: {corn_meta.get('error') or 'empty'}", file=sys.stderr)
        sys.exit(1)

    soy_df = pd.DataFrame()
    soy_meta = {"source": "none"}
    cbot_meta = {"source": "none"}
    weather_data = {}
    news_meta = {"source": "none"}
    try:
        soy_df, soy_meta = fetch_dce_soymeal()
    except Exception as e:
        print(f"  [警告] 豆粕获取失败: {e}", file=sys.stderr)
    try:
        _cbot_df, cbot_meta = fetch_cbot_corn()
    except Exception as e:
        print(f"  [警告] CBOT 获取失败: {e}", file=sys.stderr)
    try:
        weather_data, weather_meta = fetch_weather()
    except Exception as e:
        print(f"  [警告] 天气获取失败: {e}", file=sys.stderr)
    try:
        _news, news_meta = fetch_policy_news()
    except Exception as e:
        print(f"  [警告] 政策新闻获取失败: {e}", file=sys.stderr)

    result = run_v7(
        corn_df,
        soy_df=soy_df if not soy_df.empty else None,
        cbot_chg=cbot_meta.get("chg_pct"),
        weather_score=float(weather_data.get("weather_score", 0.0) or 0.0),
        policy_signal=float(news_meta.get("policy_signal", 0.0) or 0.0),
        run_ml=args.run_ml,
    )
    result["data_metadata"] = {
        "corn": corn_meta,
        "soymeal": soy_meta,
        "cbot_corn": cbot_meta,
        "weather": weather_meta,
        "policy_news": news_meta,
    }

    if args.save:
        append_json_record(result)

    print(format_v51_output(result))


if __name__ == "__main__":
    main()
