#!/usr/bin/env python3
"""
港口升贴水跟踪 (P2-6)

定义：港口升贴水 = 港口现货价 - 期货基准价
  - 正升水 → 现货溢价 → 需求旺盛
  - 负升水(贴水) → 供给充足

数据源：
  - 现货：akshare spot_corn_price_soozhu（全国均价）→ 近似港口价
  - 期货：DCE玉米主力合约
  - 实际港口价需要从天下粮仓/Mysteel获取（非免费）

用法：
  python3 port_basis.py                     # 显示当前基差
  python3 port_basis.py --manual <basis>    # 手动输入基差
"""

import json
from pathlib import Path
from datetime import datetime

WORKSPACE = Path(__file__).parent
CACHE = WORKSPACE / "port_basis_cache.json"


def fetch_basis():
    """
    计算基差 = 现货港口价 - 期货主力价
    
    现货价用 spot_corn_price_soozhu（全国均价，可近似港口价）
    期货用 akshare 最新 DCE 主力
    """
    import akshare as ak
    
    result = {}
    
    # 国内现货价
    try:
        df = ak.spot_corn_price_soozhu()
        result["spot"] = {
            "price": float(df.iloc[-1]["价格"]) * 1000,  # 元/kg → 元/吨
            "date": str(df.iloc[-1]["日期"]),
        }
    except Exception as e:
        result["spot"] = {"error": str(e)}
    
    # 期货主力价
    try:
        df = ak.futures_zh_daily_sina(symbol="C0")
        df['date'] = df['date'].astype(str)
        latest = df.iloc[-1]
        result["futures"] = {
            "price": float(latest["close"]),
            "date": latest["date"],
            "high": float(latest["high"]),
            "low": float(latest["low"]),
        }
    except Exception as e:
        result["futures"] = {"error": str(e)}
    
    result["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return result


def main():
    data = fetch_basis()
    
    print("=" * 50)
    print("港口升贴水(基差)跟踪")
    print("=" * 50)
    
    spot = data.get("spot", {})
    fut = data.get("futures", {})
    
    if "price" in spot:
        print(f"\n国内现货 ({spot.get('date', '?')})")
        print(f"  价格: {spot['price']:.0f} 元/吨")
    
    if "price" in fut:
        print(f"\n期货主力 ({fut.get('date', '?')})")
        print(f"  收盘: {fut['price']:.0f} 元/吨")
        print(f"  日内: {fut.get('low', 0):.0f} ~ {fut.get('high', 0):.0f}")
    
    if "price" in spot and "price" in fut:
        basis = spot["price"] - fut["price"]
        basis_pct = basis / fut["price"] * 100
        
        print(f"\n基差（现货 - 期货）:")
        print(f"  绝对值: {basis:+.0f} 元/吨")
        print(f"  百分比: {basis_pct:+.2f}%")
        
        if basis > 50:
            print(f"  🟢 正升水 → 现货溢价，需求旺盛")
        elif basis > 20:
            print(f"  ⚪ 轻微升水")
        elif basis > -20:
            print(f"  ⚪ 基本平水")
        elif basis > -50:
            print(f"  ⚪ 轻微贴水")
        else:
            print(f"  🔴 大幅贴水 → 供给充足，需求疲软")
    
    # 缓存
    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"\n已缓存至 {CACHE}")


def get_basis_signal():
    """供 analysis.py 调用"""
    try:
        data = fetch_basis()
        spot = data.get("spot", {})
        fut = data.get("futures", {})
        
        if "price" in spot and "price" in fut:
            basis = spot["price"] - fut["price"]
            if basis > 50:
                return {'direction': 1, 'score': 0.5, 'detail': f'基差+{basis:.0f}元/吨(升水)'}
            elif basis < -50:
                return {'direction': -1, 'score': 0.5, 'detail': f'基差{basis:.0f}元/吨(贴水)'}
            else:
                return {'direction': 0, 'score': 0.1, 'detail': f'基差{basis:+.0f}元/吨(平水)'}
    except:
        pass
    return {'direction': 0, 'score': 0, 'detail': '数据不可用'}


if __name__ == "__main__":
    main()
