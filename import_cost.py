#!/usr/bin/env python3
"""
玉米进口成本监控模块 (P2-3)

计算DCE玉米 vs 进口玉米到港完税价的价差
判断进口利润窗口→对国内价格的影响

进口成本 = CBOT玉米期货价 × 汇率 × 关税系数 + 运费 + 港杂费

信号逻辑：
  进口利润 > 100元/吨 → 进口套利窗口打开 → 利空国内（进口增加预期）
  进口利润 < -100元/吨 → 进口亏损 → 利多国内（进口减少预期）
  中间 → 中性

用法：
  python3 import_cost.py              # 显示当前进口成本
  python3 import_cost.py --history     # 显示历史价差变化
"""

import json
from pathlib import Path
from datetime import datetime, timedelta

WORKSPACE = Path(__file__).parent
CACHE = WORKSPACE / "import_cost_cache.json"

# 固定参数
# CBOT 玉米: 1美分/蒲式耳 = 0.39368 美元/吨
# 1蒲式耳玉米 = 0.0254 公吨
BUSHEL_TO_TON = 0.0254
CENT_TO_DOLLAR = 0.01
TARIFF_RATE = 0.65  # 65% 进口关税（配额外）; 配额内1%
VAT_RATE = 0.09     # 9% 增值税
PORT_FEE = 80       # 港杂费（元/吨）
FREIGHT_GULF_TO_CHINA = 55  # 美湾→中国的海运费（美元/吨，动态）
TRQ_TARIFF = 0.01   # 配额内关税 1%
TRQ_AMOUNT = 7.2    # 玉米进口配额（百万吨/年）
EXTRA_TRADE_TAX = 0.03  # 加征关税（贸易战相关）


