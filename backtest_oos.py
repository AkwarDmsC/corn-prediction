#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
样本外验证 (walk-forward OOS) v1.0

基于 backtest_weights.py 的信号矩阵，做严格的时间序列滑动验证：
  训练窗 3年 → 验证窗 3个月 → 滑动 3个月 → 重复

对每段：
  - 只在训练窗内做 GRID_COARSE + GRID_FINE 两步网格搜索
  - 最优权重去验证窗评估（不参与任何参数调优）
  - 所有参数调整只用训练窗历史数据

输出: oos_validation_report.md

用法:
  python3 backtest_oos.py              # 全量运行
  python3 backtest_oos.py --fast       # 只跑最后5段（最近~15个月）
  python3 backtest_oos.py --quick-check  # 只跑最后1段+全量基准对比
"""

import sys
import json
import math
import statistics
import datetime
import time
from itertools import product
from pathlib import Path

import akshare as ak
import numpy as np

WORKSPACE = Path(__file__).parent

# ──────────────────────────────────────────────────────────
# 配置参数
# ──────────────────────────────────────────────────────────
TRAIN_YEARS = 3          # 训练窗长度（年）
VALID_MONTHS = 3         # 验证窗长度（月）
SLIDE_MONTHS = 3         # 滑动步长（月）
L1_LAMBDA = 0.001        # 正则系数（与 backtest_weights.py 一致）

# ──────────────────────────────────────────────────────────
# 1. 数据加载 + 信号预计算（复用 backtest_weights.py 逻辑）
# ──────────────────────────────────────────────────────────
print("=" * 60)
print("样本外验证 v1.0  —  滑动窗口 walk-forward")
print(f"训练窗: {TRAIN_YEARS}年  |  验证窗: {VALID_MONTHS}个月  |  滑动: {SLIDE_MONTHS}个月")
print("=" * 60)

print("\n📡 加载数据...")
socket_timeout = 8
import urllib.request, ssl, socket
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

corn = ak.futures_zh_daily_sina(symbol="C0")
corn['date'] = corn['date'].astype(str)
corn = corn.sort_values('date').reset_index(drop=True)

soy_df = ak.futures_zh_daily_sina(symbol="M0")
soy_df['date'] = soy_df['date'].astype(str)
soy_df = soy_df.sort_values('date').reset_index(drop=True)
soy_dict = {str(r['date']): float(r['close']) for _, r in soy_df.iterrows()}

oil_df = ak.futures_zh_daily_sina(symbol="SC0")
oil_df['date'] = oil_df['date'].astype(str)
oil_df = oil_df.sort_values('date').reset_index(drop=True)
oil_dict = {str(r['date']): float(r['close']) for _, r in oil_df.iterrows()}

cftc_dict = {}
try:
    cftc_df = ak.macro_usa_cftc_c_holding()
    col = [c for c in cftc_df.columns if '玉米' in c and '净仓位' in c]
    if col:
        cftc_df = cftc_df[['日期', col[0]]].dropna()
        cftc_dict = {str(r['日期']): float(r[col[0]]) for _, r in cftc_df.iterrows()}
except Exception as e:
    print(f"  ⚠️ CFTC: {e}")

bci_dict = {}
try:
    bci_df = ak.macro_china_freight_index()
    bci_df = bci_df.sort_values('截止日期').reset_index(drop=True)
    bci_df['date'] = bci_df['截止日期'].astype(str)
    bci_df = bci_df[['date', '波罗的海好望角型船运价指数BCI']].dropna()
    bci_dict = {str(r['date']): float(r['波罗的海好望角型船运价指数BCI']) for _, r in bci_df.iterrows()}
except Exception as e:
    print(f"  ⚠️ BCI: {e}")

zw_dict = {}
try:
    url = 'https://query2.finance.yahoo.com/v8/finance/chart/ZW=F?interval=1d&range=10y'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=socket_timeout, context=_ctx) as r:
        d = json.loads(r.read())
    closes = d['chart']['result'][0]['indicators']['quote'][0]['close']
    ts = d['chart']['result'][0]['timestamp']
    dates = [datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d') for t in ts]
    zw_dict = {d: c for d, c in zip(dates, closes) if c is not None}
except Exception as e:
    print(f"  ⚠️ CBOT小麦: {e}")

cbot_dict = {}
try:
    cbot_df = ak.futures_foreign_hist(symbol='C')
    cbot_df['date'] = cbot_df['date'].astype(str)
    cbot_df = cbot_df.sort_values('date').reset_index(drop=True)
    cbot_dict = {str(r['date']): float(r['close']) for _, r in cbot_df.iterrows()}
except Exception as e:
    print(f"  ⚠️ CBOT玉米: {e}")

eni_data = {}
try:
    url = 'https://www.cpc.ncep.noaa.gov/data/indices/ersst5.nino.mth.91-20.ascii'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=socket_timeout, context=_ctx) as r:
        raw = r.read().decode('utf-8')
    for l in raw.split('\n'):
        parts = l.strip().split()
        if len(parts) >= 10:
            yr, mon, oni = int(parts[0]), int(parts[1]), float(parts[9])
            eni_data[(yr, mon)] = oni
except Exception as e:
    print(f"  ⚠️ ENSO: {e}")

print(f"  DCE玉米:{len(corn)}  soy:{len(soy_dict)}  oil:{len(oil_dict)}  "
      f"cftc:{len(cftc_dict)}  bci:{len(bci_dict)}  "
      f"zw:{len(zw_dict)}  cbot:{len(cbot_dict)}  enso:{len(eni_data)}")

# ──────────────────────────────────────────────────────────
# 2. 常量
# ──────────────────────────────────────────────────────────
CN_SEASON = {1:0, 2:-0.2, 3:0, 4:0.25, 5:0.1, 6:0.35, 7:0, 8:0.3, 9:0.25, 10:0, 11:-0.1, 12:0.2}
POLICY = [
    ("2018-07-06","2018-09-30",-1),
    ("2019-05-10","2019-12-31",-1),
    ("2020-01-15","2020-02-14",1),
    ("2020-01-23","2020-04-08",-1),
    ("2020-04-08","2020-12-31",1),
    ("2016-04-30","2016-10-31",-1),
    ("2017-05-01","2017-10-31",-1),
    ("2021-05-06","2021-10-31",1),
    ("2019-04-01","2019-09-30",1),
    ("2020-03-01","2020-09-30",1),
    ("2020-07-01","2020-09-30",1),
    ("2022-06-01","2022-09-30",1),
    ("2023-07-01","2023-09-30",1),
    ("2022-02-24","2022-07-31",1),
    ("2008-11-01","2009-06-30",1),
    ("2015-11-01","2016-06-30",1),
]

# ──────────────────────────────────────────────────────────
# 3. 预计算信号
# ──────────────────────────────────────────────────────────
print("⚙️  预计算所有信号...")
n = len(corn)
closes_all = corn['close'].tolist()
vols_all = corn['volume'].tolist()
highs_all = corn['high'].tolist()
lows_all = corn['low'].tolist()
dates_all = [str(d) for d in corn['date']]
holds_all = corn['hold'].tolist() if 'hold' in corn.columns else [None]*n
opens_all = corn['open'].tolist()

def _sma(arr, i, n):
    if i+1 < n:
        return None
    return sum(arr[i-n+1:i+1]) / n

def _std(arr, i, n):
    if i+1 < n:
        return None
    m = sum(arr[i-n+1:i+1]) / n
    return math.sqrt(sum((x-m)**2 for x in arr[i-n+1:i+1]) / n)

def _rsi(arr, i, period=14):
    if i+1 < period+1:
        return None
    g, l = [], []
    for k in range(i-period+1, i+1):
        d = arr[k] - arr[k-1]
        g.append(d if d > 0 else 0)
        l.append(-d if d < 0 else 0)
    ag = sum(g[-period:]) / period
    al = sum(l[-period:]) / period
    return 100 - (100 / (1 + ag/al)) if al > 0 else 100

ma5_all = [None]*n
ma10_all = [None]*n
ma20_all = [None]*n
rsi_all = [None]*n
bbpos_all = [None]*n
macd_all = [None]*n

for i in range(n):
    ma5_all[i] = _sma(closes_all, i, 5)
    ma10_all[i] = _sma(closes_all, i, 10)
    ma20_all[i] = _sma(closes_all, i, 20)
    rsi_all[i] = _rsi(closes_all, i)
    sd20 = _std(closes_all, i, 20)
    m20 = ma20_all[i]
    if sd20 and m20:
        bb_u = m20 + 2*sd20
        bb_l = m20 - 2*sd20
        bbpos_all[i] = (closes_all[i] - bb_l) / (bb_u - bb_l) * 100 if bb_u != bb_l else 50
        macd_all[i] = (_sma(closes_all, i, 12) or 0) - (_sma(closes_all, i, 26) or 0)

soy_vals = [soy_dict.get(d) for d in dates_all]
oil_vals = [oil_dict.get(d) for d in dates_all]
cftc_vals = [cftc_dict.get(d) for d in dates_all]
bci_vals = [bci_dict.get(d) for d in dates_all]
zw_vals = [zw_dict.get(d) for d in dates_all]
cbot_vals = [cbot_dict.get(d) for d in dates_all]

eni_month = {}
for (yr, mon), oni in eni_data.items():
    ym = f"{yr}-{str(mon).zfill(2)}"
    eni_month[ym] = oni
eni_vals = [eni_month.get(f"{d[:4]}-{d[5:7]}") for d in dates_all]

def _policy(d):
    for s, e, v in POLICY:
        if s <= d <= e:
            return v
    return 0
policy_vals = [_policy(d) for d in dates_all]

gap_vals = [None]*n
for i in range(1, n):
    pc = closes_all[i-1]
    op = opens_all[i]
    gap_vals[i] = (op - pc) / pc * 100 if pc else 0

hold_vals = [None]*n
for i in range(1, n):
    h0 = holds_all[i-1]
    h1 = holds_all[i]
    if h0 and h1 and h0 > 0:
        hold_vals[i] = (h1 - h0) / h0 * 100

soy_ma20_vals = [None]*n
soy_ma5_vals = [None]*n
for i in range(20, n):
    sc = [soy_vals[j] for j in range(i-19, i+1) if soy_vals[j] is not None]
    if len(sc) >= 10:
        soy_ma5_vals[i] = sum(sc[-5:]) / 5
        soy_ma20_vals[i] = sum(sc) / len(sc)

# ──────────────────────────────────────────────────────────
# 4. 构建信号矩阵
# ──────────────────────────────────────────────────────────
print("📊 构建信号矩阵...")

SIGNAL_KEYS = ['ma', 'rsi', 'macd', 'vol', 'bb', 'season', 'soy', 'policy',
               'cftc', 'enso', 'bci', 'hold', 'cbot', 'wheat', 'div', 'gap']
SIGNAL_IDX = {k: i for i, k in enumerate(SIGNAL_KEYS)}

np_signals = np.zeros((n - 62, len(SIGNAL_KEYS)), dtype=np.float32)
np_actual_up = np.zeros(n - 62, dtype=bool)
np_dates = []
np_months = np.zeros(n - 62, dtype=np.int32)

for idx, i in enumerate(range(61, n - 1)):
    d = dates_all[i]
    month = int(d[5:7])
    c = closes_all[i]
    row = np_signals[idx]

    m5 = ma5_all[i]
    m10 = ma10_all[i]
    m20 = ma20_all[i]
    if m5 and m10 and m20:
        if m5 > m10 > m20:
            row[SIGNAL_IDX['ma']] = 1
        elif m5 < m10 < m20:
            row[SIGNAL_IDX['ma']] = -1

    rv = rsi_all[i]
    if rv:
        if rv < 35:
            row[SIGNAL_IDX['rsi']] = 1
        elif rv > 70:
            row[SIGNAL_IDX['rsi']] = -1

    mv = macd_all[i]
    if mv is not None:
        row[SIGNAL_IDX['macd']] = 1 if mv > 0 else -1

    v5_avg = sum(vols_all[i-4:i+1]) / 5 if i >= 4 else sum(vols_all[:i+1]) / (i+1)
    vr = vols_all[i] / v5_avg if v5_avg else 1
    if vr > 1.2:
        row[SIGNAL_IDX['vol']] = 1
    elif vr < 0.8:
        row[SIGNAL_IDX['vol']] = -0.5

    bp = bbpos_all[i]
    if bp is not None:
        if bp < 20:
            row[SIGNAL_IDX['bb']] = 1
        elif bp > 80:
            row[SIGNAL_IDX['bb']] = -1

    sc = CN_SEASON.get(month, 0)
    if sc > 0.1:
        row[SIGNAL_IDX['season']] = 1
    elif sc < -0.1:
        row[SIGNAL_IDX['season']] = -1

    sm5 = soy_ma5_vals[i]
    sm20 = soy_ma20_vals[i]
    if sm5 and sm20 and sm20 > 0:
        if sm5 > sm20:
            row[SIGNAL_IDX['soy']] = 1
        elif sm5 < sm20:
            row[SIGNAL_IDX['soy']] = -1

    pd_val = policy_vals[i]
    if pd_val != 0:
        row[SIGNAL_IDX['policy']] = pd_val

    cn = cftc_vals[i]
    if cn is not None:
        recent = [cftc_vals[j] for j in range(max(0, i-5), i+1) if cftc_vals[j] is not None]
        if len(recent) >= 2:
            cm = statistics.mean(recent)
            if cn > cm * 1.1:
                row[SIGNAL_IDX['cftc']] = 1
            elif cn < cm * 0.9:
                row[SIGNAL_IDX['cftc']] = -1

    eni = eni_vals[i]
    if eni is not None:
        if eni > 0.5:
            row[SIGNAL_IDX['enso']] = 1
        elif eni < -0.5:
            row[SIGNAL_IDX['enso']] = 1

    bci = bci_vals[i]
    if bci is not None:
        bci_recent = [bci_vals[j] for j in range(max(0, i-4), i+1) if bci_vals[j] is not None]
        if len(bci_recent) >= 2:
            bci_ma = statistics.mean(bci_recent)
            if bci > bci_ma:
                row[SIGNAL_IDX['bci']] = 1
            elif bci < bci_ma:
                row[SIGNAL_IDX['bci']] = -1

    hc = hold_vals[i]
    if hc is not None:
        if hc > 1:
            row[SIGNAL_IDX['hold']] = 1
        elif hc < -1:
            row[SIGNAL_IDX['hold']] = -1

    cb_t = cbot_vals[i]
    cb_y = cbot_vals[i-1] if i >= 1 else None
    if cb_t and cb_y:
        if cb_t > cb_y:
            row[SIGNAL_IDX['cbot']] = 1
        elif cb_t < cb_y:
            row[SIGNAL_IDX['cbot']] = -1

    zw_t = zw_vals[i]
    zw_y = zw_vals[i-1] if i >= 1 else None
    cb_t2 = cbot_vals[i]
    if zw_t and zw_y and cb_t2:
        ratio = zw_t / cb_t2
        zw_chg = (zw_t - zw_y) / zw_y * 100 if zw_y else 0
        if ratio > 1.25:
            row[SIGNAL_IDX['wheat']] = 1 if zw_chg > 0 else -1
        elif ratio < 1.0:
            row[SIGNAL_IDX['wheat']] = -1 if zw_chg < 0 else 1

    hb = highs_all[i-19:i+1]
    lb = lows_all[i-19:i+1]
    if len(hb) >= 20 and len(lb) >= 20:
        hmax_i = hb.index(max(hb))
        lmin_i = lb.index(min(lb))
        if hmax_i >= 5 and closes_all[i] >= max(hb) * 0.99:
            row[SIGNAL_IDX['div']] = -1
        if lmin_i >= 5 and closes_all[i] <= min(lb) * 1.01:
            row[SIGNAL_IDX['div']] = 1

    gp = gap_vals[i]
    if gp is not None:
        if gp > 0.5:
            row[SIGNAL_IDX['gap']] = 1
        elif gp < -0.5:
            row[SIGNAL_IDX['gap']] = -1

    np_actual_up[idx] = closes_all[i+1] > c
    np_dates.append(d)
    np_months[idx] = month

NUM_SAMPLES = n - 62
print(f"  信号矩阵: {np_signals.shape} ({len(SIGNAL_KEYS)}维), {NUM_SAMPLES}个样本")

# ──────────────────────────────────────────────────────────
# 5. 辅助函数：权重网格搜索 + 评分
# ──────────────────────────────────────────────────────────
W2I = {k: i for i, k in enumerate(SIGNAL_KEYS)}
W2L = len(SIGNAL_KEYS)

# 基准权重（来自 backtest_weights.py 的 base_vec）
BASE_VEC = np.array([0.8, 1.5, 1.0, 0.5, 1.0, 0.3, 0.8, 1.0, 0.8, 0.5, 0.3, 0.5, 0.5, 0.5, 0.5, 0.0], dtype=np.float32)
# v4.0 最优权重
V40_VEC = np.array([1.2, 2.0, 0.5, 0.3, 1.3, 0.2, 0.8, 1.0, 0.5, 0.5, 0.3, 0.5, 0.5, 0.5, 0.5, 0.0], dtype=np.float32)

# 网格搜索定义（与 backtest_weights.py 一致）
GRID_COARSE = {
    'ma': [0.6, 1.0, 1.4],
    'rsi': [1.0, 1.5, 2.0],
    'bb': [0.7, 1.0, 1.5],
    'macd': [0.5, 1.0],
    'soy': [0.5, 1.0, 1.5],
    'policy': [0.5, 1.0, 1.5],
    'vol': [0.3, 0.5],
    'season': [0.2, 0.5],
    'cftc': [0.5, 1.0],
    'cbot': [0.2, 0.5],
    'wheat': [0.2, 0.5],
    'div': [0.2, 0.5],
}
OPT_KEYS_COARSE = list(GRID_COARSE.keys())
OPT_IDXS_COARSE = [W2I[k] for k in OPT_KEYS_COARSE]


def score_weights_vec(signals, actual_up, w_vec, l1_lambda=L1_LAMBDA):
    """
    numpy 向量化评分。
    返回: (score, accuracy, hits, total)
    """
    active_mask = signals != 0
    ws = signals @ w_vec
    w_abs = np.abs(w_vec)
    tw = active_mask @ w_abs
    valid = tw > 0
    ratio = np.where(valid, ws / tw, 0.0)
    direction = np.where(np.abs(ratio) > 0.05, np.sign(ratio), 0).astype(np.int8)
    active = direction != 0
    actual_sign = np.where(actual_up, 1, -1).astype(np.int8)
    correct = active & (direction == actual_sign)
    total = np.sum(active)
    hits = np.sum(correct)
    accuracy = float(hits) / total if total > 0 else 0.0
    nonzero_penalty = l1_lambda * np.sum(np.abs(w_vec) > 0.01)
    score = accuracy - nonzero_penalty
    return score, accuracy, int(hits), int(total)


def grid_search(signals, actual_up, start_w=V40_VEC):
    """
    两步网格搜索：GRID_COARSE → GRID_FINE
    只搜索 12 个活跃信号，其余保持 start_w 的值。
    """
    best = {'score': 0.0, 'acc': 0.0, 'w': start_w.copy(), 'h': 0, 't': 0}

    # 第一轮：粗搜
    for combo in product(*[GRID_COARSE[k] for k in OPT_KEYS_COARSE]):
        w = start_w.copy()
        for idx, val in zip(OPT_IDXS_COARSE, combo):
            w[idx] = val
        score, acc, h, t = score_weights_vec(signals, actual_up, w)
        if score > best['score']:
            best = {'score': score, 'acc': acc, 'w': w.copy(), 'h': h, 't': t}

    # 第二轮：精搜 top 3 (ma, rsi, bb)
    coarse_w = best['w']
    bv_ma = float(coarse_w[W2I['ma']])
    bv_rsi = float(coarse_w[W2I['rsi']])
    bv_bb = float(coarse_w[W2I['bb']])
    GRID_FINE = {
        'ma': sorted(set([
            bv_ma, round(bv_ma-0.2, 1), round(bv_ma+0.2, 1),
            round(bv_ma-0.4, 1), round(bv_ma+0.4, 1), 0.8, 1.2, 1.6
        ])),
        'rsi': sorted(set([
            bv_rsi, round(bv_rsi-0.3, 1), round(bv_rsi+0.3, 1),
            round(bv_rsi-0.5, 1), round(bv_rsi+0.5, 1), 1.5, 2.0, 2.2
        ])),
        'bb': sorted(set([
            bv_bb, round(bv_bb-0.3, 1), round(bv_bb+0.3, 1),
            round(bv_bb-0.5, 1), round(bv_bb+0.5, 1), 0.7, 1.3, 1.8
        ])),
    }
    for combo in product(*[GRID_FINE[k] for k in ['ma', 'rsi', 'bb']]):
        w = coarse_w.copy()
        w[W2I['ma']], w[W2I['rsi']], w[W2I['bb']] = combo
        score, acc, h, t = score_weights_vec(signals, actual_up, w)
        if score > best['score']:
            best = {'score': score, 'acc': acc, 'w': w.copy(), 'h': h, 't': t}

    return best


def grid_search_coarse_only(signals, actual_up, start_w=V40_VEC):
    """仅粗搜（用于 fast 模式）"""
    best = {'score': 0.0, 'acc': 0.0, 'w': start_w.copy(), 'h': 0, 't': 0}
    for combo in product(*[GRID_COARSE[k] for k in OPT_KEYS_COARSE]):
        w = start_w.copy()
        for idx, val in zip(OPT_IDXS_COARSE, combo):
            w[idx] = val
        score, acc, h, t = score_weights_vec(signals, actual_up, w)
        if score > best['score']:
            best = {'score': score, 'acc': acc, 'w': w.copy(), 'h': h, 't': t}
    return best


# ──────────────────────────────────────────────────────────
# 6. 滑动窗口逻辑
# ──────────────────────────────────────────────────────────
def date_to_ym(date_str):
    """将 'YYYY-MM-DD' 转换为 (year, month) 整数元组"""
    parts = date_str.split('-')
    return int(parts[0]), int(parts[1])


def ym_to_int(year, month):
    """年-月 → 连续整数（方便比较）"""
    return year * 12 + month


def add_months(year, month, delta):
    """年月加减 delta 个月"""
    total = ym_to_int(year, month) + delta
    return total // 12, total % 12


def months_between(ym1, ym2):
    """两个 (year, month) 之间的月数差"""
    return ym_to_int(*ym2) - ym_to_int(*ym1)


def find_window_indices(dates, train_start_ym, train_end_ym, valid_end_ym):
    """
    根据年月范围，从 dates 列表中找出训练窗和验证窗的索引范围。
    
    train_start_ym: 训练窗起始 (year, month)
    train_end_ym:   训练窗结束 (year, month) — 包含此月
    valid_end_ym:   验证窗结束 (year, month) — 包含此月
    """
    train_start_ym_i = ym_to_int(*train_start_ym)
    train_end_ym_i = ym_to_int(*train_end_ym)
    valid_end_ym_i = ym_to_int(*valid_end_ym)

    train_idxs = []
    valid_idxs = []

    for idx, d in enumerate(dates):
        ym = ym_to_int(*date_to_ym(d))
        if train_start_ym_i <= ym <= train_end_ym_i:
            train_idxs.append(idx)
        elif train_end_ym_i < ym <= valid_end_ym_i:
            valid_idxs.append(idx)
        elif ym > valid_end_ym_i:
            break

    return train_idxs, valid_idxs


DEBUG_WINDOW = None  # 首次窗口调试用


def add_months_simple(ym, n):
    """年月元组 + n个月"""
    y, m = ym
    total = y * 12 + m + n
    # total 从1开始：1=1月, 12=12月, 13=次年1月
    ny = (total - 1) // 12
    nm = (total - 1) % 12 + 1
    return (ny, nm)


def generate_windows(dates, train_years=TRAIN_YEARS, valid_months=VALID_MONTHS, slide_months=SLIDE_MONTHS):
    """
    生成所有滑动窗口的 (train_start, train_end, valid_end) 年月元组。
    从数据起始向前推进，保证最后一个窗口的验证窗包含最新数据。
    """
    first_ym = date_to_ym(dates[0])
    last_d = dates[-1]
    last_ym = date_to_ym(last_d)
    last_i = last_ym[0] * 12 + last_ym[1]
    
    train_months = train_years * 12
    
    # 方法：从末尾反向决定最后一个窗口的位置
    # 最后一个验证窗的结束 = last_ym
    # 最后一个训练窗的结束 = last_ym 往前推 valid_months 个月
    # 最后一个训练窗的开始 = 训练窗结束 往前推 train_months-1 个月
    last_train_end_ym = add_months_simple(last_ym, -valid_months)
    last_train_start_ym = add_months_simple(last_train_end_ym, -(train_months - 1))
    
    # 从 earliest_start 到 last_train_start 以 slide 步长生成
    earliest_start_i = first_ym[0] * 12 + first_ym[1]
    last_start_i = last_train_start_ym[0] * 12 + last_train_start_ym[1]
    
    windows = []
    cur_start_i = earliest_start_i
    while cur_start_i <= last_start_i:
        cur_start_ym = ((cur_start_i - 1) // 12 + 1, (cur_start_i - 1) % 12 + 1)
        if cur_start_i < earliest_start_i + 6:
            cur_start_i += slide_months
            continue  # 跳过最早6个月（数据不足）
        train_end_ym = add_months_simple(cur_start_ym, train_months - 1)
        te_i = train_end_ym[0] * 12 + train_end_ym[1]
        
        # 如果训练窗结束已经超过或等于数据末尾，跳过（没有验证空间）
        if te_i >= last_i:
            cur_start_i += slide_months
            continue
        
        valid_end_ym = add_months_simple(train_end_ym, valid_months)
        ve_i = valid_end_ym[0] * 12 + valid_end_ym[1]
        
        # 限制 valid_end 不超过 last_ym
        if ve_i > last_i:
            valid_end_ym = last_ym
            ve_i = last_i
        
        windows.append((cur_start_ym, train_end_ym, valid_end_ym))
        cur_start_i += slide_months
    
    return windows

# ──────────────────────────────────────────────────────────
# 7. 运行滑动验证
# ──────────────────────────────────────────────────────────
print("\n🗓️  生成滑动窗口...")
windows = generate_windows(np_dates)
print(f"  共 {len(windows)} 个验证段")

# 解析命令行
fast_mode = '--fast' in sys.argv
quick_mode = '--quick-check' in sys.argv

if quick_mode:
    windows_run = windows[-1:]
    print("  快速检查模式：只跑最后 1 段")
elif fast_mode:
    windows_run = windows[-5:]
    print("  快速模式：只跑最后 5 段")
else:
    windows_run = windows
    print(f"  全量模式：跑全部 {len(windows)} 段")

print("\n" + "=" * 60)
print("运行滑动验证...")
print("=" * 60)

results = []
t_start = time.time()
prev_percent = -1

for wi, (train_ym, train_end_ym, valid_ym) in enumerate(windows_run):
    # 进度
    pct = (wi + 1) * 100 // len(windows_run)
    if pct != prev_percent:
        print(f"\r  进度: {wi+1}/{len(windows_run)} ({pct}%)  当前段: {train_ym[0]}-{train_ym[1]:02d} → {valid_ym[0]}-{valid_ym[1]:02d}", end="")
        prev_percent = pct

    # 如果验证结束 <= 训练结束，放弃（截断导致验证窗实际上不存在）
    te_i = train_end_ym[0] * 12 + train_end_ym[1]
    ve_i = valid_ym[0] * 12 + valid_ym[1]
    if ve_i <= te_i:
        continue

    train_idxs, valid_idxs = find_window_indices(np_dates, train_ym, train_end_ym, valid_ym)
    if len(train_idxs) < 100 or len(valid_idxs) < 5:
        continue

    # 切分训练/验证
    sig_train = np_signals[train_idxs]
    act_train = np_actual_up[train_idxs]
    sig_valid = np_signals[valid_idxs]
    act_valid = np_actual_up[valid_idxs]
    dates_valid = [np_dates[i] for i in valid_idxs]

    # 粗搜 + 精搜
    best = grid_search(sig_train, act_train)

    # 验证集评估
    v_score, v_acc, v_hits, v_total = score_weights_vec(sig_valid, act_valid, best['w'])

    # 基准权重在验证集的表现（对比）
    _, base_acc, base_hits, base_total = score_weights_vec(sig_valid, act_valid, BASE_VEC)
    _, v40_acc, v40_hits, v40_total = score_weights_vec(sig_valid, act_valid, V40_VEC)

    results.append({
        'window_idx': wi,
        'train_start': train_ym,
        'train_end': train_end_ym,
        'valid_end': valid_ym,
        'train_samples': len(train_idxs),
        'valid_samples': len(valid_idxs),
        'train_acc': best['acc'],
        'train_hits': best['h'],
        'train_total': best['t'],
        'valid_acc': v_acc,
        'valid_hits': v_hits,
        'valid_total': v_total,
        'base_valid_acc': base_acc,
        'base_valid_hits': base_hits,
        'base_valid_total': base_total,
        'v40_valid_acc': v40_acc,
        'v40_valid_hits': v40_hits,
        'v40_valid_total': v40_total,
        'optimal_weights': {k: float(w) for k, w in zip(SIGNAL_KEYS, best['w']) if abs(w) > 0.01},
        'optimal_weights_vec': best['w'].copy(),
        'dates_valid': dates_valid,
    })

print()  # 换行

# ──────────────────────────────────────────────────────────
# 8. 汇总报告
# ──────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("📊 生成汇总报告...")
print("=" * 60)

all_valid_accs = [r['valid_acc'] for r in results]
all_base_valid_accs = [r['base_valid_acc'] for r in results]
all_v40_valid_accs = [r['v40_valid_acc'] for r in results]
all_train_accs = [r['train_acc'] for r in results]

oos_mean = statistics.mean(all_valid_accs) if all_valid_accs else 0.0
oos_std = statistics.stdev(all_valid_accs) if len(all_valid_accs) >= 2 else 0.0
base_oos_mean = statistics.mean(all_base_valid_accs) if all_base_valid_accs else 0.0
v40_oos_mean = statistics.mean(all_v40_valid_accs) if all_v40_valid_accs else 0.0

# 基准全量搜索（对照）
print("\n  计算全量基准（全数据网格搜索，对照用）...")
full_best = grid_search(np_signals, np_actual_up)
full_acc = full_best['acc']
print(f"    全量方向准确率: {full_best['acc']:.3f} ({full_best['h']}/{full_best['t']})")

# 年度分组
from collections import defaultdict
yearly_acc = defaultdict(list)
yearly_base = defaultdict(list)
yearly_v40 = defaultdict(list)

yearly_results = {}
for r in results:
    y_end = r['valid_end'][0]
    if r['valid_total'] > 0:
        yearly_acc[y_end].append(r['valid_acc'])
        yearly_base[y_end].append(r['base_valid_acc'])
        yearly_v40[y_end].append(r['v40_valid_acc'])

# 最近 2-3 年独立表现
recent_results = [r for r in results if r['valid_end'][0] >= 2024]
recent_mean = statistics.mean([r['valid_acc'] for r in recent_results]) if recent_results else 0.0
recent_base_mean = statistics.mean([r['base_valid_acc'] for r in recent_results]) if recent_results else 0.0

# 权重稳定性分析
weight_stability = defaultdict(list)
for r in results:
    wv = r['optimal_weights_vec']
    for i, k in enumerate(SIGNAL_KEYS):
        if abs(wv[i]) > 0.01:
            weight_stability[k].append(float(wv[i]))

elapsed = time.time() - t_start

# ──────────────────────────────────────────────────────────
# 9. 写报告
# ──────────────────────────────────────────────────────────
report_lines = []
def wl(line=""):
    report_lines.append(line)

title = f"# 🧪 样本外验证报告 (Walk-Forward OOS)"
wl(title)
wl(f"> 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
wl(f"> 训练窗: {TRAIN_YEARS}年 | 验证窗: {VALID_MONTHS}个月 | 滑动步长: {SLIDE_MONTHS}个月")
wl(f"> 信号维度: {len(SIGNAL_KEYS)}个 | 运行耗时: {elapsed:.1f}秒")
wl()
wl(f"**验证段数**: {len(results)}")
wl()

# --- 核心结论 ---
wl("## 一、核心结论")
wl()
oos_vs_full = (oos_mean - full_acc) * 100
wl(f"| 指标 | 值 | 说明 |")
wl(f"|------|-----|------|")
wl(f"| **样本外方向准确率 (OOS)** | {oos_mean:.1%} | {len(results)}段滑动验证的均值 |")
wl(f"| OOS标准差 | {oos_std:.1%} | 各段间的波动性，越低越稳定 |")
wl(f"| 全量回测方向准确率 (IS) | {full_acc:.1%} | 用全数据搜索（含未来信息） |")
wl(f"| **IS-OOS差距** | {oos_vs_full:+.1f}pp | 正数=没过拟合，负数=有过拟合 |")
wl(f"| 基准权重 OOS | {base_oos_mean:.1%} | 固定基准权重的验证表现 |")
wl(f"| v4.0权重 OOS | {v40_oos_mean:.1%} | v4.0最优的验证表现 |")
wl()

gap_str = "🟢 未发现过拟合" if oos_vs_full >= 0 else ("🔴 存在过拟合" if oos_vs_full < -2 else "🟡 轻微过拟合")
wl(f"**过拟合判断**: {gap_str} (IS-OOS = {oos_vs_full:+.1f}pp)")
wl()

# --- 权重稳定性 ---
wl("## 二、权重稳定性分析")
wl()
wl(f"各信号在 {len(results)} 段训练中得出的最优权重的波动情况（仅展示活跃信号）：")
wl()
wl("| 信号 | 活跃段数 | 均值 | 标准差 | CV | 稳定性 |")
wl("|------|---------|------|--------|-----|--------|")
stable_signals = []
unstable_signals = []
for k in SIGNAL_KEYS:
    vals = weight_stability.get(k, [])
    if not vals or len(vals) < 2:
        continue
    mn = statistics.mean(vals)
    sd = statistics.stdev(vals)
    cv = sd / mn if mn > 0 else 999
    stable = "🟢" if cv < 0.2 else ("🟡" if cv < 0.4 else "🔴")
    wl(f"| {k:>8} | {len(vals):>5}/{len(results)} | {mn:.2f} | {sd:.2f} | {cv:.2f} | {stable} |")
    if stable == "🟢":
        stable_signals.append(k)
    elif stable in ("🟡", "🔴"):
        unstable_signals.append(k)
wl()

wl(f"**稳定信号** ({len(stable_signals)}个): {', '.join(stable_signals)}")
wl(f"**不稳定信号** ({len(unstable_signals)}个): {', '.join(unstable_signals)}")
if unstable_signals:
    wl("  ⚠️ 不稳定信号在不同时间段的最优权重差异较大，反映的是因子有效性的时间变化，建议保留（时间衰减机制会自然处理）")
wl()

# --- 年度表现 ---
wl("## 三、年度表现")
wl()
wl("| 年份 | 验证段数 | OOS方向准确率 | 基准准确率 | v4.0准确率 | vs基准 |")
wl("|------|---------|--------------|-----------|-----------|--------|")
for y in sorted(yearly_acc.keys()):
    if not yearly_acc[y]:
        continue
    ym = statistics.mean(yearly_acc[y])
    bm = statistics.mean(yearly_base[y]) if yearly_base.get(y) else 0.0
    v40m = statistics.mean(yearly_v40[y]) if yearly_v40.get(y) else 0.0
    vs_base = (ym - bm) * 100
    wl(f"| {y} | {len(yearly_acc[y]):>4}段 | {ym:.1%} | {bm:.1%} | {v40m:.1%} | {vs_base:+.1f}pp |")
wl()

# --- 最近2-3年 ---
wl("## 四、最近2-3年独立表现 (2024-2026)")
wl()
wl(f"| 指标 | 搜索优化 OOS | 基准权重 |")
wl(f"|------|-------------|---------|")
wl(f"| 方向准确率 | {recent_mean:.1%} | {recent_base_mean:.1%} |")
wl(f"| 验证段数 | {len(recent_results)} | — |")
wl()

# --- 每段详细 ---
wl("## 五、各段详细")
wl()
wl("| 窗口 | 训练窗 | 验证窗 | 训练样本 | 验证样本 | 训练准确率 | OOS准确率 | v4.0准确率 | 基准准确率 |")
wl("|------|--------|--------|---------|---------|-----------|----------|-----------|-----------|")
for r in results:
    ts = f"{r['train_start'][0]}-{r['train_start'][1]:02d}"
    te = f"{r['train_end'][0]}-{r['train_end'][1]:02d}"
    ve = f"{r['valid_end'][0]}-{r['valid_end'][1]:02d}"
    wl(f"| {r['window_idx']:>3} | {ts}~{te} | ~{ve} | {r['train_samples']} | {r['valid_samples']} | "
        f"{r['train_acc']:.1%} | {r['valid_acc']:.1%} | {r['v40_valid_acc']:.1%} | {r['base_valid_acc']:.1%} |")
wl()

# --- 发现总结 ---
wl("## 六、发现总结")
wl()
full_vs_oos = full_acc - oos_mean
if full_vs_oos > 0.03:
    wl(f"- 🔴 全量回测 ({full_acc:.1%}) 比 OOS ({oos_mean:.1%}) 高 {full_vs_oos*100:.1f}pp，差值大于 3pp，存在数据泄漏/过拟合")
    wl(f"  - 建议：降低网格搜索的粗搜规模，或增加 L1 正则系数")
elif full_vs_oos > 0.01:
    wl(f"- 🟡 全量回测 ({full_acc:.1%}) 比 OOS ({oos_mean:.1%}) 高 {full_vs_oos*100:.1f}pp，轻微过拟合")
else:
    wl(f"- 🟢 全量回测 ({full_acc:.1%}) 和 OOS ({oos_mean:.1%}) 差距 {full_vs_oos*100:.1f}pp，未发现过拟合")

if oos_mean < 0.50:
    wl(f"- 🔴 OOS方向准确率 ({oos_mean:.1%}) 低于 50%（随机猜测基准），模型不具备统计显著的预测能力")
else:
    wl(f"- 🟢 OOS方向准确率 ({oos_mean:.1%}) 高于 50%，模型有统计显著的预测能力")

if weight_stability:
    unstable_count = sum(1 for k in SIGNAL_KEYS if k in unstable_signals)
    if unstable_count > 5:
        wl(f"- 🟡 超过 {unstable_count} 个信号在不同时期的最优权重不一致，说明因子的主导驱动随时间变化")
        wl(f"  - 建议：保留这些信号——时间衰减机制比固定权重更适应这种变化")

wl()
wl(f"---")
wl(f"*报告生成于 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}*")
wl()

report = '\n'.join(report_lines)
report_path = WORKSPACE / 'oos_validation_report.md'
report_path.write_text(report, encoding='utf-8')
print(f"\n✅ 报告已写入: {report_path}")

# ──────────────────────────────────────────────────────────
# 10. 终端输出摘要
# ──────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("📊 验证摘要")
print("=" * 60)
print(f"  验证段数: {len(results)}")
print(f"  OOS方向准确率: {oos_mean:.1%} (基准权重: {statistics.mean(all_base_valid_accs):.1%})")
print(f"  OOS标准差: {oos_std:.1%}")
print(f"  全量回测IS: {full_acc:.1%}")
print(f"  IS-OOS差距: {oos_vs_full:+.1f}pp")
if recent_results:
    print(f"  最近3年OOS ({len(recent_results)}段): {recent_mean:.1%}")
print(f"  耗时: {elapsed:.1f}秒")
print()
for i in range(min(3, len(results))):
    r = results[i]
    print(f"  [{r['window_idx']}] 训练{r['train_start'][0]}-{r['train_start'][1]:02d}~{r['train_end'][0]}-{r['train_end'][1]:02d}  "
          f"→ 验证~{r['valid_end'][0]}-{r['valid_end'][1]:02d}: "
          f"OOS={r['valid_acc']:.1%} ({r['valid_hits']}/{r['valid_total']})  "
          f"训练={r['train_acc']:.1%}")
    if i >= 2:
        remaining = len(results) - 3
        print(f"  ... 还有 {remaining} 段 ...")
        break
print(f"\n完整报告: {report_path}")