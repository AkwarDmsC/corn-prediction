#!/usr/bin/env python3
"""
v6 管线调度（pipeline.py）
cron 入口：编排 data -> signals -> predictor -> formatter -> validate

用法：
  python3 v6/pipeline.py [--run-ml] [--save]

依赖：
  所有 v6 模块均在 corn/v6/ 下，不依赖 v5.1 代码。
  --run-ml 需要 v6/models/*.pkl 存在。
  数据获取来自 data.py（Phase 2 后完整启用）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import PREDICTIONS_JSON, VERSION
from signals import compute_signal_snapshot
from predictor import analyze_corn_v6
from formatter import format_v51_output, format_json_output


def run_v6(
    corn_df: pd.DataFrame,
    *,
    soy_df: pd.DataFrame | None = None,
    cbot_chg: float | None = None,
    weather_score: float = 0.0,
    policy_signal: float = 0.0,
    day_session: dict | None = None,
    night_session: dict | None = None,
    run_ml: bool = True,
) -> dict:
    """执行完整 v6 预测管线"""
    now = datetime.now()
    result = analyze_corn_v6(
        corn_df,
        soy_df=soy_df,
        cbot_chg=cbot_chg,
        weather_score=weather_score,
        policy_signal=policy_signal,
        day_session=day_session,
        night_session=night_session,
        now=now,
        run_ml=run_ml,
    )
    return result


def append_json_record(result: dict) -> None:
    """追加一条预测记录到 v6_predictions.json"""
    PREDICTIONS_JSON.parent.mkdir(parents=True, exist_ok=True)
    record = format_json_output(result)
    record["source"] = f"pipeline.py {VERSION}"
    if PREDICTIONS_JSON.exists():
        history = json.loads(PREDICTIONS_JSON.read_text(encoding="utf-8"))
    else:
        history = []
    history.append(record)
    PREDICTIONS_JSON.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description="v6 玉米预测管线")
    parser.add_argument("--run-ml", action="store_true", default=False,
                        help="运行 ML 模型（默认跳过，避免缺少 .pkl 文件导致报错）")
    parser.add_argument("--save", action="store_true", default=False,
                        help="保存预测记录到 v6_predictions.json")
    args = parser.parse_args()

    from data import (
        fetch_cbot_corn,
        fetch_dce_corn,
        fetch_dce_soymeal,
        fetch_policy_news,
        fetch_weather,
    )

    corn_df, corn_meta = fetch_dce_corn()
    if corn_df.empty:
        print(f"v6 pipeline {VERSION} | 数据获取失败: {corn_meta.get('error') or 'DCE corn empty'}", file=sys.stderr)
        sys.exit(1)

    soy_df, soy_meta = fetch_dce_soymeal()
    _cbot_df, cbot_meta = fetch_cbot_corn()
    weather, weather_meta = fetch_weather()
    _news, news_meta = fetch_policy_news()

    result = run_v6(
        corn_df,
        soy_df=soy_df if not soy_df.empty else None,
        cbot_chg=cbot_meta.get("chg_pct"),
        weather_score=float(weather.get("weather_score", 0.0) or 0.0),
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
