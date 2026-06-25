"""
v6 数据获取层

职责：抓取外部数据并标准化为 predictor/signals 可消费的 DataFrame
或轻量 dict/list。此模块不计算技术信号，也不依赖 v6 预测/格式化模块。
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Tuple
from urllib.parse import quote

import pandas as pd
import requests

from config import HISTORY_DIR

DEFAULT_TIMEOUT = 15


def _meta(source: str, freshness: str = "", error: str | None = None, **extra: Any) -> Dict[str, Any]:
    data = {
        "source": source,
        "freshness": freshness or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": error,
    }
    data.update(extra)
    return data


def _empty_ohlcv() -> pd.DataFrame:
    return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])


def _call_with_timeout(func: Callable[..., Any], timeout: int = DEFAULT_TIMEOUT, **kwargs: Any) -> Any:
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(func, **kwargs)
    try:
        return future.result(timeout=timeout)
    except TimeoutError as exc:
        raise TimeoutError(f"timeout after {timeout}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _coalesce_column(df: pd.DataFrame, names: Iterable[str]) -> pd.Series | None:
    normalized = {str(col).strip().lower(): col for col in df.columns}
    for name in names:
        key = name.strip().lower()
        if key in normalized:
            return df[normalized[key]]
    return None


def _normalize_ohlcv(raw: pd.DataFrame, days: int) -> pd.DataFrame:
    if raw is None or raw.empty:
        return _empty_ohlcv()

    col_map = {
        "date": ["date", "日期", "交易日期", "trade_date", "time"],
        "open": ["open", "开盘", "开盘价"],
        "high": ["high", "最高", "最高价"],
        "low": ["low", "最低", "最低价"],
        "close": ["close", "收盘", "收盘价", "最新价"],
        "volume": ["volume", "成交量", "vol"],
    }
    out = pd.DataFrame()
    for target, candidates in col_map.items():
        series = _coalesce_column(raw, candidates)
        if series is None:
            return _empty_ohlcv()
        out[target] = series

    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    out = out.sort_values("date").tail(days).reset_index(drop=True)
    return out[["date", "open", "high", "low", "close", "volume"]]


def _freshness_from_df(df: pd.DataFrame) -> str:
    if df.empty or "date" not in df:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return pd.to_datetime(df["date"].iloc[-1]).strftime("%Y-%m-%d")


def _fetch_ak_ohlcv(func_name: str, source: str, days: int, **kwargs: Any) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    try:
        import akshare as ak

        func = getattr(ak, func_name)
        try:
            raw = _call_with_timeout(func, timeout=DEFAULT_TIMEOUT, **kwargs)
        except TypeError:
            kwargs.pop("market", None)
            raw = _call_with_timeout(func, timeout=DEFAULT_TIMEOUT, **kwargs)
        df = _normalize_ohlcv(raw, days)
        return df, _meta(source, _freshness_from_df(df), None, rows=len(df))
    except Exception as exc:
        return _empty_ohlcv(), _meta(source, error=str(exc), rows=0)


def fetch_dce_corn(days: int = 90) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """获取 DCE 玉米主力合约日线，标准列：date/open/high/low/close/volume。"""
    return _fetch_ak_ohlcv(
        "futures_zh_daily_sina",
        "akshare.futures_zh_daily_sina:C0",
        days,
        symbol="C0",
    )


def fetch_dce_soymeal(days: int = 90) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """获取 DCE 豆粕主力合约日线。"""
    return _fetch_ak_ohlcv(
        "futures_zh_daily_sina",
        "akshare.futures_zh_daily_sina:M0",
        days,
        symbol="M0",
    )


def fetch_cbot_corn(days: int = 10) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """获取 CBOT 玉米连续合约日线，并在 metadata 中返回 chg_pct。"""
    df, meta = _fetch_ak_ohlcv(
        "futures_foreign_hist",
        "akshare.futures_foreign_hist:CBOT/C",
        days,
        symbol="C",
        market="CBOT",
    )
    chg_pct = None
    if len(df) >= 2:
        prev = float(df["close"].iloc[-2])
        latest = float(df["close"].iloc[-1])
        chg_pct = round((latest - prev) / prev * 100, 4) if prev else None
    meta["chg_pct"] = chg_pct
    return df, meta


def fetch_soybean_cbot(days: int = 10) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """获取 CBOT 大豆连续合约日线，供后续相关性扩展使用。"""
    return _fetch_ak_ohlcv(
        "futures_foreign_hist",
        "akshare.futures_foreign_hist:CBOT/S",
        days,
        symbol="S",
        market="CBOT",
    )


CORN_REGIONS = [
    ("黑龙江", 45.75, 126.63, 0.11, "东北"),
    ("吉林", 43.88, 125.32, 0.10, "东北"),
    ("辽宁", 41.80, 123.43, 0.08, "东北"),
    ("内蒙古", 40.82, 111.65, 0.08, "东北"),
    ("山东", 36.65, 117.12, 0.10, "黄淮海"),
    ("河南", 34.76, 113.65, 0.10, "黄淮海"),
    ("河北", 38.04, 114.48, 0.08, "黄淮海"),
    ("甘肃", 36.06, 103.83, 0.05, "西北"),
    ("宁夏", 38.47, 106.27, 0.04, "西北"),
    ("新疆", 43.83, 87.62, 0.06, "西北"),
    ("四川", 30.67, 104.06, 0.07, "西南"),
    ("云南", 25.04, 102.71, 0.06, "西南"),
    ("贵州", 26.65, 106.63, 0.07, "西南"),
]


def _score_weather_region(name: str, lat: float, lon: float, weight: float, zone: str) -> Dict[str, Any]:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        "&current_weather=true&timezone=Asia%2FShanghai&forecast_days=7"
    )
    response = requests.get(url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    data = response.json()
    current = data.get("current_weather", {})
    daily = data.get("daily", {})
    temp = float(current.get("temperature", 20) or 20)
    precip = sum(float(x or 0) for x in daily.get("precipitation_sum", []))
    hot_days = sum(1 for t in daily.get("temperature_2m_max", []) if float(t or 0) > 35)

    score = 0.0
    factors = []
    if temp > 38:
        score -= 1.0
        factors.append("高温")
    elif temp > 35:
        score -= 0.3
        factors.append("偏热")
    elif 15 <= temp <= 30:
        score += 0.2
        factors.append("适宜")

    if precip > 30:
        score -= 0.8
        factors.append("洪涝")
    elif precip > 10:
        score += 0.3
        factors.append("降水充足")
    elif precip < 3:
        score -= 0.5
        factors.append("干旱")

    if hot_days >= 3:
        score -= 0.5
        factors.append(f"{hot_days}天高温")

    return {
        "name": name,
        "zone": zone,
        "weight": weight,
        "temp": round(temp, 1),
        "precip": round(precip, 1),
        "hot_days": hot_days,
        "score": round(max(-2.0, min(2.0, score)), 2),
        "factors": factors,
    }


def fetch_weather() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """获取中国玉米产区天气评分。返回 weather_score 范围约束在 -2 到 +2。"""
    try:
        results = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(_score_weather_region, name, lat, lon, weight, zone)
                for name, lat, lon, weight, zone in CORN_REGIONS
            ]
            deadline = datetime.now() + timedelta(seconds=DEFAULT_TIMEOUT)
            for future in futures:
                remaining = max(0.1, (deadline - datetime.now()).total_seconds())
                try:
                    results.append(future.result(timeout=remaining))
                except Exception:
                    continue

        if not results:
            raise RuntimeError("no weather regions returned")

        total_weight = sum(float(r["weight"]) for r in results)
        score = sum(float(r["score"]) * float(r["weight"]) for r in results) / total_weight
        score = round(max(-2.0, min(2.0, score)), 2)
        payload = {"weather_score": score, "regions": results}
        return payload, _meta("open-meteo", error=None, region_count=len(results))
    except Exception as exc:
        return {"weather_score": 0.0, "regions": []}, _meta("open-meteo", error=str(exc), region_count=0)


POLICY_KEYWORDS = {
    "临储拍卖": -1.5,
    "政策拍卖": -1.5,
    "定向拍卖": -1.0,
    "投放市场": -1.0,
    "轮出": -0.5,
    "竞价销售": -1.0,
    "临储收购": 1.5,
    "敞开收购": 1.0,
    "保护价": 1.0,
    "补贴政策": 0.5,
    "生产者补贴": 0.5,
    "扩大进口": -1.0,
    "进口玉米": -0.5,
    "零关税": -0.5,
    "减产": 1.0,
    "产量下降": 0.8,
    "旱情": 0.8,
    "高温干旱": 0.8,
    "洪涝": 0.8,
    "丰收": -0.8,
    "丰产": -0.8,
    "产量增加": -0.5,
    "饲料需求": 0.3,
    "需求旺盛": 0.5,
    "需求疲软": -0.5,
    "保供稳价": -0.3,
    "调控": -0.3,
    "拍卖": -0.5,
    "玉米涨": 0.3,
    "玉米跌": -0.3,
    "支撑": 0.3,
    "承压": -0.3,
}

_NEWS_KEYWORDS = [
    "玉米期货",
    "玉米主力",
    "玉米价格",
    "玉米行情",
    "DCE玉米",
    "USDA玉米",
    "玉米出口",
    "玉米种植",
    "玉米拍卖",
    "农产品期货",
]


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _score_policy_item(title: str, content: str) -> Dict[str, Any]:
    text = f"{title} {_strip_html(content)}"
    score = 0.0
    matched = []
    for keyword, weight in POLICY_KEYWORDS.items():
        if keyword in text:
            score += weight
            matched.append((keyword, weight))
    score = round(max(-2.5, min(2.5, score)), 2)
    return {"score": score, "matched": matched[:4]}


def _is_corn_relevant(title: str, content: str) -> bool:
    text = f"{title} {_strip_html(content)}"
    return any(keyword in text for keyword in ["玉米", "DCE", "大商所", "连玉米", "CBOT corn"])


def _search_eastmoney(keyword: str) -> List[Dict[str, Any]]:
    param = json.dumps({
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "pageNum": 1,
        "pageSize": 20,
    }, ensure_ascii=False)
    url = f"https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery&param={quote(param)}"
    response = requests.get(
        url,
        timeout=DEFAULT_TIMEOUT,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://so.eastmoney.com/",
        },
    )
    response.raise_for_status()
    match = re.search(r"jQuery\((.*)\)", response.text)
    if not match:
        return []
    data = json.loads(match.group(1))
    return data.get("result", {}).get("cmsArticleWebOld", []) or []


def _search_newsapi(days: int) -> List[Dict[str, Any]]:
    api_key = os.environ.get("NEWSAPI_KEY")
    if not api_key:
        return []
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    response = requests.get(
        "https://newsapi.org/v2/everything",
        timeout=DEFAULT_TIMEOUT,
        params={
            "q": "corn OR maize OR USDA corn",
            "from": from_date,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 20,
            "apiKey": api_key,
        },
    )
    response.raise_for_status()
    articles = response.json().get("articles", []) or []
    return [
        {
            "title": item.get("title", ""),
            "content": item.get("description", "") or item.get("content", ""),
            "date": item.get("publishedAt", ""),
            "mediaName": (item.get("source") or {}).get("name", "NewsAPI"),
            "url": item.get("url", ""),
        }
        for item in articles
    ]


def fetch_policy_news(days: int = 10) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """获取玉米相关政策/市场新闻，并在 metadata 中返回 policy_signal。"""
    source = "eastmoney"
    try:
        cutoff = datetime.now() - timedelta(days=days)
        items: List[Dict[str, Any]] = []
        seen = set()

        for keyword in _NEWS_KEYWORDS:
            for item in _search_eastmoney(keyword):
                title = _strip_html(item.get("title", ""))
                content = _strip_html(item.get("content", ""))[:300]
                if not title or title[:50] in seen or not _is_corn_relevant(title, content):
                    continue
                pub_time = item.get("date", "")
                try:
                    pub_dt = datetime.strptime(pub_time[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if pub_dt < cutoff:
                    continue
                seen.add(title[:50])
                scored = _score_policy_item(title, content)
                items.append({
                    "keyword": keyword,
                    "title": title,
                    "content": content,
                    "time": pub_time,
                    "source": item.get("mediaName", ""),
                    "url": item.get("url", ""),
                    **scored,
                })
                if len(items) >= 30:
                    break
            if len(items) >= 30:
                break

        if not items:
            source = "newsapi"
            for item in _search_newsapi(days):
                title = _strip_html(item.get("title", ""))
                content = _strip_html(item.get("content", ""))[:300]
                if not title or title[:50] in seen:
                    continue
                seen.add(title[:50])
                scored = _score_policy_item(title, content)
                items.append({
                    "keyword": "NewsAPI",
                    "title": title,
                    "content": content,
                    "time": item.get("date", ""),
                    "source": item.get("mediaName", "NewsAPI"),
                    "url": item.get("url", ""),
                    **scored,
                })

        items.sort(key=lambda x: x.get("time", ""), reverse=True)
        signal = _policy_signal(items)
        freshness = items[0]["time"][:19] if items else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return items, _meta(source, freshness, None, policy_signal=signal, count=len(items))
    except Exception as exc:
        return [], _meta(source, error=str(exc), policy_signal=0.0, count=0)


def _policy_signal(items: List[Dict[str, Any]]) -> float:
    if not items:
        return 0.0
    recent = items[:8]
    weights = [1.0, 0.8, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]
    w_sum = sum(weights[:len(recent)])
    raw = sum(float(item.get("score", 0.0)) * weight for item, weight in zip(recent, weights)) / w_sum
    return round(max(-1.0, min(1.0, raw / 2.5)), 3)


def fetch_trading_calendar(year: int = 2026) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """获取交易日历。若 akshare 字段变化，尽量保留原始列并附带标准 date 列。"""
    source = "akshare.tool_trade_date_hist_sina"
    try:
        import akshare as ak

        raw = _call_with_timeout(ak.tool_trade_date_hist_sina, timeout=DEFAULT_TIMEOUT)
        df = raw.copy()
        date_col = _coalesce_column(df, ["trade_date", "date", "日期"])
        if date_col is not None:
            df["date"] = pd.to_datetime(date_col, errors="coerce")
            df = df[df["date"].dt.year == int(year)].dropna(subset=["date"]).reset_index(drop=True)
        return df, _meta(source, _freshness_from_df(df) if "date" in df else str(year), None, rows=len(df))
    except Exception as exc:
        return pd.DataFrame(columns=["date"]), _meta(source, error=str(exc), rows=0)


def write_cache(name: str, payload: Dict[str, Any]) -> None:
    """小型 JSON 缓存工具，供后续扩展使用。"""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = [
    "fetch_dce_corn",
    "fetch_dce_soymeal",
    "fetch_cbot_corn",
    "fetch_weather",
    "fetch_policy_news",
    "fetch_soybean_cbot",
    "fetch_trading_calendar",
    "write_cache",
]
