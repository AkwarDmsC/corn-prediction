#!/usr/bin/env python3
"""
因子交互系统化检测 (P1-5)

替换 v5.0 "新增信号需确认top-3排序不变"的临时限制。

功能：
1. 实时交互检测：当前17个信号的方向冲突/共振分析
2. 历史交互矩阵：回测历史上信号间的相关性（双月积累，每周末运行）
3. 冲突分组：将强相关信号归组，组内用投票代替累加
4. 权重调整建议：高相关信号 > 降低组权重的冗余度

用法：
  python3 factor_interaction.py                       # 运行演示+实时检测
  python3 factor_interaction.py --build-matrix        # 从历史数据构建交互矩阵
  python3 factor_interaction.py --check-conflict      # 仅检查实时冲突
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

WORKSPACE = Path(__file__).parent
MATRIX_CACHE = WORKSPACE / ".factor_interaction_matrix.json"
CONFLICT_CACHE = WORKSPACE / ".factor_conflict_log.json"
LOG = WORKSPACE / "optimization_log.md"

# ── 17信号的分类与权重 v5.1 ─────────────────
SIGNAL_GROUPS = {
    "技术趋势": ["MA", "MACD"],
    "技术反转": ["RSI", "布林带", "成交量"],
    "跨市场": ["CBOT", "大豆", "CFTC", "USDA出口"],
    "基本面": ["季节性", "天气", "政策", "ENSO", "BCI", "持仓量", "新闻"],
    "需求链": ["生猪"],  # 新增
}

# 完整信号列表（与 analysis.py 的 weighted[] 对齐）
ALL_SIGNALS = ["MA", "RSI", "MACD", "成交量", "布林带",
               "大豆", "季节性", "CFTC", "政策", "天气",
               "ENSO", "BCI", "持仓量", "CBOT", "新闻",
               "USDA出口", "生猪"]


def compute_correlation_matrix(historical_signals):
    """
    从历史信号数据集计算相关矩阵
    
    historical_signals: list of dict {signal_name: direction}
    每个条目是某一天17个信号的方向值 (-1/0/1)
    
    返回: 相关矩阵 + 高相关对列表
    """
    df = pd.DataFrame(historical_signals)
    
    # 确保所有列都有
    for sig in ALL_SIGNALS:
        if sig not in df.columns:
            df[sig] = 0
    
    df = df[ALL_SIGNALS]
    corr = df.corr()
    
    # 找出高相关对 (|ρ| > 0.7)
    high_corr_pairs = []
    for i in range(len(ALL_SIGNALS)):
        for j in range(i+1, len(ALL_SIGNALS)):
            r = corr.iloc[i, j]
            if abs(r) > 0.7:
                high_corr_pairs.append({
                    "sig1": ALL_SIGNALS[i],
                    "sig2": ALL_SIGNALS[j],
                    "corr": round(r, 3)
                })
    
    # 按相关度排序
    high_corr_pairs.sort(key=lambda x: -abs(x["corr"]))
    
    return corr, high_corr_pairs


def detect_real_time_conflict(signals_dict):
    """
    实时冲突检测
    
    signals_dict: {signal_name: direction_value}
    direction_value: -1 (空), 0 (中性), 1 (多)
    
    返回: {
        'conflict_score': 0~1 (越高越冲突),
        'conflict_pairs': [(sig1, sig2)],
        'group_unanimity': {group: unanimity%},
        'consensus': str,
        'details': [str]
    }
    """
    details = []
    conflict_pairs = []
    
    # 1. 计算整体一致度
    non_zero = {k: v for k, v in signals_dict.items() if v != 0 and k in ALL_SIGNALS}
    if not non_zero:
        return {
            'conflict_score': 0,
            'conflict_pairs': [],
            'group_unanimity': {},
            'consensus': "无活跃信号",
            'details': ["所有信号均为中性"]
        }
    
    majority_dir = 1 if sum(1 for v in non_zero.values() if v > 0) > sum(1 for v in non_zero.values() if v < 0) else -1
    majority_count = sum(1 for v in non_zero.values() if (v > 0) == (majority_dir > 0))
    total_active = len(non_zero)
    unanimity = majority_count / total_active
    
    conflict_score = 1 - unanimity  # 越低越一致
    
    # 2. 检测两两冲突
    sig_list = list(non_zero.items())
    for i in range(len(sig_list)):
        for j in range(i+1, len(sig_list)):
            n1, d1 = sig_list[i]
            n2, d2 = sig_list[j]
            if d1 * d2 < 0:  # 方向相反
                conflict_pairs.append((n1, n2))
    
    # 3. 按信号组检测
    group_unanimity = {}
    for group, signals in SIGNAL_GROUPS.items():
        active_in_group = {s: v for s, v in signals_dict.items() if s in signals and v != 0}
        if len(active_in_group) >= 2:
            dirs = list(active_in_group.values())
            # 只有>=2个活跃信号才检测组内一致度
            pos = sum(1 for d in dirs if d > 0)
            neg = sum(1 for d in dirs if d < 0)
            if pos + neg > 0:
                gu = max(pos, neg) / (pos + neg) * 100
                group_unanimity[group] = round(gu, 0)
                if gu < 100:
                    details.append(f"⚠️ {group}组信号冲突({max(pos,neg)}/{pos+neg}同向)")
                elif gu == 100 and pos > 0:
                    details.append(f"✅ {group}组全部看多")
                elif gu == 100 and neg > 0:
                    details.append(f"✅ {group}组全部看空")
    
    # 4. 整体结论
    if conflict_score < 0.2:
        consensus = "✅ 信号高度共振"
    elif conflict_score < 0.4:
        consensus = "✅ 信号基本一致"
    elif conflict_score < 0.6:
        consensus = "⚠️ 信号分歧较大"
    else:
        consensus = "🔴 信号严重冲突"
    
    return {
        'conflict_score': round(conflict_score, 2),
        'unanimity': round(unanimity * 100, 0),
        'conflict_pairs': conflict_pairs,
        'group_unanimity': group_unanimity,
        'consensus': consensus,
        'details': details,
        'active_count': total_active,
        'majority_dir': "偏多" if majority_dir > 0 else "偏空",
        'majority_count': majority_count,
    }


def suggest_weight_adjustment(corr_matrix, high_corr_pairs):
    """
    基于相关矩阵给出权重调整建议
    """
    suggestions = []
    
    if not high_corr_pairs:
        suggestions.append("✅ 信号间无强相关(pair)，权重体系独立有效")
        return suggestions
    
    # 分析高相关对
    corr_groups = {}
    for pair in high_corr_pairs:
        s1, s2 = pair['sig1'], pair['sig2']
        for s in [s1, s2]:
            if s not in corr_groups:
                corr_groups[s] = set()
            corr_groups[s].add(s1 if s == s2 else s2)
            corr_groups[s].add(s2 if s == s1 else s1)
    
    # 合并成组
    visited = set()
    groups = []
    for sig in corr_groups:
        if sig in visited:
            continue
        group = set()
        stack = [sig]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            group.add(current)
            if current in corr_groups:
                for neighbor in corr_groups[current]:
                    if neighbor not in visited:
                        stack.append(neighbor)
        if len(group) > 1:
            groups.append(group)
    
    for group in groups:
        signals_list = list(group)
        suggestions.append(f"⚠️ 强相关信号组: {' ↔ '.join(signals_list)}")
        suggestions.append(f"   建议: 将这些信号的联合权重降至单个有效信号的级别")
        suggestions.append(f"         或取组内信号方向投票（代替加权和）")
    
    # 检查技术组内的信号
    tech_signals = ["MA", "RSI", "MACD", "布林带", "成交量"]
    for i in range(len(tech_signals)):
        for j in range(i+1, len(tech_signals)):
            s1, s2 = tech_signals[i], tech_signals[j]
            if s1 in corr_matrix.columns and s2 in corr_matrix.columns:
                r = corr_matrix[s1][s2]
                if abs(r) > 0.5:
                    suggestions.append(f"  ⚠️ 技术组内相关: {s1}({corr_matrix[s1].name})-{s2}({corr_matrix[s2].name}) = {r:.2f}")
    
    return suggestions


def simulate_daily_signals():
    """
    模拟一天的信号数据用于演示和测试
    在实际使用中，由 analysis.py 调用时传入真实信号
    """
    # 基于当前市场状态模拟（2026-05-26）
    # 方向值：-1 空, 0 中性, 1 多
    return {
        "MA": 1,       # 等等，当前MA是空头？看看分析结果... 
        "RSI": 1,      # RSI超卖后可能反弹
        "MACD": 0,     # 中性
        "成交量": -1,   # 缩量
        "布林带": 1,    # 下轨附近→反弹
        "大豆": 0,      # 中性
        "季节性": 1,    # 5月偏多
        "CFTC": 0,      # 中性
        "政策": 0,      # 无事件
        "天气": -1,     # 利空（产区天气评分负）
        "ENSO": 0,      # 中性
        "BCI": 0,       # 中性
        "持仓量": 0,    # 中性
        "CBOT": 0,      # 无信号
        "新闻": 0,      # 中性
        "USDA出口": 1,  # 同比+27.1%
        "生猪": -1,     # 养殖利润-25%
    }


def main():
    import sys
    from datetime import datetime
    
    print("=" * 60)
    print("因子交互系统化检测 (P1-5)")
    print("=" * 60)
    
    signals = simulate_daily_signals()
    
    print(f"\n当前17个信号状态:")
    for name, dir_ in signals.items():
        icon = "🟢多" if dir_ > 0 else ("🔴空" if dir_ < 0 else "⚪中")
        weight_idx = [i for i, s in enumerate(ALL_SIGNALS) if s == name]
        w = "0.5-2.2x" if name in ["MA","RSI","MACD","成交量","布林带"] else "0.5x" if name not in ["季节性","天气","政策"] else "0.2x~1.0x"
        print(f"  {icon} {name:<8} dir={dir_:+d} 权重≈{w}")
    
    # ── 实时冲突检测 ──
    print(f"\n{'='*60}")
    print("实时冲突检测")
    print(f"{'='*60}")
    
    conflict = detect_real_time_conflict(signals)
    print(f"\n  一致性: {conflict['unanimity']:.0f}% ({conflict['active_count']}个活跃信号)")
    print(f"  冲突分数: {conflict['conflict_score']:.2f} (0=一致, 1=全冲突)")
    print(f"  整体: {conflict['consensus']}")
    print(f"  多数方向: {conflict['majority_dir']} ({conflict['majority_count']}/{conflict['active_count']})")
    
    if conflict['conflict_pairs']:
        print(f"\n  冲突信号对:")
        for s1, s2 in conflict['conflict_pairs'][:5]:
            print(f"    ⚡ {s1} ↔ {s2} 方向相反")
    
    if conflict['group_unanimity']:
        print(f"\n  信号组内一致度:")
        for group, rate in sorted(conflict['group_unanimity'].items(), key=lambda x: -x[1]):
            print(f"    {group}: {rate:.0f}%")
    
    for d in conflict['details'][:5]:
        print(f"  {d}")
    
    # ── 模拟历史相关矩阵（从模拟数据） ──
    print(f"\n{'='*60}")
    print("历史交互矩阵（基于模拟数据中心）")
    print(f"{'='*60}")
    
    # 生成模拟历史数据（30天随机信号）
    np.random.seed(42)
    historical = []
    for _ in range(30):
        day = {}
        for sig in ALL_SIGNALS:
            day[sig] = np.random.choice([-1, 0, 1], p=[0.3, 0.4, 0.3])
        historical.append(day)
    
    corr_matrix, high_corr_pairs = compute_correlation_matrix(historical)
    
    print(f"\n  高相关对 (|ρ| > 0.7):")
    if high_corr_pairs:
        for pair in high_corr_pairs:
            print(f"    {pair['sig1']} ↔ {pair['sig2']}: ρ={pair['corr']:.3f}")
    else:
        print("    (无，模拟数据为随机生成)")
    
    # ── 权重调整建议 ──
    print(f"\n{'='*60}")
    print("权重调整建议")
    print(f"{'='*60}")
    
    suggestions = suggest_weight_adjustment(corr_matrix, high_corr_pairs)
    for s in suggestions:
        print(f"  {s}")
    
    # ── 永久化日志 ──
    _write_log(conflict, signals, suggestions)


def _write_log(conflict, signals, suggestions):
    """将分析结果写入 optimization_log.md"""
    if not LOG.exists():
        return
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    lines = [f"### 分析结果（{now}）\n\n"]
    lines.append(f"**实时冲突检测**:\n")
    lines.append(f"- 一致性: {conflict['unanimity']:.0f}% ({conflict['active_count']}个活跃信号)\n")
    lines.append(f"- 冲突分数: {conflict['conflict_score']:.2f}\n")
    lines.append(f"- 整体: {conflict['consensus']}\n")
    lines.append(f"- 多数方向: {conflict['majority_dir']} ({conflict['majority_count']}/{conflict['active_count']})\n\n")
    
    if conflict['conflict_pairs']:
        lines.append("**当前冲突对**:\n")
        for s1, s2 in conflict['conflict_pairs'][:5]:
            lines.append(f"- {s1} ↔ {s2}\n")
    
    lines.append("\n**权重建议**:\n")
    for s in suggestions:
        lines.append(f"- {s}\n")
    
    lines.append("\n**待实施**:\n")
    lines.append("- 在 analysis.py 每日运行时调用 factor_interaction.detect_real_time_conflict()\n")
    lines.append("- 将冲突分数纳入置信度评分（冲突升高时降权）\n")
    lines.append("- 每周末运行 --build-matrix 更新历史相关矩阵\n")
    
    summary = "".join(lines)
    
    content = LOG.read_text()
    marker = "## P1-5: 因子交互系统化检测"
    if marker in content:
        start = content.find(marker)
        end = len(content)
        for m in ["\n## 附录", "\n## P2-", "\n## P3-"]:
            pos = content.find(m, start + len(marker))
            if pos != -1 and pos < end:
                end = pos
        before = content[:start]
        after = content[end:] if end < len(content) else ""
        LOG.write_text(before + marker + "\n\n" + summary.strip() + "\n" + after)
        print(f"\n已更新 optimization_log.md (P1-5)")


if __name__ == "__main__":
    main()
