#!/usr/bin/env python3
"""
中国玉米期货综合回测 + 权重优化 v1.1
性能优化: 一次性预计算所有信号,仅跑权重网格
"""
import akshare as ak
import statistics
import math
import urllib.request
import json
import datetime
import ssl
import socket
from itertools import product
import numpy as np

socket.setdefaulttimeout(8)
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

# ============================================================
# 1. 加载所有数据
# ============================================================
print("📡 加载数据...")
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
except:
    pass

bci_dict = {}
try:
    bci_df = ak.macro_china_freight_index()
    bci_df = bci_df.sort_values('截止日期').reset_index(drop=True)
    bci_df['date'] = bci_df['截止日期'].astype(str)
    bci_df = bci_df[['date', '波罗的海好望角型船运价指数BCI']].dropna()
    bci_dict = {str(r['date']): float(r['波罗的海好望角型船运价指数BCI']) for _, r in bci_df.iterrows()}
except:
    pass

zw_dict = {}
try:
    url = 'https://query2.finance.yahoo.com/v8/finance/chart/ZW=F?interval=1d&range=10y'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=8, context=_ctx) as r:
        d = json.loads(r.read())
    closes = d['chart']['result'][0]['indicators']['quote'][0]['close']
    ts = d['chart']['result'][0]['timestamp']
    dates = [datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d') for t in ts]
    zw_dict = {d: c for d, c in zip(dates, closes) if c is not None}
except:
    pass

cbot_dict = {}
try:
    cbot_df = ak.futures_foreign_hist(symbol='C')
    cbot_df['date'] = cbot_df['date'].astype(str)
    cbot_df = cbot_df.sort_values('date').reset_index(drop=True)
    cbot_dict = {str(r['date']): float(r['close']) for _, r in cbot_df.iterrows()}
except:
    pass

eni_data = {}
try:
    url = 'https://www.cpc.ncep.noaa.gov/data/indices/ersst5.nino.mth.91-20.ascii'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=8, context=_ctx) as r:
        raw = r.read().decode('utf-8')
    for l in raw.split('\n'):
        parts = l.strip().split()
        if len(parts) >= 10:
            yr, mon, oni = int(parts[0]), int(parts[1]), float(parts[9])
            eni_data[(yr, mon)] = oni
except:
    pass

print(f"  DCE玉米:{len(corn)}条 soy:{len(soy_dict)}条 oil:{len(oil_dict)}条 "
      f"cftc:{len(cftc_dict)}条 bci:{len(bci_dict)}条 "
      f"zw:{len(zw_dict)}条 cbot:{len(cbot_dict)}条 enso:{len(eni_data)}条")

# ============================================================
# 2. 常量
# ============================================================
CN_SEASON = {1:0,2:-0.2,3:0,4:0.25,5:0.1,6:0.35,7:0,8:0.3,9:0.25,10:0,11:-0.1,12:0.2}
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

# ============================================================
# 3. 一次性预计算所有信号
# ============================================================
print("⚙️  预计算所有信号...")

n = len(corn)
closes_all = corn['close'].tolist()
vols_all  = corn['volume'].tolist()
highs_all = corn['high'].tolist()
lows_all  = corn['low'].tolist()
dates_all = [str(d) for d in corn['date']]
holds_all = corn['hold'].tolist() if 'hold' in corn.columns else [None]*n
opens_all = corn['open'].tolist()

# 预计算均线/RSI/布林带
ma5_all  = [None]*n
ma10_all = [None]*n
ma20_all = [None]*n
rsi_all  = [None]*n
bbpos_all = [None]*n
macd_all  = [None]*n

def _sma(arr, i, n):
    if i+1 < n: return None
    return sum(arr[i-n+1:i+1])/n

def _std(arr, i, n):
    if i+1 < n: return None
    m = sum(arr[i-n+1:i+1])/n
    return math.sqrt(sum((x-m)**2 for x in arr[i-n+1:i+1])/n)

def _rsi(arr, i, period=14):
    if i+1 < period+1: return None
    g,l = [],[]
    for k in range(i-period+1, i+1):
        d = arr[k]-arr[k-1]
        g.append(d if d>0 else 0)
        l.append(-d if d<0 else 0)
    ag=sum(g[-period:])/period; al=sum(l[-period:])/period
    return 100-(100/(1+ag/al)) if al>0 else 100

