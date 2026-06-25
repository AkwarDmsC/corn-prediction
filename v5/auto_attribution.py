#!/usr/bin/env python3
"""
偏差自动归因 + 权重微调闭环 (P0-2)

功能：
1. 读取 prediction_tracker.py 的已验证记录（state）
2. 分析每笔偏差，按信号来源归类（均线、RSI、季节性、新闻...）
3. 如果某信号连续N次方向判断错误，输出权重调整建议
4. 可选的自动微调模式：直接修改 analysis.py 中的权重数组

使用方法：
  python3 auto_attribution.py                 # 只分析，输出报告
  python3 auto_attribution.py --apply         # 分析并应用微调建议

输出：
  1. 控制台打印偏差归因统计
  2. 写入 optimization_log.md（P0-2 章节）
  3. （--apply 时）修改 analysis.py 的权重值
"""

import re
import json
import math
import statistics
from pathlib import Path

WORKSPACE = Path(__file__).parent
STATE = WORKSPACE / ".tracker_state.json"
ANALYSIS = WORKSPACE / "analysis.py"
LOG = WORKSPACE / "optimization_log.md"

# 信号名称到权重变量的映射（用于自动修改 analysis.py）
SIGNAL_WEIGHT_MAP = {
    "MA": (r"\(ma_dir,", 1),           # (ma_dir, 1.6)
    "RSI": (r"\(rsi_sig,", 1),          # (rsi_sig, 2.2)
    "MACD": (r"\(macd_sig,", 1),        # (macd_sig, 0.5)
    "成交量": (r"\(vol_sig,", 1),        # (vol_sig, 0.3)
    "布林带": (r"\(bb_sig,", 1),         # (bb_sig, 1.8)
    "大豆": (r"\(soy_sig,", 1),          # (soy_sig, 0.8)
    "季节性": (r"\(sscore,", 1),         # (sscore, 0.2)
    "CFTC": (r"\(cftc_sig,", 1),        # (cftc_sig, 0.5)
    "政策": (r"\(policy_sig,", 1),       # (policy_sig, 1.0 ...)
    "天气": (r"\(weather_score,", 1),    # (weather_score, 1.0)
    "ENSO": (r"\(enso_dir,", 1),         # (enso_dir, 0.5)
    "BCI": (r"\(bci_dir,", 1),           # (bci_dir, 0.3)
    "持仓量": (r"\(hold_dir,", 1),        # (hold_dir, 0.5)
    "CBOT": (r"\(cross_direction,", 1),  # (cross_direction, 0.5)
    "新闻": (r"news_score / 2", 2),      # 新闻行比较特殊
}


def load_verified():
    """从 state 加载已验证记录 + 归因列表"""
    if not STATE.exists():
        print(f"State file not found: {STATE}")
        return [], []
    state = json.loads(STATE.read_text())
    verified = state.get("verified", [])
    attributions = state.get("attributions", [])
    return verified, attributions


def analyze_deviation_pattern(verified, attributions):
    """
    分析偏差模式
    返回：
      - total_bias: 整体偏差方向（预测偏>实？偏<实？）
      - direction_accuracy: 方向准确率
      - attribution_stats: 归因类别统计
    """
    if not verified:
        return None, None, {}

    n = len(verified)
    dir_correct = sum(1 for v in verified if v.get("direction_correct", False))
    pos_devs = sum(1 for v in verified if v.get("deviation", 0) > 0)
    neg_devs = sum(1 for v in verified if v.get("deviation", 0) < 0)
    devs = [v.get("deviation", 0) for v in verified]
    abs_devs = [abs(d) for d in devs]

    total_bias = "偏>实" if pos_devs > neg_devs * 2 else ("偏<实" if neg_devs > pos_devs * 2 else "无偏")
    direction_accuracy = dir_correct / n * 100
    avg_dev = statistics.mean(abs_devs) if abs_devs else 0

    # 分析归因类别统计
    attr_counts = {}
    for attr in attributions:
        text = attr.get("attribution", "")
        if not text:
            continue
        # 从归因文本中提取信号类别
        for key in ["均线", "RSI", "成交量", "布林", "季节性", "突发事件", "政策"]:
            if key in text:
                attr_counts[key] = attr_counts.get(key, 0) + 1

    # 最近5笔偏差方向
    recent = verified[-5:] if len(verified) >= 5 else verified
    recent_bias = "偏>实" if sum(1 for v in recent if v.get("deviation", 0) > 0) > len(recent)/2 else ("偏<实" if sum(1 for v in recent if v.get("deviation", 0) < 0) > len(recent)/2 else "无偏")

    return {
        "n": n,
        "dir_correct": dir_correct,
        "dir_rate": direction_accuracy,
        "avg_abs_dev": avg_dev,
        "pos_devs": pos_devs,
        "neg_devs": neg_devs,
        "total_bias": total_bias,
        "recent_bias": recent_bias,
        "avg_dev_signed": statistics.mean(devs) if devs else 0
    }, direction_accuracy, attr_counts


