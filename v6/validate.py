"""
v6 验证追踪层

职责：
  - 用结构化 JSON 记录预测（不依赖文本正则解析）
  - 次日自动验证：读取 DCE 实际收盘 → 匹配预测 → 计算偏差 → 归因
  - 输出统计报告

记录格式：v6_predictions.json（每行一条预测记录，结构同 format_json_output）
验证后追加 verified 字段。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from config import PREDICTIONS_JSON, HISTORY_DIR


@dataclass
class VerificationResult:
    """单次验证结果"""
    input_date: str = ""
    session: str = ""         # "day" or "night"
    pred_base: float = 0.0
    pred_close: float = 0.0
    pred_low: float = 0.0
    pred_high: float = 0.0
    actual_close: float = 0.0
    deviation: float = 0.0
    deviation_pct: float = 0.0
    range_hit: bool = False
    direction_correct: bool = False
    confidence: str = ""
    pattern_note: str = ""    # 偏差模式描述


def _load_history() -> List[Dict[str, Any]]:
    """加载 v6 预测历史"""
    if not PREDICTIONS_JSON.exists():
        return []
    return json.loads(PREDICTIONS_JSON.read_text(encoding="utf-8"))


def _save_history(records: List[Dict[str, Any]]) -> None:
    """覆写 v6 预测历史"""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    PREDICTIONS_JSON.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_actual_close(on_date: str, corn_df: pd.DataFrame, session: str = "day") -> Optional[float]:
    """
    获取指定交易日指定 session 的实际收盘价。

    Args:
        on_date: 日期字符串 "YYYY-MM-DD"
        corn_df: DCE 玉米日线 DataFrame（需含 date, close 列）
        session: "day"（15:00 收盘）或 "night"（23:00 收盘）

    Returns:
        收盘价，或 None（未找到）
    """
    df = corn_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    row = df[df["date"].dt.strftime("%Y-%m-%d") == on_date]
    if row.empty:
        return None
    return float(row.iloc[-1]["close"])


def verify_all(corn_df: pd.DataFrame) -> List[VerificationResult]:
    """
    验证所有未验证的预测记录。

    Args:
        corn_df: DCE 玉米日线 DataFrame（包含预测日之后的实际数据）

    Returns:
        本次新验证的结果列表
    """
    records = _load_history()
    results = []

    for rec in records:
        if rec.get("verified"):
            continue

        input_date = rec.get("input_date", "")
        if not input_date:
            continue

        next_day = rec.get("next_trading_day", "")
        actual = get_actual_close(next_day, corn_df)
        if actual is None:
            continue  # 实际数据尚未就绪

        # ── 日盘验证 ──
        day = rec.get("day", {})
        if day and day.get("pred"):
            vr = VerificationResult(
                input_date=input_date,
                session="day",
                pred_base=day["base"],
                pred_close=day["pred"],
                pred_low=day["low"],
                pred_high=day["high"],
                actual_close=actual,
                deviation=round(actual - day["pred"], 2),
                deviation_pct=round((actual - day["pred"]) / day["pred"] * 100, 4) if day["pred"] else 0.0,
                range_hit=day["low"] <= actual <= day["high"],
                direction_correct=_check_direction(day.get("direction", ""), actual - day["base"]),
                confidence=rec.get("confidence", {}).get("level", "") or rec.get("signal", {}).get("confidence", ""),
            )
            results.append(vr)
            rec["verified"] = {
                "day": asdict(vr),
                "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }

        # ── 夜盘验证 ──
        night = rec.get("night", {})
        if night and night.get("pred"):
            night_actual = get_actual_close(next_day, corn_df, session="night")
            if night_actual is not None:
                vr_n = VerificationResult(
                    input_date=input_date,
                    session="night",
                    pred_base=night["base"],
                    pred_close=night["pred"],
                    pred_low=night["low"],
                    pred_high=night["high"],
                    actual_close=night_actual,
                    deviation=round(night_actual - night["pred"], 2),
                    deviation_pct=round((night_actual - night["pred"]) / night["pred"] * 100, 4) if night["pred"] else 0.0,
                    range_hit=night["low"] <= night_actual <= night["high"],
                    direction_correct=_check_direction(night.get("direction", ""), night_actual - night["base"]),
                    confidence=rec.get("confidence", {}).get("level", "") or rec.get("signal", {}).get("confidence", ""),
                )
                results.append(vr_n)
                if "verified" not in rec:
                    rec["verified"] = {}
                rec["verified"]["night"] = asdict(vr_n)
                rec["verified"]["verified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    _save_history(records)
    if results:
        try:
            from sim_trade import backfill_trading_log
            backfill_trading_log()
        except Exception:
            pass
    return results


def _check_direction(direction_text: str, actual_change: float) -> bool:
    """根据方向文本和实际涨跌判断方向是否正确"""
    if not direction_text:
        return False
    is_bullish = "偏强" in direction_text or "看涨" in direction_text
    is_bearish = "偏弱" in direction_text or "看跌" in direction_text
    if is_bullish and actual_change > 0:
        return True
    if is_bearish and actual_change < 0:
        return True
    # 震荡整理视作中性，不置错
    if "震荡整理" in direction_text or "↔" in direction_text:
        return True
    return False


def summary() -> Dict[str, Any]:
    """生成验证统计摘要"""
    records = _load_history()
    verified = [r for r in records if r.get("verified")]

    if not verified:
        return {
            "total_records": len(records),
            "verified_count": 0,
            "message": "尚无已验证记录",
        }

    day_results = []
    night_results = []

    for r in verified:
        v = r["verified"]
        if "day" in v:
            day_results.append(v["day"])
        if "night" in v:
            night_results.append(v["night"])

    def _stats(results):
        if not results:
            return {"count": 0}
        n = len(results)
        devs = [r["deviation"] for r in results]
        mean_dev = sum(devs) / n
        range_hits = sum(1 for r in results if r["range_hit"])
        dir_correct = sum(1 for r in results if r["direction_correct"])
        return {
            "count": n,
            "mean_deviation": round(mean_dev, 2),
            "range_hit_rate": f"{range_hits}/{n} ({range_hits/n*100:.0f}%)",
            "direction_accuracy": f"{dir_correct}/{n} ({dir_correct/n*100:.0f}%)",
        }

    return {
        "total_records": len(records),
        "verified_count": len(verified),
        "day": _stats(day_results),
        "night": _stats(night_results),
    }