for i in range(n):
    c5 = closes_all[:i+1]
    ma5_all[i]  = _sma(closes_all, i, 5)
    ma10_all[i] = _sma(closes_all, i, 10)
    ma20_all[i] = _sma(closes_all, i, 20)
    rsi_all[i]  = _rsi(closes_all, i)
    sd20 = _std(closes_all, i, 20)
    m20 = ma20_all[i]
    if sd20 and m20:
        bb_u = m20+2*sd20; bb_l = m20-2*sd20
        bbpos_all[i] = (closes_all[i]-bb_l)/(bb_u-bb_l)*100 if bb_u!=bb_l else 50
        macd_all[i] = (_sma(closes_all,i,12) or 0) - (_sma(closes_all,i,26) or 0)

# 预计算soy_dict和oil_dict对应值
soy_vals = [soy_dict.get(d) for d in dates_all]
oil_vals = [oil_dict.get(d) for d in dates_all]
cftc_vals = [cftc_dict.get(d) for d in dates_all]
bci_vals  = [bci_dict.get(d) for d in dates_all]
zw_vals   = [zw_dict.get(d) for d in dates_all]
cbot_vals = [cbot_dict.get(d) for d in dates_all]

# 预计算ENSO
eni_month = {}
for (yr, mon), oni in eni_data.items():
    ym = f"{yr}-{str(mon).zfill(2)}"
    eni_month[ym] = oni
eni_vals = [eni_month.get(f"{d[:4]}-{d[5:7]}") for d in dates_all]

# 预计算政策事件
def _policy(d):
    for s,e,v in POLICY:
        if s<=d<=e: return v
    return 0
policy_vals = [_policy(d) for d in dates_all]

# 预计算隔夜缺口
gap_vals = [None]*n
for i in range(1, n):
    pc = closes_all[i-1]
    op = opens_all[i]
    gap_vals[i] = (op-pc)/pc*100 if pc else 0

# 预计算持仓量变化
hold_vals = [None]*n
for i in range(1, n):
    h0 = holds_all[i-1]; h1 = holds_all[i]
    if h0 and h1 and h0>0:
        hold_vals[i] = (h1-h0)/h0*100

# 预计算soy MA相关性
soy_ma20_vals = [None]*n
soy_ma5_vals  = [None]*n
for i in range(20, n):
    sc = [soy_vals[j] for j in range(i-19,i+1) if soy_vals[j] is not None]
    if len(sc) >= 10:
        soy_ma5_vals[i]  = sum(sc[-5:])/5
        soy_ma20_vals[i] = sum(sc)/len(sc)

print(f"  预计算完成，共{n}个交易日")

# ============================================================
# 4. 构建每日信号向量（numpy ndarray，支持向量化 score_weights）
# ============================================================
print("📊 构建信号向量...")

SIGNAL_KEYS = ['ma','rsi','macd','vol','bb','season','soy','policy','cftc','enso','bci','hold','cbot','wheat','div','gap']
SIGNAL_IDX = {k: i for i, k in enumerate(SIGNAL_KEYS)}
np_signals = np.zeros((n-62, len(SIGNAL_KEYS)), dtype=np.float32)
np_actual_up = np.zeros(n-62, dtype=bool)
np_dates = []
np_months = np.zeros(n-62, dtype=np.int32)