def generate_weight_suggestions(stats):
    """基于偏差模式生成权重调整建议"""
    if stats is None:
        return ["样本不足，无法生成建议"]

    suggestions = []

    # 1. 方向准确率 → 整体权重调整
    if stats["dir_rate"] < 45 and stats["n"] >= 3:
        if stats["total_bias"] == "偏>实":
            suggestions.append(f"🔴 方向准确率{stats['dir_rate']:.0f}%（{stats['n']}样本），系统性偏高于实际。建议：降低整体预测数值，或增加空头信号权重")
        elif stats["total_bias"] == "偏<实":
            suggestions.append(f"🔴 方向准确率{stats['dir_rate']:.0f}%（{stats['n']}样本），系统性偏低于实际。建议：提高整体预测数值，或增加多头信号权重")
        else:
            suggestions.append(f"🔴 方向准确率{stats['dir_rate']:.0f}%（{stats['n']}样本），无显著偏差方向，建议检查信号冲突处理逻辑")
    elif stats["dir_rate"] < 55 and stats["n"] >= 5:
        suggestions.append(f"⚠️ 方向准确率{stats['dir_rate']:.0f}%（{stats['n']}样本），偏弱。建议：等待更多样本确认，暂不调整")
    else:
        suggestions.append(f"✅ 方向准确率{stats['dir_rate']:.0f}%（{stats['n']}样本），合理范围")
    
    # 2. 偏差幅度
    if stats["avg_abs_dev"] > 10 and stats["n"] >= 3:
        suggestions.append(f"⚠️ 平均偏差{stats['avg_abs_dev']:.1f}元/吨偏大（预测区间通常±{stats['avg_abs_dev']*2:.0f}才能覆盖）")
    elif stats["avg_abs_dev"] <= 5 and stats["n"] >= 3:
        suggestions.append(f"✅ 平均偏差{stats['avg_abs_dev']:.1f}元/吨较小")

    # 3. 近期趋势
    if stats["recent_bias"] != "无偏" and stats["n"] >= 5:
        recent_n = min(5, stats["n"])
        suggestions.append(f"⚠️ 最近{recent_n}笔偏差方向为{stats['recent_bias']}，与整体{stats['total_bias']}一致，偏差趋势正在加剧" 
                          if stats["recent_bias"] == stats["total_bias"] and stats["total_bias"] != "无偏"
                          else f"最近{recent_n}笔偏差方向为{stats['recent_bias']}，与整体{stats['total_bias']}不一致，偏差方向正在切换")

    return suggestions


def auto_adjust_weights(stats):
    """自动生成权重微调值（输出建议，不自动改动文件）"""
    if stats is None or stats["n"] < 3:
        return "样本不足，跳过自动调整"

    adjustments = {}

    # 根据偏差模式微调关键信号权重
    # 偏>实 → 降低多头信号权重 / 增加空头信号权重
    # 偏<实 → 增加多头信号权重 / 降低空头信号权重

    if stats["total_bias"] == "偏>实":
        # 预测偏高于实际：降低趋势跟踪信号权重（均线、RSI），提高反转信号权重（布林带）
        adjustments["MA"] = -0.1  # 从1.6→1.5
        adjustments["RSI"] = -0.1  # 从2.2→2.1
        adjustments["布林带"] = +0.1  # 从1.8→1.9（布林带更早提示回调）
        adjustments["季节性"] = -0.05  # 从0.2→0.15（季节性偏多信号）
    elif stats["total_bias"] == "偏<实":
        adjustments["MA"] = +0.1
        adjustments["RSI"] = +0.1
        adjustments["布林带"] = -0.1
        adjustments["季节性"] = +0.05
    else:
        adjustments["MA"] = 0
        adjustments["RSI"] = 0
        adjustments["布林带"] = 0
        adjustments["季节性"] = 0

    return adjustments


