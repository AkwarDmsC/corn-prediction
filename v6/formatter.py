"""
v6 输出格式化层
职责：纯输出渲染。不计算任何预测逻辑。
"""
from __future__ import annotations

from typing import Any, Dict

from signals import _direction_label


def format_v51_output(result: Dict[str, Any]) -> str:
    """格式化预测结果为中文文本（兼容 v5.1 输出格式）"""
    sig = result["signal"]
    ind = sig["indicators"]
    day = result["day"]
    night = result["night"]
    hl = result.get("hl")
    errors = result.get("model_errors", {})
    lines = [
        f"中国玉米期货综合预测 {result['version']} | {result['generated_at']}",
        f"数据日期: {result['input_date']} | 下一交易日: {result['next_trading_day']}",
        "",
        "1. 预测置信度: " + sig["confidence"],
        "2. 信号一致性评分: " + sig["confidence_pct"],
        "3. 主方向: " + _direction_label(sig["filtered_consistency"]),
        f"4. 参考区间: {result['full_day_range']['low']:.0f} ~ {result['full_day_range']['high']:.0f} 元/吨",
        "5. 概率分布: 基准60% / 乐观20% / 悲观20%",
        "6. 倾向判断: " + _direction_label(sig["filtered_consistency"]),
        "",
        "── 日盘(15:00) ──",
        f"  基准:{day['base']:.0f}元 → 预测:{day['pred']:.0f}({day['change_pct']:+.2f}%) | 区间:{day['low']:.0f}~{day['high']:.0f}",
        f"  方向:{day['direction']}",
        "── 夜盘(23:00) ──",
        f"  基准:{night['base']:.0f}元 → 预测:{night['pred']:.0f}({night['change_pct']:+.2f}%) | 区间:{night['low']:.0f}~{night['high']:.0f}",
        f"  方向:{night['direction']}",
        f"── 全日参考区间 ── {result['full_day_range']['low']:.0f} ~ {result['full_day_range']['high']:.0f} 元/吨",
        "",
        "7. 综合信号:",
        f"  MA5/10/20/60: {ind['ma5']:.0f}/{ind['ma10']:.0f}/{ind['ma20']:.0f}/{ind['ma60']:.0f}" if ind.get("ma60") else "  MA: 样本不足",
        f"  RSI14: {ind['rsi14']:.1f} | BB位置: {ind['bb_position']:.1f}% | MACD柱: {ind['macd_hist']:.2f}",
        f"  成交量比: {ind['vol_ratio']:.2f}x | MA60过滤: {sig['trend_filter']}",
        "8. 信号冲突标记: " + ("; ".join(sig["conflicts"]) if sig["conflicts"] else "无"),
        "9. 关键价位预警: " + _price_alert(ind, sig["close"]),
        "10. 基本面: CBOT/天气/政策由调用方传入；低置信外部信号保留展示但不加权",
        "11. 季节性: 已淘汰，不参与方向加权",
        f"12. 收盘价预测: 起点 {day['base']:.0f} / 基准 {day['pred']:.0f} / 乐观 {day['high']:.0f} / 悲观 {day['low']:.0f}",
        f"13. 数据时效提示: 下一交易日 {result['next_trading_day']}",
        "14. 偏差归因: 留待 prediction_tracker.py 验证后填写",
    ]
    if "ridge_pred" in night:
        lines.append(f"夜盘ML: Ridge={night['ridge_pred']:+.2f} RF={night['rf_pred']:+.2f} CBOT调整={night['cbot_adj']:+.1f}")
    if hl:
        lines.append(f"HL模型: 高价 {hl['pred_high']:.0f} | 低价 {hl['pred_low']:.0f} | 波幅 {hl['range']:.0f}")
    if errors:
        lines.append("模型回退: " + "; ".join(f"{k}={v}" for k, v in errors.items()))
    return "\n".join(lines)


def format_json_output(result: Dict[str, Any]) -> Dict[str, Any]:
    """返回结构化 JSON 输出（可直接序列化）"""
    sig = result["signal"]
    ind = sig["indicators"]
    day = result["day"]
    night = result["night"]
    hl = result.get("hl")
    return {
        "version": result["version"],
        "generated_at": result["generated_at"],
        "input_date": result["input_date"],
        "next_trading_day": result["next_trading_day"],
        "confidence": {
            "level": sig["confidence"],
            "pct": sig["confidence_pct"],
        },
        "day": {
            "base": day["base"],
            "pred": day["pred"],
            "low": day["low"],
            "high": day["high"],
            "change": day["change"],
            "change_pct": day["change_pct"],
            "direction": day["direction"],
        },
        "night": {
            "base": night["base"],
            "pred": night["pred"],
            "low": night["low"],
            "high": night["high"],
            "change": night.get("change"),
            "change_pct": night.get("change_pct"),
            "direction": night["direction"],
            "ml": {
                "ridge_pred": night.get("ridge_pred"),
                "rf_pred": night.get("rf_pred"),
                "ensemble_pred": night.get("ensemble_pred"),
                "ml_change": night.get("ml_change"),
                "cbot_adj": night.get("cbot_adj"),
            } if "ridge_pred" in night else None,
        },
        "hl": {
            "pred_high": hl["pred_high"],
            "pred_low": hl["pred_low"],
            "range": hl["range"],
        } if hl else None,
        "full_day_range": result["full_day_range"],
        "indicators": {
            "ma5": ind.get("ma5"),
            "ma10": ind.get("ma10"),
            "ma20": ind.get("ma20"),
            "ma60": ind.get("ma60"),
            "ma_dir": ind.get("ma_dir"),
            "rsi14": ind.get("rsi14"),
            "bb_position": ind.get("bb_position"),
            "macd_hist": ind.get("macd_hist"),
            "vol_ratio": ind.get("vol_ratio"),
            "trend_filter": sig["trend_filter"],
        },
        "signals": {k: {"value": v["value"], "weight": v["weight"]} for k, v in sig["signals"].items()},
        "effective_signals": {k: {"value": v["value"], "weight": v["weight"]} for k, v in sig["effective_signals"].items()},
        "conflicts": sig["conflicts"],
        "model_errors": result.get("model_errors", {}),
    }


def _price_alert(indicators: Dict[str, Any], close: float) -> str:
    """关键价位预警文本"""
    upper = indicators.get("bb_upper")
    lower = indicators.get("bb_lower")
    rsi_val = indicators.get("rsi14")
    bb_pos = indicators.get("bb_position")
    if upper and close >= upper * 0.995 and rsi_val and rsi_val > 65:
        return "接近布林上轨且RSI偏高，警惕回调"
    if lower and close <= lower * 1.005 and rsi_val and rsi_val < 35:
        return "接近布林下轨且RSI偏低，留意反弹"
    if bb_pos and bb_pos > 80:
        return "上轨附近"
    if bb_pos and bb_pos < 20:
        return "下轨附近"
    return "无明显突破"


__all__ = [
    "format_v51_output",
    "format_json_output",
]
