#!/usr/bin/env python3
"""
黑天鹅检测与极端行情模式 (P3-1)

核心思想：不预测黑天鹅，而是识别 → 切换模式
四类黑天鹅 + 自动检测规则 + 极端行情输出模板

检测规则:
  1. 极端天气: 产区气温>38°C持续3天 / 日降水>100mm / 早霜预警
  2. 政策突变: 新闻中出现"临储拍卖""进口配额""补贴"等关键词+大额变动
  3. 国际冲击: CBOT单日>±5% / 俄乌粮食协议相关新闻
  4. 疫情/物流: 主产区封控/港口停运等新闻

极端行情模式:
  - 强制方向（使用黑天鹅方向替代加权信号）
  - 扩大区间（正常×2-3倍）
  - 收紧风控（置信度上限锁定）
  - 屏蔽冲突（即使信号有分歧强制输出）

用法:
  python3 black_swan.py              # 检测当前黑天鹅风险并报告
  python3 black_swan.py --check      # 快速检查（供analysis.py cron调用）

返回:
  {
    'is_black_swan': False/True,
    'type': 'weather'/'policy'/'international'/'logistics'/None,
    'direction': -1/0/1,    # 强制方向
    'severity': 'low'/'medium'/'high',
    'interval_multiplier': 2.0,  # 区间倍数
    'reason': str,
    'expiry': str,          # 模式过期时间
  }
"""

import json
import requests
from pathlib import Path
from datetime import datetime, timedelta

WORKSPACE = Path(__file__).parent
CACHE = WORKSPACE / "black_swan_cache.json"

# NewsAPI key（已配置）
NEWS_API_KEY = ""  # ponytail: 改用环境变量 NEWSAPI_KEY

# 产区监控（Open-Meteo）
CORN_REGIONS = {
    "东北": {"lat": 44.0, "lon": 126.0},      # 吉林/黑龙江
    "黄淮海": {"lat": 35.0, "lon": 115.0},    # 河南/山东/河北
    "华北": {"lat": 39.0, "lon": 114.0},      # 北京/天津/河北北部
    "西北": {"lat": 37.0, "lon": 104.0},      # 甘肃/宁夏/内蒙古西部
}

# 四类黑天鹅的关键词
EXTREME_WEATHER_KW = [
    "高温干旱", "持续暴雨", "洪涝", "早霜", "寒潮", "冻害",
    "干旱", "台风", "冰雹", "drought", "flood", "frost",
    "heat wave", "typhoon", "hail", "extreme weather",
]

POLICY_SHOCK_KW = [
    "临储拍卖", "暂停", "进口配额", "加征关税", "取消关税",
    "补贴", "抛储", "收储", "拍卖放量", "进口管制",
    "reserve auction", "import quota", "tariff", "subsidy",
    "stockpile release", "export ban",
]

INTERNATIONAL_SHOCK_KW = [
    "CBOT暴跌", "CBOT暴涨", "粮食协议", "黑海", "谷物出口",
    "俄乌", "粮食禁运", "import ban", "export ban",
    "CBOT", "plunge", "surge", "grain deal", "Black Sea",
    "乌克兰", "俄罗斯", "USDA surprise",
]

LOGISTICS_KW = [
    "封控", "封锁", "停运", "港口", "铁路", "物流中断",
    "lockdown", "port closure", "shipping", "logistics",
    "quarantine", "blockade", "strike",
]