def fetch_data():
    """获取计算进口成本所需的所有数据"""
    import akshare as ak
    import pandas as pd
    
    result = {}
    
    # 1. 国内玉米现货（港口价）
    try:
        df = ak.spot_corn_price_soozhu()
        result["domestic_spot"] = {
            "price": float(df.iloc[-1]["价格"]),  # 元/kg
            "date": str(df.iloc[-1]["日期"]),
            "price_per_ton": float(df.iloc[-1]["价格"]) * 1000,
        }
        # 7天变化
        if len(df) >= 7:
            chg = float(df.iloc[-1]["价格"]) - float(df.iloc[-7]["价格"])
            result["domestic_spot"]["chg_7d"] = round(chg * 1000, 0)
        else:
            result["domestic_spot"]["chg_7d"] = 0
    except Exception as e:
        result["domestic_spot"] = {"error": str(e)}
    
    # 2. CBOT玉米期货
    try:
        df = ak.futures_foreign_hist(symbol="C")
        df['date'] = df['date'].astype(str)
        latest = df.iloc[-1]
        result["cbot_corn"] = {
            "price_cents": float(latest["close"]),  # 美分/蒲式耳
            "date": latest["date"],
        }
        # 转为美元/吨
        cents_per_bu = float(latest["close"])
        dollars_per_ton = cents_per_bu * CENT_TO_DOLLAR / BUSHEL_TO_TON
        result["cbot_corn"]["price_usd_per_ton"] = round(dollars_per_ton, 1)
        
        # 7天前
        if len(df) >= 7:
            old = float(df.iloc[-7]["close"])
            result["cbot_corn"]["chg_7d"] = round(old - cents_per_bu, 1)
        else:
            result["cbot_corn"]["chg_7d"] = 0
    except Exception as e:
        result["cbot_corn"] = {"error": str(e)}
    
    # 3. 美元/人民币汇率
    try:
        df = ak.currency_boc_sina()
        # 用央行中间价
        mid_col = [c for c in df.columns if '中间价' in c][0]
        latest_rate = float(df.iloc[-1][mid_col])
        result["usd_cny"] = {
            "rate": round(latest_rate / 100, 4),  # 中行中间价单位是元/100美元→转元/美元
            "date": str(df.iloc[-1]["日期"]),
        }
    except Exception as e:
        # 备用：用固定汇率
        result["usd_cny"] = {"rate": 7.25, "date": "估算"}
    
    result["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return result


def compute_import_cost(data):
    """
    计算进口到港完税成本（配额外）
    
    公式：
      CBOT价格(美元/吨) × 汇率 × (1 + 关税) × (1 + 增值税) + 海运费 + 港杂费
    
    返回: 配额外完税成本 + 配额内完税成本
    """
    cbot = data.get("cbot_corn", {})
    fx = data.get("usd_cny", {})
    
    if "error" in cbot or "error" in fx:
        return None, None, "数据不足"
    
    cbot_usd = cbot.get("price_usd_per_ton", 0)
    fx_rate = fx.get("rate", 7.25)
    
    # 海运费（从BCI推算）
    freight = FREIGHT_GULF_TO_CHINA
    try:
        # 尝试从成品的 BCI 数据获取最新运费
        bci_cache = WORKSPACE / "bci_cache.json"
        if bci_cache.exists():
            bci_data = json.loads(bci_cache.read_text())
            if "latest" in bci_data and bci_data["latest"] > 1000:
                # BCI 1000 ≈ 运费约40美元/吨
                freight = max(35, bci_data["latest"] / 25)
    except:
        pass
    
    # 配额外：65%关税 + 9%增值税 + 加征关税
    extra_tariff_cost = cbot_usd * fx_rate * (1 + TARIFF_RATE + EXTRA_TRADE_TAX) * (1 + VAT_RATE) + freight + PORT_FEE
    
    # 配额内：1%关税 + 9%增值税（配额720万吨/年）
    quota_cost = cbot_usd * fx_rate * (1 + TRQ_TARIFF) * (1 + VAT_RATE) + freight + PORT_FEE
    
    return round(extra_tariff_cost, 0), round(quota_cost, 0)


def main():
    data = fetch_data()
    cbot = data.get("cbot_corn", {})
    fx = data.get("usd_cny", {})
    domestic = data.get("domestic_spot", {})
    
    print("=" * 60)
    print("玉米进口成本监控")
    print("=" * 60)
    
    # 国内现货
    if "price_per_ton" in domestic:
        dp = domestic["price_per_ton"]
        print(f"\n国内现货 ({domestic.get('date', '?')})")
        print(f"  价格: {dp:.0f} 元/吨")
        print(f"  7天变化: {domestic.get('chg_7d', 0):+.0f} 元/吨")
    
    # CBOT
    if "price_usd_per_ton" in cbot:
        print(f"\nCBOT玉米 ({cbot.get('date', '?')})")
        print(f"  {cbot.get('price_cents', 0):.0f} 美分/蒲式耳")
        print(f"  折合: {cbot['price_usd_per_ton']:.0f} 美元/吨")
        print(f"  7天变化: {cbot.get('chg_7d', 0):+.0f} 美分")
    
    # 汇率
    if "rate" in fx:
        print(f"\n汇率 ({fx.get('date', '?')})")
        print(f"  1美元 = {fx['rate']:.4f} 人民币")
    
    # 进口成本
    extra_cost, quota_cost = compute_import_cost(data)
    if extra_cost and quota_cost:
        print(f"\n进口到港成本")
        print(f"  配额内(1%关税): {quota_cost:.0f} 元/吨")
        print(f"  配额外(65%关税): {extra_cost:.0f} 元/吨")
        
        if "price_per_ton" in domestic:
            dp = domestic["price_per_ton"]
            extra_spread = round(dp - extra_cost, 0)
            quota_spread = round(dp - quota_cost, 0)
            
            print(f"\n价差（国内 - 进口）:")
            print(f"  国内 vs 配额内: {quota_spread:+.0f} 元/吨")
            print(f"  国内 vs 配额外: {extra_spread:+.0f} 元/吨")
            
            # 信号
            if extra_spread > 100:
                print(f"\n  🟢 国内溢价大 → 进口利润窗口打开 → 利空国内")
            elif extra_spread > 0:
                print(f"\n  ⚪ 国内略高于进口")
            elif extra_spread > -100:
                print(f"\n  ⚪ 进口成本略高于国内")
            else:
                print(f"\n  🔴 进口严重亏损 → 利多国内（进口减少预期）")
    
    # 缓存
    result = {
        "data": data,
        "extra_cost": extra_cost,
        "quota_cost": quota_cost,
        "extra_spread": round(dp - extra_cost, 0) if extra_cost and "price_per_ton" in domestic else None,
        "quota_spread": round(dp - quota_cost, 0) if quota_cost and "price_per_ton" in domestic else None,
    }
    CACHE.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n  已缓存至 {CACHE}")


def get_import_signal():
    """
    供 analysis.py 调用的进口成本信号
    
    返回: {'direction': -1/0/1, 'score': 0~1, 'detail': str}
    """
    try:
        data = fetch_data()
        extra_cost, quota_cost = compute_import_cost(data)
        domestic = data.get("domestic_spot", {})
        
        if extra_cost is None or "price_per_ton" not in domestic:
            return {'direction': 0, 'score': 0, 'detail': '数据不足'}
        
        dp = domestic["price_per_ton"]
        spread = dp - extra_cost
        
        if spread > 150:
            return {'direction': -1, 'score': 0.7, 'detail': f'进口利润{spread:.0f}元/吨,利空'}
        elif spread > 80:
            return {'direction': -1, 'score': 0.4, 'detail': f'进口微利{spread:.0f}元/吨,轻微利空'}
        elif spread < -150:
            return {'direction': 1, 'score': 0.7, 'detail': f'进口亏损{spread:.0f}元/吨,利多'}
        elif spread < -80:
            return {'direction': 1, 'score': 0.4, 'detail': f'进口亏损{spread:.0f}元/吨,轻微利多'}
        else:
            return {'direction': 0, 'score': 0, 'detail': f'进口价差{spread:+.0f}元/吨,中性'}
    except Exception as e:
        return {'direction': 0, 'score': 0, 'detail': f'计算失败: {e}'}


if __name__ == "__main__":
    main()
