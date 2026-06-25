#!/usr/bin/env python3
"""
DCE玉米期权Put/Call Ratio信号 (P2-5)

数据来源：大连商品交易所(DCE) 期权日频行情
  akshare: option_hist_dce(symbol='玉米期权', trade_date='YYYYMMDD')
  但由于DCE网站有反爬虫机制(WAF)，数据获取不稳定

备用方案：
  1. 从DCE官网手动下载CSV（http://www.dce.com.cn/）
  2. ameba/公开市场数据聚合

信号逻辑：
  Put/Call Ratio（持仓量）:
    > 1.2 → 看跌情绪占优 → 利空
    < 0.8 → 看涨情绪占优 → 利多
    中间 → 中性
  Put/Call Ratio（成交量）:
    > 1.5 → 恐慌情绪 → 短期利空
    < 0.6 → 乐观情绪 → 短期利多

用法：
  python3 option_pcr.py               # 尝试获取数据并输出
  python3 option_pcr.py --manual      # 手动输入PCR值
"""

import json
from pathlib import Path
from datetime import datetime, timedelta

WORKSPACE = Path(__file__).parent
CACHE = WORKSPACE / "option_pcr_cache.json"


def fetch_dce_option():
    """
    从DCE获取玉米期权数据
    尝试akshare，失败返回None
    """
    import akshare as ak
    
    # 尝试最近5个交易日
    today = datetime.now()
    for i in range(10):
        d = (today - timedelta(days=i)).strftime('%Y%m%d')
        try:
            df = ak.option_hist_dce(symbol='玉米期权', trade_date=d)
            if len(df) > 0 and '合约编码' in df.columns:
                return df, d
        except:
            continue
    return None, None


def compute_pcr(df):
    """
    从DCE期权DataFrame计算Put/Call Ratio
    """
    if df is None or len(df) == 0:
        return None
    
    # DCE数据结构：合约名称包含 "C-XX-C-XXXXX"(Call) 或 "C-XX-P-XXXXX"(Put)
    # 提取看涨/看跌
    calls = df[df['合约编码'].str.contains('-C-', na=False)] if '合约编码' in df.columns else df[df.iloc[:, 0].str.contains('-C-', na=False)]
    puts = df[df['合约编码'].str.contains('-P-', na=False)] if '合约编码' in df.columns else df[df.iloc[:, 0].str.contains('-P-', na=False)]
    
    if len(calls) == 0 or len(puts) == 0:
        return None
    
    # 寻找持仓量/成交量列
    oi_col = None
    vol_col = None
    for c in df.columns:
        if '持仓量' in c or '持仓' in c:
            oi_col = c
        if '成交量' in c:
            vol_col = c
    
    if not oi_col:
        return None
    
    call_oi = calls[oi_col].sum()
    put_oi = puts[oi_col].sum()
    
    pcr_oi = put_oi / call_oi if call_oi > 0 else None
    
    if vol_col:
        call_vol = calls[vol_col].sum()
        put_vol = puts[vol_col].sum()
        pcr_vol = put_vol / call_vol if call_vol > 0 else None
    else:
        pcr_vol = None
    
    return {
        'pcr_oi': round(pcr_oi, 3) if pcr_oi else None,
        'pcr_vol': round(pcr_vol, 3) if pcr_vol else None,
        'call_oi': int(call_oi),
        'put_oi': int(put_oi),
        'call_vol': int(call_vol) if vol_col else None,
        'put_vol': int(put_vol) if vol_col else None,
    }