for idx, i in enumerate(range(61, n-1)):
    d = dates_all[i]
    month = int(d[5:7])
    c = closes_all[i]

    row = np_signals[idx]

    # MA
    m5=ma5_all[i]; m10=ma10_all[i]; m20=ma20_all[i]
    if m5 and m10 and m20:
        if m5>m10>m20: row[SIGNAL_IDX['ma']]=1
        elif m5<m10<m20: row[SIGNAL_IDX['ma']]=-1

    # RSI
    rv=rsi_all[i]
    if rv:
        if rv<35: row[SIGNAL_IDX['rsi']]=1
        elif rv>70: row[SIGNAL_IDX['rsi']]=-1

    # MACD
    mv=macd_all[i]
    if mv is not None: row[SIGNAL_IDX['macd']]=1 if mv>0 else -1

    # 成交量
    v5_avg = sum(vols_all[i-4:i+1])/5 if i>=4 else sum(vols_all[:i+1])/(i+1)
    vr = vols_all[i]/v5_avg if v5_avg else 1
    if vr>1.2: row[SIGNAL_IDX['vol']]=1
    elif vr<0.8: row[SIGNAL_IDX['vol']]=-0.5

    # 布林带
    bp=bbpos_all[i]
    if bp is not None:
        if bp<20: row[SIGNAL_IDX['bb']]=1
        elif bp>80: row[SIGNAL_IDX['bb']]=-1

    # 季节性
    sc=CN_SEASON.get(month,0)
    if sc>0.1: row[SIGNAL_IDX['season']]=1
    elif sc<-0.1: row[SIGNAL_IDX['season']]=-1

    # 大豆
    sm5=soy_ma5_vals[i]; sm20=soy_ma20_vals[i]
    if sm5 and sm20 and sm20>0:
        if sm5>sm20: row[SIGNAL_IDX['soy']]=1
        elif sm5<sm20: row[SIGNAL_IDX['soy']]=-1

    # 政策
    pd_val=policy_vals[i]
    if pd_val!=0: row[SIGNAL_IDX['policy']]=pd_val

    # CFTC
    cn=cftc_vals[i]
    if cn is not None:
        recent=[cftc_vals[j] for j in range(max(0,i-5),i+1) if cftc_vals[j] is not None]
        if len(recent)>=2:
            cm=statistics.mean(recent)
            if cn>cm*1.1: row[SIGNAL_IDX['cftc']]=1
            elif cn<cm*0.9: row[SIGNAL_IDX['cftc']]=-1

    # ENSO
    eni=eni_vals[i]
    if eni is not None:
        if eni>0.5: row[SIGNAL_IDX['enso']]=1
        elif eni<-0.5: row[SIGNAL_IDX['enso']]=1

    # BCI
    bci=bci_vals[i]
    if bci is not None:
        bci_recent=[bci_vals[j] for j in range(max(0,i-4),i+1) if bci_vals[j] is not None]
        if len(bci_recent)>=2:
            bci_ma=statistics.mean(bci_recent)
            if bci>bci_ma: row[SIGNAL_IDX['bci']]=1
            elif bci<bci_ma: row[SIGNAL_IDX['bci']]=-1

    # 持仓变化
    hc=hold_vals[i]
    if hc is not None:
        if hc>1: row[SIGNAL_IDX['hold']]=1
        elif hc<-1: row[SIGNAL_IDX['hold']]=-1

    # CBOT传导
    cb_t=cbot_vals[i]
    cb_y=cbot_vals[i-1] if i>=1 else None
    if cb_t and cb_y:
        if cb_t>cb_y: row[SIGNAL_IDX['cbot']]=1
        elif cb_t<cb_y: row[SIGNAL_IDX['cbot']]=-1

    # 小麦替代
    zw_t=zw_vals[i]; zw_y=zw_vals[i-1] if i>=1 else None
    cb_t2=cbot_vals[i]
    if zw_t and zw_y and cb_t2:
        ratio=zw_t/cb_t2
        zw_chg=(zw_t-zw_y)/zw_y*100 if zw_y else 0
        if ratio>1.25: row[SIGNAL_IDX['wheat']]=1 if zw_chg>0 else -1
        elif ratio<1.0: row[SIGNAL_IDX['wheat']]=-1 if zw_chg<0 else 1

    # 背离
    hb=highs_all[i-19:i+1]; lb=lows_all[i-19:i+1]
    if len(hb)>=20 and len(lb)>=20:
        hmax_i=hb.index(max(hb)); lmin_i=lb.index(min(lb))
        if hmax_i>=5 and closes_all[i]>=max(hb)*0.99: row[SIGNAL_IDX['div']]=-1
        if lmin_i>=5 and closes_all[i]<=min(lb)*1.01: row[SIGNAL_IDX['div']]=1

    # 隔夜缺口
    gp=gap_vals[i]
    if gp is not None:
        if gp>0.5: row[SIGNAL_IDX['gap']]=1
        elif gp<-0.5: row[SIGNAL_IDX['gap']]=-1

    np_actual_up[idx] = closes_all[i+1] > c
    np_dates.append(d)
    np_months[idx] = month

