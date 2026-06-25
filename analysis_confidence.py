#!/usr/bin/env python3
"""
置信度→胜率量化映射 (P0-1)

数据来源：predictions.md + predictions_backup_20260525.md + .tracker_state.json
输出：每种置信度等级对应的实际方向准确率
"""

import re
import json
import statistics
from pathlib import Path
from datetime import datetime

WORKSPACE = Path(__file__).parent
BACKUP = WORKSPACE / "predictions_backup_20260525.md"
CURRENT = WORKSPACE / "predictions.md"
STATE = WORKSPACE / ".tracker_state.json"


def extract_confidence_date_map():
    """从预测文件中提取 (date_str, confidence_level) 映射"""
    conf_map = {}
    for fpath in [BACKUP, CURRENT]:
        if not fpath.exists():
            continue
        content = fpath.read_text()
        blocks = re.split(r'\n---\n', content)
        for block in blocks:
            date_m = re.search(r'## 预测 (\d{4}-\d{2}-\d{2})', block)
            if not date_m:
                continue
            date_str = date_m.group(1)
            conf_m = re.search(r'\|\s*置信度\s*\|\s*([^\|]+)', block)
            if not conf_m:
                continue
            confidence = conf_m.group(1).strip().replace('|', '').strip()
            if '高' in confidence and '低' not in confidence:
                conf_level = "高"
            elif '中' in confidence:
                conf_level = "中"
            elif '低' in confidence:
                conf_level = "低"
            else:
                conf_level = "其他"
            conf_map[date_str] = conf_level
    return conf_map


def extract_from_state(state_data, conf_map):
    """从 state 已验证记录提取"""
    results = []
    for v in state_data.get('verified', []):
        date_str = v.get('date')
        if not date_str:
            continue
        results.append({
            'date': date_str,
            'session': '日盘',
            'confidence': conf_map.get(date_str, "未标注"),
            'dir_ok': v.get('direction_correct', False),
            'range_hit': v.get('range_hit', False),
            'dev': v.get('deviation')
        })
    return results


def main():
    conf_map = extract_confidence_date_map()
    print(f"找到 {len(conf_map)} 个预测日期对应的置信度")
    for d, c in sorted(conf_map.items()):
        print(f"  {d}: {c}")

    state_data = json.loads(STATE.read_text()) if STATE.exists() else {}
    results = extract_from_state(state_data, conf_map)
    print(f"\n从 state 提取到 {len(results)} 条已验证记录")
    for r in results:
        print(f"  {r['date']} {r['session']} 置信={r['confidence']} "
              f"方向={'✅' if r['dir_ok'] else '❌'} 区间={'✅' if r['range_hit'] else '❌'} "
              f"偏差={r.get('dev', '?')}")

    if not results:
        print("\n⚠️ 没有已验证记录")
        return

    groups = {}
    for r in results:
        c = r['confidence']
        if c not in groups:
            groups[c] = []
        groups[c].append(r)

    all_n = len(results)
    all_dir_ok = sum(1 for r in results if r['dir_ok'])
    all_range_hit = sum(1 for r in results if r['range_hit'])
    all_devs = [r['dev'] for r in results if r['dev'] is not None]
    all_dir_rate = all_dir_ok / all_n * 100 if all_n else 0
    all_range_rate = all_range_hit / all_n * 100 if all_n else 0
    all_avg_dev = statistics.mean([abs(d) for d in all_devs]) if all_devs else 0

    print("\n" + "=" * 70)
    print("         置信度 → 胜率量化映射")
    print("=" * 70)
    print(f"{'等级':<10} {'样本':<6} {'方向准确率':<14} {'区间命中率':<12} {'平均偏差':<12} {'偏差趋势':<12}")
    print("-" * 70)

    for level in ["高", "中", "低", "其他", "未标注"]:
        if level not in groups:
            print(f"{level:<10} {'—':<6} {'—':<14} {'—':<12} {'—':<12} {'—':<12}")
            continue
        rows = groups[level]
        n = len(rows)
        dir_ok_n = sum(1 for r in rows if r['dir_ok'])
        range_hit_n = sum(1 for r in rows if r['range_hit'])
        dir_rate = dir_ok_n / n * 100 if n > 0 else 0
        range_rate = range_hit_n / n * 100 if n > 0 else 0
        devs = [r['dev'] for r in rows if r['dev'] is not None]
        avg_dev = statistics.mean([abs(d) for d in devs]) if devs else 0
        pos_devs = sum(1 for d in devs if d is not None and d > 0) if devs else 0
        neg_devs = sum(1 for d in devs if d is not None and d < 0) if devs else 0
        bias = "偏>实" if pos_devs > neg_devs * 2 else ("偏<实" if neg_devs > pos_devs * 2 else "无偏")
        if n >= 3:
            dir_sym = " ✅" if dir_rate >= 55 else " ⚠️" if dir_rate >= 45 else " 🔴"
        else:
            dir_sym = f" ⚠️({n})"
        print(f"{level:<10} {n:<6} {dir_rate:.0f}%{dir_sym:<10} {range_rate:.0f}%{' ✅' if range_rate >= 60 else ' ⚠️' if range_rate >= 40 else ' 🔴':<9} {avg_dev:.1f}元{avg_dev <= 8 and ' ✅' or ' ⚠️':<7} {bias:<12}")

    print("-" * 70)
    all_grade = f"{all_dir_rate:.0f}%{' ✅' if all_dir_rate >= 55 else ' ⚠️' if all_dir_rate >= 45 else ' 🔴'}"
    print(f"{'总计':<10} {all_n:<6} {all_grade:<14} {all_range_rate:.0f}%{' ✅' if all_range_rate >= 60 else ' ⚠️' if all_range_rate >= 40 else ' 🔴':<9} {all_avg_dev:.1f}元{' ✅' if all_avg_dev <= 8 else ' ⚠️':<7} {'—':<12}")

    # 写入日志
    _write_log(results, groups, all_n, all_dir_rate, all_range_rate, all_avg_dev)


