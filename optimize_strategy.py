#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略优化回测 v1.0

基于 backtest_strategy.py 的分析结论，逐一测试优化方案：
  1. 趋势过滤器：MA60方向禁止逆势做多/做空
  2. 做多/做空分离权重：每个信号的多/空权重可独立调节
  3. 信号去芜存菁：弱相关信号降权/归档

对比基准: backtest_strategy.py --weights v40 --half 的结果
"""

import sys, math, statistics, json, datetime, urllib.request, ssl
from pathlib import Path
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
soy_df = ak.futures_zh_daily_sina(symbol="M0").sort_values('date').reset_index(drop=True)
soy_df['date'] = soy_df['date'].astype(str)
soy_dict = {str(r['date']): float(r['close']) for _, r in soy_df.iterrows()}
oil_df = ak.futures_zh_daily_sina(symbol="SC0").sort_values('date').reset_index(drop=True)
oil_df['date'] = oil_df['date'].astype(str)
oil_dict = {str(r['date']): float(r['close']) for _, r in oil_df.iterrows()}

cftc_dict={}
try:
    cftc_df = ak.macro_usa_cftc_c_holding()
    col=[c for c in cftc_df.columns if '玉米' in c and '净仓位' in c]
    if col: cftc_dict={str(r['日期']):float(r[col[0]]) for _,r in cftc_df[['日期',col[0]]].dropna().iterrows()}
except: pass

bci_dict={}
try:
    bci_df=ak.macro_china_freight_index().sort_values('截止日期').reset_index(drop=True)
    bci_df['date']=bci_df['截止日期'].astype(str)
    bci_dict={str(r['date']):float(r['波罗的海好望角型船运价指数BCI']) for _,r in bci_df[['date','波罗的海好望角型船运价指数BCI']].dropna().iterrows()}
except: pass

zw_dict={}
try:
    url='https://query2.finance.yahoo.com/v8/finance/chart/ZW=F?interval=1d&range=10y'
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req,timeout=8,context=_ctx) as r:
        d=json.loads(r.read())
    c=d['chart']['result'][0]['indicators']['quote'][0]['close']
    ts=d['chart']['result'][0]['timestamp']
    zw_dict={datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d'):c for t,c in zip(ts,c) if c is not None}
except: pass

cbot_dict={}
try:
    cbot_df=ak.futures_foreign_hist(symbol='C')
    cbot_df['date']=cbot_df['date'].astype(str)
    cbot_df=cbot_df.sort_values('date').reset_index(drop=True)
    cbot_dict={str(r['date']):float(r['close']) for _,r in cbot_df.iterrows()}
except: pass

eni_data={}
try:
    url='https://www.cpc.ncep.noaa.gov/data/indices/ersst5.nino.mth.91-20.ascii'
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req,timeout=8,context=_ctx) as r:
        raw=r.read().decode('utf-8')
    for l in raw.split('\n'):
        parts=l.strip().split()
        if len(parts)>=10:
            try: eni_data[(int(parts[0]),int(parts[1]))]=float(parts[9])
            except: continue
except: pass

n=len(corn)
closes_all=corn['close'].tolist()
vols_all=corn['volume'].tolist()
highs_all=corn['high'].tolist()
lows_all=corn['low'].tolist()
dates_all=[str(d) for d in corn['date']]
holds_all=corn['hold'].tolist() if 'hold' in corn.columns else [None]*n
opens_all=corn['open'].tolist()

print(f"  DCE玉米:{n} soy:{len(soy_dict)} oil:{len(oil_dict)} cftc:{len(cftc_dict)} "
      f"bci:{len(bci_dict)} zw:{len(zw_dict)} cbot:{len(cbot_dict)} enso:{len(eni_data)}")

# ── 信号预计算 ──
print("⚙️  预计算信号...")
def _sma(arr,i,p): return sum(arr[i-p+1:i+1])/p if i+1>=p else None
def _std(arr,i,p):
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

ma5=[None]*n;ma10=[None]*n;ma20=[None]*n;ma60=[None]*n;rsi=[None]*n;bbpos=[None]*n;macd=[None]*n
for i in range(n):
    ma5[i]=_sma(closes_all,i,5); ma10[i]=_sma(closes_all,i,10); ma20[i]=_sma(closes_all,i,20)
    ma60[i]=_sma(closes_all,i,60)
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

# 预计算 MA60 趋势方向
ma60_dir=[None]*n
for i in range(60,n):
    if ma60[i] and ma60[i-1]:
        ma60_dir[i]=1 if ma60[i]>ma60[i-1] else (-1 if ma60[i]<ma60[i-1] else 0)

# ── 信号矩阵 ──
KEYS=['ma','rsi','macd','vol','bb','season','soy','policy','cftc','enso','bci','hold','cbot','wheat','div','gap']
KIDX={k:i for i,k in enumerate(KEYS)}
ns=np.zeros((n-62,len(KEYS)),dtype=np.float32)
nup=np.zeros(n-62,dtype=bool)
ndates=[]; ncloses=[]; nopens=[]; nchgs=[]; nma60_dir=[]

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
    nma60_dir.append(ma60_dir[i] if ma60_dir[i] else 0)

amask=ns!=0; N=n-62
print(f"  信号矩阵: {ns.shape}")
print("  MA60趋势方向: ", f"看多{sum(1 for x in nma60_dir if x>0)} 看空{sum(1 for x in nma60_dir if x<0)} 中性{sum(1 for x in nma60_dir if x==0)}")

# ── 权重 ──
V40=np.array([1.2,2.0,0.5,0.3,1.3,0.2,0.8,1.0,0.5,0.5,0.3,0.5,0.5,0.5,0.5,0.0],dtype=np.float32)

def gen_signals(wv):
    ws=ns@wv; tw=amask@np.abs(wv);v=tw>0;r=np.where(v,ws/tw,0.0)
    return r,np.where(np.abs(r)>0.05,np.sign(r),0).astype(np.int8)

def run_bt(dates,opens,closes,signals,sig_str=None,si=0,ei=None,ct=0.0,
           trade_type='directional',trend_filter=False,ma60_dir_arr=None):
    """趋势过滤版本：ma60_dir 为做多时禁止做空，为做空时禁止做多"""
    if ei is None: ei=len(dates)
    dates=dates[si:ei]; opens=opens[si:ei]; closes=closes[si:ei]; signals=signals[si:ei]
    m60=ma60_dir_arr[si:ei] if trend_filter and ma60_dir_arr is not None else [0]*len(signals)
    trades=[]; ipos=0; ep=0; ed=None; eq=CAPITAL; peak=CAPITAL; dret=[]

    for i in range(len(dates)-1):
        s=signals[i]
        if trade_type=='long_only' and s==-1: s=0
        elif trade_type=='short_only' and s==1: s=0
        if sig_str is not None and ct>0 and abs(sig_str[i])<ct: s=0
        # 趋势过滤：逆势时强制平仓或禁止开仓
        if trend_filter and m60[i]!=0:
            if s*m60[i] < 0: s=0  # 逆势信号直接归零
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

    # 按幅度分组分析
    large=[t for t in trades if abs(t['pnl'])>=0.015]
    large_sum=sum(t['pnl'] for t in large)

    return {'trades':trades,'metrics':{
        'total_trades':len(trades),'total_return_pct':round(tr,2),
        'annual_return_pct':round(ar,2),'sharpe_ratio':round(sharpe,3),
        'max_drawdown_pct':round(mdd,2),'win_rate_pct':round(wr,2),
        'avg_win_pct':round(aw*100,2),'avg_loss_pct':round(al*100,2),
        'profit_factor':round(pf,2),'final_equity':round(eq,2),
        'large_trades':len(large),'large_pnl_pct':round(large_sum*100,2)}}

def print_result(name,res):
    m=res['metrics']
    print(f"\n{'='*55}")
    print(f"📊 {name}")
    print(f"{'='*55}")
    vs=res.get('baseline',{})
    items=[('总交易次数',m['total_trades']),('总收益率',f"{m['total_return_pct']:+.2f}%"),
           ('年化收益率',f"{m['annual_return_pct']:+.2f}%"),('夏普比率',m['sharpe_ratio']),
           ('最大回撤',f"{m['max_drawdown_pct']:.2f}%"),('胜率',f"{m['win_rate_pct']:.1f}%"),
           ('平均盈利',f"{m['avg_win_pct']:+.2f}%"),('平均亏损',f"{m['avg_loss_pct']:.2f}%"),
           ('盈亏比',m['profit_factor']),('最终权益',f"¥{m['final_equity']:,.2f}")]
    if 'large_trades' in m:
        items.append(('大波动(>=1.5%)',f"{m['large_trades']}笔 {m['large_pnl_pct']:+.2f}%"))
    for k,v in items:
        if k in vs:
            diff=m.get(k,0)-vs.get(k,0) if isinstance(vs.get(k),(int,float)) and isinstance(m.get(k),(int,float)) else ''
            extra=f" ({diff:+.2f})" if diff!='' else ''
            print(f"  {k:>14}: {v}{extra}")
        else:
            print(f"  {k:>14}: {v}")

# ── 主流程 ──
print(f"\n{'#'*70}")
print(f"# 策略优化回测 — v4.0权重")
print(f"{'#'*70}")

r,sigs=gen_signals(V40)
half_idx=N//2

# ── 基准：无趋势过滤（后半段） ──
base=run_bt(ndates,nopens,ncloses,sigs,r,si=half_idx,ei=None)
print_result("🔵 基准: 无趋势过滤 (后半段)",base)

# ── 方案1: MA60趋势过滤 ──
print(f"\n{'─'*55}")
print(f"🟢 P1: MA60趋势过滤器")
print(f"{'─'*55}")

tf=run_bt(ndates,nopens,ncloses,sigs,r,si=half_idx,ei=None,trend_filter=True,ma60_dir_arr=nma60_dir)
tf['baseline']=base['metrics']
print_result("有趋势过滤 (后半段)",tf)

# 近3年对比
r3i=max(0,N-750)
base3=run_bt(ndates,nopens,ncloses,sigs,r,si=r3i,ei=None)
tf3=run_bt(ndates,nopens,ncloses,sigs,r,si=r3i,ei=None,trend_filter=True,ma60_dir_arr=nma60_dir)
tf3['baseline']=base3['metrics']
print(f"\n  ── 近3年对比 ──")
print_result("基准 无趋势过滤",base3)
print_result("优化 有趋势过滤",tf3)

# ── 方案2: 做多/做空分离 ──
print(f"\n{'─'*55}")
print(f"🟢 P2: 做多/做空分离权重")
print(f"{'─'*55}")
# 做空时,各信号权重减半的版本
v40_short_half=V40.copy()
for k in ['ma','rsi','bb','season']:
    if k in KEYS:
        v40_short_half[KIDX[k]]=V40[KIDX[k]]*0.5

rs2,sigs2=gen_signals(v40_short_half)
sep=run_bt(ndates,nopens,ncloses,sigs2,rs2,si=half_idx,ei=None)
sep['baseline']=base['metrics']
print_result("做空减半+趋势过滤",sep)

# ── 方案3: 精简信号 ──
print(f"\n{'─'*55}")
print(f"🟢 P3: 精简信号体系")
print(f"{'─'*55}")
# 只保留核心信号(ma,rsi,bb,macd,vol,cbot,policy,soy)，其他归零
v40_lean=V40.copy()
for k in ['season','cftc','enso','bci','hold','wheat','div','gap']:
    v40_lean[KIDX[k]]=0.0
rs3,sigs3=gen_signals(v40_lean)
lean=run_bt(ndates,nopens,ncloses,sigs3,rs3,si=half_idx,ei=None,trend_filter=True,ma60_dir_arr=nma60_dir)
lean['baseline']=base['metrics']
print_result("精简8信号+趋势过滤",lean)

# ── 方案4: 最优组合 ──
print(f"\n{'─'*55}")
print(f"🟢 P4: 最优组合 (精简+趋势过滤+做空分离)")
print(f"{'─'*55}")
v40_best=V40.copy()
for k in ['season','cftc','enso','bci','hold','wheat','div','gap']:
    v40_best[KIDX[k]]=0.0
for k in ['ma','rsi','bb','season']:
    if k in KEYS:
        v40_best[KIDX[k]]=V40[KIDX[k]]*0.5
rs4,sigs4=gen_signals(v40_best)
best=run_bt(ndates,nopens,ncloses,sigs4,rs4,si=half_idx,ei=None,trend_filter=True,ma60_dir_arr=nma60_dir)
best['baseline']=base['metrics']
print_result("最优组合 (后半段)",best)

# 近3年最优
r3i=max(0,N-750)
best3=run_bt(ndates,nopens,ncloses,sigs4,rs4,si=r3i,ei=None,trend_filter=True,ma60_dir_arr=nma60_dir)
best3['baseline']=tf3['metrics']
print(f"\n  ── 近3年最优组合 ──")
print_result("基准 无优化",base3)
print_result("最优组合",best3)

# ── 全段对比表 ──
print(f"\n{'#'*70}")
print(f"# 对比汇总")
print(f"{'#'*70}")
print(f"{'策略':>25} {'年化':>8} {'夏普':>7} {'回撤':>7} {'胜率':>6} {'盈亏比':>7} {'大波幅':>10}")
print(f"{'─'*70}")
for name,res in [("基准",base),("趋势过滤",tf),("精简8信号",lean),("最优组合",best)]:
    m=res['metrics']
    print(f"{name:>25} {m['annual_return_pct']:>+7.2f}% {m['sharpe_ratio']:>7.3f} "
          f"{m['max_drawdown_pct']:>6.2f}% {m['win_rate_pct']:>5.1f}% "
          f"{m['profit_factor']:>7.2f} {m['large_pnl_pct']:>+9.2f}%")

# 写入报告
report_path=WORKSPACE / 'optimization_report.md'
lines=[
    "# 🛠️ 策略优化对比报告\n",
    f"> 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
    "## 优化方案对比（后半段 2015-2026）\n",
    "| 策略 | 年化 | 夏普 | 最大回撤 | 胜率 | 盈亏比 | 大波幅盈亏 |",
    "|------|------|------|---------|------|--------|----------|",
]
for name,res in [("🔵 基准(无优化)",base),("🟢 趋势过滤",tf),("🟢 精简8信号+趋势",lean),("🟢 最优组合",best)]:
    m=res['metrics']
    lines.append(f"| {name} | {m['annual_return_pct']:+.2f}% | {m['sharpe_ratio']} | {m['max_drawdown_pct']:.1f}% | "
                 f"{m['win_rate_pct']:.1f}% | {m['profit_factor']} | {m['large_pnl_pct']:+.2f}% |")
lines.extend([
    "",
    "## 近3年对比（2023-2026）\n",
    "| 策略 | 年化 | 夏普 | 最大回撤 | 胜率 | 盈亏比 |",
    "|------|------|------|---------|------|--------|",
    f"| 🔵 基准 | {base3['metrics']['annual_return_pct']:+.2f}% | {base3['metrics']['sharpe_ratio']} | {base3['metrics']['max_drawdown_pct']:.1f}% | {base3['metrics']['win_rate_pct']:.1f}% | {base3['metrics']['profit_factor']} |",
    f"| 🟢 最优组合 | {best3['metrics']['annual_return_pct']:+.2f}% | {best3['metrics']['sharpe_ratio']} | {best3['metrics']['max_drawdown_pct']:.1f}% | {best3['metrics']['win_rate_pct']:.1f}% | {best3['metrics']['profit_factor']} |",
    "",
    "## 结论\n",
])
m_base=base['metrics']; m_best=best['metrics']
if m_best['sharpe_ratio']>m_base['sharpe_ratio']:
    lines.append(f"- ✅ 趋势过滤 + 精简8信号 + 做空权重分离：夏普 {m_base['sharpe_ratio']} → {m_best['sharpe_ratio']}")
if m_best['large_pnl_pct']>m_base['large_pnl_pct']:
    lines.append(f"- ✅ 大波幅亏损从 {m_base['large_pnl_pct']:.1f}% 收窄到 {m_best['large_pnl_pct']:.1f}%")
if m_best['max_drawdown_pct']<m_base['max_drawdown_pct']:
    lines.append(f"- ✅ 最大回撤从 {m_base['max_drawdown_pct']:.1f}% 降低到 {m_best['max_drawdown_pct']:.1f}%")
lines.append(f"\n{'─'*50}\n")
lines.append("*报告由 optimize_strategy.py 自动生成*\n")
report_path.write_text('\n'.join(lines),encoding='utf-8')
print(f"\n✅ 报告已写入: {report_path}")