# ─────────────────────────────────────────
# 检测引擎
# ─────────────────────────────────────────
def check_weather_extreme():
    """检测产区极端天气"""
    try:
        import openmeteo_requests
        import requests_cache
        import pandas as pd
        
        cache_session = requests_cache.CachedSession('.openmeteo_cache', expire_after=3600)
        om = openmeteo_requests.Client(session=cache_session)
        
        results = []
        for region, coord in CORN_REGIONS.items():
            params = {
                "latitude": coord["lat"],
                "longitude": coord["lon"],
                "daily": ["temperature_2m_max", "precipitation_sum"],
                "forecast_days": 7,
                "timezone": "Asia/Shanghai"
            }
            responses = om.weather_api("https://api.open-meteo.com/v1/forecast", params=params)
            response = responses[0]
            
            daily = response.Daily()
            temps = daily.Variables(0).ValuesAsNumpy()
            precip = daily.Variables(1).ValuesAsNumpy()
            
            max_temp = max(temps) if len(temps) > 0 else 0
            max_precip = max(precip) if len(precip) > 0 else 0
            extreme_days = sum(1 for t in temps if t > 38) + sum(1 for p in precip if p > 100)
            
            results.append({
                "region": region,
                "max_temp": round(float(max_temp), 1),
                "max_precip": round(float(max_precip), 1),
                "extreme_days": int(extreme_days),
                "alert": extreme_days >= 2,
            })
        
        active = [r for r in results if r["alert"]]
        if active:
            regions_str = ", ".join(r["region"] for r in active)
            severities = [r["extreme_days"] for r in active]
            severity = "high" if max(severities) >= 3 else "medium"
            return {
                "detected": True,
                "detail": f"极端天气: {regions_str}未来7天有{max(severities)}天极端条件",
                "severity": severity,
                "direction": 1,  # 极端天气利多
                "regions": active,
            }
        return {"detected": False}
    except ImportError:
        pass
    except Exception:
        pass
    return {"detected": False}


def check_news_for_shock(days_back=3):
    """检测新闻中的黑天鹅信号"""
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    
    def search_news(query, keywords):
        try:
            r = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": query,
                    "from": from_date,
                    "language": "zh",
                    "pageSize": 10,
                    "sortBy": "relevancy",
                    "apiKey": NEWS_API_KEY,
                },
                timeout=8
            )
            if r.status_code != 200:
                return []
            
            articles = r.json().get("articles", [])
            hits = []
            for a in articles:
                text = f"{a.get('title', '')} {a.get('description', '')}".lower()
                found = [kw for kw in keywords if kw.lower() in text]
                if found:
                    hits.append({
                        "title": a["title"][:60],
                        "date": a.get("publishedAt", "")[:10],
                        "matched": found[:3],
                    })
            return hits[:5]
        except:
            return []
    
    results = {}
    
    # 政策突变检测
    policy_hits = search_news("玉米 OR 粮食 OR 农产品 拍卖 OR 关税 OR 进口 OR 补贴", POLICY_SHOCK_KW)
    if policy_hits:
        results["policy"] = policy_hits
    
    # 国际冲击检测
    intl_hits = search_news("CBOT OR 玉米 OR 俄乌 OR 粮食 暴涨 OR 暴跌 OR 协议", INTERNATIONAL_SHOCK_KW)
    if intl_hits:
        results["international"] = intl_hits
    
    # 疫情/物流
    log_hits = search_news("玉米 OR 粮食 OR 物流 封控 OR 停运 OR 港口", LOGISTICS_KW)
    if log_hits:
        results["logistics"] = log_hits
    
    return results


def check_cbot_shock():
    """检测CBOT最近有无暴涨暴跌"""
    try:
        import akshare as ak
        df = ak.futures_foreign_hist(symbol="C")
        df['date'] = df['date'].astype(str)
        if len(df) < 2:
            return None
        
        latest = float(df.iloc[-1]["close"])
        prev = float(df.iloc[-2]["close"])
        chg_pct = (latest - prev) / prev * 100
        
        if abs(chg_pct) >= 5:
            return {
                "detected": True,
                "chg_pct": round(chg_pct, 1),
                "direction": 1 if chg_pct > 0 else -1,
                "detail": f"CBOT单日{chg_pct:+.1f}%",
                "severity": "high" if abs(chg_pct) >= 7 else "medium",
            }
        
        # 3日内累计涨跌
        if len(df) >= 4:
            three_days_ago = float(df.iloc[-4]["close"])
            cumulative = (latest - three_days_ago) / three_days_ago * 100
            if abs(cumulative) >= 8:
                return {
                    "detected": True,
                    "chg_pct": round(cumulative, 1),
                    "direction": 1 if cumulative > 0 else -1,
                    "detail": f"CBOT 3日累计{cumulative:+.1f}%",
                    "severity": "medium",
                }
        
        return None
    except:
        return None


