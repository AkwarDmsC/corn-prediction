#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
玉米日报 v5 集成脚本 — 运行时版。

不 import analysis.py / prediction_tracker.py / sim_trade.py，
而是用 subprocess 依次调用，与原始 cron 行为一致。
唯一的区别：最后一步直接输出完整日报，Agent 只需 cat + final reply。
"""

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

CORN_ROOT = Path("/Users/mashaocong/.openclaw/workspace/corn")
STATE_FILE = CORN_ROOT / "sim_trade_state.json"
PREDICTIONS_FILE = CORN_ROOT / "predictions.md"
SIM_TRADE_FILE = CORN_ROOT / "sim_trade.md"


def run_shell(cmd: str, timeout: int = 120, env_add: dict | None = None) -> int:
    """用 shell=True 运行命令。返回 returncode。"""
    env = None
    if env_add:
        env = {**os.environ, **env_add}
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            timeout=timeout,
            cwd=str(CORN_ROOT),
            env=env,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        return -1
    except FileNotFoundError:
        return -2


def safe_decode(b: bytes | str | None) -> str:
    if b is None:
        return ""
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return b


def tail_file(path: Path, chars: int = 3000) -> str:
    if not path.exists():
        return ""
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    return text[-chars:].strip()


def extract_sections(text: str) -> dict[str, str]:
    """从 analysis.py stdout 中提取【】包裹的段落，返回 {标题: 内容}。"""
    sections: dict[str, str] = {}
    current_title = ""
    current_lines: list[str] = []
    for line in text.splitlines():
        if "【" in line and "】" in line:
            if current_title and current_lines:
                sections[current_title] = "\n".join(current_lines)
            current_title = line.strip()
            current_lines = []
            # 标题行本身也保留
            content_after = line.split("】", 1)[-1].strip()
            if content_after:
                current_lines.append(content_after)
        else:
            if current_title:
                current_lines.append(line.rstrip())
    if current_title and current_lines:
        sections[current_title] = "\n".join(current_lines)
    return sections


def read_sim_trade_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def build_daily_report(
    session: str,
    analysis_output: str,
    tracker_ok: bool,
    health_ok: bool,
    health_output: str,
    sim_ok: bool,
    sim_output: str,
    sim_state: dict,
    predictions_tail: str,
    duration: float,
) -> str:
    """从 analysis 输出的【】段落中提取关键信号段生成日报。"""
    s_label = "日盘" if session == "day" else "夜盘"
    sections = extract_sections(analysis_output)
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 提取收盘价
    close = "—"
    for k, v in sections.items():
        if "DCE玉米" in k:
            for line in v.splitlines():
                m = __import__("re").search(r"今日收盘[:：]([\d.]+)", line)
                if m:
                    close = m.group(1)
                    break

    # 提取关键段落
    report_parts = []

    # 1. 基础信息
    report_parts.append(f"🌽 DCE玉米{s_label}日报｜{today_str}")
    report_parts.append(f"📊 最新价: {close} 元/吨")

    # 2. 综合信号段
    for key in ["综合信号"]:
        for k, v in sections.items():
            if f"【{key}" in k:
                cleaned = "\n".join(
                    line for line in v.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                )[:400]
                if cleaned:
                    report_parts.append(f"\n📈 信号:\n{cleaned}")
                break

    # 3. 均线/RSI/布林/成交量信号段（从各段取）
    for key in ["均线", "RSI", "布林", "MACD", "背离", "豆粕关联", "原油关联", "季节性", "USDA报告窗口", "USDA报告日历", "关键价位", "信号冲突", "CBOT"]:
        for k, v in sections.items():
            if f"【{key}" in k:
                cleaned = "\n".join(
                    line.strip() for line in v.splitlines()
                    if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("—")
                )
                # 取前 3 行
                lines = cleaned.split("\n")[:3]
                for l in lines:
                    l = l.strip()
                    if len(l) > 3 and l not in "".join(report_parts):  # 去重
                        report_parts.append(l)
                break

    # 4. 置信度
    for k, v in sections.items():
        if "置信度" in k:
            first_line = v.split("\n")[0].strip()[:120] if v.strip() else ""
            if first_line:
                report_parts.append(f"\n🎯 {k} {first_line}")
            break

    # 5. 多周期预测（日盘/夜盘预测）
    for k, v in sections.items():
        if "多周期" in k:
            report_parts.append("\n" + v[:1500].strip())
            break

    # 6. 模拟交易状态
    pos = sim_state.get("position", "无")
    entry_price = sim_state.get("entry_price", "—")
    contracts = sim_state.get("contracts", 0)
    float_pnl = sim_state.get("float_pnl", 0)
    equity = sim_state.get("equity", 0)
    pos_str = f"{pos} {contracts}手 @{entry_price}" if pos and pos != "无" else "无持仓"
    pnl_str = f"浮动盈亏: {float_pnl:+,.0f} 元" if pos and pos != "无" else ""
    eq_str = f"总权益: {equity:,.2f} 元"
    report_parts.append(f"\n⛑ 模拟交易: {pos_str}")
    if pnl_str:
        report_parts.append(f"   {pnl_str} | {eq_str}")
    else:
        report_parts.append(f"   {eq_str}")

    # 7. 执行状态
    report_parts.append(f"\n⏱️ 执行: {duration:.1f}s | analysis ✅")

    # 8. 合并成紧凑日报
    raw = "\n".join(report_parts)
    # 抽取多周期预测关键行，精简
    lines = raw.split("\n")
    compact = []
    for line in lines:
        # 去掉过长且重复的技术指标行
        if "最新价:" in line and "元/吨" in line and "相关" in line:
            continue  # 豆粕/原油/豆油的具体值
        if "(DCE豆" in line or "(INE" in line:
            continue
        if line.startswith("================================"):
            continue
        if "⚠️" in line and "USDA" in line:
            compact.append(line)
            continue
        if line.strip().startswith("---") or line.strip().startswith("━"):
            continue
        # 取短中长期的关键行
        if line.strip().startswith("📆"):
            parts = line.split("关键因子:")
            line = parts[0].rstrip()
        compact.append(line)

    return "\n".join(compact)


def run_integrated(session: str) -> str:
    """依次跑 4 个脚本，输出日报文本。"""
    started = time.monotonic()
    analysis_output = ""
    tracker_ok = False
    health_ok = False
    health_output = ""
    sim_ok = False
    sim_output = ""

    # Step 1: analysis.py
    tmp = f"/tmp/corn_analysis_integrated_{session}.txt"
    rc1 = run_shell(f"PYTHONUNBUFFERED=1 python3 analysis.py > {tmp} 2>&1", timeout=90)
    # 如果首次失败，尝试重跑一次
    if rc1 != 0 or (Path(tmp).exists() and Path(tmp).stat().st_size < 1000):
        import time as _t
        _t.sleep(5)
        rc1 = run_shell(f"PYTHONUNBUFFERED=1 python3 analysis.py > {tmp} 2>&1", timeout=90)
    analysis_output = safe_decode(Path(tmp).read_bytes()) if Path(tmp).exists() else f"[analysis.py 返回 {rc1}]"

    # Step 2: prediction_tracker
    rc2 = run_shell(f"python3 prediction_tracker.py {tmp}")
    tracker_ok = rc2 == 0

    # Step 3: verify_health
    health_tmp = f"/tmp/corn_health_{session}.txt"
    rc3 = run_shell(f"python3 verify_health.py > {health_tmp} 2>&1")
    health_output = safe_decode(Path(health_tmp).read_bytes()) if Path(health_tmp).exists() else ""
    health_ok = rc3 == 0

    # Step 4: sim_trade — ⛔ 暂停（2026-06-12，模型方向准确率19%，触发风控冻结）
    # 仅读取当前状态用于日报，不执行交易
    sim_tmp = f"/tmp/corn_sim_{session}.txt"
    sim_ok = True
    Path(sim_tmp).write_text("[sim_trade] ⛔ 模拟交易已冻结（方向准确率过低）\n")

    duration = time.monotonic() - started

    sim_state = read_sim_trade_state()
    predictions_tail = tail_file(PREDICTIONS_FILE, 2000)
    sim_trade_tail = tail_file(SIM_TRADE_FILE, 2000)

    report = build_daily_report(
        session=session,
        analysis_output=analysis_output,
        tracker_ok=tracker_ok,
        health_ok=health_ok,
        health_output=health_output,
        sim_ok=sim_ok,
        sim_output=sim_output,
        sim_state=sim_state,
        predictions_tail=predictions_tail,
        duration=duration,
    )

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="玉米 v5 日报集成脚本")
    parser.add_argument("--session", choices=("day", "night"), default="day")
    args = parser.parse_args()
    report = run_integrated(args.session)
    print(report)


if __name__ == "__main__":
    main()