def pcr_to_signal(pcr_data):
    """将PCR转为交易信号"""
    if pcr_data is None:
        return {'direction': 0, 'score': 0, 'detail': '无期权数据', 'pcr': None}
    
    pcr_oi = pcr_data.get('pcr_oi')
    pcr_vol = pcr_data.get('pcr_vol')
    
    reasons = []
    direction = 0
    score = 0.0
    
    if pcr_oi:
        reasons.append(f"PCR(oi)={pcr_oi:.2f}")
        if pcr_oi > 1.2:
            direction -= 1
            score += 0.5
            reasons.append("看跌主导(利空)")
        elif pcr_oi < 0.8:
            direction += 1
            score += 0.5
            reasons.append("看涨主导(利多)")
        else:
            reasons.append("中性")
    
    if pcr_vol:
        reasons.append(f"PCR(vol)={pcr_vol:.2f}")
        if pcr_vol > 1.5:
            direction -= 1
            score += 0.3
            reasons.append("成交量看跌(恐慌)")
        elif pcr_vol < 0.6:
            direction += 1
            score += 0.3
            reasons.append("成交量看涨(乐观)")
    
    # 归一化
    direction = max(-1, min(1, direction))
    score = min(score, 1.0)
    
    return {
        'direction': direction,
        'score': score,
        'detail': ' | '.join(reasons),
        'pcr': pcr_oi,
    }


def main():
    import sys
    
    print("=" * 50)
    print("DCE玉米期权 Put/Call Ratio 信号")
    print("=" * 50)
    
    if '--manual' in sys.argv:
        # 手动输入模式
        try:
            idx = sys.argv.index('--manual')
            pcr = float(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else None
            if pcr is None:
                pcr = float(input("Put/Call Ratio (持仓量): "))
            print(f"\n手动输入 PCR = {pcr:.2f}")
            signal = pcr_to_signal({
                'pcr_oi': pcr,
                'call_oi': 0,
                'put_oi': 0,
            })
        except (IndexError, ValueError):
            print("用法: python3 option_pcr.py --manual <pcr_value>")
            return
    else:
        # 自动获取
        print(f"\n从DCE获取玉米期权数据...")
        df, date_str = fetch_dce_option()
        
        if df is not None:
            print(f"  获取成功 (交易日: {date_str})")
            print(f"  数据行数: {len(df)}")
            
            pcr_data = compute_pcr(df)
            if pcr_data:
                print(f"\n计算PCR:")
                print(f"  看涨持仓: {pcr_data['call_oi']:,}")
                print(f"  看跌持仓: {pcr_data['put_oi']:,}")
                print(f"  PCR(oi): {pcr_data['pcr_oi']}")
                if pcr_data.get('pcr_vol'):
                    print(f"  PCR(vol): {pcr_data['pcr_vol']}")
            else:
                pcr_data = None
                print(f"  ⚠️ 无法计算PCR")
        else:
            pcr_data = None
            print(f"  ⚠️ DCE数据获取失败（反爬机制）")
            print(f"  → 使用手动模式: python3 option_pcr.py --manual <PCR值>")
            print(f"  → PCR可从 https://www.dce.com.cn/ 查询")
    
    signal = pcr_to_signal(pcr_data)
    
    emoji = "🟢" if signal['direction'] > 0 else ("🔴" if signal['direction'] < 0 else "⚪")
    print(f"\n期权信号:")
    print(f"  方向: {signal['direction']} (1=多, -1=空, 0=中性)")
    print(f"  强度: {signal['score']:.2f}")
    print(f"  PCR: {signal.get('pcr', 'N/A')}")
    print(f"  {emoji} {signal['detail']}")
    
    # 缓存
    cache = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "pcr_data": pcr_data,
        "signal": signal,
        "note": "DCE数据受反爬限制，尝试自动获取失败时请手动更新 --manual"
    }
    CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    print(f"\n已缓存至 {CACHE}")


def get_option_signal():
    """供 analysis.py 调用"""
    try:
        if CACHE.exists():
            cache = json.loads(CACHE.read_text())
            if (datetime.now() - datetime.fromisoformat(cache.get("fetched_at", "2000-01-01"))).days < 2:
                return cache.get("signal", {'direction': 0, 'score': 0, 'detail': '缓存过期'})
        
        # 自动获取（可能会失败）
        df, _ = fetch_dce_option()
        if df is not None:
            pcr_data = compute_pcr(df)
            if pcr_data:
                signal = pcr_to_signal(pcr_data)
                cache = {"fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "signal": signal}
                CACHE.write_text(json.dumps(cache, indent=2))
                return signal
    except:
        pass
    
    return {'direction': 0, 'score': 0, 'detail': 'DCE数据暂不可用'}


if __name__ == "__main__":
    main()