def _write_log(results, groups, all_n, all_dir_rate, all_range_rate, all_avg_dev):
    log_path = WORKSPACE / "optimization_log.md"
    if not log_path.exists():
        print("optimization_log.md not found")
        return

    covered = len([r for r in results if r['confidence'] != '未标注'])

    table_rows = ""
    for level in ["高", "中", "低", "其他", "未标注"]:
        if level not in groups:
            continue
        rows = groups[level]
        n = len(rows)
        dir_ok_n = sum(1 for r in rows if r['dir_ok'])
        range_hit_n = sum(1 for r in rows if r['range_hit'])
        dir_rate = dir_ok_n / n * 100 if n > 0 else 0
        range_rate = range_hit_n / n * 100 if n > 0 else 0
        devs = [r['dev'] for r in rows if r['dev'] is not None]
        avg_dev = statistics.mean([abs(d) for d in devs]) if devs else 0
        suggestion = "✅ 合理" if n >= 3 and dir_rate >= 55 else "⚠️ 偏弱" if n >= 3 and dir_rate >= 45 else "🔴 需校准" if n >= 3 else f"样本不足({n})"
        table_rows += f"| {level} | {n} | {dir_rate:.0f}% | {range_rate:.0f}% | {avg_dev:.1f}元/吨 | {suggestion} |\n"

    summary = f"""### 分析结果（{datetime.now().strftime('%Y-%m-%d %H:%M')}）

共扫描 {len(results)} 条已验证记录

| 置信度 | 样本 | 方向准确率 | 区间命中率 | 平均偏差 | 建议 |
|-------|------|-----------|-----------|---------|------|
{table_rows}| 合计 | {all_n} | {all_dir_rate:.0f}% | {all_range_rate:.0f}% | {all_avg_dev:.1f}元/吨 | — |

**结论**:
- 置信度标注覆盖率：{covered}/{all_n}（历史记录大多为N/A）
- 已建立分析管道（analysis_confidence.py），每次运行自动更新
- **待 confidence 字段覆盖率≥30%后再做首次正式校准**
"""

    content = log_path.read_text()
    marker = "## P0-1: 置信度→胜率量化映射"
    if marker in content:
        start = content.find(marker)
        end = len(content)
        for m in ["\n## P0-2", "\n## P0-", "\n## P1-"]:
            pos = content.find(m, start + len(marker))
            if pos != -1 and pos < end:
                end = pos
        before = content[:start]
        after = content[end:] if end < len(content) else ""
        log_path.write_text(before + marker + "\n\n" + summary.strip() + "\n" + after)
        print(f"已更新 optimization_log.md")


if __name__ == "__main__":
    main()
