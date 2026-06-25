#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
产区天气模块 v1.0
获取中国玉米产区天气评分
数据源：Open-Meteo API (免费，无需key)
覆盖：东北(40%)、华北(28%)、西南(4%)、西北(5%)
"""


import urllib.request
import json
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from constants import WEATHER_CACHE

CACHE_FILE = WEATHER_CACHE

# 中国玉米产区（纬度, 经度, 权重）
CORN_REGIONS = [
    ("哈尔滨", 45.75, 126.63, 0.12, "东北"),
    ("佳木斯", 46.80, 130.35, 0.08, "东北"),
    ("长春", 43.88, 125.32, 0.10, "东北"),
    ("沈阳", 41.80, 123.43, 0.08, "东北"),
    ("四平", 43.17, 124.35, 0.07, "东北"),
    ("济南", 36.65, 117.12, 0.10, "华北"),
    ("郑州", 34.76, 113.65, 0.09, "华北"),
    ("石家庄", 38.04, 114.48, 0.08, "华北"),
    ("昆明", 25.04, 102.71, 0.04, "西南"),
    ("乌鲁木齐", 43.83, 87.62, 0.05, "西北"),
]

def _fetch_one(name, lat, lon, weight, zone):
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
           f"&current_weather=true&timezone=Asia%2FShanghai&forecast_days=7")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=7) as r:
        data = json.loads(r.read())
    cw = data.get("current_weather", {})
    daily = data.get("daily", {})
    temp = cw.get("temperature", 20)
    precip = sum(daily.get("precipitation_sum", []))
    hot_days = sum(1 for t in daily.get("temperature_2m_max", []) if t > 35)

    score = 0
    factors = []
    if temp > 38:
        score -= 1.0; factors.append("高温⚠️")
    elif temp > 35:
        score -= 0.3; factors.append("偏热")
    elif 15 <= temp <= 30:
        score += 0.2; factors.append("适宜")

    if precip > 30:
        score -= 0.8; factors.append("洪涝⚠️")
    elif precip > 10:
        score += 0.3; factors.append("降水充足")
    elif precip < 3:
        score -= 0.5; factors.append("干旱⚠️")

    if hot_days >= 3:
        score -= 0.5; factors.append(f"{hot_days}天高温")

    return {
        "name": name, "zone": zone, "weight": weight,
        "temp": round(temp, 1), "precip": round(precip, 1),
        "hot_days": hot_days, "score": round(score, 1),
        "factors": factors
    }

def get_corn_weather():
    """获取所有产区天气，返回综合评分和详情"""
    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_fetch_one, n, la, lo, w, z)
                   for n, la, lo, w, z in CORN_REGIONS}
        for f in as_completed(futures, timeout=20):
            try:
                results.append(f.result())
            except Exception:
                pass

    if not results:
        return None

    # 加权综合评分
    total_score = sum(r["score"] * r["weight"] for r in results)
    total_weight = sum(r["weight"] for r in results)
    overall = round(total_score / total_weight, 1)

    # 缓存
    try:
        CACHE_FILE.write_text(json.dumps({
            "overall": overall, "regions": results
        }, ensure_ascii=False))
    except:
        pass

    return {"overall": overall, "regions": results}

def get_weather_summary():
    """返回简洁的天气摘要字符串"""
    data = get_corn_weather()
    if not data:
        return "天气数据获取失败"

    o = data["overall"]
    emoji = "🟢" if o >= 0.3 else "🔴" if o <= -0.3 else "⚪"
    signal = "轻微利多" if o >= 0.3 else "轻微利空" if o <= -0.3 else "中性"

    # 按产区分组
    zones = {}
    for r in data["regions"]:
        z = r["zone"]
        if z not in zones:
            zones[z] = []
        zones[z].append(r)

    summary_parts = [f"{emoji}天气{signal}（评分{o:+.1f}）"]

    for zone, regs in sorted(zones.items()):
        zone_avg = statistics.mean(r["score"] for r in regs)
        ze = "🟢" if zone_avg >= 0.3 else "🔴" if zone_avg <= -0.3 else "⚪"
        # 找最异常的点
        worst = min(regs, key=lambda x: x["score"])
        best = max(regs, key=lambda x: x["score"])
        if worst["score"] < -0.3:
            summary_parts.append(f"{ze}{zone}总体{zone_avg:+.1f}（{worst['name']}{worst['score']:+.1f}分{worst['factors'][0] if worst['factors'] else ''}）")
        else:
            summary_parts.append(f"{ze}{zone}{zone_avg:+.1f}分")

    return " | ".join(summary_parts)

if __name__ == "__main__":
    print("[天气] 中国玉米产区天气")
    print("="*50)
    data = get_corn_weather()
    if data:
        o = data["overall"]
        e = "🟢" if o >= 0.3 else "🔴" if o <= -0.3 else "⚪"
        s = "轻微利多" if o >= 0.3 else "轻微利空" if o <= -0.3 else "中性"
        print(f"\n综合评分: {e} {o:+.1f} / {s}")

        zones = {}
        for r in data["regions"]:
            z = r["zone"]
            if z not in zones:
                zones[z] = []
            zones[z].append(r)

        for zone, regs in sorted(zones.items()):
            z_avg = statistics.mean(r["score"] for r in regs)
            ze = "🟢" if z_avg >= 0.3 else "🔴" if z_avg <= -0.3 else "⚪"
            print(f"\n  {ze}【{zone}】（均值: {z_avg:+.1f}）")
            for r in sorted(regs, key=lambda x: -x["score"]):
                e = "🟢" if r["score"] >= 0.3 else "🔴" if r["score"] <= -0.3 else "⚪"
                print(f"    {e} {r['name']}: {r['score']:+.1f} | {r['temp']}°C | 降水{r['precip']}mm/7d | {r['hot_days']}天>35°C | {' '.join(r['factors'])}")
    else:
        print("[天气] 获取失败")

