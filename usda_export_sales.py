#!/usr/bin/env python3
"""
USDA 出口销售数据接入模块 (P1-3)

数据来源：USDA FAS ESRQS (Export Sales Reporting & Query System)
  API: https://apps.fas.usda.gov/esrqs/StaticReports/CWRCommoditySummary.xml
  提供周度更新，每周四发布（覆盖截至上周四的一周数据）
  CommodityCode=401 = CORN - UNMILLED
  CommodityCode=801 = SOYBEANS
  CommodityCode=901 = SOYBEAN CAKE AND MEAL

用法：
  python3 usda_export_sales.py                     # 获取最新数据并打印
  python3 usda_export_sales.py --history            # 获取历史（含前几周）
  python3 usda_export_sales.py --diff               # 周环比变化
"""

import requests
import xml.etree.ElementTree as ET
import json
from pathlib import Path
from datetime import datetime, timedelta

WORKSPACE = Path(__file__).parent
CACHE_FILE = WORKSPACE / "usda_export_cache.json"

USDA_URL = "https://apps.fas.usda.gov/esrqs/StaticReports/CWRCommoditySummary.xml"
COMMODITIES = {
    401: "CORN - UNMILLED",
    801: "SOYBEANS",
    901: "SOYBEAN CAKE AND MEAL",
}