NUM_SAMPLES = n-62
print(f"  信号矩阵: {np_signals.shape} (16维), {NUM_SAMPLES}个样本")

# ============================================================
# 5. 权重网格搜索
# ============================================================
# 5. 权重网格搜索（numpy 向量化，~1000x 加速）
# ============================================================
SIGNAL_KEYS_W = ['ma','rsi','macd','vol','bb','season','soy','policy','cftc','enso','bci','hold','cbot','wheat','div','gap']
W2I = {k:i for i,k in enumerate(SIGNAL_KEYS_W)}
W2L = len(SIGNAL_KEYS_W)

# 基准权重向量
base_vec = np.array([0.8, 1.5, 1.0, 0.5, 1.0, 0.3, 0.8, 1.0, 0.8, 0.5, 0.3, 0.5, 0.5, 0.5, 0.5, 0.0], dtype=np.float32)
# v4.0 最优权重
v40_vec = np.array([1.2, 2.0, 0.5, 0.3, 1.3, 0.2, 0.8, 1.0, 0.5, 0.5, 0.3, 0.5, 0.5, 0.5, 0.5, 0.0], dtype=np.float32)

# 预计算活跃 mask
_active_mask = np_signals != 0

def score_weights_vec(w_vec, l1_lambda=0.001):
    """
    numpy 向量化：一次矩阵乘法完成所有 5000+ 样本的加权求和。
    方向判断和命中统计全部用 numpy 操作，无 Python 循环。
    
    l1_lambda: L1正则化系数。对非零权重数量做惩罚，
    促使冗余信号权重归零。默认值 0.001。
    
    评分 = 方向准确率 - l1_lambda × Σ(w_i ≠ 0 的数量)
    """
    ws = np_signals @ w_vec
    w_abs = np.abs(w_vec)
    tw = _active_mask @ w_abs
    valid = tw > 0
    ratio = np.where(valid, ws / tw, 0.0)
    direction = np.where(np.abs(ratio) > 0.05, np.sign(ratio), 0).astype(np.int8)
    active = direction != 0
    actual_sign = np.where(np_actual_up, 1, -1).astype(np.int8)
    correct = active & (direction == actual_sign)
    total = np.sum(active)
    hits = np.sum(correct)
    accuracy = float(hits)/total if total > 0 else 0.0
    
    # L1正则化：非零权重的信号数量惩罚
    nonzero_penalty = l1_lambda * np.sum(np.abs(w_vec) > 0.01)
    score = accuracy - nonzero_penalty
    
    return (score, accuracy, int(hits), int(total)) if total > 0 else (0.0, 0.0, 0, 0)

# 基准
_, base_hit, base_h, base_t = score_weights_vec(base_vec)
print(f"\n基准方向准确率: {base_hit:.3f} ({base_h}/{base_t})")

# v4.0
_, v40_hit, v40_h, v40_t = score_weights_vec(v40_vec)
print(f"v4.0: {v40_hit:.3f} ({v40_h}/{v40_t}) (+{(v40_hit-base_hit)*100:+.2f}pp)")

# 全信号网格搜索 (12 信号 × 5-7 档 = 2,160,000 组合)
# 粗精两步搜索：先粗搜 12 信号找到区域，再对 top 3 信号精搜
GRID_COARSE = {
    'ma':     [0.6, 1.0, 1.4],
    'rsi':    [1.0, 1.5, 2.0],
    'bb':     [0.7, 1.0, 1.5],
    'macd':   [0.5, 1.0],
    'soy':    [0.5, 1.0, 1.5],
    'policy': [0.5, 1.0, 1.5],
    'vol':    [0.3, 0.5],
    'season': [0.2, 0.5],
    'cftc':   [0.5, 1.0],
    'cbot':   [0.2, 0.5],
    'wheat':  [0.2, 0.5],
    'div':    [0.2, 0.5],
}

