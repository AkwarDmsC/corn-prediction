#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国玉米期货每日分析脚本 v5.0
DCE(大连商品交易所)玉米期货 C0 · 单位:元/吨
入口脚本，由cron定时触发，整合数据获取、技术分析、预测生成、新闻影响记录

依赖模块: news, weather, news_impact, prediction_tracker
"""

import akshare as ak
import json
import sys
import os as _os
import statistics
import math
import signal
from datetime import datetime, timedelta

# 本地模块
import weather as wc
import news as pn
import usda_export_sales as usda
import hog_signal as hog
import import_cost as impcost
import ethanol_signal as eth
import port_basis as pb
import black_swan as bs
import signal_decay as sd
import factor_interaction as fi
import data_freshness as df

# ============================================================
# 网络超时保护
# 所有数据获取均受30秒超时保护，防止网络抖动时永久挂起
# ============================================================
TIMEOUT_SECONDS = 30

class TimeoutError(Exception):
    pass

def _timeout_handler(signum, frame):
    raise TimeoutError(f"数据获取超时（{TIMEOUT_SECONDS}秒）")

def fetch_with_timeout(func, *args, **kwargs):
    """对任意函数加TIMEOUT_SECONDS超时保护，返回None表示超时"""
    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(TIMEOUT_SECONDS)
    try:
        result = func(*args, **kwargs)
        return result
    except TimeoutError:
        print(f"  ⚠️ 超时中断: {func.__name__}（{TIMEOUT_SECONDS}秒）")
        return None
    except Exception as e:
        print(f"  ⚠️ 获取异常: {func.__name__} → {e}")
        return None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

# ============================================================
# 交易日判断(v1.8, v1.9 自动节假日)
# DCE玉米期货:周一~周五开盘,周六周日休市,国内法定节假日休市
# 数据来源: akshare.tool_trade_date_hist_sina() (A股交易所日历,DCE跟随)
# ============================================================

# 启动时一次性加载全量交易日历(约8700+条,覆盖未来数年)，超时保护
try:
    _TRADE_DATE_DF = fetch_with_timeout(ak.tool_trade_date_hist_sina)
    if _TRADE_DATE_DF is not None:
        _TRADE_DATE_SET = set(_TRADE_DATE_DF['trade_date'].astype(str).tolist())
        _TRADE_DATE_LOADED = True
    else:
        _TRADE_DATE_SET = None
        _TRADE_DATE_LOADED = False
except Exception:
    _TRADE_DATE_SET = None
    _TRADE_DATE_LOADED = False

def is_trading_day(dt):
    """
    判断DCE玉米期货是否为开盘日(含国内法定节假日)
    - 周六周日:非开盘日
    - 周五15:00后:视同休市(下一交易日是周一,夜盘21:00开始但对当日分析无增量数据)
    - 国内法定节假日:自动从A股交易日历排除(春节/国庆/劳动节/清明/端午/中秋等)
    - 若日历加载失败,回退到纯周末判断
    """
    date_str = dt.strftime('%Y-%m-%d')
    # 已休市:周五15:00后
    if dt.weekday() == 4 and dt.hour >= 15:
        return False
    # 非交易日:周末(已在日历中会直接被排除,但提前判断可减少查询)
    if dt.weekday() >= 5:
        return False
    # 查A股交易所日历(含所有中国法定节假日调休)
    if _TRADE_DATE_LOADED and _TRADE_DATE_SET is not None:
        return date_str in _TRADE_DATE_SET
    # 兜底:仅用周末判断(节假日可能误判,但极少发生)
    return True

def next_trading_day(dt):
    """
    计算下一个DCE玉米期货交易日(自动跳过周末+法定节假日)
    - 使用A股交易所日历:春节/国庆等长假自动处理
    """
    d = dt + timedelta(days=1)
    max_iter = 30  # 最多找30天,足够覆盖最长假期
    for _ in range(max_iter):
        if is_trading_day(d):
            return d
        d += timedelta(days=1)
    # 兜底:不应走到这里
    return d

def trading_day_gap(dt):
    """
    计算到下一交易日的天数(自然日)
    - 0:今天仍可交易(在交易日日历中 且 15:00前)
    - >=1:已休市,数字=距下一交易日的天数
    """
    if is_trading_day(dt) and dt.hour < 15:
        return 0  # 盘中
    return (next_trading_day(dt) - dt).days

def get_trading_day_label(dt):
    """返回描述文字：当前日期的交易日状态"""
    gap = trading_day_gap(dt)
    ntd = next_trading_day(dt)
    wd = dt.weekday()
    if gap == 0:
        if wd == 4:
            return f"今日（{dt.strftime('%m/%d %a')}）为交易日，{dt.strftime('%H:%M')}（14:59前可参考）"
        return f"今日（{dt.strftime('%m/%d %a')}）为交易日，数据最新"
    elif gap == 1:
        return f"今日（{dt.strftime('%m/%d %a')}）已休市，下一交易日：{ntd.strftime('%m/%d %a')}（{gap}天后）"
    elif gap == 2:
        return f"今日（{dt.strftime('%m/%d %a')}）休市·周末，数据为周五收盘，间隔2天至{ntd.strftime('%m/%d %a')}"
    elif gap >= 3:
        # 周五15:00后/长假首日
        holiday_label = "已休市（收盘后）" if wd == 4 else "休市（节假日/调休）"
        return f"今日（{dt.strftime('%m/%d %a')}）{holiday_label}，间隔{gap}天至{ntd.strftime('%m/%d %a')}"
    else:
        return f"今日（{dt.strftime('%m/%d %a')}）休市，下一交易日：{ntd.strftime('%m/%d %a')}"

# ============================================================
# 重大政策事件日历(v2.0 - 基于回测数据优化)
# 事件对DCE玉米方向(回测胜率 > 55%的给予更高权重)
# ============================================================
POLICY_EVENTS_CN = {
    # 事件名称: (方向, 回测胜率)
    # v3.0新增草地贪夜蛾(回测62.5%·全量事件最高)
    "草地贪夜蛾高发期(历史回测)": (1, 62.5),   # ⭐回测最强事件
    "中美贸易战升级":           (-1, 58.4),
    "COVID后疫情时期":          (1, 57.8),
    "临储取消改革预期":         (-1, 57.0),
    "供给侧改革":              (1, 55.3),
    "中美贸易战开打":           (-1, 55.0),
    "临储拍卖去库存":           (-1, 54.8),
    "北半球极端高温干旱":        (1, 53.5),
    "4万亿刺激计划":           (1, 51.9),
    "COVID-19武汉封城":        (-1, 51.4),
    # 以下事件回测胜率偏低,不加权
    "俄乌战争":               (0, 45.3),
    "华北极端降雨":            (0, 46.9),
    "中美第一阶段协议签署":     (0, 47.1),
}

# backtest_cn_v2.py 精确事件日历（含回测胜率，5124样本）
# (开始日期, 结束日期, 事件名称, 方向, 回测胜率)
POLICY_EVENTS_CN_V2 = [
    # 中美贸易战
    ("2018-07-06", "2018-09-30", "中美贸易战开打（对美国玉米加征关税）", -1, 58.4),
    ("2019-05-10", "2019-12-31", "中美贸易战升级", -1, 60.1),
    ("2020-01-15", "2020-02-14", "中美第一阶段协议签署", 1, 47.1),
    # COVID-19
    ("2020-01-23", "2020-04-08", "COVID-19武汉封城", -1, 51.4),
    ("2020-04-08", "2020-12-31", "COVID后疫情时期（粮食安全囤货）", 1, 57.8),
    # 临储拍卖政策
    ("2016-04-30", "2016-10-31", "临储取消改革预期", -1, 57.0),
    ("2017-05-01", "2017-10-31", "临储拍卖去库存", -1, 54.8),
    ("2021-05-06", "2021-10-31", "临储拍卖火爆（高成交率+高溢价）", 1, 60.5),
    # 草地贪夜蛾
    ("2019-04-01", "2019-09-30", "历史事件·草地贪夜蛾高发期（回测偏多胜率62.5%）", 1, 62.5),
    ("2020-03-01", "2020-09-30", "历史事件·草地贪夜蛾防控期（回测偏多胜率57.2%）", 1, 57.2),
    # 极端天气
    ("2020-07-01", "2020-09-30", "北半球极端高温干旱/洪涝", 1, 53.5),
    ("2022-06-01", "2022-09-30", "北半球极端高温干旱（玉米减产担忧）", 1, 55.8),
    ("2023-07-01", "2023-09-30", "华北极端降雨（玉米产区洪涝）", 1, 46.9),
    # 俄乌战争
    ("2022-02-24", "2022-07-31", "俄乌战争（全球粮食危机）", 1, 45.3),
    # 政策刺激
    ("2008-11-01", "2009-06-30", "4万亿刺激计划", 1, 51.9),
    ("2015-11-01", "2016-06-30", "供给侧改革", -1, 55.3),
]

def get_policy_event(now):
    """
    返回当前适用的政策事件及方向（回测胜率）
    v3.0: 集成 backtest_cn_v2.py 的12个真实历史事件回测结果
    优先按精确日期匹配，再按实时新闻，最后按月份回退
    """
    date_s = now.strftime("%Y-%m-%d")
    # 1) 精确日期匹配（从backtest_cn_v2回测数据）
    for start, end, name, direction, hit_rate in POLICY_EVENTS_CN_V2:
        if start <= date_s <= end:
            return name, direction, hit_rate

    # 2) 实时新闻检测
    import news as pn
    try:
        result, news_items = pn.get_policy_news(days_back=7, total_timeout=45)
        if result.get("has_news") and abs(result.get("overall_score", 0)) >= 1.0:
            best = max(result["top_items"], key=lambda x: abs(x["score"]))
            score = best["score"]
            direction = 1 if score > 0 else -1
            hit_rate = 50 + abs(score) * 5
            title = best["title"][:30]
            print(f"\n  📰 实时政策新闻检测:{best['signal_emoji'] if 'signal_emoji' in best else ''}{title}..." +
                  f"(评分{score:+.1f}, 利好/利空方向)")
            return title, direction, min(hit_rate, 62)
    except Exception:
        pass

    # 3) 回退：按月份猜测（仅保留持续性的季节事件）
    for name, (direction, hit_rate) in POLICY_EVENTS_CN.items():
        m = now.month
        if "草地贪夜蛾" in name and m in [4, 5, 6, 7, 8, 9]:
            return name, direction, hit_rate
        if "极端高温" in name and m in [6, 7, 8, 9]:
            return name, direction, hit_rate
    return None, 0, 0


# ============================================================
# 常量
# ============================================================

# 中国玉米季节性(v2.0 - 基于21年回测数据校准,4930样本)
# 回测结果:整体方向准确率53.1%(v3.0优化后),季节性因月而异46.7%-61.4%
# 月度排名:3月55.4%最高,11月48.9%最低
# 核心发现:10月假设偏空,实际胜率50.6%(偏多),其余月份方向大致正确但强度需调整
CN_SEASON_SCORE = {
    # v3.0回测校准:弱势月(2月47.5%、3月46.7%、10月50.6%)进一步降权
    # 2-3月为年后淡季+春耕炒作预期,模型准确率低,降低季节依赖
    1:  0.0,    # 春节,胜率47.5%(淡季)
    2: -0.2,    # 需求淡季,胜率47.5%(下调,弱势月)
    3:  0.0,    # 春耕炒作,胜率46.7%(下调,弱势月)
    4:  0.25,   # 春耕面积炒作,胜率52.5%
    5:  0.1,    # 青黄不接,胜率50.7%
    6:  0.35,   # 夏季青黄不接,胜率53.7%
    7:  0.0,    # 生长期,胜率50.5%
    8:  0.3,    # 定产关键期,胜率53.0%
    9:  0.25,   # 新粮陆续上市,胜率52.5%
    10: 0.0,    # 新粮上市,胜率50.6%(下调,弱势月)
    11: -0.1,   # 卖粮高峰,胜率48.9%
    12:  0.2,   # 政策市,胜率52.0%
}
CN_SEASON_DESC = {
    1: "春节备货结束,需求清淡,胜率47.5%",
    2: "需求淡季,胜率47.5%(弱势月·降权)",
    3: "春耕炒作,胜率46.7%(弱势月·降权)",
    4: "春耕面积炒作,胜率52.5%",
    5: "青黄不接,胜率50.7%",
    6: "夏季青黄不接,胜率53.7%",
    7: "生长期,胜率50.5%(无明显季节性)",
    8: "定产关键期,胜率53.0%",
    9: "新粮陆续上市,胜率52.5%",
    10: "新粮上市高峰,胜率50.6%(弱势月·降权)",
    11: "卖粮高峰,胜率48.9%",
    12: "政策市,胜率52.0%",
}

# USDA报告(影响国际价格,传导到中国)
US_HOLIDAYS_2026 = [
    (1,1),(1,19),(2,16),(4,3),(5,25),(7,3),(9,7),(11,26),(12,25)
]

# ============================================================
# 数据获取(akshare)
# ============================================================

def fetch_dce_corn(n=90):
    """获取DCE玉米期货日线（超时保护）"""
    df = fetch_with_timeout(ak.futures_zh_daily_sina, symbol="C0")
    if df is None:
        return None
    return df.tail(n).reset_index(drop=True)

def fetch_dce_soymeal(n=90):
    df = fetch_with_timeout(ak.futures_zh_daily_sina, symbol="M0")
    if df is None:
        return None
    return df.tail(n).reset_index(drop=True)

def fetch_ine_oil(n=30):
    df = fetch_with_timeout(ak.futures_zh_daily_sina, symbol="SC0")
    if df is None:
        return None
    return df.tail(n).reset_index(drop=True)

def fetch_dce_beanoil(n=90):
    df = fetch_with_timeout(ak.futures_zh_daily_sina, symbol="Y0")
    if df is None:
        return None
    return df.tail(n).reset_index(drop=True)

def fetch_cbot_wheat(n=10):
    """
    获取CBOT小麦期货 ZW=F（美分/蒲式耳）
    Yahoo Finance接口，5条足够（看趋势方向）
    """
    import urllib.request, json, datetime
    try:
        url = f'https://query2.finance.yahoo.com/v8/finance/chart/ZW=F?interval=1d&range={n}d'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        result = data['chart']['result'][0]
        closes = result['indicators']['quote'][0]['close']
        timestamps = result['timestamp']
        dates = [datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d') for t in timestamps]
        df = __import__('pandas').DataFrame({'date': dates, 'close': closes}).dropna()
        return df.tail(n).reset_index(drop=True)
    except Exception:
        return None

def fetch_cbot_soybeans(n=10):
    """获取CBOT大豆 ZS=F（美分/蒲式耳）——已有豆粕替代，这里备用"""
    import urllib.request, json, datetime
    try:
        url = f'https://query2.finance.yahoo.com/v8/finance/chart/ZS=F?interval=1d&range={n}d'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        result = data['chart']['result'][0]
        closes = result['indicators']['quote'][0]['close']
        timestamps = result['timestamp']
        dates = [datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d') for t in timestamps]
        df = __import__('pandas').DataFrame({'date': dates, 'close': closes}).dropna()
        return df.tail(n).reset_index(drop=True)
    except Exception:
        return None

def fetch_bci():
    """
    获取波罗的海好望角型船运价指数(BCI)
    数据来源: akshare macro_china_freight_index
    BCI > MA5 → 运费上涨 → 进口成本上升 → 利多国内玉米
    BCI < MA5 → 运费下跌 → 进口成本下降 → 利空国内玉米
    返回: (bci_latest, bci_ma5, bci_chg_pct, bci_dir)
      bci_dir: 1=上涨趋势, -1=下跌趋势, 0=中性
    注意: BCI与DCE玉米方向一致率约48%,信号偏弱,权重0.3x
    """
    try:
        df = fetch_with_timeout(ak.macro_china_freight_index)
        if df is None:
            return None, None, 0, 0
        df = df.sort_values('截止日期').reset_index(drop=True)
        bci_df = df[['截止日期','波罗的海好望角型船运价指数BCI']].dropna().tail(10)
        bci_df.columns = ['date', 'bci']
        bci_df['date'] = bci_df['date'].astype(str)
        if len(bci_df) < 2:
            return None, None, 0, 0
        bci_vals = bci_df['bci'].tolist()
        latest = bci_vals[-1]
        ma5 = sum(bci_vals[-5:]) / 5 if len(bci_vals) >= 5 else latest
        prev = bci_vals[-2] if len(bci_vals) >= 2 else latest
        chg_pct = (latest - prev) / prev * 100
        direction = 1 if latest > ma5 else (-1 if latest < ma5 else 0)
        return latest, ma5, chg_pct, direction
    except Exception:
        return None, None, 0, 0

def compute_overnight_gap(corn_df, n=20):
    """
    计算DCE玉米隔夜缺口统计（v1.12新增）
    - 上一交易日收盘 vs 当日开盘的差值
    - 缺口 > |1%| 视为显著缺口
    - 返回近n日缺口列表及统计摘要
    """
    import pandas as pd
    df = corn_df.sort_values('date').reset_index(drop=True)
    if len(df) < 3:
        return None
    gaps = []
    prev_close = None
    for _, row in df.iterrows():
        if prev_close is not None:
            gap_pct = (row['open'] - prev_close) / prev_close * 100
            gaps.append({
                'date': row['date'],
                'open': row['open'],
                'prev_close': prev_close,
                'gap_pct': gap_pct,
                'abs_gap': abs(gap_pct),
                'direction': '↑跳空' if gap_pct > 0.05 else ('↓跳空' if gap_pct < -0.05 else '→')
            })
        prev_close = row['close']
    gaps_df = pd.DataFrame(gaps)
    if len(gaps_df) == 0:
        return None
    # 近期统计
    recent = gaps_df.tail(n)
    avg_gap = recent['abs_gap'].mean()
    max_up = recent['gap_pct'].max()
    max_dn = recent['gap_pct'].min()
    sig_gaps = recent[recent['abs_gap'] > 1.0]  # 显著缺口>1%
    # 最近缺口
    last = gaps_df.iloc[-1] if len(gaps_df) > 0 else None
    return {
        'recent_df': recent,
        'avg_abs_gap': avg_gap,
        'max_up': max_up,
        'max_dn': max_dn,
        'sig_gap_count': len(sig_gaps),
        'last': last,
        'all_df': gaps_df
    }

def fetch_enso():
    """
    获取当前ENSO状态（厄尔尼诺/拉尼娜）
    数据来源: NOAA CPC NINO3.4 异常指数（月度，滞后约1-2个月）
    - ONI > +0.5°C  → 厄尔尼诺
    - ONI < -0.5°C  → 拉尼娜
    - -0.5~+0.5      → 中性（无显著影响）
    返回: (oni_latest, oni_prev, phase_label, phase_dir, lag_months)
      phase_dir: 1=利多, -1=利空, 0=中性
      lag_months: 数据滞后月数（供参考，>2个月时应在输出中标注）
    """
    import urllib.request
    try:
        url = 'https://www.cpc.ncep.noaa.gov/data/indices/ersst5.nino.mth.91-20.ascii'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode('utf-8')
        lines = [l.strip() for l in data.split('\n') if l.strip() and not l.startswith(' YR')]
        records = []
        for l in lines:
            parts = l.split()
            if len(parts) >= 10:
                yr, mon = int(parts[0]), int(parts[1])
                oni = float(parts[9])  # NINO3.4异常值（最后一列，即ONI）
                records.append((yr, mon, oni))
        if len(records) < 2:
            return None, None, '获取失败', 0
        records.sort(key=lambda x: (x[0], x[1]))
        latest = records[-1]
        prev = records[-2] if len(records) >= 2 else latest
        # 计算数据滞后月数
        today = datetime.now()
        lag_months = (today.year - latest[0]) * 12 + (today.month - latest[1])
        # 判断相位
        oni_val = latest[2]
        if oni_val > 0.5:
            phase = '厄尔尼诺'
            direction = 1  # 厄尔尼诺→美豆/美玉米产量不确定，但南美干旱→利多国际CBOT→传导DCE
        elif oni_val < -0.5:
            phase = '拉尼娜'
            direction = 1  # 拉尼娜→南美干旱减产→全球玉米供应紧→利多
        else:
            phase = '中性'
            direction = 0
        return latest, prev, phase, direction, lag_months
    except Exception:
        return None, None, '获取失败', 0

def fetch_usd_cny():
    """获取USD/CNY汇率（通过 exchangerate-api.com，超时保护）"""
    try:
        import urllib.request
        url = 'https://api.exchangerate-api.com/v4/latest/USD'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json
            data = json.loads(resp.read())
            rate = float(data['rates']['CNY'])
            print(f"[数据] ✅ 美元/人民币(USD/CNY): {rate}")
            return rate
    except Exception as e:
        print(f"[数据] ⚠️ USD/CNY获取异常: {e}，使用默认值6.80")
        return 6.80

def fetch_cftc_corn():
    """获取CFTC美国玉米期货持仓细分数据(P2-1升级版)
    
    返回: DataFrame with columns:
      date: 日期
      total_net: 总净持仓（所有报告持仓合计）
      merchant_net: 商业净持仓（掉期商/生产商/加工商）
      fund_net: 非商业净持仓（管理基金/投机 ≈ 总 - 商业）
    """
    try:
        # 总持仓
        df_total = fetch_with_timeout(ak.macro_usa_cftc_c_holding)
        # 商业持仓（掉期商/生产商）
        df_merchant = fetch_with_timeout(ak.macro_usa_cftc_merchant_goods_holding)
        
        if df_total is None or df_merchant is None:
            return None
        
        # 提取玉米列
        def extract_corn(df):
            long_col = [c for c in df.columns if '玉米' in c and '多头仓位' in c]
            short_col = [c for c in df.columns if '玉米' in c and '空头仓位' in c]
            net_col = [c for c in df.columns if '玉米' in c and '净仓位' in c]
            if net_col:
                result = df[['日期', net_col[0]]].copy()
                result.columns = ['date', 'net']
            elif long_col and short_col:
                result = df[['日期', long_col[0], short_col[0]]].copy()
                result.columns = ['date', 'long', 'short']
                result['net'] = result['long'] - result['short']
            else:
                return None
            result['date'] = result['date'].astype(str)
            return result
        
        total = extract_corn(df_total)
        merchant = extract_corn(df_merchant)
        
        if total is None or merchant is None:
            return None
        
        # 合并
        merged = total.merge(merchant[['date', 'net']], on='date', how='outer', suffixes=('_total', '_merchant'))
        merged = merged.dropna(subset=['net_total']).sort_values('date').tail(60)  # 近60周
        
        merged.columns = ['date', 'total_net', 'merchant_net']
        # 非商业 ≈ 总 - 商业
        merged['fund_net'] = merged['total_net'] - merged['merchant_net']
        
        return merged
    except Exception as e:
        print(f"  ⚠️ CFTC获取失败: {e}")
        return None

def fetch_china_news():
    """获取中国玉米相关政策新闻（超时保护）"""
    try:
        news = fetch_with_timeout(ak.news_article, symbol="农产品")
        if news is None:
            return []
        corn_news = [n for n in news if any(k in str(n.get('title','')) for k in ['玉米','粮食','临储','进口','USDA','农业'])]
        return corn_news[:5]
    except:
        return []

# ============================================================
# 指标计算
# ============================================================

def sma(prices, n):
    if len(prices) < n: return None
    return sum(prices[-n:]) / n

def stddev(prices, n):
    if len(prices) < n: return None
    vals = prices[-n:]
    m = sum(vals) / n
    return math.sqrt(sum((x-m)**2 for x in vals) / n)

def rsi(prices, period=14):
    if len(prices) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(d if d > 0 else 0)
        losses.append(-d if d < 0 else 0)
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def compute_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal + 5: return None, None, None, None
    def ema(data, n):
        k = 2 / (n + 1)
        e = data[0]
        result = [e]
        for d in data[1:]:
            e = d * k + e * (1 - k)
            result.append(e)
        return result
    data_len = max(slow + signal + 10, 40)
    closes = prices[-data_len:]
    ef = ema(closes, fast)
    es = ema(closes, slow)
    macd_line = [ef[i] - es[i] for i in range(len(ef))]
    sig = ema(macd_line[slow:], signal)
    mv = macd_line[-1]
    sv = sig[-1]
    hist = mv - sv
    prev_hist = macd_line[-2] - sig[-2] if len(macd_line) >= 2 else 0
    return round(mv,4), round(sv,4), round(hist,4), round(prev_hist,4), macd_line

def detect_divergence(prices, highs, lows, macd_line):
    div = {'top': False, 'bottom': False, 'reasons': []}
    if len(prices) < 25 or macd_line is None: return div
    hb = highs[-20:]; lb = lows[-20:]; ml = macd_line[-20:]
    # 顶背离
    hmax_idx = hb.index(max(hb))
    if hmax_idx >= 5 and max(ml[:hmax_idx]) < ml[hmax_idx]:
        div['top'] = True
        div['reasons'].append("价格创新高但MACD未配合")
    # 底背离
    lmin_idx = lb.index(min(lb))
    if lmin_idx >= 5 and min(ml[:lmin_idx]) > ml[lmin_idx]:
        div['bottom'] = True
        div['reasons'].append("价格创新低但MACD未跟随")
    return div

def correlation(x, y):
    n = min(len(x), len(y))
    if n < 5: return 0
    mx = sum(x[-n:])/n; my = sum(y[-n:])/n
    num = sum((x[-n+i]-mx)*(y[-n+i]-my) for i in range(n))
    dx = math.sqrt(sum((v-mx)**2 for v in x[-n:]))
    dy = math.sqrt(sum((v-my)**2 for v in y[-n:]))
    return num/(dx*dy) if dx*dy > 0 else 0

def get_usda_window(now):
    """
    USDA窗口检测(v2.0 - 基于121次报告回测)
    返回: (是否窗口, 窗口类型, 月度传导统计)
    传导规律:
    - 平均传导系数: 0.919 (DCE约92%的CBOT幅度)
    - 当天同向率: 58.7% (CBOT涨→DCE跟涨概率)
    - 月度最强: 11月(80%)、6月(70%)、10月(70%)
    - 月度最弱: 5月(45%)、7月(50%)、2月(50%)
    """
    import calendar
    first_wd = calendar.monthrange(now.year, now.month)[0]
    second_wed = (2 - first_wd + 7) % 7 + 1 + 7
    wasde_date = datetime(now.year, now.month, second_wed)
    hours = abs((wasde_date - now).total_seconds() / 3600)

    # 月度传导统计
    month_stats = {
        1: {"same_dir_rate": 60.0, "dce_avg": 0.10, "label": "60%同向,+0.10%平均"},
        2: {"same_dir_rate": 50.0, "dce_avg": -0.24, "label": "50%同向,-0.24%平均"},
        3: {"same_dir_rate": 60.0, "dce_avg": 0.12, "label": "60%同向,+0.12%平均"},
        4: {"same_dir_rate": 50.0, "dce_avg": -0.14, "label": "50%同向,-0.14%平均"},
        5: {"same_dir_rate": 45.5, "dce_avg": 0.20, "label": "46%同向⚠️最弱,+0.20%平均"},
        6: {"same_dir_rate": 70.0, "dce_avg": 0.83, "label": "70%同向最强,+0.83%平均"},
        7: {"same_dir_rate": 50.0, "dce_avg": -0.29, "label": "50%同向⚠️注意CBOT利空传导"},
        8: {"same_dir_rate": 60.0, "dce_avg": 0.04, "label": "60%同向,+0.04%平均"},
        9: {"same_dir_rate": 60.0, "dce_avg": -0.06, "label": "60%同向,-0.06%平均"},
        10: {"same_dir_rate": 70.0, "dce_avg": 0.36, "label": "70%同向,+0.36%平均"},
        11: {"same_dir_rate": 80.0, "dce_avg": 0.24, "label": "80%同向最强,+0.24%平均"},
        12: {"same_dir_rate": 50.0, "dce_avg": 0.04, "label": "50%同向,+0.04%平均"},
    }

    if hours <= 24:
        stats = month_stats.get(now.month, {})
        return True, 'wasde', stats
    return False, None, {}




def get_usda_report_info_for_cn(now):
    """获取本周USDA报告时间(国际版→中国版移植)"""
    from datetime import timedelta, datetime as dt2
    import calendar

    def second_wednesday(y, m):
        first_wd = calendar.monthrange(y, m)[0]
        first_wed = (2 - first_wd + 7) % 7 + 1
        return first_wed + 7

    year, month = now.year, now.month
    wasde_day = second_wednesday(year, month)

    days_since_mon = now.weekday()
    this_mon = dt2(year, month, now.day) - timedelta(days=days_since_mon)
    crop_bj = dt2(this_mon.year, this_mon.month, this_mon.day, 5)
    next_mon = this_mon + timedelta(days=7)
    next_crop_bj = dt2(next_mon.year, next_mon.month, next_mon.day, 5)

    days_since_thu = (now.weekday() - 3) % 7
    this_thu = dt2(year, month, now.day) - timedelta(days=days_since_thu)
    export_bj = dt2(this_thu.year, this_thu.month, this_thu.day, 21, 30)
    next_thu = this_thu + timedelta(days=7)
    next_export_bj = dt2(next_thu.year, next_thu.month, next_thu.day, 21, 30)

    wasde_bj = dt2(year, month, wasde_day, 9)

    return {
        'wasde': wasde_bj,
        'crop': crop_bj,
        'export': export_bj,
        'crop_next': next_crop_bj,
        'export_next': next_export_bj,
    }

# ============================================================
# 主程序
# ============================================================

def main():
    now = datetime.now()
    print(f"\n{'='*60}")
    print(f"[分析] 中国玉米期货每日分析 v5.0 | {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    print("[数据] 正在获取DCE期货数据(akshare)...")

    # ── 获取数据 ──
    try:
        corn_df = fetch_dce_corn(90)
        print(f"  ✅ DCE玉米C0:{len(corn_df)}条 | 最新:{corn_df.iloc[-1]['date']} 收{corn_df.iloc[-1]['close']}")
    except Exception as e:
        print(f"  ❌ DCE玉米获取失败:{e}"); return

    # ── 分钟K线回退：日线缺当天数据时用分钟K线重构 ──
    try:
        _min_df = fetch_with_timeout(ak.futures_zh_minute_sina, "C0", "5")
        if _min_df is not None:
            import pandas as _pd
            _min_df['datetime'] = _pd.to_datetime(_min_df['datetime'])
            _min_df['date'] = _min_df['datetime'].dt.date.astype(str)
            _min_df['hour'] = _min_df['datetime'].dt.hour
            today_str = now.strftime('%Y-%m-%d')
            _today_min = _min_df[_min_df['date'] == today_str]
            if len(_today_min) > 0:
                _day_min = _today_min[(_today_min['hour'] >= 9) & (_today_min['hour'] < 15)]
                if len(_day_min) > 0 and str(corn_df.iloc[-1]['date']) != today_str:
                    # 日线缺今天数据，从分钟K线重构追加
                    _day_open = float(_day_min.iloc[0]['close'])
                    _day_high = float(_day_min['high'].max())
                    _day_low = float(_day_min['low'].min())
                    _day_close = float(_day_min.iloc[-1]['close'])
                    _day_vol = int(_day_min['volume'].sum())
                    import numpy as _np
                    _new_row = _pd.DataFrame({
                        'date': [today_str], 'open': [_day_open], 'high': [_day_high],
                        'low': [_day_low], 'close': [_day_close], 'volume': [_day_vol],
                        'hold': [corn_df.iloc[-1].get('hold', _np.nan)],
                        'settle': [corn_df.iloc[-1].get('settle', _np.nan)]
                    })
                    corn_df = _pd.concat([corn_df, _new_row], ignore_index=True)
                    print(f"  ↩️ 日线缺{today_str}数据，从分钟K线重构追加: 开{_day_open:.0f} 高{_day_high:.0f} 低{_day_low:.0f} 收{_day_close:.0f}")
    except Exception as _e:
        print(f"  ⚠️ 分钟K线回退失败: {_e}")

    # ── 数据时效性检查(v1.8 新增:周末/休市日提示)──────────────────
    data_date_str = str(corn_df.iloc[-1]['date'])
    try:
        data_date = datetime.strptime(data_date_str, '%Y-%m-%d')
    except:
        data_date = data_date_str
    gap = trading_day_gap(now)
    # ── 黑天鹅模式检测 (P3-1, 三级制) ──
    bs_mode = bs.get_black_swan_mode()
    level = bs_mode.get('level', 0)
    if bs_mode.get('active', False):
        sep = "=" * 60
        icons = {1: "🟡警戒", 2: "🟠确认", 3: "🔴危机"}
        level_label = icons.get(level, "⚡")
        print(f"\n{sep}")
        print(f"🌪️【黑天鹅模式 · {level_label}】")
        print(f"  原因: {bs_mode.get('reason', '?')}")
        print(f"  等级: {level} (1=警戒 2=确认 3=危机)")
        
        if level == 3:
            # 危机：强制方向
            d_label = "偏多" if bs_mode['direction'] > 0 else ("偏空" if bs_mode['direction'] < 0 else "无")
            print(f"  强制方向: {d_label}")
            if bs_mode['direction'] != 0:
                direction = bs_mode['direction']
                base_chg = direction * unit_move * 2
            conf_emoji = "🟠"; conf_label = "⚡危机模式"
            conf_note = f"强制方向|区间{bs_mode['interval_mult']}x|{bs_mode['reason']}"
        elif level == 2:
            # 确认：方向偏倾向50% + 信号50%
            print(f"  方向偏倾向: {bs_mode['direction_bias']} (偏倾向权重{bs_mode['bias_weight']*100:.0f}%)")
            if bs_mode['direction_bias'] != 0 and direction != 0:
                # 方向一致：强化；不一致：降低信号强度
                if bs_mode['direction_bias'] * direction > 0:
                    direction = direction  # 保持一致，置信度提升
                else:
                    direction = int((1 - bs_mode['bias_weight']) * direction + bs_mode['bias_weight'] * bs_mode['direction_bias'])
            conf_emoji = "🟡"; conf_label = f"⚡确认模式({level})"
            conf_note = f"方向偏倾向|区间{bs_mode['interval_mult']}x"
        else:
            # 警戒：扩大区间，方向不变
            print(f"  方向偏倾向: {bs_mode['direction_bias']}")
            conf_emoji = "🟡"; conf_label = f"⚠️警戒模式({level})"
            conf_note = f"警戒|区间{bs_mode['interval_mult']}x"
        
        print(f"  区间倍数: {bs_mode['interval_mult']}x")
        print(f"  过期: {bs_mode.get('expiry', '?')}")
        print(sep)
        scen_d2 = max(int(scen_d2 * bs_mode['interval_mult']), 30)

    ntd = next_trading_day(now)
    label = get_trading_day_label(now)
    if gap == 0:
        print(f"[数据] 📌 数据时效: ✅ {label}")
    else:
        print(f"[数据] 📌 数据时效: ⚠️ {label}")
        print(f"      → 当前数据日期: {data_date_str}({data_date.strftime('%a')})")
        print(f"      → 下一交易日: {ntd.strftime('%Y-%m-%d')}({ntd.strftime('%a')}) 间隔 {gap} 天")
        print(f"      → 预测的「第二日」实指: {ntd.strftime('%Y-%m-%d')}({ntd.strftime('%a')}), 非自然日次日")
        if gap >= 2:
            print(f"      → ⚠️ 间隔 {gap} 天, 期间外盘(CBOT)波动可能无法及时反映")
        if gap == 3:
            print(f"      → 📌 建议: 周五收盘后参考意义下降, 可关注周一开盘缺口")

    # ── 隔夜缺口统计(v1.12新增)─────────────────────────────────────
    try:
        gap_stats = compute_overnight_gap(corn_df, n=20)
        if gap_stats and gap_stats['last'] is not None:
            last_gap = gap_stats['last']
            last_date = last_gap['date']
            last_dir = last_gap['direction']
            last_pct = last_gap['gap_pct']
            avg_abs = gap_stats['avg_abs_gap']
            sig_count = gap_stats['sig_gap_count']
            # 判断是否异常
            if abs(last_pct) > avg_abs * 2 and abs(last_pct) > 0.5:
                gap_alert = "⚠️异常缺口" if abs(last_pct) > 1.0 else "⚠️偏大缺口"
                print(f"[数据] 🌙 隔夜缺口: {last_date} {last_dir}{last_pct:+.3f}% | 均值{avg_abs:.3f}% | {gap_alert}")
            else:
                print(f"[数据] 🌙 隔夜缺口: {last_date} {last_dir}{last_pct:+.3f}% | 均值{avg_abs:.3f}% | 近20日显著缺口{sig_count}次")
    except Exception as e:
        pass  # 非关键，不报错

    try:
        soy_df = fetch_dce_soymeal(90)
        print(f"  ✅ DCE豆粕M0:{len(soy_df)}条 | 最新:{soy_df.iloc[-1]['date']} 收{soy_df.iloc[-1]['close']}")
    except Exception as e:
        print(f"  ❌ DCE豆粕获取失败:{e}")

    try:
        oil_df = fetch_ine_oil(30)
        print(f"  ✅ INE原油SC0:{len(oil_df)}条 | 最新:{oil_df.iloc[-1]['date']} 收{oil_df.iloc[-1]['close']}")
    except Exception as e:
        print(f"  ❌ INE原油获取失败:{e}")

    try:
        bean_df = fetch_dce_beanoil(90)
        print(f"  ✅ DCE豆油Y0:{len(bean_df)}条 | 最新:{bean_df.iloc[-1]['date']} 收{bean_df.iloc[-1]['close']}")
    except Exception as e:
        print(f"  ❌ DCE豆油获取失败:{e}")

    try:
        cbot_df = ak.futures_foreign_hist(symbol="C")
        cbot_df["date"] = cbot_df["date"].astype(str)
        cbot_df = cbot_df.sort_values("date").reset_index(drop=True)
        cbot_latest = cbot_df.iloc[-1]
        cbot_prev = cbot_df.iloc[-2]
        cbot_chg = (cbot_latest["close"] - cbot_prev["close"]) / cbot_prev["close"] * 100
        print(f"[数据] ✅ CBOT玉米C: {len(cbot_df)}条 | 最新 {cbot_latest['date']} 收盘 {cbot_latest['close']:.2f}美分({cbot_chg:+.2f}%)")
        # ── CBOT均线(跨市场领先信号)─────────────
        cbot_prices = cbot_df['close'].tolist()
        cbot_ma5 = sum(cbot_prices[-5:]) / 5 if len(cbot_prices) >= 5 else None
        cbot_ma10 = sum(cbot_prices[-10:]) / 10 if len(cbot_prices) >= 10 else None
        cbot_ma5_dir = 1 if cbot_ma5 and cbot_ma5 > cbot_prices[-2] else (-1 if cbot_ma5 and cbot_ma5 < cbot_prices[-2] else 0)
        cbot_ma10_dir = 1 if cbot_ma10 and cbot_ma10 > cbot_prices[-3] else (-1 if cbot_ma10 and cbot_ma10 < cbot_prices[-3] else 0)
        # CBOT趋势领先信号(MA一致方向时有效)
        # CBOT当日涨跌方向(已验证传导有效,53.7%同向率)
        # 使用CBOT当日收盘价相对前日的变化方向
        if len(cbot_prices) >= 2:
            cbot_today = cbot_prices[-1]
            cbot_yesterday = cbot_prices[-2]
            cbot_day_chg = (cbot_today - cbot_yesterday) / cbot_yesterday * 100
            cbot_trend = 1 if cbot_day_chg > 0 else (-1 if cbot_day_chg < 0 else 0)
        else:
            cbot_day_chg = 0; cbot_trend = 0
        cbot_trend_label = {1: "CBOT涨↑", 0: "CBOT平", -1: "CBOT跌↓"}.get(cbot_trend, "CBOT平")
        print(f"[信号] → CBOT当日{cbot_day_chg:+.2f}% → 传导至DCE，DCE同向概率约54%")
    except Exception as e:
        print(f"[数据] ❌ CBOT玉米获取失败: {e}")
        cbot_prices = None; cbot_ma5 = None; cbot_ma10 = None; cbot_ma5_dir = 0; cbot_ma10_dir = 0; cbot_trend = 0

    # ── CBOT小麦ZW=F（v1.10新增:竞争作物监测·饲料替代效应）──────────
    wheat_trend = 0  # 默认值（确保在try块外也可用）
    try:
        wheat_df = fetch_cbot_wheat(10)
        if wheat_df is not None and len(wheat_df) >= 2:
            w_latest = wheat_df.iloc[-1]['close']
            w_prev = wheat_df.iloc[-2]['close']
            w_chg = (w_latest - w_prev) / w_prev * 100
            w_prices = wheat_df['close'].tolist()
            w_trend = 1 if w_chg > 0 else (-1 if w_chg < 0 else 0)
            w_trend_label = {1: "涨↑", 0: "平", -1: "跌↓"}.get(w_trend, "平")
            print(f"[数据] 🌾 CBOT小麦ZW: {w_latest:.1f}美分({w_chg:+.2f}%) | {w_trend_label}")
            # 小麦/玉米价比（饲料替代信号，ZW和ZC同为CBOT美分/蒲式耳，可直接比）
            if cbot_df is not None and len(cbot_df) >= 2:
                zc_latest = cbot_df.iloc[-1]['close']   # CBOT玉米，美分/蒲式耳
                ratio = round(w_latest / zc_latest, 3) if zc_latest else None
                # 比值 > 1.25: 小麦显著贵于玉米→饲料厂减少小麦用玉米替代→偏多玉米
                # 比值 < 1.00: 小麦便宜→替代增加→偏空玉米
                if ratio:
                    if ratio > 1.25:
                        ratio_sig = "⚠️小麦贵于玉米→饲料替代减少(利多)"
                        ratio_dir = 1
                    elif ratio < 1.00:
                        ratio_sig = "⚠️小麦便宜→替代增加(利空)"
                        ratio_dir = -1
                    else:
                        ratio_sig = "✅比值正常"
                        ratio_dir = 0
                    print(f"      → 小麦/玉米价比: {ratio:.3f} | {ratio_sig}")
                    wheat_trend = w_trend  # 小麦涨跌方向信号
                    # 若价比信号与小麦方向共振，强化权重
                    if ratio_dir != 0 and ratio_dir == (1 if w_chg > 0 else -1):
                        print(f"      → ✅ 价比+方向共振，饲料替代信号增强")
                        wheat_trend = ratio_dir * 2  # 强化信号
                    elif ratio_dir != 0:
                        wheat_trend = ratio_dir  # 单独用价比方向
            else:
                wheat_trend = w_trend  # 无CBOT玉米时，用小麦本身方向
        else:
            w_chg = 0
    except Exception as e:
        w_chg = 0
        print(f"  🌾 CBOT小麦获取失败(可忽略):{e}")

    try:
        usd_cny = fetch_usd_cny()
        print(f"[数据] ✅ 美元/人民币: {usd_cny:.4f}")
    except:
        usd_cny = 6.80

    # CFTC美国玉米净持仓
    try:
        cftc_df = fetch_cftc_corn()
        if cftc_df is not None and len(cftc_df) > 0:
            cftc_latest = cftc_df.iloc[-1]
            cftc_net = float(cftc_latest['total_net'])
            cftc_merchant_net = float(cftc_latest['merchant_net'])
            cftc_fund_net = float(cftc_latest['fund_net'])
            cftc_date = str(cftc_latest['date'])
            
            # 多维度信号（P2-1增设）
            recent_totals = cftc_df['total_net'].tail(5).tolist()
            cftc_mean = statistics.mean(recent_totals)
            cftc_dir_label = "净多" if cftc_net > cftc_mean * 1.1 else ("净空" if cftc_net < cftc_mean * 0.9 else "中性")
            
            # 基金方向 vs 商业方向（套保vs投机）
            fund_label = "🟢多头" if cftc_fund_net > 0 else ("🔴空头" if cftc_fund_net < 0 else "⚪中性")
            merch_label = "做多" if cftc_merchant_net > 0 else ("做空" if cftc_merchant_net < 0 else "中性")
            
            print(f"[数据] ✅ CFTC美国玉米持仓({cftc_date})")
            print(f"        总净持仓: {cftc_net:+,.0f}手({cftc_dir_label}, 5周均值{cftc_mean:+,.0f})")
            print(f"        商业持仓(套保): {cftc_merchant_net:+,.0f}手({merch_label})")
            print(f"        基金持仓(投机): {cftc_fund_net:+,.0f}手({fund_label})")
        else:
            cftc_net = None; cftc_dir_label = None; cftc_fund_net = None; cftc_merchant_net = None
    except:
        cftc_net = None; cftc_dir_label = None; cftc_fund_net = None; cftc_merchant_net = None

    # ── 产区天气(Open-Meteo API) ──
    weather_score = 0
    try:
        weather = wc.get_corn_weather()
        if weather:
            overall = weather["overall"]
            if overall >= 0.3:
                o_emoji = "🟢"; o_signal = "轻微利多"
            elif overall <= -0.3:
                o_emoji = "🔴"; o_signal = "轻微利空"
            else:
                o_emoji = "⚪"; o_signal = "中性"
            weather_score = overall
            print(f"[天气] {o_emoji} {overall:+.1f}({o_signal})")
        else:
            print("[天气] 获取失败")
    except Exception as e:
        weather_score = 0
        print(f"  🌡️ 产区天气:获取失败")

    # ── ENSO监测(厄尔尼诺/拉尼娜, v1.10新增)──────────────────────────────
    enso_phase = '中性'; enso_dir = 0; enso_latest_yr = None; enso_latest_mon = None; enso_lag = 0
    try:
        enso_cur, enso_prev, enso_phase, enso_dir, enso_lag = fetch_enso()
        if enso_cur:
            yr, mon, oni_val = enso_cur
            _, _, oni_prev = enso_prev if enso_prev else (None, None, oni_val)
            # 月度标签
            phase_emoji = {'厄尔尼诺': '🔴', '拉尼娜': '🔵', '中性': '⚪'}.get(enso_phase, '⚪')
            # ONI趋势
            oni_chg = oni_val - oni_prev
            trend_str = f"({oni_chg:+.2f} vs上月)" if oni_prev else ""
            # 滞后警告
            lag_str = f" ⚠️数据滞后{enso_lag}个月，参考价值有限" if enso_lag >= 2 else ""
            print(f"[数据] 🌊 ENSO状态: {phase_emoji} {enso_phase}(ONI={oni_val:+.2f}°C){trend_str}{lag_str}")
            # 信号描述
            if enso_dir == 1:
                print(f"      → 活跃{enso_phase}中，全球玉米供应端有支撑，关注南美产量")
            elif enso_dir == 0:
                print(f"    → 中性，无显著异常气候信号")
            else:
                print(f"    → 活跃{enso_phase}中，关注澳大利亚/巴西产区降雨")
            enso_latest_yr = yr; enso_latest_mon = mon
        else:
            print(f"  🌊 ENSO状态:获取失败")
    except Exception as e:
        print(f"  🌊 ENSO状态:获取失败({e})")

    # ── BCI波罗的海运费指数(v1.11新增:进口成本前置指标)────────────
    bci_cur = None; bci_ma5 = None; bci_dir = 0; bci_chg = 0
    try:
        bci_cur, bci_ma5, bci_chg, bci_dir = fetch_bci()
        if bci_cur:
            bci_trend_label = {1: "上涨趋势↑", 0: "震荡", -1: "下跌趋势↓"}.get(bci_dir, "?")
            bci_signal = "进口成本上升(利多)" if bci_dir == 1 else ("进口成本下降(利空)" if bci_dir == -1 else "运费正常")
            print(f"  🚢 BCI波罗的海运费:{bci_cur:.0f}(MA5={bci_ma5:.0f}) | {bci_trend_label}")
            print(f"    → {bci_signal} | BCI MA5={bci_ma5:.0f}(权重0.3x·信号偏弱)")
        else:
            print(f"  🚢 BCI数据获取失败")
    except Exception as e:
        print(f"  🚢 BCI数据获取失败({e})")

    # ── DCE持仓量变化(v1.11新增:资金对趋势确认程度)─────────────────────
    hold_dir = 0; hold_now = None
    try:
        if corn_df is not None and len(corn_df) >= 2:
            hold_now = corn_df.iloc[-1]['hold']
            hold_prev = corn_df.iloc[-2]['hold']
            hold_chg_pct = (hold_now - hold_prev) / hold_prev * 100 if hold_prev else 0
            hold_dir = 1 if hold_chg_pct > 1 else (-1 if hold_chg_pct < -1 else 0)  # 变化>1%才算明显
            hold_label = {1: "持仓增加↑(资金入场)", 0: "持仓持平", -1: "持仓减少↓(资金离场)"}.get(hold_dir, "?")
            print(f"  📊 DCE持仓量:{hold_now:,.0f}手({'+' if hold_chg_pct>=0 else ''}{hold_chg_pct:+.1f}%) | {hold_label}")
    except Exception as e:
        pass  # 持仓数据非关键，失败不提示

    # ── USDA全窗口检测(国际版→中国版移植:wasde/export/crop全分类)────────
    import calendar
    usda_holidays = [(1,1),(1,19),(2,16),(4,3),(5,25),(7,3),(9,7),(11,26),(12,25)]
    usda_win = False; usda_type = None; usda_detail = {}
    for m,d in usda_holidays:
        if now.month==m and abs(now.day-d)<=1:
            usda_win=True; usda_type='holiday'; usda_detail={'label':'美国假日'}; break
    if not usda_win:
        first_wd=calendar.monthrange(now.year,now.month)[0]
        second_wed=(2-first_wd+7)%7+1+7
        wasde_date=datetime(now.year,now.month,second_wed)
        wasde_hours=abs((wasde_date-now).total_seconds()/3600)
        if wasde_hours<=36:
            usda_win=True; usda_type='wasde'; usda_detail={'hours':round(wasde_hours,1),'label':f'WASDE {wasde_hours:.0f}h内'}
    if usda_win:
        label = usda_detail.get('label', usda_type) if usda_detail else usda_type
        usda_win_label = {'wasde': '⚠️WASDE', 'export': '📦出口销售', 'pre_export': '📦出口销售(预)',
                         'post_export': '📦出口销售(后)', 'crop': '🌱作物进度', 'pre_crop': '🌱作物进度(预)',
                         'post_crop': '🌱作物进度(后)', 'holiday': '🇺🇸美国假日'}.get(usda_type, '⚠️')
        print(f"  {usda_win_label}报告窗口(CBOT波动将传导至DCE)")
    else:
        usda_win = False; usda_type = None

    # ── 数据准备 ──    # ── 数据准备 ──
    corn_prices = corn_df['close'].tolist()
    corn_highs = corn_df['high'].tolist()
    corn_lows = corn_df['low'].tolist()
    corn_vols = corn_df['volume'].tolist()

    today_corn = corn_df.iloc[-1]
    yesterday_corn = corn_df.iloc[-2]
    latest_close = today_corn['close']
    prev_close = yesterday_corn['close']
    change = latest_close - prev_close
    change_pct = change / prev_close * 100

    # ── 日盘/夜盘分离数据(v2.0新增) ──
    # 使用5分钟K线重构日盘(09:00-15:00)和夜盘(21:00-23:00)各自的开高低收
    _sess = fetch_with_timeout(ak.futures_zh_minute_sina, "C0", "5")
    _day_sess = None; _night_sess = None; _today_str = now.strftime('%Y-%m-%d')
    if _sess is not None:
        import pandas as _pd
        _sess['datetime'] = _pd.to_datetime(_sess['datetime'])
        _sess['date'] = _sess['datetime'].dt.date
        _sess['hour'] = _sess['datetime'].dt.hour
        _today = _pd.to_datetime(_today_str).date()
        _td = _sess[_sess['date'] == _today]
        if len(_td) > 0:
            _day = _td[(_td['hour'] >= 9) & (_td['hour'] < 15)]
            _night = _td[(_td['hour'] >= 21) | (_td['hour'] < 5)]
            if len(_day) > 0:
                _day_sess = {'open': float(_day.iloc[0]['close']), 'high': float(_day['high'].max()),
                             'low': float(_day['low'].min()), 'close': float(_day.iloc[-1]['close']),
                             'volume': int(_day['volume'].sum())}
            if len(_night) > 0:
                _night_sess = {'open': float(_night.iloc[0]['close']), 'high': float(_night['high'].max()),
                               'low': float(_night['low'].min()), 'close': float(_night.iloc[-1]['close']),
                               'volume': int(_night['volume'].sum())}
        if _day_sess and _night_sess:
            _sess_gap = _night_sess['close'] - _day_sess['close']
            _sess_gap_pct = _sess_gap / _day_sess['close'] * 100
            print(f"\n  🌙 日夜盘: 日盘收{_day_sess['close']:.0f} → 夜盘收{_night_sess['close']:.0f} (gap {_sess_gap:+.0f},{_sess_gap_pct:+.2f}%)")
    else:
        print(f"\n  ⚠️ 分钟数据获取失败,日夜盘分离功能不可用")

    current_month = now.month

    # ── 日夜盘技术信号 ──
    # 用日盘和夜盘分别计算关键技术信号
    if _day_sess and len(corn_prices) >= 2:
        _day_close_for_signal = _day_sess['close']
        _night_close_for_signal = _night_sess['close'] if _night_sess else _day_close_for_signal
        # 日盘RSI(基于日盘收盘序列)
        # 使用正确的RSI公式(调用rsi函数)，价格序列末尾替换为session收盘
        _dprices = corn_prices[:-1] + [_day_close_for_signal]
        _d_rsi = rsi(_dprices, period=14) if len(_dprices) >= 15 else None
        _night_rsi = None
        if _night_sess and len(corn_prices) >= 2:
            _nprices = corn_prices[:-1] + [_night_close_for_signal]
            _night_rsi = rsi(_nprices, period=14) if len(_nprices) >= 15 else None
        _drsi_str = f"{_d_rsi:.0f}" if _d_rsi is not None else "?"
        _nrsi_str = f"{_night_rsi:.0f}" if _night_rsi is not None else "?"
        print(f"  技术信号: 日盘RSI={_drsi_str} | 夜盘RSI={_nrsi_str}")

    # ── 政策事件检测 ──
    policy_event, policy_dir, policy_hit_rate = get_policy_event(now)
    if policy_event:
        print(f"\n  ⚠️ 历史政策窗口(回测参考):{policy_event}(回测胜率{policy_hit_rate}%)")

    # 政策事件冲突检测
    if policy_event:
        event_bias = "利多" if policy_dir > 0 else "利空"
        print(f"  → 事件倾向:{event_bias},权重{'高' if policy_hit_rate>=55 else '中低'}")

    # ── 技术指标 ──
    ma5 = sma(corn_prices, 5)
    ma10 = sma(corn_prices, 10)
    ma20 = sma(corn_prices, 20)
    ma60 = sma(corn_prices, 60) if len(corn_prices) >= 60 else None

    ma20_sd = stddev(corn_prices, 20)
    bb_upper = ma20 + 2*ma20_sd if ma20_sd else None
    bb_lower = ma20 - 2*ma20_sd if ma20_sd else None
    bb_pos = ((latest_close - bb_lower)/(bb_upper - bb_lower)*100
              if bb_upper and bb_lower and bb_upper != bb_lower else 50)

    rsi_val = rsi(corn_prices)
    macd_val, macd_sig, hist, prev_hist, macd_line = compute_macd(corn_prices)
    div = detect_divergence(corn_prices, corn_highs, corn_lows, macd_line)

    avg_vol = statistics.mean(corn_vols[-5:]) if len(corn_vols) >= 5 else 1
    vol_ratio = today_corn['volume'] / avg_vol if avg_vol > 0 else 1

    # ── 跨市场综合信号(CBOT领先+DCE过滤)────────
    try:
        cbot_recent_chg = cbot_chg if 'cbot_chg' in dir() else None
        # 跨市场信号(CBOT趋势领先+DCE情绪过滤,直接内联)
        cross_notes = []
        if cbot_trend != 0:
            cross_notes.append(f"CBOT玉米({cbot_trend:+.0f})")
        if wheat_trend != 0:
            cross_notes.append(f"CBOT小麦({wheat_trend:+.0f})")
        if usda_win:
            boost_map = {'wasde': 15, 'export': 8, 'pre_export': 8, 'post_export': 8, 'crop': 6, 'pre_crop': 6, 'post_crop': 6, 'holiday': 5}
            cross_conf_boost = boost_map.get(usda_type, 0)
            if cross_conf_boost > 0:
                cross_notes.append(f"USDA窗口(+{cross_conf_boost}pp)")
        else:
            cross_conf_boost = 0
        cross_direction = cbot_trend
        if cross_notes:
            print(f"  🔗跨市场:{' | '.join(cross_notes)}")
        else:
            # CBOT趋势单独提示
            if cbot_trend != 0:
                trend_emoji = "🟢" if cbot_trend > 0 else "🔴"
                print(f"  {trend_emoji}CBOT当日涨跌→DCE传导(参考,同向率54%)")
    except Exception as e:
        cross_direction = 0; cross_conf_boost = 0
    # ── 豆粕关联 ──
    if soy_df is not None and not soy_df.empty:
        soy_prices = soy_df['close'].tolist()
        soy_corr = correlation(corn_prices, soy_prices)
        soy_ma5 = sma(soy_prices, 5)
        soy_ma20 = sma(soy_prices, 20) if len(soy_prices) >= 20 else None
        soy_trend = 1 if (soy_ma5 and soy_ma20 and soy_ma5 > soy_ma20) else -1 if (soy_ma5 and soy_ma20 and soy_ma5 < soy_ma20) else 0
    else:
        soy_prices = []; soy_corr = 0; soy_ma5 = None; soy_ma20 = None; soy_trend = 0

    # ── 原油关联 ──
    if oil_df is not None and not oil_df.empty:
        oil_prices = oil_df['close'].tolist()
        oil_cur = oil_prices[-1]
        oil_prev = oil_prices[-2] if len(oil_prices) >= 2 else oil_cur
        oil_chg_pct = (oil_cur - oil_prev) / oil_prev * 100
    else:
        oil_prices = []; oil_cur = 0; oil_prev = 0; oil_chg_pct = 0

    # ── 豆油关联 ──
    if bean_df is not None and not bean_df.empty:
        bean_prices = bean_df['close'].tolist()
        bean_cur = bean_prices[-1]
        bean_prev = bean_prices[-2] if len(bean_prices) >= 2 else bean_cur
        bean_chg_pct = (bean_cur - bean_prev) / bean_prev * 100
    else:
        bean_prices = []; bean_cur = 0; bean_prev = 0; bean_chg_pct = 0

    # ── 新闻 ──
# ── 政策新闻动态(东方财富) ──
    import os as _os
    from constants import NEWS_LOG as _NL
    news_score = 0
    NEWS_LOG = _NL
    try:
        result, news_items = pn.get_policy_news(days_back=10, total_timeout=45)
        print("\n【今日政策/市场新闻】")
        if result["has_news"]:
            print(f"  {result['signal_emoji']} {result['signal']}(综合评分: {result['overall_score']:+.2f})共{result['count']}条")
            news_log_lines = [f"\n**{_today_str}** | 综合评分: {result['overall_score']:+.2f} | {result['signal_emoji']} {result['signal']}\n"]

            # 检测是否与上一条记录重复（读取日志最后一行标题判断）
            _dup_count = 0
            _unique_count = 0
            try:
                with open(NEWS_LOG, "r", encoding="utf-8") as _lk:
                    _last_titles = set(line.strip() for line in _lk.readlines() if line.startswith("- "))
            except FileNotFoundError:
                _last_titles = set()

            for j, item in enumerate(result["top_items"][:6], 1):
                sc = item["score"]
                emoji = "🟢" if sc > 0 else "🔴" if sc < 0 else "⚪"
                title_short = item['title'][:50]
                _is_dup = f"- {emoji} {sc:+.1f}分 | {title_short}" in _last_titles
                if _is_dup:
                    _dup_count += 1
                else:
                    _unique_count += 1
                dup_tag = " (重复)" if _is_dup else ""
                print(f"  {j}. {emoji}{sc:+.1f}分 {title_short}{dup_tag}")
                news_log_lines.append(f"- {emoji} {sc:+.1f}分 | {title_short}\n")

            # 如果全是重复，加标注
            if _unique_count == 0 and _dup_count > 0:
                news_log_lines.append("> ⚠️ 当日无新增有效新闻（与上日内容一致）\n")
            elif _unique_count < _dup_count:
                news_log_lines.append(f"> ℹ️ 仅{_unique_count}条新增，{_dup_count}条与上日重复\n")

            # 追加写入新闻日志
            with open(NEWS_LOG, "a", encoding="utf-8") as _nf:
                _nf.writelines(news_log_lines)
            news_score = result["overall_score"]

            # ── 记录新闻影响数据库 ──
            try:
                _import_il = __import__("news_impact", fromlist=["record_news_impact"])
                _import_il.record_news_impact(news_items, _today_str)
            except Exception as _ile:
                print(f"  (新闻影响记录跳过: {_ile})")
        else:
            print("  暂无相关政策新闻")
            with open(NEWS_LOG, "a", encoding="utf-8") as _nf:
                _nf.write(f"\n**{_today_str}** | 无相关政策新闻\n")
    except Exception as e:
        news_score = 0
        print(f"\n【今日政策/市场新闻】获取失败({e})")
        with open(NEWS_LOG, "a", encoding="utf-8") as _nf:
            _nf.write(f"\n**{_today_str}** | 新闻获取失败: {e}\n")

    # ── 输出 ──
    print(f"\n{'='*60}")
    print(f"【DCE玉米期货 C0】")
    print(f"  今日收盘:{latest_close:.0f} 元/吨({change:+.0f},{change_pct:+.2f}%)")
    print(f"  昨结算价:{yesterday_corn['close']:.0f} | 今结算:{today_corn.get('settle', latest_close):.0f}")
    print(f"  成交量:{today_corn['volume']:,.0f} 手(5日均量 {avg_vol:,.0f},比率{vol_ratio:.2f}x)")

    print(f"\n【均线】")
    print(f"  MA5={ma5:.0f}  MA10={ma10:.0f}  MA20={ma20:.0f}" + (f"  MA60={ma60:.0f}" if ma60 else ""))
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20: print(f"  → 多头排列 ↑")
        elif ma5 < ma10 < ma20: print(f"  → 空头排列 ↓")
        else: print(f"  → 混乱排列 ↔")

    print(f"\n【布林带(20,2σ)】")
    if bb_upper and bb_lower:
        print(f"  上轨={bb_upper:.0f}  中轨={ma20:.0f}  下轨={bb_lower:.0f}")
        print(f"  带宽={bb_upper-bb_lower:.0f}  位置={bb_pos:.0f}%" + ("(超买区⚠️" if bb_pos>80 else "(超卖区⚠️" if bb_pos<20 else ""))

    print(f"\n【RSI(14)】")
    if rsi_val:
        zone = "超买⚠️" if rsi_val>70 else "超卖⚠️" if rsi_val<30 else "偏强" if rsi_val>60 else "偏弱" if rsi_val<40 else "中性"
        print(f"  RSI={rsi_val:.1f} {zone}")

    print(f"\n【MACD(12,26,9)】")
    if macd_val is not None:
        print(f"  MACD线={macd_val:.2f}  信号线={macd_sig:.2f}  柱状图={hist:+.3f}")
        print(f"  → {'金叉↑(看多)' if hist>0 else '死叉↓(看空)'}  {'动能增强' if hist>prev_hist else '动能减弱'}")
    else:
        print(f"  数据不足")

    print(f"\n【背离信号】")
    if div['top']: print(f"  ⚠️ 顶背离:{' '.join(div['reasons'])}")
    elif div['bottom']: print(f"  ✅ 底背离:{' '.join(div['reasons'])}")
    else: print(f"  无明显背离")

    # ── 综合信号 ──
    print(f"\n{'='*60}")
    print(f"【综合信号】")
    signals = []
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20: signals.append("均线多头✅")
        elif ma5 < ma10 < ma20: signals.append("均线空头❌")
        else: signals.append("均线混乱⚠️")
    if bb_pos > 80: signals.append(f"布林超买({bb_pos:.0f}%)⚠️")
    elif bb_pos < 20: signals.append(f"布林超卖({bb_pos:.0f}%)⚠️")
    if rsi_val:
        if rsi_val > 70: signals.append(f"RSI超买({rsi_val:.0f})⚠️")
        elif rsi_val < 30: signals.append(f"RSI超卖({rsi_val:.0f})⚠️")
    if macd_val is not None:
        signals.append(f"MACD{'看多✅' if hist>0 else '看空❌'}")
    if vol_ratio > 1.2: signals.append(f"放量{'✅' if change>0 else '❌'}")
    elif vol_ratio < 0.8: signals.append(f"缩量⚠️")
    if div['top']: signals.append("顶背离⚠️")
    if div['bottom']: signals.append("底背离✅")
    for s in signals: print(f"  • {s}")

    # ── 豆粕/豆油关联 ──
    print(f"\n【豆粕关联(DCE豆粕M0)】")
    print(f"  最新价:{soy_prices[-1]:.0f} 元/吨")
    soy_ma20_str = f"{soy_ma20:.0f}" if soy_ma20 else "?"
    print(f"  均线:MA5={soy_ma5:.0f}  MA20={soy_ma20_str}")
    print(f"  相关性:{soy_corr:.3f}" + ("(强正相关)" if soy_corr>0.5 else "(弱相关)"))
    print(f"  → 大豆偏{'多' if soy_trend>0 else '空'}趋势" if soy_trend != 0 else "  → 豆粕趋势不明")
    if soy_corr > 0.5 and soy_trend > 0 and (ma5 and ma10 and ma20 and ma5>ma10>ma20):
        print(f"  → ✅ 玉米+豆粕共振偏多")
    elif soy_corr > 0.5 and soy_trend < 0 and (ma5 and ma10 and ma20 and ma5<ma10<ma20):
        print(f"  → ❌ 玉米+豆粕共振偏空")
    elif soy_corr > 0.5:
        print(f"  → ⚠️ 玉米+豆粕分化")

    print(f"\n【原油关联(INE原油SC0)】")
    print(f"  最新价:{oil_cur:.1f} 元/桶")
    print(f"  变化:{oil_chg_pct:+.2f}%")
    if oil_chg_pct > 2: print(f"  → 原油大涨(+{oil_chg_pct:.1f}%),乙醇原料成本上升,利多玉米✅")
    elif oil_chg_pct < -2: print(f"  → 原油大跌({oil_chg_pct:.1f}%),拖累玉米❌")
    else: print(f"  → 原油平稳,影响中性")

    print(f"\n【豆油关联(DCE豆油Y0)】")
    print(f"  最新价:{bean_cur:.0f} 元/吨")
    print(f"  变化:{bean_chg_pct:+.2f}%")

    # ── 季节性 ──
    print(f"\n【中国玉米季节性】")
    sscore = CN_SEASON_SCORE.get(current_month, 0)
    sdesc = CN_SEASON_DESC.get(current_month, "")
    sdir = "偏多↑" if sscore>0 else "偏空↓" if sscore<0 else "中性"
    print(f"  {current_month}月:{sdesc}")
    print(f"  季节性评分:{sscore:+.1f}({sdir})")

    # ── USDA窗口(国际版→中国版:全类型检测)─────────
    # usda_win, usda_type 已在前面通过国际版USDA窗口逻辑设置
    # 月度传导统计(121次报告回测数据)
    month_stats = {
        1: {"rate": 60, "avg": 0.10}, 2: {"rate": 50, "avg": -0.24},
        3: {"rate": 60, "avg": 0.12}, 4: {"rate": 50, "avg": -0.14},
        5: {"rate": 46, "avg": 0.20},  6: {"rate": 70, "avg": 0.83},
        7: {"rate": 50, "avg": -0.29}, 8: {"rate": 60, "avg": 0.04},
        9: {"rate": 60, "avg": -0.06}, 10: {"rate": 70, "avg": 0.36},
        11: {"rate": 80, "avg": 0.24}, 12: {"rate": 50, "avg": 0.04},
    }
    if usda_win:
        m_stats = month_stats.get(now.month, {})
        rate = m_stats.get("rate", 50)
        dce_avg = m_stats.get("avg", 0)
        type_label = {'wasde': 'WASDE供需', 'export': '出口销售', 'pre_export': '出口销售(前)',
                      'post_export': '出口销售(后)', 'crop': '作物进度', 'pre_crop': '作物进度(前)',
                      'post_crop': '作物进度(后)', 'holiday': '美国假日'}.get(usda_type, usda_type)
        print(f"\n【USDA报告窗口】⚠️ {type_label}")
        print(f"  当前敏感窗口,月度传导同向率{rate:.0f}%/历史DCE平均{dce_avg:+.2f}%")
        print(f"  → CBOT波动→DCE传导系数0.92(1%=0.92%)")
    else:
        print(f"\n【USDA报告窗口】✅ 无USDA敏感窗口")

    # ── USDA报告日历(国际版→中国版移植)────────
    usda_cal = get_usda_report_info_for_cn(now)
    w = usda_cal['wasde']
    c = usda_cal['crop']
    e = usda_cal['export']
    print(f"\n【USDA报告日历】")
    print(f"  本月WASDE:{w.strftime('%m-%d(周三)%H:%M')}北京时间")
    print(f"  作物进度:{c.strftime('%m-%d(周二)%H:%M')} | 出口销售:{e.strftime('%m-%d(周四)%H:%M')}北京时间")

    # ── 关键价位 ──
    recent_highs = corn_highs[-5:]
    recent_lows = corn_lows[-5:]
    resistance = max(recent_highs)
    support = min(recent_lows)
    print(f"\n【关键价位】")
    print(f"  阻力(近5日高点):{resistance:.0f}")
    print(f"  支撑(近5日低点):{support:.0f}")
    if bb_upper: print(f"  布林上轨:{bb_upper:.0f}")
    if bb_lower: print(f"  布林下轨:{bb_lower:.0f}")

    # ── 信号冲突 ──
    print(f"\n【信号冲突检测】")
    conflicts = []
    if ma5 and ma10 and ma20 and ma5>ma10>ma20 and rsi_val and rsi_val>65 and vol_ratio<0.8:
        conflicts.append("⚠️技术面诱多:均线多头+RSI偏高+缩量,警惕假突破")
    if sscore < -0.3 and ma5 and ma10 and ma20 and ma5>ma10>ma20 and macd_val and macd_val>0:
        conflicts.append(f"⚠️技术面/季节性对冲:{current_month}月季节性偏空,技术面信号需打折")
    if usda_win:
        m_label = usda_detail.get('label', 'CBOT波动将传导至DCE') if usda_detail else "CBOT波动将传导至DCE"
        conflicts.append(f"⚠️USDA窗口:{m_label},注意隔夜风险")
    # ENSO信号冲突检测
    if enso_dir != 0 and sscore < -0.3:
        conflicts.append(f"⚠️ENSO/季节性对冲:{enso_phase}但{current_month}月季节性偏空,两者方向相反")
    if conflicts:
        for c in conflicts:
            print(f"  {c}")
    else:
        print(f"  无显著冲突")

    # ── 置信度评分(v3.0 - 基于4930样本回测数据优化)───
    # 回测命中率(4930样本):RSI52.5%·政策52.5%·CFTC51.2%·大豆52.2%
    # v5.0 权重: numpy 向量化两步搜索（31K 粗搜+512 精搜）
    # 方向准确率:基准52.2% → 最优53.8%(+1.6pp)
    # 提升: MA↑(1.2→1.6) RSI↑(2.0→2.2) BB↑(1.3→1.8) 其余维持
    ma_dir = 1 if (ma5 and ma10 and ma20 and ma5>ma10>ma20) else (-1 if (ma5 and ma10 and ma20 and ma5<ma10<ma20) else 0)
    rsi_sig = 1 if (rsi_val and rsi_val<40) else (-1 if (rsi_val and rsi_val>65) else 0)
    macd_sig = 1 if (hist and hist>0) else -1
    vol_sig = 1 if vol_ratio>1.2 else (-0.5 if vol_ratio<0.8 else 0)
    soy_sig = soy_corr * soy_trend if soy_corr>0.5 else 0
    oil_sig = 1 if oil_chg_pct>2 else (-1 if oil_chg_pct<-2 else 0)
    # 布林带位置信号(v3.0新增):价格位置<20%超卖→+1,>80%超买→-1
    bb_sig = 1 if (bb_pos and bb_pos<20) else (-1 if (bb_pos and bb_pos>80) else 0)

    # CFTC信号(回测胜率51.2%,轻微有效)
    # v5.2: 淘汰信号，不再调用API
    cftc_sig = 0
    fund_sig = 0
    merchant_sig = 0

    # 政策事件信号(回测胜率52.5%,有效时加权)
    policy_sig = 0
    if policy_event and policy_hit_rate >= 52:
        policy_sig = policy_dir * (policy_hit_rate - 50) / 50  # 标准化到约0~0.4

    # v5.2: 淘汰信号的展示值（不做API调用，仅占位）
    usda_sig = 0; usda_score = 0; usda_note = "(已淘汰)"
    hog_sig = 0; hog_score = 0
    import_sig = 0; import_score = 0
    eth_sig = 0; eth_score = 0
    basis_sig = 0; basis_score = 0

    # 加权信号(v5.0):基于两步搜索（31K粗搜+512精搜）
    # 方向准确率:基准52.2% → 最优53.8%(+1.6pp)
    # 提升: MA↑(1.2→1.6) RSI↑(2.0→2.2) BB↑(1.3→1.8)
    # 新增(2026-05-26): USDA出口销售(0.5x) + 生猪/饲料需求(0.5x) + 进口成本(0.3x)
    # 时间衰减(2026-05-26): 每个信号乘近20笔准确率衰减系数 0.3x~1.5x
    decay = sd.get_decay_factors()
    # 低频信号名称到 data_freshness key 的映射
    _FRESH_KEYS = {
        "USDA出口": "usda_export", "生猪": "hog_profit",
        "进口成本": "import_cost", "乙醇": "ethanol_policy",
        "港口升贴水": "port_basis", "库存消费比": "usda_wasde",
    }
    def _dw(base_w, signal_name):
        td = decay.get(signal_name, 1.0)  # 时间衰减
        fk = _FRESH_KEYS.get(signal_name)
        if fk:
            td *= df.get_freshness_multiplier(fk)  # 时效性衰减
        return base_w * td

    # ── 精简信号加权（v5.2） ──
    # 基于 optimize_strategy.py 回测结论：保留8核心信号+天气
    # 权重对齐OOS walk-forward 67段均值（MA 0.93→1.2, RSI 1.73→1.9, BB 1.31→1.5）
    # 折中方案：OOS均值+当前权重取中点，避免剧烈跳动
    # 淘汰信号完全不参与加权计算（v5.2：不再有痕迹权重，避免稀释方向判断）
    weighted = [
        (ma_dir, _dw(1.2, "MA")),           # 保留 · MA（OOS 0.93, 当前 1.6 → 折中 1.2）
        (rsi_sig, _dw(1.9, "RSI")),          # 保留 · RSI（OOS 1.73, 当前 2.2 → 折中 1.9）
        (macd_sig, _dw(0.6, "MACD")),         # 保留 · MACD（OOS 0.75, 当前 0.5 → 折中 0.6）
        (vol_sig, _dw(0.35, "成交量")),          # 保留 · 成交量（OOS 0.38, 当前 0.3 → 折中 0.35）
        (bb_sig, _dw(1.5, "布林带")),           # 保留 · 布林带（OOS 1.31, 当前 1.8 → 折中 1.5）
        (soy_sig, _dw(0.7, "大豆")),          # 保留 · 大豆共振（OOS 0.63, 当前 0.8 → 折中 0.7）
        (policy_sig, _dw(1.0, "政策") if policy_hit_rate >= 52 else 0),  # 保留 · 政策
        (cross_direction, _dw(0.4, "CBOT")),  # 保留 · CBOT传导（OOS 0.34, 当前 0.5 → 折中 0.4）
        (weather_score, _dw(1.0 if abs(weather_score or 0) <= 0.3 else 2.0, "天气")),  # 保留 · 天气/动态
    ]
    total_weight = sum(abs(w) for _, w in weighted)
    weighted_sum = sum(s*w for s, w in weighted)
    raw_consistency = abs(weighted_sum)/total_weight if total_weight > 0 else 0
    consistency_pct = int(raw_consistency * 100)

    agreeing = sum(1 for s,w in weighted if s!=0 and s*weighted_sum>0)
    total_active = sum(1 for _,w in weighted if w>0)

    # ── 因子交互检测 (P1-5) ──
    # 仅检测活跃核心信号+特殊信号，淘汰信号不做交互检测
    signals_dict = {
        "MA": ma_dir, "RSI": rsi_sig, "MACD": macd_sig,
        "成交量": vol_sig, "布林带": bb_sig,
        "大豆": soy_sig, "政策": policy_sig,
        "CBOT": cross_direction, "天气": weather_score,
        "⛔季节性": sscore, "⛔CFTC": cftc_sig,
        "⛔ENSO": enso_dir, "⛔BCI": bci_dir,
        "⛔持仓量": hold_dir, "⛔新闻": max(-1, min(1, news_score / 2)),
        "⛔USDA出口": usda_sig * usda_score,
        "⛔生猪": hog_sig * hog_score,
        "⛔进口成本": import_sig * import_score,
        "⛔乙醇": eth_sig * eth_score,
    }
    # 将连续值离散化为方向
    dir_signals = {k: (1 if v > 0.1 else (-1 if v < -0.1 else 0)) for k, v in signals_dict.items()}
    conflict = fi.detect_real_time_conflict(dir_signals)
    conflict_penalty = conflict['conflict_score'] * 0.5  # 最高降50%置信度
    
    # 冲突分数修正判断阈值
    effective_agreeing_threshold = 5 - conflict_penalty * 4  # 冲突时提高门槛

    # USDA窗口惩罚
    # USDA窗口:不再惩罚(回测显示同向率58.7%,是有效信息)

    # 分层判断（引入因子交互冲突惩罚）
    effective_note = ""
    if conflict['conflict_pairs']:
        pairs_str = ", ".join(f"{a}↔{b}" for a,b in conflict['conflict_pairs'][:3])
        effective_note = f" | 冲突: {pairs_str}"
    
    if agreeing >= max(5, effective_agreeing_threshold) and raw_consistency > 0.4:
        conf_emoji = "🟢"; conf_label = "高置信"; conf_note = f"{agreeing}/{total_active}指标共振{effective_note}"
    elif agreeing >= max(4, effective_agreeing_threshold - 1) and raw_consistency > 0.3:
        conf_emoji = "🟡"; conf_label = "中置信"; conf_note = f"偏多信号为主({agreeing}/{total_active}){effective_note}"
    else:
        conf_emoji = "🔴"; conf_label = "低置信"; conf_note = f"信号分歧({agreeing}/{total_active}){effective_note}"

    # v5.2: CFTC展示（淘汰信号，不再参与方向判断）
    cftc_info = "CFTC(已淘汰) | 基金(已淘汰)"
    policy_info = f"政策:{policy_event[:12]}...({'利多' if policy_dir>0 else '利空'})" if policy_event else "无政策事件"

    print(f"\n{'='*60}")
    print(f"【置信度评分(v3.0 - 基于4930样本回测优化)】")
    print(f"  {conf_emoji} {conf_label}(一致性={consistency_pct}% | {agreeing}/{total_active}指标同向)")
    print(f"  → {conf_note}")
    rsi_str = f"{rsi_val:.0f}" if rsi_val is not None else "?"
    print(f"  技术:均线{'多头' if ma_dir>0 else '空头' if ma_dir<0 else '纠缠'} | RSI={rsi_str} | MACD={'正' if macd_sig>0 else '负'}")
    print(f"  外部:{cftc_info} | {policy_info}")
    usda_label = f"⚠️{usda_type}" if usda_win else '否'
    print(f"  季节性:{sdir}(v3.0·权重0.3x)| USDA窗口:{usda_label}")
    
    # v5.2: 淘汰信号的展示（不做API调用，固定标记）
    print(f"  基本面:USDA出口(已淘汰) | 生猪(已淘汰) | 进口(已淘汰) | 乙醇(已淘汰) | 基差(已淘汰)")
    if conflict['conflict_pairs']:
        conflict_summary = " | ".join(f"{a}↔{b}" for a,b in conflict['conflict_pairs'][:3])
        print(f"  ⚡ 信号冲突: {conflict_summary} (冲突评分={conflict['conflict_score']:.2f})")

    # ── 四个时间维度预测 ──
    print(f"\n{'='*60}")
    print(f"【多周期预测】")

    # ========== 第二日预测(日盘+夜盘双预测) ==========
    # v5.2 统一量纲：net_dir_ratio 归一化到 -1~+1，每单位≈3元/吨
    # 阈值过滤：净加权方向<5%视为无方向信号（对齐回测0.05阈值）
    net_dir_ratio = weighted_sum / total_weight if total_weight > 0 else 0
    if abs(net_dir_ratio) > 0.05:
        # v5.2: 使用归一化的 net_dir_ratio 替代 weighted_sum，量纲统一
        direction = 1 if net_dir_ratio > 0 else -1
        dir_strength = abs(net_dir_ratio)
    else:
        direction = 0
        dir_strength = 0

    # ── MA60趋势过滤器（v5.1新增） ──
    # 基于交易策略回测发现：大波动时方向反指是主要亏损原因
    # MA60方向明确时，禁止逆势信号开仓
    # MA60上升趋势(slope>0) → 只做多，MA60下降趋势(slope<0) → 只做空
    ma60 = sma(corn_prices, 60) if len(corn_prices) >= 60 else None
    ma60_slope = (ma60 - sma(corn_prices[:], 60)) if ma60 else None
    if ma60 and len(corn_prices) >= 120:
        ma60_prev = sma(corn_prices[:-20], 60) if len(corn_prices) >= 80 else None
        if ma60_prev:
            ma60_trend = 1 if ma60 > ma60_prev * 1.002 else (-1 if ma60 < ma60_prev * 0.998 else 0)
        else:
            ma60_trend = 0
    else:
        ma60_trend = 0

    if ma60_trend != 0 and direction != 0:
        if direction * ma60_trend < 0:
            # v5.2: 逆势信号降权50%而非归零
            dir_strength *= 0.5

    recent_vol = statistics.stdev(corn_prices[-20:]) if len(corn_prices)>=20 else 20
    # v5.2: base_chg = dir_strength * direction * unit_move（量纲统一用归一化强度）
    unit_move = max(recent_vol * 0.15, 1.5)  # 每单位方向最小1.5元，避免零波动
    base_chg = dir_strength * direction * unit_move
    scen_d2 = max(int(recent_vol * 4), 18)  # ±4σ≈95% coverage; 下限18元/吨
    ntd = next_trading_day(now)

    def _make_pred(base_price, label, sig_rsi):
        pred = round(base_price + base_chg, 0)
        opt = round(pred + scen_d2, 0)
        pes = round(pred - scen_d2, 0)
        if bb_upper:
            opt = min(opt, bb_upper)
            pred = min(pred, bb_upper)
        if bb_lower:
            pes = max(pes, bb_lower)
        # 趋势确认机制: 均线空头排列时强制输出偏弱，避免季节性误判
        if ma_dir < 0:
            d_dir = "↘ 震荡偏弱(均线空头)"
        elif ma_dir > 0:
            d_dir = "↗ 震荡偏强(均线多头)" if direction > 0 else ("↘ 震荡偏弱" if direction < 0 else "↔ 震荡整理")
        else:
            d_dir = "↗ 震荡偏强" if direction>0 else "↘ 震荡偏弱" if direction<0 else "↔ 震荡整理"
        chg_pct = ((pred - base_price) / base_price * 100) if base_price else 0
        return pred, pes, opt, d_dir, chg_pct

    # 日盘预测: 基于日盘收盘(15:00)
    day_base = _day_sess['close'] if _day_sess else latest_close
    _d_rsi_final = _d_rsi if '_d_rsi' in dir() else rsi_val
    d_pred, d_pes, d_opt, d_dir, d_chg = _make_pred(day_base, "日盘", _d_rsi_final)
    print(f"  ── 日盘(15:00) ──")
    print(f"    基准:{d_pred:.0f}元/吨 → 区间:{d_pes:.0f}~{d_opt:.0f}")
    print(f"    方向:{d_dir}")

    # 夜盘预测: 基于夜盘收盘(23:00) + ML模型 + CBOT传导(v2.0)
    night_base = (_night_sess['close'] if _night_sess else day_base)

    # 尝试使用ML夜盘预测模型（基于5172个样本训练）
    try:
        import sys as _sys, os as _os
        _script_dir = _os.path.dirname(_os.path.abspath(__file__))
        if _script_dir not in _sys.path:
            _sys.path.insert(0, _script_dir)
        from predict_night import predict_night_session
        _night_result = predict_night_session(
            night_base_price=night_base,
            corn_df=corn_df,
            cbot_chg=cbot_chg,
            cbot_coef=10.0
        )
        n_pred = _night_result['pred']
        n_pes = _night_result['pred_low']
        n_opt = _night_result['pred_high']
        n_dir = _night_result['direction']
        n_chg = _night_result['chg']
        n_conf = _night_result['confidence']
        print(f"  🌙 夜盘ML模型: Ridge={_night_result['night_model_info']['ridge_pred']:+.2f} "
              f"RF={_night_result['night_model_info']['rf_pred']:+.2f} "
              f"→ 预测变化={n_chg:+.1f}元/吨 "
              f"置信度={n_conf}")
        if cbot_chg is not None and abs(cbot_chg) > 0.1:
            print(f"  📌 CBOT传导: CBOT{cbot_chg:+.2f}% → DCE调整约{_night_result['cbot_adj']:+.0f}元")
    except Exception as _e:
        # 回退到规则预测
        n_pred, n_pes, n_opt, n_dir, n_chg = _make_pred(night_base, "夜盘", _night_rsi)
        if cbot_chg is not None:
            cbot_adj = cbot_chg * 10
            n_pred += cbot_adj; n_opt += cbot_adj; n_pes += cbot_adj
            n_dir = f"↗ 偏强(CBOT+{cbot_chg:+.1f}%)" if cbot_chg > 0.3 else n_dir if cbot_chg > -0.3 else f"↘ 偏弱(CBOT{cbot_chg:+.1f}%)"
            print(f"  📌 夜盘CBOT传导: CBOT{cbot_chg:+.2f}% → DCE调整约{cbot_adj:+.0f}元（规则回退）")
        print(f"  ⚠️ 夜盘ML模型失败({_e})，使用规则预测")
    print(f"  ── 夜盘(23:00) ──")
    print(f"    基准:{n_pred:.0f}元/吨 → 区间:{n_pes:.0f}~{n_opt:.0f}")
    print(f"    方向:{n_dir}")

    # 合并全日区间(用于参考)
    full_opt = max(d_opt, n_opt)
    full_pes = min(d_pes, n_pes)
    print(f"  ── 全日参考区间 ── {full_pes:.0f} ~ {full_opt:.0f} 元/吨")
    # ML模型预测次高/次低价
    try:
        import sys as _sys, os as _os
        _script_dir = _os.path.dirname(_os.path.abspath(__file__))
        if _script_dir not in _sys.path:
            _sys.path.insert(0, _script_dir)
        from predict_hl import predict_hl
        pred_h, pred_l, _, latest_row = predict_hl()
        if pred_h:
            hl_range = pred_h - pred_l
            hl_pct = hl_range / latest_close * 100
            # 从predict_hl返回的最新行计算今日波幅
            today_range = float(latest_row["high"].values[0] - latest_row["low"].values[0])
            range_icon = "↗放大" if hl_range > today_range * 1.1 else "↘收缩" if hl_range < today_range * 0.9 else "↔相当"
            print(f"    模型预测高价:{pred_h:.0f} | 低价:{pred_l:.0f} | 波幅:{hl_range:.0f}元({hl_pct:.1f}%) {range_icon}")
    except Exception as _e:
        print(f"    [HL模型] 加载失败: {_e}")
    if policy_event:
        print(f"    注意:历史事件参考({policy_event})期间,方向判断需谨慎")

    # ========== 短期预测(1-4周) ==========
    # 参考:MA20方向、RSI所处区域、成交量趋势、布林带位置
    bb_width = (bb_upper - bb_lower) / ma20 * 100 if (bb_upper and bb_lower and ma20) else 5
    squeeze = bb_width < 4  # 布林收口视为挤压

    # 短期信号综合
    st_dir_score = 0
    st_factors = []
    # MA20趋势
    if ma5 and ma10 and ma20:
        if ma5 > ma20 * 1.01:
            st_dir_score += 1
            st_factors.append("MA5>MA20偏多")
        elif ma5 < ma20 * 0.99:
            st_dir_score -= 1
            st_factors.append("MA5<MA20偏空")
    # RSI区域
    if rsi_val:
        if rsi_val < 40:
            st_dir_score += 1
            st_factors.append(f"RSI超卖({rsi_val:.0f})")
        elif rsi_val > 60:
            st_dir_score -= 1
            st_factors.append(f"RSI超买({rsi_val:.0f})")
    # 布林带位置
    if bb_pos and bb_pos < 20:
        st_dir_score += 1
        st_factors.append("布林下轨超卖")
    elif bb_pos and bb_pos > 80:
        st_dir_score -= 1
        st_factors.append("布林上轨超买")
    # 成交量趋势
    if vol_ratio > 1.2:
        st_factors.append("成交量放大")
    # 政策事件
    if policy_dir != 0:
        st_dir_score += policy_dir
        st_factors.append(f"政策({('利多' if policy_dir>0 else '利空')})")
    # 季节性(短期)
    if sscore > 0.3:
        st_dir_score += 1
        st_factors.append("季节性偏多")
    elif sscore < -0.3:
        st_dir_score -= 1
        st_factors.append("季节性偏空")

    st_range = recent_vol * 5  # 回测校准: 5σ覆盖~76%(20日),无方向偏置效果更稳定
    st_base = latest_close + st_dir_score * st_range * 0.15
    st_base = latest_close  # 回测:方向偏置无效(信号50%准确率),去掉偏置
    st_opt = round(st_base + st_range * 0.5, 0)  # 对称区间±K/2*recent_vol
    st_pes = round(st_base - st_range * 0.5, 0)
    st_pes = max(st_pes, bb_lower * 0.95) if bb_lower else st_pes
    st_dir = "↗ 偏多" if st_dir_score > 0 else "↘ 偏空" if st_dir_score < 0 else "↔ 中性"
    print(f"\n  📆 短期(1-4周)")
    print(f"    方向:{st_dir}(信号强度:{st_dir_score:+d}/8)")
    print(f"    关键因子:{' | '.join(st_factors[:4]) if st_factors else '无显著因子'}")
    print(f"    区间:{st_pes:.0f} ~ {st_opt:.0f} 元/吨")
    bb_l = bb_lower if bb_lower else None
    bb_u = bb_upper if bb_upper else None
    bb_l_str = f"{bb_l:.0f}" if bb_l else "?"
    bb_u_str = f"{bb_u:.0f}" if bb_u else "?"
    print(f"    参考价位:支撑 {bb_l_str} | 阻力 {bb_u_str}")

    # ========== 中期预测(1-3个月) ==========
    # 参考:MA60方向、豆粕相关性趋势、政策事件窗口、季度季节性
    mt_dir_score = 0
    mt_factors = []
    # MA60趋势(简化:MA60与MA20比较判断中长期方向)
    ma60 = sma(corn_prices, 60)
    if ma60 and ma20:
        if ma60 < ma20 * 0.99:
            mt_dir_score -= 1
            mt_factors.append("MA60<MA20空头排列")
        elif ma60 > ma20 * 1.01:
            mt_dir_score += 1
            mt_factors.append("MA60>MA20多头排列")
    # 豆粕趋势(中期正相关)
    if soy_corr > 0.6 and soy_trend > 0:
        mt_dir_score += 1
        mt_factors.append(f"豆粕强势({soy_corr:.2f})")
    elif soy_corr > 0.6 and soy_trend < 0:
        mt_dir_score -= 1
        mt_factors.append(f"豆粕弱势({soy_corr:.2f})")
    # 政策事件
    if policy_dir != 0:
        mt_dir_score += policy_dir * 1.5
        mt_factors.append(f"政策事件({('利多' if policy_dir>0 else '利空')})")
    # 季度季节性
    mt_season = {3:1, 4:1, 5:0.5, 6:1, 7:0, 8:1, 9:1, 10:0.5, 11:-1, 12:0, 1:0, 2:0}
    ms = mt_season.get(current_month, 0)
    if ms > 0:
        mt_dir_score += 1
        mt_factors.append(f"季度季节性偏多")
    elif ms < 0:
        mt_dir_score -= 1
        mt_factors.append(f"季度季节性偏空")
    # CFTC中期信号
    if cftc_sig > 0:
        mt_dir_score += 1
        mt_factors.append("CFTC净多")
    elif cftc_sig < 0:
        mt_dir_score -= 1
        mt_factors.append("CFTC净空")

    # 中期波动率(季度级别)
    qtr_vol = statistics.stdev(corn_prices[-60:]) if len(corn_prices) >= 60 else recent_vol * 2
    mt_range = qtr_vol * 3  # 回测校准: 3σ季度波动(20日≈1月)
    mt_base = latest_close  # 回测:方向偏置无效,对称区间更稳定
    mt_opt = round(mt_base + mt_range * 0.8, 0)
    mt_pes = round(mt_base - mt_range * 0.8, 0)
    mt_dir = "↗ 偏多" if mt_dir_score > 0 else "↘ 偏空" if mt_dir_score < 0 else "↔ 中性"
    print(f"\n  📆 中期(1-3个月)")
    print(f"    方向:{mt_dir}(信号强度:{int(mt_dir_score):+d})")
    print(f"    关键因子:{' | '.join(mt_factors[:4]) if mt_factors else '无显著因子'}")
    print(f"    区间:{mt_pes:.0f} ~ {mt_opt:.0f} 元/吨")
    print(f"    季度关注:{current_month}月({'青黄不接/拍卖政策' if current_month in [5,6,7] else '新粮上市压力' if current_month in [10,11] else '季节性平稳'})")

    # ========== 长期预测(3-12个月) ==========
    # 参考:年季节性、多年价格中枢、政策趋势
    lt_factors = []
    lt_dir_score = 0

    # 年均价中枢趋势
    if len(corn_prices) >= 250:
        yearly_avg_current = statistics.mean(corn_prices[-250:])
        yearly_avg_prev = statistics.mean(corn_prices[-500:-250]) if len(corn_prices) >= 500 else yearly_avg_current
        if yearly_avg_current > yearly_avg_prev * 1.02:
            lt_dir_score += 1
            lt_factors.append("年均价上升趋势")
        elif yearly_avg_current < yearly_avg_prev * 0.98:
            lt_dir_score -= 1
            lt_factors.append("年均价下降趋势")

    # 长期季节性(年度周期)
    lt_season_yearly = {
        1: ("年后淡季", -0.5), 2: ("年后淡季", -0.5),
        3: ("春耕备耕", 0.5), 4: ("春耕炒作", 1),
        5: ("青黄不接", 1), 6: ("拍卖高峰", -0.5),
        7: ("定产炒作", 1), 8: ("高温炒作", 0.5),
        9: ("收获预期", -1), 10: ("新粮上市", -1),
        11: ("卖粮高峰", -0.5), 12: ("年前备货", 0.5),
    }
    lt_name, lt_score = lt_season_yearly.get(current_month, ("平稳", 0))
    if lt_score > 0:
        lt_dir_score += int(lt_score)
        lt_factors.append(f"长期季节性({lt_name})")
    elif lt_score < 0:
        lt_dir_score += int(lt_score)
        lt_factors.append(f"长期季节性({lt_name})")

    # 结构性政策因素
    lt_policy = "临储取消+市场化" if current_month in [4,5,6] else "拍卖政策主导"
    lt_factors.append(f"政策:{lt_policy}")

    # 长期区间(年波动)
    yearly_vol = statistics.stdev(corn_prices[-250:]) if len(corn_prices) >= 250 else qtr_vol * 2
    lt_range = yearly_vol * 2
    lt_base = latest_close + lt_dir_score * lt_range * 0.15
    lt_opt = round(lt_base + lt_range * 1.2, 0)
    lt_pes = round(lt_base - lt_range * 1.2, 0)
    lt_opt = round(lt_base + lt_range * 1.2, 0)
    lt_pes = round(lt_base - lt_range * 1.2, 0)
    lt_dir = "↗ 偏多" if lt_dir_score > 0 else "↘ 偏空" if lt_dir_score < 0 else "↔ 中性"
    print(f"\n  📆 长期(3-12个月)")
    print(f"    方向:{lt_dir}(信号强度:{int(lt_dir_score):+d})")
    print(f"    关键因子:{' | '.join(lt_factors[:4]) if lt_factors else '无显著因子'}")
    print(f"    区间:{lt_pes:.0f} ~ {lt_opt:.0f} 元/吨")
    print(f"    年价格中枢参考:{yearly_avg_current:.0f} 元/吨(近一年均价)" if len(corn_prices) >= 250 else "")

    print(f"\n{'='*60}")
    print(f"✅ 分析完成 | DCE玉米C0 v2.0 | {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

if __name__ == '__main__':
    main()

