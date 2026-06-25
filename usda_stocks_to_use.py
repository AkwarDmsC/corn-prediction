#!/usr/bin/env python3
"""
USDA 玉米库存消费比(Stocks-to-Use)月度跟踪 (P2-2)

数据来源：USDA WASDE月度报告（每月9-12日发布）
每次WASDE发布日自动调用此脚本，填入当月数据

也可以从USDA出口销售XML中部分推导：
  - 累计出口 + 未交付 = 总出口承诺
  - 总出口承诺 / 总供给 ≈ 出口消费比（非完整库存消费比）

完整库存消费比需要WASDE数据。
手动追踪方案：每次WASDE报告日执行 --update 填入数据
"""

import json
from pathlib import Path
from datetime import datetime

WORKSPACE = Path(__file__).parent
CACHE = WORKSPACE / "usda_stocks_to_use.json"


DEFAULT_DATA = {
    "meta": {
        "description": "USDA玉米库存消费比跟踪",
        "unit": "百万英斗(Million Bushels) or %",
        "last_updated": ""
    },
    "records": [],
    "current": {}
}


def load_data():
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    return dict(DEFAULT_DATA)


def save_data(data):
    data["meta"]["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"数据已保存至 {CACHE}")


def update_record(year, ending_stocks, total_use, source="WASDE"):
    """
    添加/更新一条库存消费比记录
    
    参数:
      year: 市场年 (e.g. "2025/26")
      ending_stocks: 期末库存 (百万英斗)
      total_use: 总消费量 (百万英斗)
      source: 数据来源 (WASDE/ERS/手动)
    """
    data = load_data()
    
    stocks_to_use = round(ending_stocks / total_use * 100, 1) if total_use > 0 else 0
    
    # 更新或新增
    existing = [r for r in data["records"] if r["year"] == year]
    record = {
        "year": year,
        "ending_stocks": ending_stocks,
        "total_use": total_use,
        "stocks_to_use_pct": stocks_to_use,
        "source": source,
        "updated": datetime.now().strftime("%Y-%m-%d")
    }
    
    if existing:
        for i, r in enumerate(data["records"]):
            if r["year"] == year:
                data["records"][i] = record
                break
    else:
        data["records"].append(record)
    
    data["current"] = record
    data["records"].sort(key=lambda r: r["year"], reverse=True)
    save_data(data)
    
    return record


def get_signal(stocks_to_use_pct):
    """将库存消费比转为玉米供需信号"""
    if stocks_to_use_pct is None:
        return {'direction': 0, 'score': 0, 'detail': '无数据'}
    
    # 历史范围: 中国玉米库存消费比通常在20-80%
    # 美国玉米通常在8-15%
    # 这里用的是美国数据（USDA WASDE）
    if stocks_to_use_pct <= 8:
        return {'direction': 1, 'score': 0.9, 'detail': f'库存极低({stocks_to_use_pct}%), 供应紧张'}
    elif stocks_to_use_pct <= 10:
        return {'direction': 1, 'score': 0.6, 'detail': f'库存偏低({stocks_to_use_pct}%), 供应偏紧'}
    elif stocks_to_use_pct <= 12:
        return {'direction': 0, 'score': 0, 'detail': f'库存适中({stocks_to_use_pct}%), 供需平衡'}
    else:
        return {'direction': -1, 'score': 0.6, 'detail': f'库存偏高({stocks_to_use_pct}%), 供应过剩'}


def main():
    import sys
    
    data = load_data()
    
    print("=" * 50)
    print("USDA玉米库存消费比跟踪")
    print("=" * 50)
    
    records = data.get("records", [])
    current = data.get("current", {})
    
    if records:
        print(f"\n历史记录 ({len(records)}条):")
        print(f"{'市场年':<12} {'期末库存':<12} {'总消费':<12} {'库存消费比':<12} {'来源':<8}")
        print("-" * 56)
        for r in records[:10]:
            print(f"{r['year']:<12} {r['ending_stocks']:<12.0f} {r['total_use']:<12.0f} {r['stocks_to_use_pct']:<11.1f}% {r.get('source', 'WASDE'):<8}")
    
    if current:
        print(f"\n最新数据: {current['year']}")
        print(f"  期末库存: {current['ending_stocks']:.0f} 百万英斗")
        print(f"  总消费: {current['total_use']:.0f} 百万英斗")
        print(f"  库存消费比: {current['stocks_to_use_pct']:.1f}%")
        
        signal = get_signal(current.get('stocks_to_use_pct'))
        emoji = "🟢" if signal['direction'] > 0 else ("🔴" if signal['direction'] < 0 else "⚪")
        print(f"  信号: {emoji} {signal['detail']}")
    
    # 更新模式
    if '--update' in sys.argv:
        try:
            idx = sys.argv.index('--update')
            year = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else input("市场年 (e.g. 2025/26): ")
            stocks = float(sys.argv[idx + 2]) if len(sys.argv) > idx + 2 else float(input("期末库存 (百万英斗): "))
            use = float(sys.argv[idx + 3]) if len(sys.argv) > idx + 3 else float(input("总消费 (百万英斗): "))
            
            r = update_record(year, stocks, use)
            print(f"\n✅ 已更新 {year}: 库存消费比 = {r['stocks_to_use_pct']:.1f}%")
        except (IndexError, ValueError):
            print("用法: python3 usda_stocks_to_use.py --update '2025/26' 1546 14658")
    elif '--latest' in sys.argv:
        # 从 USDS 出口销售XML推算近似出口消费比
        try:
            import usda_export_sales as ues
            import requests
            import xml.etree.ElementTree as ET
            
            xml_text = ues.fetch_usda_xml()
            if xml_text:
                corn = ues.get_corn_export_sales(xml_text)
                if corn:
                    # 用累计出口近似总消费的一部分
                    exports = corn['cumulative_exports'] * 0.03937  # K吨转百万英斗
                    total_commit = corn['total_commitment'] * 0.03937
                    print(f"\n  近似数据:")
                    print(f"  累计出口: {exports:.0f} 百万英斗")
                    print(f"  总承诺: {total_commit:.0f} 百万英斗")
        except Exception as e:
            print(f"  ⚠️ 获取近似数据失败: {e}")


if __name__ == "__main__":
    main()
