#!/usr/bin/env python3
"""
乙醇/生柴政策信号监控 (P2-4)

通过 NewsAPI 跟踪玉米乙醇和生物柴油相关政策事件
同时依赖原油价格作为间接信号（原油↑→乙醇经济性↑→玉米需求↑）

数据源:
  1. NewsAPI (free tier, 500 queries/day) — 中英文新闻跟踪
  2. 原油期货价格（已有 INE SC0）—乙醇需求联动
  3. EPA RFS 政策事件（手动+自动）

信号逻辑:
  - EPA 提高掺混量 → 利多玉米
  - 原油价格高位 → 乙醇经济性提升 → 利多玉米
  - 中国乙醇政策变动 → 影响国内需求
  - 生柴强制掺混上调 → 利好豆油需求（间接利多大豆→玉米联动）

用法:
  python3 ethanol_signal.py                  # 跟踪最新乙醇政策新闻并生成信号
  python3 ethanol_signal.py --score-only     # 只输出信号分数（供analysis.py调用）
"""

import json
import requests
from pathlib import Path
from datetime import datetime, timedelta

WORKSPACE = Path(__file__).parent
CACHE = WORKSPACE / "ethanol_cache.json"

# NewsAPI
NEWS_API_KEY = ""  # ponytail: 改用环境变量 NEWSAPI_KEY

# 乙醇/生柴政策关键词
ETHANOL_KEYWORDS = [
    "乙醇", "燃料乙醇", "生质乙醇", "生物燃料", "再生燃料",
    "E15", "E10", "RFS", "Renewable Fuel Standard",
    "EPA", "生质柴油", "再生柴油", "biodiesel", "renewable diesel",
    "玉米乙醇", "corn ethanol", "乙醇汽油", "E85",
    "RIN", "Renewable Identification Number",
]

# 政策方向映射
POSITIVE_KEYWORDS = [
    "提高", "增加", "上调", "扩大", "强制", "mandate", "increase",
    "raise", "boost", "expand", "record", "新高", "创纪录", "支撑",
]
NEGATIVE_KEYWORDS = [
    "降低", "减少", "下调", "取消", "豁免", "waiver", "exempt",
    "reduce", "lower", "cut", "suspend", "取消",
]


def fetch_ethanol_news(days_back=7):
    """从NewsAPI获取乙醇相关新闻"""
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    
    results = []
    
    # 中文搜索
    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": "乙醇 OR 生物燃料 OR 生质柴油",
                "from": from_date,
                "language": "zh",
                "sortBy": "relevancy",
                "pageSize": 10,
                "apiKey": NEWS_API_KEY,
            },
            timeout=10
        )
        if r.status_code == 200:
            for a in r.json().get("articles", []):
                results.append({
                    "title": a.get("title", ""),
                    "source": a.get("source", {}).get("name", ""),
                    "date": a.get("publishedAt", "")[:10],
                    "url": a.get("url", ""),
                    "desc": a.get("description", ""),
                    "lang": "zh",
                })
    except Exception as e:
        print(f"  ⚠️ 中文新闻获取失败: {e}")
    
    # 英文搜索（USDA/EPA相关）
    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": "corn ethanol OR RFS OR renewable fuel standard",
                "from": from_date,
                "language": "en",
                "sortBy": "relevancy",
                "pageSize": 10,
                "apiKey": NEWS_API_KEY,
            },
            timeout=10
        )
        if r.status_code == 200:
            for a in r.json().get("articles", []):
                results.append({
                    "title": a.get("title", ""),
                    "source": a.get("source", {}).get("name", ""),
                    "date": a.get("publishedAt", "")[:10],
                    "url": a.get("url", ""),
                    "desc": a.get("description", ""),
                    "lang": "en",
                })
    except Exception as e:
        print(f"  ⚠️ 英文新闻获取失败: {e}")
    
    # 去重
    seen = set()
    unique = []
    for r in results:
        key = r["title"][:60]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    
    return unique