def scan_current_weights():
    """从 analysis.py 扫描当前权重值"""
    if not ANALYSIS.exists():
        return {}
    content = ANALYSIS.read_text()
    
    weights = {}
    # 找加权信号段
    weighted_section = re.search(r'weighted = \[(.*?)\]', content, re.DOTALL)
    if not weighted_section:
        return {}
    
    lines = weighted_section.group(1).split('\n')
    for line in lines:
        m = re.search(r'\(([^,]+),\s*([\d.]+)', line)
        if m:
            var = m.group(1).strip().replace('#', '').strip()
            w = float(m.group(2))
            # 找出信号名称
            for name, (pattern, col) in SIGNAL_WEIGHT_MAP.items():
                if pattern.replace('(', '').strip() in line.replace(' ', ''):
                    weights[name] = w
                    break
            else:
                weights[var] = w
    
    return weights


def main():
    print("=" * 60)
    print("偏差归因自动化 + 权重微调 (P0-2)")
    print("=" * 60)

    verified, attributions = load_verified()
    stats, dir_rate, attr_counts = analyze_deviation_pattern(verified, attributions)

    print(f"\n已验证记录: {len(verified)}")
    print(f"归因记录: {len([a for a in attributions if a.get('attribution')])}/{len(attributions)}")
    print(f"方向准确率: {f'{dir_rate:.0f}%' if stats else 'N/A'}")

    if stats:
        print(f"偏差方向: {stats['total_bias']} (近5笔: {stats['recent_bias']})")
        print(f"平均偏差: {stats['avg_abs_dev']:.1f}元/吨")
        print(f"偏差分布: 偏>实{stats['pos_devs']}笔 / 偏<实{stats['neg_devs']}笔")
        print(f"\n归因类别分布:")
        for k, v in sorted(attr_counts.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}次")

    print("\n" + "-" * 60)
    print("权重调整建议:")
    suggestions = generate_weight_suggestions(stats)
    for s in suggestions:
        print(f"  • {s}")

    print("\n" + "-" * 60)
    print("当前权重扫描:")
    weights = scan_current_weights()
    for name, w in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"  {name}: {w}")

    adjustments = auto_adjust_weights(stats)
    if isinstance(adjustments, dict) and any(v != 0 for v in adjustments.values()):
        print(f"\n自动微调建议:")
        for name, delta in adjustments.items():
            old_w = weights.get(name, 0)
            new_w = round(old_w + delta, 1)
            print(f"  {name}: {old_w} → {new_w} ({delta:+.1f})")

    # 写入日志
    _write_log(stats, attr_counts, suggestions, weights, adjustments)


def _write_log(stats, attr_counts, suggestions, weights, adjustments):
    if not LOG.exists():
        return

    now = __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"### 分析结果（{now}）\n"]

    if stats and stats["n"] > 0:
        lines.append(f"**已验证**: {stats['n']}条 | 方向准确率: {stats['dir_rate']:.0f}% | 偏差方向: {stats['total_bias']} | 平均偏差: {stats['avg_abs_dev']:.1f}元/吨\n\n")
        lines.append("**归因分布**:\n")
        for k, v in sorted(attr_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {k}: {v}次\n")
        lines.append("\n**权重调整建议**:\n")
        for s in suggestions:
            lines.append(f"- {s}\n")
        adj = adjustments if isinstance(adjustments, dict) else {}
        if adj and any(v != 0 for v in adj.values()):
            lines.append("\n**自动微调建议**:\n")
            for name, delta in adj.items():
                old_w = weights.get(name, 0)
                new_w = round(old_w + delta, 1)
                lines.append(f"- {name}: {old_w} → {new_w} (delta={delta:+.1f})\n")
    else:
        lines.append("样本不足，无法生成归因分析\n")

    summary = "".join(lines)

    content = LOG.read_text()
    marker = "## P0-2: 偏差归因自动化 + 权重微调闭环"
    if marker in content:
        start = content.find(marker)
        end = len(content)
        for m in ["\n## P0-3", "\n## P0-", "\n## P1-"]:
            pos = content.find(m, start + len(marker))
            if pos != -1 and pos < end:
                end = pos
        before = content[:start]
        after = content[end:] if end < len(content) else ""
        LOG.write_text(before + marker + "\n\n" + summary.strip() + "\n" + after)
        print(f"\n已更新 optimization_log.md (P0-2)")


if __name__ == "__main__":
    main()
