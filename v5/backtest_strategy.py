#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易策略回测 v1.0

基于 DCE 玉米价格预测信号，回测多种交易策略的实盘表现。
用 backtest_weights.py 相同的信号矩阵 + 权重体系生成交易信号。

评估指标:
  年化收益率 / 夏普比率 / 最大回撤 / 盈亏比 / 胜率 / 总交易次数

用法:
  python3 backtest_strategy.py                              # 全段基准权重
  python3 backtest_strategy.py --weights v40                # v4.0 权重
  python3 backtest_strategy.py --half                       # 后半段 (2015-2026)
  python3 backtest_strategy.py --recent                     # 近3年 (2023-2026)
  python3 backtest_strategy.py --all-strategies             # 全部策略对比
"""

import sys, math, statistics, json, datetime, urllib.request, ssl
from pathlib import Path
from itertools import product

import akshare as ak
import numpy as np

WORKSPACE = Path(__file__).parent
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

# ── 配置 ──
TRANSACTION_COST = 0.0001
SLIPPAGE = 0.0005
CAPITAL = 100000
POSITION_SIZE = 0.95

# ── 数据加载 ──
print("📡 加载数据...")
corn = ak.futures_zh_daily_sina(symbol="C0").sort_values('date').reset_index(drop=True)
corn['date'] = corn['date'].astype(str)
soy_df = ak.futures_zh_daily_sina(symbol="M0")
soy_df['date'] = soy_df['date'].astype(str); soy_df = soy_df.sort_values('date').reset_index(drop=True)
soy_dict = {str(r['date']): float(r['close']) for _, r in soy_df.iterrows()}
oil_df = ak.futures_zh_daily_sina(symbol="SC0")
oil_df['date'] = oil_df['date'].astype(str); oil_df = oil_df.sort_values('date').reset_index(drop=True)
oil_dict = {str(r['date']): float(r['close']) for _, r in oil_df.iterrows()}

cftc_dict = {}
try:
    cftc_df = ak.macro_usa_cftc_c_holding()
    col = [c for c in cftc_df.columns if '玉米' in c and '净仓位' in c]
    if col:
        cftc_dict = {str(r['日期']): float(r[col[0]]) for _, r in cftc_df[['日期', col[0]]].dropna().iterrows()}
except: pass

bci_dict = {}
try:
    bci_df = ak.macro_china_freight_index().sort_values('截止日期').reset_index(drop=True)
    bci_df['date'] = bci_df['截止日期'].astype(str)
    bci_dict = {str(r['date']): float(r['波罗的海好望角型船运价指数BCI']) for _, r in bci_df[['date', '波罗的海好望角型船运价指数BCI']].dropna().iterrows()}
except: pass

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
except: pass

cbot_dict = {}
try:
    cbot_df = ak.futures_foreign_hist(symbol='C')
    cbot_df['date'] = cbot_df['date'].astype(str); cbot_df = cbot_df.sort_values('date').reset_index(drop=True)
    cbot_dict = {str(r['date']): float(r['close']) for _, r in cbot_df.iterrows()}
except: pass

eni_data = {}
try:
    url = 'https://www.cpc.ncep.noaa.gov/data/indices/ersst5.nino.mth.91-20.ascii'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=8, context=_ctx) as r:
        raw = r.read().decode('utf-8')
    for l in raw.split('\n'):
        parts = l.strip().split()
        if len(parts) >= 10:
            try:
                eni_data[(int(parts[0]), int(parts[1]))] = float(parts[9])
            except: continue
except: pass

n = len(corn)
closes_all = corn['close'].tolist()
vols_all = corn['volume'].tolist()
highs_all = corn['high'].tolist()
lows_all = corn['low'].tolist()
dates_all = [str(d) for d in corn['date']]
holds_all = corn['hold'].tolist() if 'hold' in corn.columns else [None]*n
opens_all = corn['open'].tolist()

print(f"  DCE玉米:{len(corn)} soy:{len(soy_dict)} oil:{len(oil_dict)} cftc:{len(cftc_dict)} "
      f"bci:{len(bci_dict)} zw:{len(zw_dict)} cbot:{len(cbot_dict)} enso:{len(eni_data)}")

# ── 信号预计算 ──
print("⚙️  预计算信号...")
def _sma(arr, i, p): return sum(arr[i-p+1:i+1])/p if i+1>=p else None
def _std(arr, i, p):
    if i+1<p: return None
    m=sum(arr[i-p+1:i+1])/p; return math.sqrt(sum((x-m)**2 for x in arr[i-p+1:i+1])/p)
def _rsi(arr,i,period=14):
    if i+1<period+1: return None
    g,l=[],[]
    for k in range(i-period+1,i+1):
        d=arr[k]-arr[k-1]; g.append(d if d>0 else 0); l.append(-d if d<0 else 0)
    ag=sum(g[-period:])/period; al=sum(l[-period:])/period
    return 100-(100/(1+ag/al)) if al>0 else 100

CN_SEASON={1:0,2:-0.2,3:0,4:0.25,5:0.1,6:0.35,7:0,8:0.3,9:0.25,10:0,11:-0.1,12:0.2}
POLICY=[("2018-07-06","2018-09-30",-1),("2019-05-10","2019-12-31",-1),("2020-01-15","2020-02-14",1),("2020-01-23","2020-04-08",-1),("2020-04-08","2020-12-31",1),("2016-04-30","2016-10-31",-1),("2017-05-01","2017-10-31",-1),("2021-05-06","2021-10-31",1),("2019-04-01","2019-09-30",1),("2020-03-01","2020-09-30",1),("2020-07-01","2020-09-30",1),("2022-06-01","2022-09-30",1),("2023-07-01","2023-09-30",1),("2022-02-24","2022-07-31",1),("2008-11-01","2009-06-30",1),("2015-11-01","2016-06-30",1)]

ma5=[None]*n;ma10=[None]*n;ma20=[None]*n;rsi=[None]*n;bbpos=[None]*n;macd=[None]*n
for i in range(n):
    ma5[i]=_sma(closes_all,i,5); ma10[i]=_sma(closes_all,i,10); ma20[i]=_sma(closes_all,i,20)
    rsi[i]=_rsi(closes_all,i); sd20=_std(closes_all,i,20); m20=ma20[i]
    if sd20 and m20:
        bb_u=m20+2*sd20; bb_l=m20-2*sd20
        bbpos[i]=(closes_all[i]-bb_l)/(bb_u-bb_l)*100 if bb_u!=bb_l else 50
        macd[i]=(_sma(closes_all,i,12) or 0)-(_sma(closes_all,i,26) or 0)

soy_v=[soy_dict.get(d) for d in dates_all]; oil_v=[oil_dict.get(d) for d in dates_all]
cftc_v=[cftc_dict.get(d) for d in dates_all]; bci_v=[bci_dict.get(d) for d in dates_all]
zw_v=[zw_dict.get(d) for d in dates_all]; cbot_v=[cbot_dict.get(d) for d in dates_all]
eni_m={f"{y}-{str(m).zfill(2)}":v for (y,m),v in eni_data.items()}
eni_v=[eni_m.get(f"{d[:4]}-{d[5:7]}") for d in dates_all]
policy_v=[0]*n
for s,e,v in POLICY:
    fm=[i for i,d in enumerate(dates_all) if s<=d<=e]
    for i in fm: policy_v[i]=v
gap_v=[None]*n
for i in range(1,n):
    pc=closes_all[i-1]; gap_v[i]=(opens_all[i]-pc)/pc*100 if pc else 0
hold_v=[None]*n
for i in range(1,n):
    h0,h1=holds_all[i-1],holds_all[i]
    if h0 and h1 and h0>0: hold_v[i]=(h1-h0)/h0*100
soy_ma5=[None]*n;soy_ma20=[None]*n
for i in range(20,n):
    sc=[soy_v[j] for j in range(i-19,i+1) if soy_v[j] is not None]
    if len(sc)>=10: soy_ma5[i]=sum(sc[-5:])/5; soy_ma20[i]=sum(sc)/len(sc)

# ── 信号矩阵 ──
KEYS=['ma','rsi','macd','vol','bb','season','soy','policy','cftc','enso','bci','hold','cbot','wheat','div','gap']
KIDX={k:i for i,k in enumerate(KEYS)}
ns=np.zeros((n-62,len(KEYS)),dtype=np.float32)
nup=np.zeros(n-62,dtype=bool)
ndates=[]; ncloses=[]; nopens=[]; nchgs=[]

for idx,i in enumerate(range(61,n-1)):
    d=dates_all[i]; mon=int(d[5:7]); c=closes_all[i]; row=ns[idx]
    m5,m10,m20=ma5[i],ma10[i],ma20[i]
    if m5 and m10 and m20:
        if m5>m10>m20: row[KIDX['ma']]=1
        elif m5<m10<m20: row[KIDX['ma']]=-1
    rv=rsi[i]
    if rv:
        if rv<35: row[KIDX['rsi']]=1
        elif rv>70: row[KIDX['rsi']]=-1
    mv=macd[i]
    if mv is not None: row[KIDX['macd']]=1 if mv>0 else -1
    v5a=sum(vols_all[i-4:i+1])/5 if i>=4 else sum(vols_all[:i+1])/(i+1)
    vr=vols_all[i]/v5a if v5a else 1
    if vr>1.2: row[KIDX['vol']]=1
    elif vr<0.8: row[KIDX['vol']]=-0.5
    bp=bbpos[i]
    if bp is not None:
        if bp<20: row[KIDX['bb']]=1
        elif bp>80: row[KIDX['bb']]=-1
    sc=CN_SEASON.get(mon,0)
    if sc>0.1: row[KIDX['season']]=1
    elif sc<-0.1: row[KIDX['season']]=-1
    sm5,sm20=soy_ma5[i],soy_ma20[i]
    if sm5 and sm20 and sm20>0:
        if sm5>sm20: row[KIDX['soy']]=1
        elif sm5<sm20: row[KIDX['soy']]=-1
    pv=policy_v[i]
    if pv!=0: row[KIDX['policy']]=pv
    cnv=cftc_v[i]
    if cnv is not None:
        rv2=[cftc_v[j] for j in range(max(0,i-5),i+1) if cftc_v[j] is not None]
        if len(rv2)>=2:
            cm=statistics.mean(rv2)
            if cnv>cm*1.1: row[KIDX['cftc']]=1
            elif cnv<cm*0.9: row[KIDX['cftc']]=-1
    ev=eni_v[i]
    if ev is not None:
        if abs(ev)>0.5: row[KIDX['enso']]=1
    bcv=bci_v[i]
    if bcv is not None:
        bcr=[bci_v[j] for j in range(max(0,i-4),i+1) if bci_v[j] is not None]
        if len(bcr)>=2:
            bcm=statistics.mean(bcr)
            if bcv>bcm: row[KIDX['bci']]=1
            elif bcv<bcm: row[KIDX['bci']]=-1
    hcv=hold_v[i]
    if hcv is not None:
        if hcv>1: row[KIDX['hold']]=1
        elif hcv<-1: row[KIDX['hold']]=-1
    cbt=cbot_v[i]; cby=cbot_v[i-1] if i>=1 else None
    if cbt and cby:
        if cbt>cby: row[KIDX['cbot']]=1
        elif cbt<cby: row[KIDX['cbot']]=-1
    zt,zy=zw_v[i],zw_v[i-1] if i>=1 else None; ct2=cbot_v[i]
    if zt and zy and ct2:
        r=zt/ct2; zc=(zt-zy)/zy*100 if zy else 0
        if r>1.25: row[KIDX['wheat']]=1 if zc>0 else -1
        elif r<1.0: row[KIDX['wheat']]=-1 if zc<0 else 1
    hb=highs_all[i-19:i+1]; lb=lows_all[i-19:i+1]
    if len(hb)>=20 and len(lb)>=20:
        if hb.index(max(hb))>=5 and closes_all[i]>=max(hb)*0.99: row[KIDX['div']]=-1
        if lb.index(min(lb))>=5 and closes_all[i]<=min(lb)*1.01: row[KIDX['div']]=1
    gp=gap_v[i]
    if gp is not None:
        if gp>0.5: row[KIDX['gap']]=1
        elif gp<-0.5: row[KIDX['gap']]=-1
    nup[idx]=closes_all[i+1]>c
    ndates.append(d); ncloses.append(c); nopens.append(opens_all[i])
    nchgs.append((c-closes_all[i-1])/closes_all[i-1]*100 if closes_all[i-1] else 0)

amask=ns!=0; N=n-62
print(f"  信号矩阵: {ns.shape}, {N}个样本")

# ── 权重 ──
BASE=np.array([0.8,1.5,1.0,0.5,1.0,0.3,0.8,1.0,0.8,0.5,0.3,0.5,0.5,0.5,0.5,0.0],dtype=np.float32)
V40=np.array([1.2,2.0,0.5,0.3,1.3,0.2,0.8,1.0,0.5,0.5,0.3,0.5,0.5,0.5,0.5,0.0],dtype=np.float32)
OOS_AVG=np.array([0.93,1.73,0.75,0.38,1.31,0.24,0.63,0.78,0.83,0.50,0.30,0.50,0.34,0.28,0.32,0.00],dtype=np.float32)

def gen_signals(wv,th=0.05):
    ws=ns@wv; tw=amask@np.abs(wv);v=tw>0;r=np.where(v,ws/tw,0.0)
    return r,np.where(np.abs(r)>th,np.sign(r),0).astype(np.int8)

def run_bt(dates,opens,closes,signals,strength=None,si=0,ei=None,ct=0.0,
           trade_type='directional'):
    if ei is None: ei=len(dates)
    dates=dates[si:ei]; opens=opens[si:ei]; closes=closes[si:ei]; signals=signals[si:ei]
    trades=[]; ipos=0; ep=0; ed=None; eq=CAPITAL; peak=CAPITAL; dret=[]

    for i in range(len(dates)-1):
        s=signals[i]
        if trade_type=='long_only' and s==-1: s=0
        elif trade_type=='short_only' and s==1: s=0
        if strength is not None and ct>0 and abs(strength[i])<ct: s=0

        if ipos!=0 and s!=ipos:
            xp=opens[i+1]
            pnl=(xp-ep)/ep if ipos==1 else (ep-xp)/ep
            pnl_net=pnl-(TRANSACTION_COST+SLIPPAGE)*2
            trades.append({'entry_date':ed,'exit_date':dates[i+1],'direction':ipos,
                          'entry_price':ep,'exit_price':xp,'pnl_pct':pnl_net*100,'pnl':pnl_net})
            eq*=(1+pnl_net*POSITION_SIZE); ipos=0; ep=0; ed=None
        if ipos==0 and s!=0:
            ipos=s; ep=opens[i+1]; ed=dates[i+1]
        tc=opens[i+1]; nc=closes[i+1] if i+1<len(closes) else closes[i]
        if ipos!=0:
            dr=(nc-tc)/tc if ipos==1 else (tc-nc)/tc
            eq*=(1+dr*POSITION_SIZE); dret.append(dr)
        else: dret.append(0.0)
        peak=max(peak,eq)

    if ipos!=0:
        xp=closes[-1]
        pnl=(xp-ep)/ep if ipos==1 else (ep-xp)/ep
        pnl_net=pnl-(TRANSACTION_COST+SLIPPAGE)*2
        trades.append({'entry_date':ed,'exit_date':dates[-1],'direction':ipos,
                      'entry_price':ep,'exit_price':xp,'pnl_pct':pnl_net*100,'pnl':pnl_net})
        eq*=(1+pnl_net*POSITION_SIZE)

    tr=(eq/CAPITAL-1)*100; yrs=len(dates)/250
    ar=((eq/CAPITAL)**(1/yrs)-1)*100 if yrs>0 else 0
    mdd=max(0,(peak-eq)/peak*100) if peak>0 else 0
    if len(dret)>1:
        avg_r=statistics.mean(dret); std_r=statistics.stdev(dret)
        sharpe=(avg_r*250)/(std_r*math.sqrt(250)) if std_r>0 else 0
    else: sharpe=0
    wins=[t for t in trades if t['pnl']>0]; losses=[t for t in trades if t['pnl']<=0]
    wr=len(wins)/len(trades)*100 if trades else 0
    aw=statistics.mean([t['pnl'] for t in wins]) if wins else 0
    al=abs(statistics.mean([t['pnl'] for t in losses])) if losses else 0
    pf=(sum(t['pnl'] for t in wins)/abs(sum(t['pnl'] for t in losses))) if losses and sum(t['pnl'] for t in losses)<0 else float('inf')

    return {'trades':trades,'metrics':{
        'total_trades':len(trades),'total_return_pct':round(tr,2),
        'annual_return_pct':round(ar,2),'sharpe_ratio':round(sharpe,3),
        'max_drawdown_pct':round(mdd,2),'win_rate_pct':round(wr,2),
        'avg_win_pct':round(aw*100,2),'avg_loss_pct':round(al*100,2),
        'profit_factor':round(pf,2),'final_equity':round(eq,2)}}

def analyze_trades(trades, label=""):
    """分析交易记录，按方向/时间段分组"""
    if not trades:
        return
    print(f"\n  ── 交易分析 {label} ──")
    groups = {}
    for t in trades:
        d = t['entry_date'][:7] if 'entry_date' in t and t['entry_date'] else 'unknown'
        if d not in groups: groups[d] = []
        groups[d].append(t)
    
    # 按月
    months = sorted(groups.keys())
    print(f"  时间跨度: {months[0]} ~ {months[-1]} ({len(months)}个月)")
    
    # 按方向分组
    longs = [t for t in trades if t['direction']==1]
    shorts = [t for t in trades if t['direction']==-1]
    if longs:
        lw=[t for t in longs if t['pnl']>0]; ll=[t for t in longs if t['pnl']<=0]
        print(f"  做多: {len(longs)}笔 胜率{len(lw)/len(longs)*100:.1f}% "
              f"平均盈亏{statistics.mean([t['pnl'] for t in longs])*100:.2f}%")
    if shorts:
        sw=[t for t in shorts if t['pnl']>0]; sl=[t for t in shorts if t['pnl']<=0]
        print(f"  做空: {len(shorts)}笔 胜率{len(sw)/len(shorts)*100:.1f}% "
              f"平均盈亏{statistics.mean([t['pnl'] for t in shorts])*100:.2f}%")
    
    # 按幅度分组
    small=[t for t in trades if abs(t['pnl'])<0.005]
    med=[t for t in trades if 0.005<=abs(t['pnl'])<0.015]
    large=[t for t in trades if abs(t['pnl'])>=0.015]
    print(f"  小波动(<0.5%): {len(small)}笔 盈亏{sum(t['pnl'] for t in small)*100:.2f}%")
    print(f"  中波动(0.5-1.5%): {len(med)}笔 盈亏{sum(t['pnl'] for t in med)*100:.2f}%")
    print(f"  大波动(>=1.5%): {len(large)}笔 盈亏{sum(t['pnl'] for t in large)*100:.2f}%")
    
    # 年度分析
    yearly={}
    for t in trades:
        y=t['entry_date'][:4]
        if y not in yearly: yearly[y]={'trades':[],'pnl':0}
        yearly[y]['trades'].append(t)
        yearly[y]['pnl']+=t['pnl']
    print(f"  年度盈亏:")
    for y in sorted(yearly.keys()):
        d=yearly[y]; wins=[t for t in d['trades'] if t['pnl']>0]
        wr=len(wins)/len(d['trades'])*100
        print(f"    {y}: {d['pnl']*100:+.2f}% ({len(d['trades'])}笔, 胜率{wr:.1f}%)")


def run_strategy(name,wv,dates,opens,closes,si=0,ei=None,print_detail=True,**kw):
    r,sigs=gen_signals(wv,kw.get('threshold',0.05))
    res=run_bt(dates,opens,closes,sigs,r,si=si,ei=ei,ct=kw.get('confidence_threshold',0.0),trade_type=kw.get('trade_type','directional'))
    m=res['metrics']
    if print_detail:
        seg=f" ({dates[si]}~{dates[-1]})" if si>0 or ei else " (全段)"
        print(f"\n{'='*60}\n📊 {name}{seg}\n{'='*60}")
        for k,v in [('总交易次数',m['total_trades']),('总收益率',f"{m['total_return_pct']:+.2f}%"),
                    ('年化收益率',f"{m['annual_return_pct']:+.2f}%"),('夏普比率',m['sharpe_ratio']),
                    ('最大回撤',f"{m['max_drawdown_pct']:.2f}%"),('胜率',f"{m['win_rate_pct']:.1f}%"),
                    ('平均盈利',f"{m['avg_win_pct']:+.2f}%"),('平均亏损',f"{m['avg_loss_pct']:.2f}%"),
                    ('盈亏比',m['profit_factor']),('最终权益',f"¥{m['final_equity']:,.2f}")]:
            print(f"  {k:>12}: {v}")
    return res

# ── 主流程 ──
all_strats='--all-strategies' in sys.argv
use_half='--half' in sys.argv; use_recent='--recent' in sys.argv
analyze='--analyze-trades' in sys.argv
wc='base'
for i,a in enumerate(sys.argv):
    if a.startswith('--weights='): wc=a.split('=')[1]
    elif a=='--weights' and i+1<len(sys.argv): wc=sys.argv[i+1]
wv={'base':BASE,'v40':V40,'oos_avg':OOS_AVG}.get(wc,BASE)
si=0; ei=None; sl="全段 (2005-2026)"
if use_half: si=N//2; sl="后半段 (≈2015-2026)"
elif use_recent: si=max(0,N-750); sl="近3年 (≈2023-2026)"

if all_strats:
    print(f"\n{'#'*70}")
    print(f"# 全部策略对比 — {wc}权重 — {sl}")
    print(f"{'#'*70}")
    strats=[('方向信号(0.05阈值)','directional',{}),('方向信号(无阈值)','directional',{'threshold':0.0}),
            ('只做多','long_only',{}),('只做空','short_only',{})]
    for th in [0.1,0.15,0.2]: strats.append((f'置信度过滤({th:.0%})','directional',{'confidence_threshold':th}))
    for name,stype,extra in strats:
        kw={'threshold':0.05,'trade_type':stype,**extra}
        run_strategy(name,wv,ndates,nopens,ncloses,si=si,ei=ei,**kw)
else:
    run_strategy("方向信号(0.05阈值)",wv,ndates,nopens,ncloses,si=si,ei=ei)

# 交易分析
if analyze:
    print(f"\n{'#'*70}")
    print(f"# 交易分析模式 — {wc}权重 — {sl}")
    print(f"{'#'*70}")
    # 全段分析
    r,sigs=gen_signals(wv,0.05)
    res=run_bt(ndates,nopens,ncloses,np.where(np.abs(r)>0.05,np.sign(r),0).astype(np.int8),r,si=0,ei=None)
    analyze_trades(res['trades'],"全段")
    # 后半段
    r2,sigs2=gen_signals(wv,0.05)
    hsi=N//2
    resh=run_bt(ndates,nopens,ncloses,np.where(np.abs(r2)>0.05,np.sign(r2),0).astype(np.int8),r2,si=hsi,ei=None)
    analyze_trades(resh['trades'],"后半段")
    # 近3年
    r3i=max(0,N-750)
    resr=run_bt(ndates,nopens,ncloses,np.where(np.abs(r2)>0.05,np.sign(r2),0).astype(np.int8),r2,si=r3i,ei=None)
    analyze_trades(resr['trades'],"近3年")
    sys.exit(0)

# 写入报告
report_path = WORKSPACE / 'strategy_backtest_report.md'
r, _ = gen_signals(wv, 0.05)
res = run_bt(ndates, nopens, ncloses, np.where(np.abs(r)>0.05, np.sign(r), 0).astype(np.int8), r,
             si=si, ei=ei, ct=0.0)
m = res['metrics']
lines = [
    f"# 📈 交易策略回测报告\n",
    f"> 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}  |  权重: {wc}  |  {sl}\n",
    f"**初始资金**: ¥{CAPITAL:,}  |  **手续费/滑点**: {(TRANSACTION_COST+SLIPPAGE)*100:.2f}% (开平各一次)\n",
    "## 核心指标\n",
    "| 指标 | 值 | 说明 |",
    "|------|-----|------|",
    f"| **总收益率** | {m['total_return_pct']:+.2f}% | 全程复合收益 |",
    f"| **年化收益率** | {m['annual_return_pct']:+.2f}% | 年化复合增长率 |",
    f"| **夏普比率** | {m['sharpe_ratio']} | >1良好, >2优秀 |",
    f"| **最大回撤** | {m['max_drawdown_pct']:.2f}% | 全程最大亏损幅度 |",
    f"| **胜率** | {m['win_rate_pct']:.1f}% | 方向正确占比 |",
    f"| **盈亏比** | {m['profit_factor']} | >1.5合格, >2良好 |",
    f"| **总交易次数** | {m['total_trades']} | 全程交易次数 |",
    f"| **平均盈利** | {m['avg_win_pct']:+.2f}% | 盈利交易均值 |",
    f"| **平均亏损** | {m['avg_loss_pct']:.2f}% | 亏损交易均值 |",
    f"| **最终权益** | ¥{m['final_equity']:,.2f} | 10万初始→ |",
    "",
    "## 评价摘要\n",
]
tr=m['total_return_pct']; ar=m['annual_return_pct']; sr=m['sharpe_ratio']
mdd=m['max_drawdown_pct']; wr=m['win_rate_pct']; pf=m['profit_factor']
if ar>15: lines.append(f"- ✅ **年化收益率{ar:+.1f}%**，超过通胀+无风险利率，具备实盘价值")
elif ar>5: lines.append(f"- 🟡 **年化收益率{ar:+.1f}%**，略高于无风险利率，实盘价值有限")
else: lines.append(f"- 🔴 **年化收益率{ar:+.1f}%**，低于无风险利率，实盘不具备吸引力")
if sr>1.5: lines.append(f"- ✅ **夏普比率{sr}**，风险调整后收益优秀")
elif sr>0.5: lines.append(f"- 🟡 **夏普比率{sr}**，风险调整后收益一般")
else: lines.append(f"- 🔴 **夏普比率{sr}**，风险调整后收益不理想")
if mdd>30: lines.append(f"- 🔴 **最大回撤{mdd:.1f}%>30%**，回撤过大，实盘风险较高")
elif mdd>15: lines.append(f"- 🟡 **最大回撤{mdd:.1f}%**，回撤可接受")
else: lines.append(f"- ✅ **最大回撤{mdd:.1f}%**，回撤控制良好")
if pf>2: lines.append(f"- ✅ **盈亏比{pf}**，盈利能力强")
elif pf>1.2: lines.append(f"- 🟡 **盈亏比{pf}**，盈利能力一般")
else: lines.append(f"- 🔴 **盈亏比{pf}**，盈利能力不足")

lines.extend(["", "---", f"*报告生成于 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}*", ""])
report_path.write_text('\n'.join(lines), encoding='utf-8')
print(f"\n✅ 报告已写入: {report_path}")