OPT_KEYS_COARSE = list(GRID_COARSE.keys())
opt_indices_coarse = [W2I[k] for k in OPT_KEYS_COARSE]

# 第一轮：粗搜
best = {'hit': v40_hit, 'w': v40_vec.copy(), 'h': v40_h, 't': v40_t}
for combo in product(*[GRID_COARSE[k] for k in OPT_KEYS_COARSE]):
    w = v40_vec.copy()
    for idx, val in zip(opt_indices_coarse, combo):
        w[idx] = val
    score, hit, h, t = score_weights_vec(w)
    if score > best['hit']:
        best = {'hit': hit, 'w': w.copy(), 'h': h, 't': t}

print(f"\n粗搜完成(12信号): best={best['hit']:.4f} (vs v4.0: {(best['hit']-v40_hit)*100:+.2f}pp)")

# 第二轮：只对 top 3 信号在粗搜最优附近细搜
coarse_w = best['w']
bv_ma = float(coarse_w[W2I['ma']])
bv_rsi = float(coarse_w[W2I['rsi']])
bv_bb = float(coarse_w[W2I['bb']])
GRID_FINE = {
    'ma':  sorted(set([bv_ma, round(bv_ma-0.2,1), round(bv_ma+0.2,1), round(bv_ma-0.4,1), round(bv_ma+0.4,1), 0.8, 1.2, 1.6])),
    'rsi': sorted(set([bv_rsi, round(bv_rsi-0.3,1), round(bv_rsi+0.3,1), round(bv_rsi-0.5,1), round(bv_rsi+0.5,1), 1.5, 2.0, 2.2])),
    'bb':  sorted(set([bv_bb, round(bv_bb-0.3,1), round(bv_bb+0.3,1), round(bv_bb-0.5,1), round(bv_bb+0.5,1), 0.7, 1.3, 1.8])),
}

for combo in product(*[GRID_FINE[k] for k in ['ma','rsi','bb']]):
    w = coarse_w.copy()
    w[W2I['ma']], w[W2I['rsi']], w[W2I['bb']] = combo
    score, hit, h, t = score_weights_vec(w)
    if score > best['hit']:
        best = {'hit': hit, 'w': w.copy(), 'h': h, 't': t}

print(f"精搜完成: best={best['hit']:.4f}")
print(f"\n{'='*60}")
print(f"基准: {base_hit:.3f} ({base_h}/{base_t})")
print(f"最优: {best['hit']:.3f} ({best['h']}/{best['t']})")
print(f"提升: {(best['hit']-base_hit)*100:+.2f}个百分点")
print(f"\n最优权重 (16维):")
for i, k in enumerate(SIGNAL_KEYS_W):
    b = base_vec[i]; o = best['w'][i]
    a = '=' if abs(o-b)<1e-4 else ('↑' if o>b else '↓')
    if b!=0 or o!=0:
        print(f"  {k:>8}: {b:.2f} {a} {o:.2f}")

# 月度分析（numpy 向量化版本）
print(f"\n{'='*60}")
print("月度分析:")
best_w = best['w']
ws_all = np_signals @ best_w
tw_all = _active_mask @ np.abs(best_w)
ratio_all = np.where(tw_all > 0, ws_all / tw_all, 0.0)
dir_all = np.where(np.abs(ratio_all) > 0.05, np.sign(ratio_all), 0).astype(np.int8)
act_all = dir_all != 0
actual_sign_all = np.where(np_actual_up, 1, -1).astype(np.int8)
cor_all = act_all & (dir_all == actual_sign_all)

for m in range(1, 13):
    mask = (np_months == m) & act_all
    t_cnt = np.sum(mask)
    h_cnt = np.sum(cor_all & mask)
    if t_cnt > 0:
        print(f"{m:>4}月 | {h_cnt/t_cnt:>7.1%} | {t_cnt:>6}")
    else:
        print(f"{m:>4}月 | {'无数据':>8} | {'0':>6}")

print(f"\n推荐权重配置:")
for i, k in enumerate(SIGNAL_KEYS_W):
    v = float(best['w'][i])
    if v != 0.0:
        print(f"    '{k}': {v},")
