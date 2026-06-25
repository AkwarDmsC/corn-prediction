#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻影响学习器 v1.0
记录每条新闻 + 次日价格反应，统计关键词组对价格的实际影响
"""


import json
import re
import os
import statistics
from datetime import datetime, timedelta, date
from pathlib import Path
from constants import NEWS_IMPACT_DB as _NIDB, DCE_DATA as _DCE

# ── 数据库 ──
IMPACT_DB = _NIDB
DCE_DATA = _DCE

# ── 关键词分组（用于统计）──
KEYWORD_GROUPS = {
    "拍卖投放": ["临储拍卖","政策拍卖","定向拍卖","拍卖","投放市场","竞价","轮出","政策粮投放"],
    "收储补贴": ["临储收购","敞开收购","保护价","收储","补贴","生产者补贴"],
    "进口": ["进口","进口玉米","美玉米","巴西玉米","乌克兰玉米","零关税","进口配额"],
    "产量天气": ["减产","产量下降","旱情","高温","洪涝","丰收","丰产","产量增加","种植","播种"],
    "期货行情": ["玉米主力","玉米合约","玉米期货","CBOT","芝加哥"],
    "需求": ["饲料","生猪","需求旺盛","需求疲软","深加工"],
    "政策调控": ["保供稳价","调控","供需平衡"],
    "国际市场": ["CBOT","芝加哥","USDA","美国玉米","出口"],
    "价格信号": ["震荡走低","震荡","反弹","走强","承压","支撑","弱势"],
}

def _load_impact_db():
    """加载历史影响数据库"""
    try:
        with open(IMPACT_DB, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"records": [], "last_date": "", "stats": {}}

def _save_impact_db(db):
    with open(IMPACT_DB, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def _load_dce_data():
    """加载DCE历史行情"""
    try:
        with open(DCE_DATA, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("corn", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def get_close_on_date(target_date, dce_data):
    """获取指定日期的收盘价"""
    d = target_date if isinstance(target_date, str) else target_date.strftime("%Y-%m-%d")
    for row in dce_data:
        if row.get("date") == d:
            return row.get("close")
    return None

def get_next_trading_day_close(target_date, dce_data):
    """获取指定日期之后第一个交易日（即次日）的收盘价"""
    target_d = target_date if isinstance(target_date, str) else target_date.strftime("%Y-%m-%d")
    found = False
    for row in dce_data:
        if found:
            return row.get("close")
        if row.get("date") == target_d:
            found = True
    return None

def get_next_trading_day_change_pct(target_date, dce_data):
    """获取目标日期到下一个交易日的涨跌幅(%)"""
    target_d = target_date if isinstance(target_date, str) else target_date.strftime("%Y-%m-%d")
    today_close = None
    for row in dce_data:
        if today_close is not None:
            next_close = row.get("close")
            if today_close and next_close:
                return round((next_close - today_close) / today_close * 100, 2)
            return None
        if row.get("date") == target_d:
            today_close = row.get("close")
    return None

def record_news_impact(news_items, analysis_date=None):
    """记录新闻及其后价格反应"""
    if analysis_date is None:
        analysis_date = datetime.now().strftime("%Y-%m-%d")
    if isinstance(analysis_date, datetime):
        analysis_date = analysis_date.strftime("%Y-%m-%d")

    db = _load_impact_db()
    dce_data = _load_dce_data()

    if not dce_data:
        return {"status": "no_dce_data", "records_added": 0}

    # 获取第二天涨跌幅
    change_pct = get_next_trading_day_change_pct(analysis_date, dce_data)
    next_close = get_next_trading_day_close(analysis_date, dce_data)

    added = 0
    for item in news_items:
        news_date = item.get("time", analysis_date)[:10]
        # 去重
        existing_titles = {r.get("title", "") for r in db["records"]}
        title_key = item.get("title", "")[:60]
        if title_key in existing_titles:
            continue

        record = {
            "date": news_date,
            "title": item.get("title", ""),
            "source": item.get("source", ""),
            "score": item.get("score", 0),
            "keyword_match": item.get("matched", []),
            "next_day_change_pct": change_pct,
            "next_day_close": next_close,
            "analysis_date": analysis_date,
            "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        db["records"].append(record)
        added += 1

    db["last_date"] = analysis_date
    _save_impact_db(db)
    return {"status": "ok", "records_added": added, "total": len(db["records"])}

def compute_keyword_impact():
    """统计各关键词组的历史影响"""
    db = _load_impact_db()
    records = db.get("records", [])
    if not records:
        return {"status": "no_records", "keyword_stats": {}}

    # 按关键词分组统计
    stats = {}
    for group_name, keywords in KEYWORD_GROUPS.items():
        group_records = []
        for rec in records:
            title = rec.get("title", "")
            content_key = title
            if any(kw in content_key for kw in keywords):
                group_records.append(rec)

        if not group_records:
            continue

        changes = [r["next_day_change_pct"] for r in group_records if r.get("next_day_change_pct") is not None]
        if not changes:
            continue

        up_count = sum(1 for c in changes if c > 0)
        down_count = sum(1 for c in changes if c < 0)

        stats[group_name] = {
            "样本数": len(changes),
            "上涨次数": up_count,
            "下跌次数": down_count,
            "上涨概率": round(up_count / len(changes) * 100, 1),
            "平均涨跌幅": round(statistics.mean(changes), 2),
            "中位数涨跌幅": round(statistics.median(changes), 2),
            "最大涨幅": max(changes),
            "最大跌幅": min(changes),
            "标准差": round(statistics.stdev(changes), 2) if len(changes) > 1 else 0,
        }

    # 整体统计
    all_changes = [r["next_day_change_pct"] for r in records if r.get("next_day_change_pct") is not None]
    if all_changes:
        up_all = sum(1 for c in all_changes if c > 0)
        stats["__all__"] = {
            "样本数": len(all_changes),
            "上涨次数": up_all,
            "上涨概率": round(up_all / len(all_changes) * 100, 1),
            "平均涨跌幅": round(statistics.mean(all_changes), 2),
            "中位数涨跌幅": round(statistics.median(all_changes), 2),
        }

    db["stats"] = stats
    _save_impact_db(db)
    return stats

def print_impact_report():
    """打印影响报告"""
    db = _load_impact_db()
    records = db.get("records", [])
    stats = db.get("stats", {})

    print("═" * 60)
    print(f"【玉米新闻影响数据库】")
    print(f"  共 {len(records)} 条新闻记录")
    print("═" * 60)

    if not stats:
        stats = compute_keyword_impact()

    if not stats:
        print("  暂无统计数据")
        return

    print(f"\n📊 关键词影响分析\n")
    print(f"  {'关键词组':<12} {'样本':>4} {'上涨率':>7} {'平均涨跌':>10} {'中位数':>8}")
    print(f"  {'-'*45}")

    # 先整体
    if "__all__" in stats:
        s = stats.pop("__all__")
        print(f"  {'📈 整体':<12} {s['样本数']:>4} {s['上涨概率']:>6.1f}% {s['平均涨跌幅']:>+8.2f}% {s['中位数涨跌幅']:>+7.2f}%")

    for name in ["拍卖投放", "收储补贴", "进口", "产量天气", "期货行情", "需求", "政策调控", "国际市场", "价格信号"]:
        if name not in stats:
            continue
        s = stats[name]
        if s["样本数"] < 3:
            sig = "⚠️"
        else:
            sig = "✅" if s["上涨概率"] > 55 or s["上涨概率"] < 45 else "❓"
        direction = "看涨" if s["平均涨跌幅"] > 0 else "看跌"
        print(f"  {sig} {name:<10} {s['样本数']:>4} {s['上涨概率']:>6.1f}% {s['平均涨跌幅']:>+8.2f}% {s['中位数涨跌幅']:>+7.2f}% {direction}")

    print(f"\n📋 最近5条记录:")
    for rec in records[-5:]:
        title = rec["title"][:45]
        change = rec.get("next_day_change_pct")
        change_str = f"{change:+.2f}%" if change is not None else "待验证"
        sc = rec.get("score", 0)
        print(f"  {rec['date']} | {sc:+.1f}分 | {title:<45} → {change_str}")

def run_impact_analysis(news_items, analysis_date=None):
    """运行完整的影响分析（记录+统计+输出）"""
    if analysis_date is None:
        analysis_date = datetime.now().strftime("%Y-%m-%d")

    # 记录
    result = record_news_impact(news_items, analysis_date)
    print(f"[影响] 记录: 新增{result.get('records_added',0)}条 (共{result.get('total',0)}条)")

    # 统计
    stats = compute_keyword_impact()
    print_impact_report()

    return stats

if __name__ == "__main__":
    # 独立运行：查看当前数据库
    print_impact_report()

