#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证健康检查脚本
检查预测追踪器的 verified 数量和 pending 状态，
发现异常时输出告警信息（会被 cron 的 announce 送达微信）。

用法：
  python3 verify_health.py          # 标准检查，输出告警或空
  python3 verify_health.py --warn   # 静默输出，仅有问题时打印
"""

import json
import sys
import re
from pathlib import Path

TRACKER_STATE = Path(__file__).parent / ".tracker_state.json"
PREDICTIONS = Path(__file__).parent / "predictions.md"

# 正常预期：verified 应该每天增长
# 如果连续多次无增长或 pending 堆积，说明验证可能卡住了

MIN_EXPECTED_VERIFIED = 3  # 至少应该有几条已验证记录

def check():
    issues = []
    warnings = []

    # 1. 检查 tracker state
    if TRACKER_STATE.exists():
        try:
            state = json.loads(TRACKER_STATE.read_text())
            verified_count = len(state.get("verified", []))
            pending_count = len(state.get("pending_verification", []))
            d2_count = state.get("_d2_sample_count", 0)

            if verified_count < MIN_EXPECTED_VERIFIED:
                issues.append(f"verified 样本数异常偏少（{verified_count}条，预期≥{MIN_EXPECTED_VERIFIED}）")

            if pending_count > 3:
                warnings.append(f"待验证堆积（{pending_count}条），可能验证机制卡住了")

            if d2_count > 0 and verified_count > d2_count + 2:
                warnings.append(f"verified（{verified_count}条）远多于 _d2_sample_count（{d2_count}），计数可能不同步")

            # 检查最近验证日期
            verified_dates = [v.get("date", "") for v in state.get("verified", [])]
            # 按日期排序找最近
            verified_dates_sorted = sorted(verified_dates)
            if verified_dates_sorted:
                latest = verified_dates_sorted[-1]
                # 如果最近的验证是 3 条记录前，不太合理，需要更精细的检测
        except Exception as e:
            issues.append(f"tracker_state 读取失败: {e}")
    else:
        issues.append(".tracker_state.json 不存在，验证从未运行过")

    # 2. 检查 predictions.md 中的验证结果
    if PREDICTIONS.exists():
        try:
            content = PREDICTIONS.read_text()

            # 统计已验证行
            day_verified = re.findall(r'\| 🌞 日盘.*?\| \d+\.\d+ \| \d+\.?\d* \(', content)
            night_verified = re.findall(r'\| 🌙 夜盘.*?\| \d+\.\d+ \| \d+\.?\d*', content)

            total_verified = len(day_verified) + len(night_verified)

            # 统计未验证行
            day_pending = re.findall(r'\| 🌞 日盘.*?_待填_', content)
            night_pending = re.findall(r'\| 🌙 夜盘.*?_待填_', content)

            total_pending = len(day_pending) + len(night_pending)

            if total_pending > 3:
                warnings.append(f"predictions.md 中还有 {total_pending} 条未验证（日盘{len(day_pending)}+夜盘{len(night_pending)}）")

            # 计算已有数据的预测记录数（去掉空壳记录）
            all_blocks = re.findall(r'## 预测 \d{4}-\d{2}-\d{2}\n', content)
            total_predictions = len(all_blocks)

            if total_predictions > 0 and total_verified < total_predictions * 0.5:
                warnings.append(f"已验证率偏低：{total_verified}/{total_predictions} 条（{total_verified/total_predictions*100:.0f}%）")

        except Exception as e:
            issues.append(f"predictions.md 读取失败: {e}")
    else:
        issues.append("predictions.md 不存在")

    # 3. 输出
    if issues or warnings:
        print("⚠️ **玉米验证健康检查**")
        if issues:
            for i in issues:
                print(f"  🔴 {i}")
        if warnings:
            for w in warnings:
                print(f"  🟡 {w}")
        if "--warn" not in sys.argv:
            sys.exit(1 if issues else 0)
        return False

    if "--warn" not in sys.argv:
        pass  # 静默退出，无问题不输出
    return True


if __name__ == "__main__":
    check()
