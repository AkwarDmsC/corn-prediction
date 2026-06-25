"""
v7 验证追踪层

职责：
  - 用结构化 JSON 记录预测
  - 次日自动验证：分钟K线优先，akshare日K线fallback
  - 输出验证统计 + 更新易读日报
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from v7.config import PREDICTIONS_JSON, HISTORY_DIR

REPORTS_FILE = HISTORY_DIR / "v7_reports.md"


def _dedup_key(result: Dict[str, Any]) -> str:
    """生成去重键: YYYY-MM-DD + session (day/night)"""
    now = result.get("generated_at", "")
    date_label = now[:10] if now else datetime.now().strftime("%Y-%m-%d")
    night = result.get("night", {})
    if night.get("ml_change") is not None or night.get("cbot_adj") is not None:
        session = "night"
    else:
        session = "day"
    return f"{date_label}-{session}"


def append_report(result: Dict[str, Any]) -> None:
    """追加一条易读的 Markdown 日报到 v7_reports.md（自动去重）"""
    sig = result.get("signal", {})
    day = result.get("day", {})
    night = result.get("night", {})
    hl = result.get("hl")
    ind = sig.get("indicators", {})
    eff = sig.get("effective_signals", {})

    dk = _dedup_key(result)
    REPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if REPORTS_FILE.exists():
        content = REPORTS_FILE.read_text(encoding="utf-8")
        marker = f"<!-- v7-dedup:{dk} -->"
        if marker in content:
            return

    lines = []
    now = result.get("generated_at", datetime.now().strftime("%Y-%m-%d %H:%M"))
    date_label = now[:10]

    lines.append("---")
    lines.append(f"## v7 日报 · {date_label}")
    lines.append("")
    lines.append(f"**生成时间**: {now}")
    lines.append(f"**收盘**: {sig.get('close', '?'):.0f} 元/吨")
    lines.append(f"**下一交易日**: {sig.get('next_trading_day', '?')}")
    lines.append("")
    lines.append("### 🌞 日盘")
    lines.append("| 项目 | 值 |")
    lines.append("|------|-----|")
    lines.append(f"| 方向 | {day.get('direction', '?')} |")
    lines.append(f"| 基准 | {day.get('base', '?'):.0f} |")
    lines.append(f"| 区间 | {day.get('low', '?'):.0f} ~ {day.get('high', '?'):.0f} |")
    lines.append(f"| 信号分歧 | {day.get('agreeing_count', '?')}/{day.get('total_active', '?')} |")
    if hl:
        lines.append(f"| ML高价 | {hl.get('pred_high', '?'):.0f} |")
        lines.append(f"| ML低价 | {hl.get('pred_low', '?'):.0f} |")
    lines.append("")

    lines.append("### 🌙 夜盘")
    lines.append("| 项目 | 值 |")
    lines.append("|------|-----|")
    lines.append(f"| 方向 | {night.get('direction', '?')} |")
    lines.append(f"| 基准 | {night.get('base', '?'):.0f} |")
    lines.append(f"| 区间 | {night.get('low', '?'):.0f} ~ {night.get('high', '?'):.0f} |")
    if "ml_change" in night:
        lines.append(f"| ML变化 | {night.get('ml_change', 0):+.1f} 元/吨 |")
        lines.append(f"| CBOT调整 | {night.get('cbot_adj', 0):+.1f} 元/吨 |")
    lines.append("")

    lines.append("### 📈 信号快照")
    lines.append(f"**置信度**: {sig.get('confidence', '?')} ({sig.get('confidence_pct', '?')})")
    if ind:
        lines.append("")
        lines.append("| 信号 | 值 | 状态 |")
        lines.append("|------|-----|------|")
        ma_dir = ind.get("ma_dir", 0)
        lines.append(f"| 均线 | {ind.get('ma5','?'):.0f}/{ind.get('ma10','?'):.0f}/{ind.get('ma20','?'):.0f} | {'多头' if ma_dir>0 else '空头' if ma_dir<0 else '混乱'} |")
        lines.append(f"| RSI(14) | {ind.get('rsi14','?'):.1f} | {'超买' if (ind.get('rsi14') or 50) > 65 else '超卖' if (ind.get('rsi14') or 50) < 35 else '正常'} |")
        lines.append(f"| 布林带 | {ind.get('bb_position','?'):.0f}% | {'上轨' if (ind.get('bb_position') or 0) > 80 else '下轨' if (ind.get('bb_position') or 0) < 20 else '中轨'} |")
        lines.append(f"| 成交量 | {ind.get('vol_ratio','?'):.1f}x | {'放量' if (ind.get('vol_ratio') or 0) > 1.2 else '缩量' if (ind.get('vol_ratio') or 0) < 0.8 else '正常'} |")
    if eff:
        active_signals = [(k, v) for k, v in eff.items() if v.get("weight", 0) > 0 and abs(v.get("value", 0)) > 0.05]
        if active_signals:
            lines.append("")
            lines.append("**活跃信号**:")
            for name, info in active_signals:
                dir_char = "📈" if info.get("value", 0) > 0 else "📉" if info.get("value", 0) < 0 else "➖"
                lines.append(f"- {dir_char} {name}: {info.get('value', 0):+.2f} (权重 {info.get('weight', 0):.1f})")
    lines.append("")

    lines.append("### 💡 模式")
    mode = "🔴 信号分歧 · 低置信"
    if day.get("agreeing_count", 0) >= 4 and abs(day.get("score", 0)) > 0.15:
        mode = "🟢 信号共振 · 有方向"
    elif "震荡整理" in day.get("direction", ""):
        mode = "🟡 震荡整理(信号分歧) · 不做方向判断"
    lines.append(mode)
    lines.append("")
    lines.append(f"**v7 版本**: {result.get('version', '?')} | v5 并行中")
    lines.append(f"**模型**: {'ML已加载' if hl else 'ML未加载'}")
    if result.get("model_errors"):
        for k, v in result["model_errors"].items():
            lines.append(f"- ⚠️ {k}模型: {v}")
    lines.append("")
    lines.append(f"<!-- v7-dedup:{dk} -->")
    with open(REPORTS_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


@dataclass
class VerificationResult:
    """单次验证结果"""
    input_date: str = ""
    session: str = ""
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
    pattern_note: str = ""


def _load_history() -> List[Dict[str, Any]]:
    if not PREDICTIONS_JSON.exists():
        return []
    return json.loads(PREDICTIONS_JSON.read_text(encoding="utf-8"))


def _save_history(records: List[Dict[str, Any]]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    PREDICTIONS_JSON.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fetch_minute_kline(on_date: str) -> Optional[dict]:
    """
    从分钟K线获取指定交易日的日盘/夜盘收盘。
    优先用分钟K线（更实时），不支持时返回 None。
    """
    try:
        import akshare as ak
        df = ak.futures_zh_minute_sina("C0", "5")
        df["date"] = df["datetime"].astype(str).str[:10]
        day_rows = df[df["date"] == on_date]
        if day_rows.empty:
            return None
        # 日盘：09:00~15:00 最后一根
        day_close = float(day_rows.iloc[-1]["close"])
        # 夜盘：21:00~23:00（如果有）
        night_rows = day_rows[
            day_rows["datetime"].astype(str).str[11:14].astype(int) >= 21
        ]
        night_close = float(night_rows.iloc[-1]["close"]) if not night_rows.empty else None
        return {"day": day_close, "night": night_close}
    except Exception:
        return None


def _get_actual_from_akshare(on_date: str) -> Optional[float]:
    """从 akshare 日K线获取收盘（fallback）"""
    try:
        import akshare as ak
        df = ak.futures_zh_daily_sina("C0")
        df["date"] = df["date"].astype(str)
        row = df[df["date"] == on_date]
        if row.empty:
            return None
        return float(row.iloc[-1]["close"])
    except Exception:
        return None


def verify_all() -> List[VerificationResult]:
    """
    验证所有未验证的预测记录。
    分钟K线优先 -> akshare日K线fallback。

    Returns:
        本次新验证的结果列表
    """
    records = _load_history()
    results = []

    for rec in records:
        if rec.get("verified"):
            continue

        input_date = rec.get("input_date", "")
        next_day = rec.get("next_trading_day", "")
        if not input_date or not next_day:
            continue

        minute = _fetch_minute_kline(next_day)
        day_actual = minute["day"] if minute else _get_actual_from_akshare(next_day)
        night_actual = minute.get("night") if minute else None

        if day_actual is None and night_actual is None:
            continue  # 数据尚未就绪

        rec["verified"] = {"verified_at": datetime.now().strftime("%Y-%m-%d %H:%M")}

        # ── 日盘验证 ──
        day = rec.get("day", {})
        if day and day.get("pred") and day_actual is not None:
            vr = VerificationResult(
                input_date=input_date,
                session="day",
                pred_base=day["base"],
                pred_close=day["pred"],
                pred_low=day["low"],
                pred_high=day["high"],
                actual_close=day_actual,
                deviation=round(day_actual - day["pred"], 2),
                deviation_pct=round(
                    (day_actual - day["pred"]) / day["pred"] * 100, 4
                ) if day["pred"] else 0.0,
                range_hit=day["low"] <= day_actual <= day["high"],
                direction_correct=_check_direction(
                    day.get("direction", ""), day_actual - day["base"]
                ),
                confidence=(
                    rec.get("signal", {}).get("confidence", "")
                ),
            )
            results.append(vr)
            rec["verified"]["day"] = asdict(vr)

        # ── 夜盘验证 ──
        night = rec.get("night", {})
        if night and night.get("pred") and night_actual is not None:
            vr_n = VerificationResult(
                input_date=input_date,
                session="night",
                pred_base=night["base"],
                pred_close=night["pred"],
                pred_low=night["low"],
                pred_high=night["high"],
                actual_close=night_actual,
                deviation=round(night_actual - night["pred"], 2),
                deviation_pct=round(
                    (night_actual - night["pred"]) / night["pred"] * 100, 4
                ) if night["pred"] else 0.0,
                range_hit=night["low"] <= night_actual <= night["high"],
                direction_correct=_check_direction(
                    night.get("direction", ""), night_actual - night["base"]
                ),
                confidence=(
                    rec.get("signal", {}).get("confidence", "")
                ),
            )
            results.append(vr_n)
            rec["verified"]["night"] = asdict(vr_n)

    _save_history(records)

    # 将验证结果写入 reports.md
    if results:
        _append_verify_report(results)

    return results


def _check_direction(direction_text: str, actual_change: float) -> bool:
    """方向判断是否正确"""
    if not direction_text:
        return False
    is_bullish = "偏强" in direction_text or "看涨" in direction_text
    is_bearish = "偏弱" in direction_text or "看跌" in direction_text
    if is_bullish and actual_change > 0:
        return True
    if is_bearish and actual_change < 0:
        return True
    # 震荡整理 / 中性 → 不置错
    if "震荡整理" in direction_text or "↔" in direction_text or "中性" in direction_text:
        return True
    return False


def _append_verify_report(results: List[VerificationResult]) -> None:
    """把验证结果追加到 v7_reports.md"""
    lines = []
    lines.append(f"### ✅ 验证确认 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"")

    for vr in results:
        tick = "✅" if vr.direction_correct else "❌"
        in_range = "✅区间内" if vr.range_hit else "❌区间外"
        lines.append(
            f"- **{vr.input_date}** {vr.session}: "
            f"预测{vr.pred_close:.0f}→实际{vr.actual_close:.0f} "
            f"偏差{vr.deviation:+.0f} "
            f"{in_range} | {tick}"
        )

    lines.append(f"")
    # 统计
    day_results = [r for r in results if r.session == "day"]
    night_results = [r for r in results if r.session == "night"]
    day_correct = sum(1 for r in day_results if r.direction_correct)
    night_correct = sum(1 for r in night_results if r.direction_correct)
    day_range = sum(1 for r in day_results if r.range_hit)
    night_range = sum(1 for r in night_results if r.range_hit)

    if day_results:
        lines.append(f"🌞 日盘: 方向{day_correct}/{len(day_results)} 区间{day_range}/{len(day_results)}")
    if night_results:
        lines.append(f"🌙 夜盘: 方向{night_correct}/{len(night_results)} 区间{night_range}/{len(night_results)}")

    lines.append(f"")

    REPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORTS_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


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

    day_results, night_results = [], []
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