def fetch_usda_xml():
    """从 USDA ESRQS 获取完整的 CWR 商品摘要 XML"""
    try:
        r = requests.get(USDA_URL, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ⚠️ USDA数据获取失败: {e}")
        return None


def parse_xml(xml_text):
    """解析XML，提取指定商品的出口销售数据"""
    root = ET.fromstring(xml_text)
    namespace = {"ns": "CWRCommoditySummary"}
    
    all_records = []
    for detail in root.iter("{CWRCommoditySummary}Details"):
        code = int(detail.get("CommodityCode", "0"))
        if code not in COMMODITIES:
            continue
        
        record = {
            "commodity_code": code,
            "commodity_name": detail.get("CommodityName", ""),
            "period_ending": detail.get("PeriodEndingDate", ""),
            "week": int(detail.get("MarketingYearWeekNumber", "0")),
            "mkt_year": detail.get("MarketingYear", ""),
            "new_sales": float(detail.get("NewSales", "0")),
            "cancellations": float(detail.get("BuyBacks_Cancellations", "0")),
            "net_sales": float(detail.get("NetSales", "0")),
            "weekly_exports": float(detail.get("WeeklyExports", "0")),
            "outstanding_sales": float(detail.get("OutstandingSales", "0")),
            "accumulated_exports": float(detail.get("AccumulatedExports", "0")),
            "total_commitment": float(detail.get("TotalCommitment", "0")),
            "prev_year_exports": float(detail.get("PreviousMKTYearAccumulatedExports", "0")),
        }
        all_records.append(record)
    
    return all_records


def get_corn_export_sales(xml_text):
    """提取玉米出口销售数据"""
    records = parse_xml(xml_text)
    corn_records = [r for r in records if r["commodity_code"] == 401]
    
    # 按周排序
    corn_records.sort(key=lambda r: r["week"], reverse=True)
    
    if corn_records:
        latest = corn_records[0]
        # 同比变化
        pct_change = ((latest["accumulated_exports"] - latest["prev_year_exports"]) / 
                      max(latest["prev_year_exports"], 1)) * 100
        
        return {
            "date": latest["period_ending"],
            "week": latest["week"],
            "year": latest["mkt_year"],
            "net_sales": latest["net_sales"],
            "new_sales": latest["new_sales"],
            "cancellations": latest["cancellations"],
            "weekly_exports": latest["weekly_exports"],
            "cumulative_exports": latest["accumulated_exports"],
            "outstanding": latest["outstanding_sales"],
            "total_commitment": latest["total_commitment"],
            "prev_year_cumulative": latest["prev_year_exports"],
            "yoy_change_pct": round(pct_change, 1),
            "signal": "🟢利多" if latest["net_sales"] > 0 and pct_change > 0 else ("🔴利空" if latest["net_sales"] < 0 else "⚪中性"),
        }
    return None


def get_soybean_export_sales(xml_text):
    """提取大豆出口销售数据"""
    records = parse_xml(xml_text)
    soy_records = [r for r in records if r["commodity_code"] == 801]
    soy_records.sort(key=lambda r: r["week"], reverse=True)
    
    if soy_records:
        latest = soy_records[0]
        pct_change = ((latest["accumulated_exports"] - latest["prev_year_exports"]) / 
                      max(latest["prev_year_exports"], 1)) * 100
        return {
            "date": latest["period_ending"],
            "week": latest["week"],
            "year": latest["mkt_year"],
            "net_sales": latest["net_sales"],
            "weekly_exports": latest["weekly_exports"],
            "cumulative_exports": latest["accumulated_exports"],
            "outstanding": latest["outstanding_sales"],
            "yoy_change_pct": round(pct_change, 1),
            "signal": "🟢利多" if latest["net_sales"] > 0 and pct_change > 0 else ("🔴利空" if latest["net_sales"] < 0 else "⚪中性"),
        }
    return None


def cache_data(corn_data, soy_data):
    """缓存最新数据"""
    cache = {"corn": corn_data, "soybeans": soy_data, "updated": datetime.now().isoformat()}
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    print(f"  数据已缓存至 {CACHE_FILE}")


def get_usda_signal():
    """
    获取USDA出口销售信号，供 daily_analysis_cn.py 调用
    
    返回: {
        'direction': 1 (利多) / -1 (利空) / 0 (中性),
        'score': 强度 (0~1),
        'detail': str
    }
    """
    try:
        if CACHE_FILE.exists():
            cache = json.loads(CACHE_FILE.read_text())
            # 检查缓存是否过期（最多1天有效）
            updated = datetime.fromisoformat(cache.get("updated", "2000-01-01"))
            if (datetime.now() - updated).days < 2:
                corn = cache.get("corn")
                if corn:
                    return corn_to_signal(corn)
        
        # 重新获取
        xml_text = fetch_usda_xml()
        if xml_text:
            corn = get_corn_export_sales(xml_text)
            soy = get_soybean_export_sales(xml_text)
            cache_data(corn, soy)
            if corn:
                return corn_to_signal(corn)
    except Exception as e:
        print(f"  ⚠️ USDA信号获取失败: {e}")
    
    return {'direction': 0, 'score': 0, 'detail': '数据不可用'}


def corn_to_signal(corn):
    """将玉米出口数据转为交易信号"""
    direction = 0
    score = 0
    reasons = []
    
    # 净销售>0且同比正增长 → 需求强劲
    if corn["net_sales"] > 0 and corn["yoy_change_pct"] > 0:
        direction = 1
        score = min(abs(corn["yoy_change_pct"]) / 20, 1.0) * 0.6 + 0.4
        reasons.append(f"净销售{corn['net_sales']:.0f}K吨")
        reasons.append(f"累计同比+{corn['yoy_change_pct']:.1f}%")
    elif corn["net_sales"] > 0:
        direction = 1
        score = 0.5
        reasons.append(f"净销售{corn['net_sales']:.0f}K吨")
    elif corn["net_sales"] < -100:
        direction = -1
        score = 0.6
        reasons.append(f"净取消{corn['net_sales']:.0f}K吨")
    
    return {
        'direction': direction,
        'score': score,
        'detail': f"USDA出口: {', '.join(reasons)}" if reasons else "USDA出口: 中性",
        'raw': corn
    }


def main():
    print("=" * 60)
    print("USDA 出口销售数据")
    print("=" * 60)
    
    xml_text = fetch_usda_xml()
    if not xml_text:
        print("  数据获取失败")
        return
    
    corn = get_corn_export_sales(xml_text)
    soy = get_soybean_export_sales(xml_text)
    
    if corn:
        print(f"\n🌽 玉米出口销售（截至 {corn['date']}）")
        print(f"  市场年: {corn['year']} (第{corn['week']}周)")
        print(f"  周净销售: {corn['net_sales']:,.0f} K吨")
        print(f"  周出口: {corn['weekly_exports']:,.0f} K吨")
        print(f"  累计出口: {corn['cumulative_exports']:,.0f} K吨")
        print(f"  未交付: {corn['outstanding']:,.0f} K吨")
        print(f"  总承诺: {corn['total_commitment']:,.0f} K吨")
        print(f"  同比: {corn['yoy_change_pct']:+.1f}%")
        print(f"  {corn['signal']}")
    
    if soy:
        print(f"\n🫘 大豆出口销售（截至 {soy['date']}）")
        print(f"  市场年: {soy['year']} (第{soy['week']}周)")
        print(f"  周净销售: {soy['net_sales']:,.0f} K吨")
        print(f"  累计出口: {soy['cumulative_exports']:,.0f} K吨")
        print(f"  同比: {soy['yoy_change_pct']:+.1f}%")
        print(f"  {soy['signal']}")
    
    # 缓存
    cache_data(corn, soy)
    
    # 信号测试
    signal = get_usda_signal()
    print(f"\n转换为交易信号:")
    print(f"  方向: {signal['direction']} (1=多, -1=空, 0=中性)")
    print(f"  强度: {signal['score']:.2f}")
    print(f"  说明: {signal['detail']}")
    
    # 记录历史
    from datetime import datetime
    log_file = WORKSPACE / "usda_export_log.md"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n| {now} | {corn['date'] if corn else 'N/A'} | {corn['net_sales']:,.0f}K | {corn['weekly_exports']:,.0f}K | {corn['yoy_change_pct']:+.1f}% | {corn['signal'] if corn else 'N/A'} | {soy['net_sales']:,.0f}K | {soy['signal'] if soy else 'N/A'} |"
    
    if not log_file.exists():
        log_file.write_text("# USDA 出口销售跟踪日志\n\n## 格式: 时间 | 截至日期 | 玉米净销售 | 玉米周出口 | 玉米同比 | 信号 | 大豆净销售 | 大豆信号\n\n")
    
    log_file.write_text(log_file.read_text() + entry)
    print(f"\n  已追加至 {log_file}")


if __name__ == "__main__":
    main()