def classify_news_signal(title, desc):
    """判断单条新闻的利多/利空方向"""
    text = (title + " " + (desc or "")).lower()
    
    # 严格相关性过滤：必须包含乙醇/生柴相关关键词
    is_relevant = any(k.lower() in text for k in ETHANOL_KEYWORDS)
    if not is_relevant:
        return 0, False
    
    # 排除噪声：没有农业/能源/政策关键词的新闻不算
    context_words = ["玉米", "corn", "燃料", "fuel", "炼油", "refiner",
                     "汽油", "gasoline", "柴油", "diesel", "政策", "policy",
                     "环保", "EPA", "能源", "energy", "原油", "oil",
                     "掺混", "blend", "强制", "mandate", "汽车", "vehicle"]
    has_context = any(k in text for k in context_words)
    if not has_context:
        return 0, False
    
    pos_score = sum(1 for kw in POSITIVE_KEYWORDS if kw.lower() in text)
    neg_score = sum(1 for kw in NEGATIVE_KEYWORDS if kw.lower() in text)
    
    if pos_score > neg_score:
        return 1, True
    elif neg_score > pos_score:
        return -1, True
    return 0, True


def compute_signal(news_list):
    """综合所有新闻给出乙醇信号"""
    if not news_list:
        return {'direction': 0, 'score': 0, 'detail': '无近期新闻', 'news_count': 0}
    
    total_score = 0
    relevant_count = 0
    top_news = []
    
    for n in news_list:
        direction, has_oil = classify_news_signal(n["title"], n.get("desc", ""))
        if direction != 0:
            total_score += direction
            relevant_count += 1
            top_news.append(f"{'🟢' if direction > 0 else '🔴'}{n['title'][:40]}")
    
    if relevant_count == 0:
        return {'direction': 0, 'score': 0, 'detail': '无明确政策信号', 'news_count': len(news_list)}
    
    avg_dir = total_score / relevant_count
    
    if avg_dir > 0.3:
        return {
            'direction': 1,
            'score': min(abs(total_score) / relevant_count * 0.8, 1.0),
            'detail': f"乙醇政策偏多({relevant_count}条)",
            'news_top': top_news[:3],
            'news_count': len(news_list),
            'relevant': relevant_count,
        }
    elif avg_dir < -0.3:
        return {
            'direction': -1,
            'score': min(abs(total_score) / relevant_count * 0.8, 1.0),
            'detail': f"乙醇政策偏空({relevant_count}条)",
            'news_top': top_news[:3],
            'news_count': len(news_list),
            'relevant': relevant_count,
        }
    else:
        return {
            'direction': 0,
            'score': 0.3,
            'detail': f"乙醇政策中性({relevant_count}条)",
            'news_count': len(news_list),
            'relevant': relevant_count,
        }


def main():
    print("=" * 50)
    print("乙醇/生柴政策信号跟踪")
    print("=" * 50)
    
    print(f"\n获取乙醇政策新闻...")
    news = fetch_ethanol_news(days_back=14)
    
    if news:
        print(f"  找到 {len(news)} 条相关新闻:")
        for n in news[:5]:
            direction, _ = classify_news_signal(n["title"], n.get("desc", ""))
            icon = "🟢" if direction > 0 else ("🔴" if direction < 0 else "⚪")
            print(f"  {icon} [{n['date']}] {n['title'][:60]}")
    else:
        print("  无相关新闻")
    
    signal = compute_signal(news)
    print(f"\n综合信号:")
    print(f"  方向: {signal['direction']} (1=多, -1=空, 0=中性)")
    print(f"  强度: {signal['score']:.2f}")
    print(f"  说明: {signal['detail']}")
    print(f"  新闻总数: {signal.get('news_count', 0)} | 相关: {signal.get('relevant', 0)}")
    
    if signal.get('news_top'):
        print(f"  热点: {', '.join(signal['news_top'])}")
    
    # 缓存
    cache = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "news": [{"title": n["title"], "date": n["date"], "source": n["source"], "lang": n["lang"]} for n in news[:20]],
        "signal": signal,
    }
    CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    print(f"\n已缓存至 {CACHE}")


def get_ethanol_signal():
    """供 analysis.py 调用"""
    try:
        if CACHE.exists():
            cache = json.loads(CACHE.read_text())
            updated = datetime.fromisoformat(cache.get("fetched_at", "2000-01-01"))
            if (datetime.now() - updated).days < 1:
                return cache.get("signal", {'direction': 0, 'score': 0, 'detail': '缓存过期'})
        
        news = fetch_ethanol_news(days_back=14)
        return compute_signal(news)
    except Exception as e:
        return {'direction': 0, 'score': 0, 'detail': f'获取失败: {e}'}


if __name__ == "__main__":
    main()