# ─────────────────────────────────────────
# 综合判断
# ─────────────────────────────────────────
def detect_black_swan():
    """
    综合检测所有黑天鹅类型
    返回详细判决
    """
    result = {
        "is_black_swan": False,
        "type": None,
        "direction": 0,
        "severity": "low",
        "interval_multiplier": 2.0,
        "reason": "",
        "expiry": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
        "details": [],
    }
    
    # 1. 极端天气
    weather = check_weather_extreme()
    if weather.get("detected"):
        result["is_black_swan"] = True
        result["type"] = "weather"
        result["direction"] = weather["direction"]
        result["severity"] = weather.get("severity", "medium")
        result["reason"] = weather["detail"]
        result["details"].append(weather)
        result["expiry"] = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        result["interval_multiplier"] = 3.0
    
    # 2. 国际冲击（CBOT单日≥5%）
    cbot = check_cbot_shock()
    if cbot and result["severity"] in ("low", "medium"):
        result["is_black_swan"] = True
        result["type"] = "international"
        result["direction"] = cbot["direction"]
        result["severity"] = cbot.get("severity", "high")
        result["reason"] = cbot["detail"]
        result["details"].append(cbot)
        result["expiry"] = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        result["interval_multiplier"] = 2.5
    
    # 3. 新闻检测（政策/物流/国际协议）
    news_alerts = check_news_for_shock()
    if news_alerts:
        for shock_type, hits in news_alerts.items():
            if not result["details"] or result["severity"] == "low":
                result["details"].append({"type": shock_type, "hits": hits})
                result["is_black_swan"] = True
                result["type"] = shock_type
                result["severity"] = "medium"
                result["reason"] = f"{shock_type}: {hits[0]['title'][:40]}"
                result["expiry"] = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    
    # 方向权重：如果多个黑天鹅同时触发，按优先级
    # 天气 > 国际 > 政策 > 物流
    for d in result["details"]:
        if d.get("direction"):
            result["direction"] = d["direction"]
            break
    
    return result


