#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政策新闻模块 v3.0
获取和分析玉米相关政策/市场新闻
数据源：东方财富搜索API (search-api-web.eastmoney.com)
策略：宽搜索 + 多关键词 + 相关性过滤 + 情感评分
"""


import requests
import os
import re
import json
import signal
from datetime import datetime, timedelta
from urllib.parse import quote

# 超时保护
_POLICY_TIMEOUT = 30  # 秒

class _TimeoutError(Exception):
    pass

def _policy_timeout_handler(signum, frame):
    raise _TimeoutError(f"新闻获取超时（{_POLICY_TIMEOUT}秒）")

def _search_em(keyword, page=1, size=20):
    """东方财富搜索API"""
    param = json.dumps({
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "pageNum": page,
        "pageSize": size
    }, ensure_ascii=False)
    url = f"https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery&param={quote(param)}"
    _headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://so.eastmoney.com/",
    }
    try:
        r = requests.get(url, timeout=15, headers=_headers)
        m = re.search(r'jQuery\((.*)\)', r.text)
        if m:
            return json.loads(m.group(1))
    except:
        pass
    return None

# ─────────────────────────────────────────
# 搜索关键词（三层覆盖）
# ─────────────────────────────────────────

# 第一层：精准玉米关键词（标题通常直接含"玉米"）
_KEYWORDS_DIRECT = [
    "玉米期货",
    "玉米主力",
    "玉米价格",
    "玉米行情",
    "芝加哥玉米",
    "DCE玉米",
    "美国玉米",
]

# 第二层：关联关键词（内容可能含玉米，拓宽来源）
_KEYWORDS_RELATED = [
    "农产品期货",
    "每日龙虎榜",
    "商品期货",
    "糙米拍卖",
    "USDA玉米",
    "玉米出口",
    "玉米种植",
    "玉米拍卖",
]

# 第三层：放宽关键词（资讯栏目前缀式标题，如"农产品 | xxx"）
_KEYWORDS_BROAD = [
    "农产品",
    "期货资讯",
    "饲料原料",
]


# ─────────────────────────────────────────
# 评分关键词库（与v2.0相同）
# ─────────────────────────────────────────

POLICY_KEYWORDS = {
    "临储拍卖":         -1.5,
    "政策拍卖":         -1.5,
    "定向拍卖":         -1.0,
    "投放市场":         -1.0,
    "轮出":            -0.5,
    "竞价销售":         -1.0,
    "销售底价":         -0.5,
    "成交率":           -0.3,
    "高溢价":          +0.5,
    "临储收购":         +1.5,
    "敞开收购":         +1.0,
    "保护价":           +1.0,
    "收储价格":         +1.0,
    "补贴政策":         +0.5,
    "生产者补贴":       +0.5,
    "玉米补贴":         +0.5,
    "扩大进口":         -1.0,
    "增加进口配额":     -1.0,
    "进口玉米":         -0.5,
    "美玉米进口":       -0.5,
    "巴西玉米":         -0.5,
    "乌克兰玉米":        -0.5,
    "零关税":           -0.5,
    "减产":            +1.0,
    "产量下降":         +0.8,
    "旱情":            +0.8,
    "高温干旱":         +0.8,
    "洪涝":            +0.8,
    "台风":            +0.5,
    "草地贪夜蛾":       +0.8,
    "病虫害":          +0.5,
    "丰收":            -0.8,
    "丰产":            -0.8,
    "产量增加":         -0.5,
    "产量创新高":       -1.0,
    "饲料需求":         +0.3,
    "生猪存栏":         +0.3,
    "需求旺盛":         +0.5,
    "需求疲软":         -0.5,
    "保供稳价":         -0.3,
    "调控":             -0.3,
    "供需平衡":          0.0,
    "市场化":            0.0,
    "拍卖":              -0.5,
    "政策粮投放":        -1.0,
    "轮出销售":          -0.5,
    "竞价":              -0.5,
    "震荡走低":          -0.3,
    "震荡偏弱":          -0.3,
    "弱势":              -0.3,
    "反弹":              +0.3,
    "走强":              +0.3,
    "创新高":            -0.5,
    "高位":              -0.3,
}

def is_relevant_news(title, content):
    """放宽相关性判断：标题或内容中任意一处含玉米相关词即算相关"""
    CORN_KW = ["玉米", "DCE", "大商所", "连玉米"]
    if any(kw in title for kw in CORN_KW):
        return True
    # content 可能包含 <em>玉米</em> 等HTML标签，需要先清除
    clean_content = re.sub(r'<[^>]+>', '', content)
    if any(kw in clean_content for kw in CORN_KW):
        return True
    return False

def score_item(title, content):
    text = title + " " + re.sub(r'<[^>]+>', '', content)
    score = 0
    matched = []
    for keyword, weight in POLICY_KEYWORDS.items():
        if keyword in text:
            score += weight
            matched.append((keyword, weight))

    # 特殊强信号
    strong_signals = [
        ("临储拍卖", -2.0), ("政策拍卖启动", -2.0), ("临储停拍", +2.0),
        ("扩大玉米进口", -1.5), ("增加进口配额", -1.5), ("玉米丰收", -1.0),
        ("草地贪夜蛾爆发", +1.5), ("玉米减产", +1.5), ("玉米跌", -0.3),
        ("玉米涨", +0.3), ("承压", -0.3), ("支撑", +0.3),
    ]
    for signal, bonus in strong_signals:
        if signal in title:
            score += bonus
            matched.append((signal, bonus))

    score = max(-2.5, min(2.5, score))
    score = round(score, 1)
    return {"score": score, "matched": matched[:4], "tags": []}

def _load_seen_titles(cache_path):
    seen = set()
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    seen.add(line)
    except FileNotFoundError:
        pass
    return seen

def _save_seen_titles(cache_path, new_titles):
    with open(cache_path, "a", encoding="utf-8") as f:
        for t in new_titles:
            f.write(t + "\n")

from constants import NEWS_DEDUP_CACHE as _DC
_DEDUP_CACHE = str(_DC)

def fetch_corn_news(days_back=10, max_articles=30):
    """获取玉米相关新闻（东方财富搜索API）

    Args:
        days_back: 回溯天数（放宽到10天，可以拿到更完整的周度回顾）
        max_articles: 最多返回条数
    """
    all_items = []
    seen_titles = set()
    cutoff = datetime.now() - timedelta(days=days_back)

    historical_seen = _load_seen_titles(_DEDUP_CACHE)
    new_titles_for_cache = []

    keywords = _KEYWORDS_DIRECT + _KEYWORDS_RELATED

    for kw in keywords:
        result = _search_em(kw, page=1, size=20)
        if not result or not result.get('result', {}).get('cmsArticleWebOld'):
            continue

        for item in result['result']['cmsArticleWebOld']:
            if len(all_items) >= max_articles:
                break

            title = re.sub(r'<[^>]+>', '', item.get('title', '')).strip()
            content = re.sub(r'<[^>]+>', '', item.get('content', ''))[:300]
            pub_time = item.get('date', '')
            source = item.get('mediaName', '')
            url = item.get('url', '')
            art_code = item.get('code', '')

            # 去重
            title_key = title[:50]
            if title_key in seen_titles:
                continue

            # 时间过滤
            try:
                pub_dt = datetime.strptime(pub_time[:19], "%Y-%m-%d %H:%M:%S")
            except:
                continue
            if pub_dt < cutoff:
                continue

            # 相关性过滤（宽松版：标题 OR 内容含玉米即算）
            if not is_relevant_news(title, content):
                continue

            seen_titles.add(title_key)
            analysis = score_item(title, content)

            all_items.append({
                "keyword": kw,
                "title": title,
                "content": content,
                "time": pub_time,
                "source": source,
                "url": url,
                "code": art_code,
                **analysis,
            })

        if len(all_items) >= max_articles:
            break

    # 如果直接相关关键词不够，再补充放宽关键词
    if len(all_items) < 10:
        # 从"每日龙虎榜"里找含玉米的
        for kw in ["每日龙虎榜", "农产品"]:
            result = _search_em(kw, page=1, size=20)
            if not result or not result.get('result', {}).get('cmsArticleWebOld'):
                continue
            for item in result['result']['cmsArticleWebOld']:
                if len(all_items) >= max_articles:
                    break
                title = re.sub(r'<[^>]+>', '', item.get('title', '')).strip()
                content = re.sub(r'<[^>]+>', '', item.get('content', ''))[:300]
                pub_time = item.get('date', '')
                title_key = title[:50]

                if title_key in seen_titles:
                    continue
                try:
                    pub_dt = datetime.strptime(pub_time[:19], "%Y-%m-%d %H:%M:%S")
                except:
                    continue
                if pub_dt < cutoff:
                    continue
                if not is_relevant_news(title, content):
                    continue

                seen_titles.add(title_key)
                analysis = score_item(title, content)
                all_items.append({
                    "keyword": kw,
                    "title": title,
                    "content": content,
                    "time": pub_time,
                    "source": item.get('mediaName', ''),
                    "url": item.get('url', ''),
                    **analysis,
                })

    # 按时间排序
    all_items.sort(key=lambda x: x["time"], reverse=True)

    # 更新去重缓存
    for item in all_items:
        title_key = item["title"][:50]
        if title_key not in historical_seen:
            new_titles_for_cache.append(title_key)
    if new_titles_for_cache:
        _save_seen_titles(_DEDUP_CACHE, new_titles_for_cache)

    return all_items

def analyze_news(news_items):
    if not news_items:
        return {
            "has_news": False, "count": 0, "overall_score": 0,
            "signal": "无相关政策新闻", "signal_emoji": "⚪", "top_items": [],
        }

    recent = news_items[:8]
    weights = [1.0, 0.8, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]
    w_sum = sum(weights[:len(recent)])

    direction_sum = sum(item["score"] * w for item, w in zip(recent, weights))
    direction_avg = direction_sum / w_sum if w_sum > 0 else 0

    # 更细分类
    if direction_avg >= 1.0:
        signal, emoji = "显著利多", "🟢"
    elif direction_avg >= 0.4:
        signal, emoji = "轻微利多", "🟢"
    elif direction_avg <= -1.0:
        signal, emoji = "显著利空", "🔴"
    elif direction_avg <= -0.4:
        signal, emoji = "轻微利空", "🔴"
    else:
        signal, emoji = "中性", "⚪"

    return {
        "has_news": True,
        "count": len(news_items),
        "overall_score": round(direction_avg, 2),
        "signal": signal,
        "signal_emoji": emoji,
        "top_items": recent,
    }

def get_policy_news(days_back=10, total_timeout=45):
    """获取政策新闻并分析

    Args:
        days_back: 回溯天数
        total_timeout: 总超时秒数（默认45秒），防止多个关键词串行请求卡死
    """
    print(f"[新闻] 正在获取玉米政策新闻...")
    old_handler = signal.signal(signal.SIGALRM, _policy_timeout_handler)
    signal.alarm(total_timeout)
    try:
        items = fetch_corn_news(days_back=days_back)
    except _TimeoutError:
        items = []
        print(f"[新闻]  ⚠️ 总超时（{total_timeout}秒），跳过新闻")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
    print(f"[新闻]  → 找到 {len(items)} 条相关新闻")

    if items:
        print(f"[新闻]  最新: {items[0]['time'][:16]} - {items[0]['title'][:50]}")

    result = analyze_news(items)
    return result, items

if __name__ == "__main__":
    result, items = get_policy_news(days_back=10)

    print(f"\n{'='*60}")
    print(f"[新闻] 政策新闻动态分析 v3.0")
    print(f"{'='*60}")
    print(f"  {result['signal_emoji']} {result['signal']}（综合评分: {result['overall_score']:+.2f}）")
    print(f"  共 {result['count']} 条相关新闻")

    print(f"\n[新闻] 近期新闻详情（前8条）")
    for i, item in enumerate(result["top_items"], 1):
        sc = item["score"]
        emoji = "🟢" if sc > 0 else "🔴" if sc < 0 else "⚪"
        matched = [f"{k}({w:+.1f})" for k, w in item.get("matched", [])[:2]]
        match_str = " | ".join(matched) if matched else ""
        print(f"\n  {i}. {emoji} {sc:+.1f}分 | {item['time'][:16]} | {item['source']}")
        print(f"     {item['title'][:60]}")
        if match_str:
            print(f"     关键词: {match_str}")

