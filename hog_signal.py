#!/usr/bin/env python3
"""
生猪与饲料需求数据接入模块 (P1-4)

数据来源：
  akshare:
    futures_hog_supply  — 生猪现货供应（元/kg，日频）
    futures_hog_cost    — 生猪养殖成本（元/头，日频）
    futures_hog_core    — 核心生猪数据

用途：
  判断饲料需求 → 玉米间接需求
  规则：生猪价格↑ + 成本↓ → 养殖利润↑ → 补栏↑ → 饲料需求↑ → 玉米偏多

用法：
  python3 hog_signal.py                    # 获取最新信号
  python3 hog_signal.py --log              # 追加至日志
"""

import json
from pathlib import Path
from datetime import datetime

WORKSPACE = Path(__file__).parent
CACHE_FILE = WORKSPACE / "hog_cache.json"


def fetch_hog_data():
    """获取生猪相关数据"""
    import akshare as ak
    import pandas as pd
    
    result = {}
    
    # 1. 生猪供应价格
    try:
        df = ak.futures_hog_supply()
        result["hog_supply"] = {
            "latest_price": float(df.iloc[-1]["value"]),
            "latest_date": str(df.iloc[-1]["date"]),
            "chg_7d": float(df.iloc[-1]["value"]) - float(df.iloc[-7]["value"]) if len(df) >= 7 else 0,
            "chg_30d": float(df.iloc[-1]["value"]) - float(df.iloc[-30]["value"]) if len(df) >= 30 else 0,
        }
    except Exception as e:
        result["hog_supply"] = {"error": str(e)}
    
    # 2. 养殖成本
    try:
        df = ak.futures_hog_cost()
        result["hog_cost"] = {
            "latest_cost": float(df.iloc[-1]["value"]),
            "latest_date": str(df.iloc[-1]["date"]),
            "chg_7d": float(df.iloc[-1]["value"]) - float(df.iloc[-7]["value"]) if len(df) >= 7 else 0,
            "chg_30d": float(df.iloc[-1]["value"]) - float(df.iloc[-30]["value"]) if len(df) >= 30 else 0,
        }
    except Exception as e:
        result["hog_cost"] = {"error": str(e)}
    
    # 3. 核心生猪数据（大致包含日重等信息）
    try:
        df = ak.futures_hog_core()
        result["hog_core"] = {
            "latest": float(df.iloc[-1]["value"]),
            "latest_date": str(df.iloc[-1]["date"]),
        }
    except Exception as e:
        result["hog_core"] = {"error": str(e)}
    
    return result


def compute_signal(data):
    """
    计算饲料需求→玉米信号
    
    逻辑：
    1. 生猪价格连续上涨 + 成本稳定或下降 → 利润扩张 → 补栏意愿↑ → 饲料需求↑ → 利多玉米
    2. 生猪价格持续下跌 → 亏损 → 去产能 → 饲料需求↓ → 利空玉米
    3. 稳定/波动小 → 中性
    """
    supply = data.get("hog_supply", {})
    cost = data.get("hog_cost", {})
    
    if "error" in supply or "error" in cost:
        return {'direction': 0, 'score': 0, 'detail': '数据不可用'}
    
    price = supply.get("latest_price", 0)
    chg_7d = supply.get("chg_7d", 0)
    chg_30d = supply.get("chg_30d", 0)
    cost_val = cost.get("latest_cost", 0)
    
    # 估算利润：生猪价(元/kg) × 120kg/头 - 成本(元/头) ≈ 养殖利润
    est_profit = price * 120 - cost_val
    profit_pct = est_profit / cost_val * 100 if cost_val > 0 else 0
    
    direction = 0
    score = 0
    reasons = []

    directions = []

    if chg_7d > 0.5 and profit_pct > 5:
        directions.append((1, 0.7, f"生猪7天涨价{chg_7d:+.1f}元,利润{profit_pct:.0f}%"))
    if chg_7d < -0.5 or profit_pct < -10:
        directions.append((-1, 0.6, f"生猪7天下跌{chg_7d:+.1f}元,利润{profit_pct:.0f}%"))
    
    if not directions:
        return {'direction': 0, 'score': 0, 'detail': f'生猪供需稳定(利润{profit_pct:.0f}%)'}
    
    # 取最强信号
    directions.sort(key=lambda x: -x[1])
    d, s, r = directions[0]
    return {'direction': d, 'score': s, 'detail': r}


def main():
    import sys
    
    print("=" * 60)
    print("生猪/饲料需求数据")
    print("=" * 60)
    
    data = fetch_hog_data()
    
    # 打印数据
    supply = data.get("hog_supply", {})
    cost = data.get("hog_cost", {})
    
    if "latest_price" in supply:
        price = supply["latest_price"]
        print(f"\n🐷 生猪现货: {price:.2f} 元/kg ({supply['latest_date']})")
        print(f"  7天变化: {supply['chg_7d']:+.2f} 元/kg")
        print(f"  30天变化: {supply['chg_30d']:+.2f} 元/kg")
    
    if "latest_cost" in cost:
        c = cost["latest_cost"]
        print(f"\n💰 养殖成本: {c:.0f} 元/头 ({cost['latest_date']})")
        print(f"  7天变化: {cost['chg_7d']:+.0f} 元/头")
        
        if "latest_price" in supply:
            est_profit = price * 120 - c
            profit_pct = est_profit / c * 100
            print(f"\n📊 估算养殖利润: {est_profit:.0f} 元/头 ({profit_pct:+.0f}%)")
    
    # 信号
    signal = compute_signal(data)
    print(f"\n饲料需求→玉米信号:")
    print(f"  方向: {signal['direction']} (1=多, -1=空, 0=中性)")
    print(f"  强度: {signal['score']:.2f}")
    print(f"  说明: {signal['detail']}")
    
    # 缓存
    CACHE_FILE.write_text(json.dumps({
        "data": data,
        "signal": signal,
        "updated": datetime.now().isoformat()
    }, indent=2, ensure_ascii=False))
    print(f"\n  已缓存至 {CACHE_FILE}")
    
    # 追加日志
    if '--log' in sys.argv:
        log_file = WORKSPACE / "hog_signal_log.md"
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n| {now} | {supply.get('latest_price', 'N/A')} | 7天{'+' if supply.get('chg_7d', 0) > 0 else ''}{supply.get('chg_7d', 0):.2f} | {cost.get('latest_cost', 'N/A')} | {signal['direction']} | {signal['score']:.2f} | {signal['detail']} |"
        if not log_file.exists():
            log_file.write_text("# 生猪/饲料需求跟踪日志\n\n| 时间 | 生猪价 | 7天变动 | 成本 | 信号方向 | 强度 | 说明 |\n|---|---|---|---|---|---|---|\n")
        log_file.write_text(log_file.read_text() + entry)
        print(f"  已追加至 {log_file}")


def get_hog_signal():
    """
    获取生猪需求信号（供 daily_analysis_cn.py 调用）
    
    返回: {'direction': -1/0/1, 'score': 0~1, 'detail': str}
    """
    try:
        if CACHE_FILE.exists():
            cache = json.loads(CACHE_FILE.read_text())
            updated = datetime.fromisoformat(cache.get("updated", "2000-01-01"))
            if (datetime.now() - updated).days < 1:
                return cache.get("signal", {'direction': 0, 'score': 0, 'detail': '缓存过期'})
        
        data = fetch_hog_data()
        return compute_signal(data)
    except Exception as e:
        return {'direction': 0, 'score': 0, 'detail': f'数据获取失败: {e}'}


if __name__ == "__main__":
    main()