# ─────────────────────────────────────────
# 接口函数
# ─────────────────────────────────────────
def get_black_swan_mode():
    """
    供 analysis.py cron 调用
    三级黑天鹅模式（P3-1升级版）:
      level=0: 正常模式
      level=1: 警戒（可能有黑天鹅，扩大区间1.5x，方向偏倾向）
      level=2: 确认（多数据源确认，方向50%加权+50%信号）
      level=3: 危机（硬强制，CBOT≥±7%或极端天气持续3天以上）
    
    返回: {
      'active': bool,
      'level': 0-3,
      'direction': -1/0/1,   # 强制方向（仅level3）
      'direction_bias': -1/0/1,  # 方向偏倾向（level1-2）
      'interval_mult': float,  # 区间倍数
      'reason': str,
      'expiry': str,
    }
    """
    try:
        # 冷却期检查
        cool_file = WORKSPACE / ".black_swan_cooldown"
        if cool_file.exists():
            cool_until = datetime.fromisoformat(cool_file.read_text().strip())
            if datetime.now() < cool_until:
                return {"active": False, "level": 0, "reason": "冷却期内"}
            else:
                cool_file.unlink(missing_ok=True)
        
        # 优先读缓存（15分钟有效）
        if CACHE.exists():
            cache = json.loads(CACHE.read_text())
            age = datetime.now() - datetime.fromisoformat(cache.get("checked_at", "2000-01-01"))
            if age.total_seconds() < 900:
                return cache.get("mode", {"active": False, "level": 0})
        
        bs = detect_black_swan()
        
        # 三级判定
        level = 0
        direction_bias = 0
        interval_mult = 2.0
        
        if bs["is_black_swan"]:
            sev = bs.get("severity", "low")
            # 检查是否CBOT≥±7%或天气持续3天
            cbot = check_cbot_shock()
            is_crisis = (cbot and cbot.get("detected") and abs(cbot.get("chg_pct", 0)) >= 7)
            
            if is_crisis:
                level = 3  # 危机
                interval_mult = 3.0
                direction_bias = bs["direction"]
                # 自动设置24小时冷却期
                cool_until = datetime.now() + timedelta(hours=24)
                cool_file.write_text(cool_until.isoformat())
            elif sev == "high":
                level = 2  # 确认
                interval_mult = 2.5
                direction_bias = bs["direction"]
            else:
                level = 1  # 警戒
                interval_mult = 1.5
                direction_bias = bs["direction"]
        
        # 计算方向权重占比（level1: 20%黑天鹅/80%信号, level2: 50%/50%)
        bias_weight = {1: 0.2, 2: 0.5, 3: 1.0}.get(level, 0.0)
        
        mode = {
            "active": level > 0,
            "level": level,
            "direction": bs["direction"] if level == 3 else 0,
            "direction_bias": direction_bias,
            "bias_weight": bias_weight,
            "interval_mult": interval_mult,
            "reason": bs["reason"],
            "severity": bs["severity"],
            "expiry": bs["expiry"],
            "cooling": bool(cool_file.exists()),
        }
        
        CACHE.write_text(json.dumps({
            "mode": mode,
            "bs": bs,
            "checked_at": datetime.now().isoformat(),
        }, indent=2, ensure_ascii=False))
        
        return mode
    except Exception as e:
        return {"active": False, "level": 0, "reason": f"检测失败: {e}"}


def set_cooldown(hours=24):
    """手动设置冷却期"""
    cool_file = WORKSPACE / ".black_swan_cooldown"
    cool_until = datetime.now() + timedelta(hours=hours)
    cool_file.write_text(cool_until.isoformat())
    print(f"  冷却期设置至 {cool_until}")


def main():
    print("=" * 60)
    print("🌪️ 黑天鹅检测")
    print("=" * 60)
    
    bs = detect_black_swan()
    
    print(f"\n当前状态: {'🔴 黑天鹅' if bs['is_black_swan'] else '✅ 正常模式'}")
    
    if bs["is_black_swan"]:
        type_icons = {
            "weather": "🌤️",
            "international": "🌍",
            "policy": "🏛️",
            "logistics": "🚛",
        }
        icon = type_icons.get(bs["type"], "⚡")
        print(f"  类型: {icon} {bs['type']}")
        print(f"  严重程度: {'🔴高' if bs['severity']=='high' else '🟡中'}")
        print(f"  强制方向: {'偏多' if bs['direction'] > 0 else '偏空' if bs['direction'] < 0 else '无'}")
        print(f"  区间倍数: {bs['interval_multiplier']}x")
        print(f"  原因: {bs['reason']}")
        print(f"  过期: {bs['expiry']}")
        
        for d in bs.get("details", []):
            if "type" in d and "hits" in d:
                print(f"\n  [{d['type']}] 触发新闻:")
                for h in d["hits"][:3]:
                    print(f"    {h['date']} {h['title']}")
    
    print(f"\n{'='*60}")
    
    mode = get_black_swan_mode()
    print(f"\n调用接口返回:")
    print(f"  active={mode['active']}, direction={mode['direction']}, interval_mult={mode['interval_mult']}")
    if mode.get("reason"):
        print(f"  reason: {mode['reason']}")


if __name__ == "__main__":
    main()
