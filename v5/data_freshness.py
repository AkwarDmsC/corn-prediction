#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据时效性衰减 v1.0

低频数据（USDA周报/月报、生猪养殖利润等）在日频预测中大部分时间不变，
但发布后前几天影响力最大。实现时效性自动衰减：
  - 发布日当天：权重 = 基准权重 × 1.0
  - 发布后第n个交易日：权重 × decay(n)
  - 默认衰减：线性衰减，发布后第10个交易日降到基准的0.3x

接入方式：在 signal_decay.py 的时间衰减基础上，叠加时效性衰减

用法：
  python3 data_freshness.py --check     # 检查所有信号的最新发布日期和衰减状态
  python3 data_freshness.py --update    # 更新衰老配置文件
"""

import sys, json, datetime
from pathlib import Path

WORKSPACE = Path(__file__).parent

# ── 各低频信号的发布时间表 ──
# 格式: {signal_name: {'last_publish': 'YYYY-MM-DD', 'frequency': 'weekly'|'monthly', 'base_weight': float}}
# frequency 用于预估下次发布时间
# 手动录入：如数据源无法自动获取发布日期，需要手动更新 last_publish

DEFAULT_FRESHNESS = {
    'usda_export': {
        'name': 'USDA出口销售',
        'source': 'usda_export_sales.py',
        'frequency': 'weekly',
        'publish_day': 'Thursday',  # 每周四发布
        'base_weight': 0.5,
        'decay_days': 7,            # 7个交易日后降到0.3x
        'min_weight': 0.3,
        'last_publish': None,        # 自动检测
    },
    'usda_wasde': {
        'name': 'USDA库存消费比/供需报告',
        'source': 'usda_stocks_to_use.py',
        'frequency': 'monthly',
        'publish_day': '8-12',       # 每月8-12日
        'base_weight': 0.8,
        'decay_days': 10,
        'min_weight': 0.3,
        'last_publish': None,
    },
    'hog_profit': {
        'name': '生猪饲料需求',
        'source': 'hog_signal.py',
        'frequency': 'weekly',
        'publish_day': 'Monday',     # 农业农村部周度数据
        'base_weight': 0.5,
        'decay_days': 7,
        'min_weight': 0.3,
        'last_publish': None,
    },
    'ethanol_policy': {
        'name': '乙醇/生柴政策',
        'source': 'ethanol_signal.py',
        'frequency': 'irregular',    # 不定期（新闻触发）
        'publish_day': None,
        'base_weight': 0.3,
        'decay_days': 14,
        'min_weight': 0.1,
        'last_publish': None,
    },
    'import_cost': {
        'name': '进口成本监控',
        'source': 'import_cost.py',
        'frequency': 'daily',       # 实时可算，不需要衰减
        'base_weight': 0.3,
        'decay_days': 0,
        'min_weight': 0.3,
        'last_publish': None,
    },
    'port_basis': {
        'name': '港口升贴水',
        'source': 'port_basis.py',
        'frequency': 'daily',
        'base_weight': 0.1,
        'decay_days': 0,
        'min_weight': 0.1,
        'last_publish': None,
    },
}


def load_state():
    """加载时效性状态文件"""
    state_path = WORKSPACE / '.data_freshness.json'
    if state_path.exists():
        return json.loads(state_path.read_text())
    # 初始化
    state = {'version': 1, 'updated': datetime.date.today().isoformat(), 'signals': {}}
    for k, v in DEFAULT_FRESHNESS.items():
        state['signals'][k] = {'last_publish': v.get('last_publish')}
    return state


def save_state(state):
    """保存时效性状态"""
    state['updated'] = datetime.date.today().isoformat()
    (WORKSPACE / '.data_freshness.json').write_text(json.dumps(state, indent=2, ensure_ascii=False))


def auto_detect_last_publish():
    """
    自动检测低频数据的实际最新发布日期。
    从各信号脚本的缓存文件中读取日期信息。
    """
    signals = {}
    
    # USDA出口销售：从 usda_export_sales.py 缓存读取
    # ESRQS API 返回的报告日期 = 发布日
    usda_path = WORKSPACE / 'usda_export_sales.py'
    signals['usda_export'] = None
    
    # 生猪饲料：从 hog_signal.py 缓存读取
    hog_cache = WORKSPACE / '.hog_cache.json'
    if hog_cache.exists():
        try:
            data = json.loads(hog_cache.read_text())
            if data.get('latest_date'):
                signals['hog_profit'] = data['latest_date']
        except:
            pass
    
    # USDA WASDE：从 .usda_stocks.json 读取
    wasde_cache = WORKSPACE / '.usda_stocks.json'
    if wasde_cache.exists():
        try:
            data = json.loads(wasde_cache.read_text())
            if data.get('last_report_date'):
                signals['usda_wasde'] = data['last_report_date']
        except:
            pass
    
    # 乙醇政策：从 .news_signal_cache.json 读取最新新闻日期
    ethanol_cache = WORKSPACE / '.news_signal_cache.json'
    if ethanol_cache.exists():
        try:
            data = json.loads(ethanol_cache.read_text())
            if isinstance(data, list) and len(data) > 0:
                # 取最新一条的日期
                latest = max(d.get('date', '') for d in data if d.get('date'))
                if latest:
                    signals['ethanol_policy'] = latest
        except:
            pass
    
    return signals


def compute_decay(signal_config, publish_date, today=None):
    """
    计算时效性衰减系数。
    
    Args:
        signal_config: 信号配置 dict
        publish_date: 最近发布日期 (str, YYYY-MM-DD)
        today: 当前日期 (str, YYYY-MM-DD)，None=今天
    
    Returns:
        decay_factor: 0.0 ~ 1.0
    """
    if today is None:
        today = datetime.date.today()
    elif isinstance(today, str):
        today = datetime.date.fromisoformat(today)
    
    if isinstance(publish_date, str):
        try:
            publish = datetime.date.fromisoformat(publish_date)
        except:
            return signal_config.get('base_weight', 0.5) * signal_config.get('min_weight', 0.3)
    elif publish_date is None:
        return signal_config.get('min_weight', 0.3)
    else:
        publish = publish_date
    
    # 如果 decay_days=0，表示日常数据不需要衰减
    decay_days = signal_config.get('decay_days', 7)
    if decay_days <= 0:
        return 1.0
    
    # 计算交易日数（简化：用自然日 × 0.7 估算交易日）
    calendar_days = (today - publish).days
    trading_days = max(0, int(calendar_days * 0.7))
    
    min_factor = signal_config.get('min_weight', 0.3)
    
    if trading_days >= decay_days:
        return min_factor
    else:
        # 线性衰减，1.0 → min_factor
        return 1.0 - (trading_days / decay_days) * (1.0 - min_factor)


def check_freshness():
    """检查所有信号的最新发布日期和衰减状态"""
    state = load_state()
    signals = DEFAULT_FRESHNESS
    auto_signals = auto_detect_last_publish()
    today = datetime.date.today()
    
    print(f"\n{'='*60}")
    print(f"📅 数据时效性检查 — {today.isoformat()}")
    print(f"{'='*60}")
    
    results = []
    
    # 汇总自动检测的发布日期
    for key, config in signals.items():
        # last_publish 优先级：state > auto_detect > config default
        lp = None
        if state['signals'].get(key, {}).get('last_publish'):
            lp = state['signals'][key]['last_publish']
        elif auto_signals.get(key):
            lp = auto_signals[key]
        elif config.get('last_publish'):
            lp = config['last_publish']
        
        decay = compute_decay(config, lp, today) if lp else config.get('min_weight', 0.3)
        effective_weight = config['base_weight'] * decay
        
        print(f"\n  📊 {config['name']}")
        print(f"     数据源: {config['source']}")
        print(f"     频次: {config['frequency']}")
        print(f"     最近发布日期: {lp or '未知'}")
        print(f"     时效性衰减: {decay:.2f}x")
        print(f"     基准权重: {config['base_weight']}")
        print(f"     有效权重: {effective_weight:.3f}")
        print(f"     衰减后权重: {config['base_weight'] * decay:.3f}")
        
        age_days = (today - datetime.date.fromisoformat(lp)).days if lp else 0
        status = "🟢" if decay >= 0.7 else ("🟡" if decay >= 0.4 else "🔴")
        results.append((key, status, lp or 'N/A', age_days, decay, effective_weight))
    
    print(f"\n{'='*60}")
    print(f"📋 总览")
    print(f"{'='*60}")
    print(f"{'信号':>20} {'状态':>6} {'距今(天)':>8} {'衰减':>6} {'有效权重':>8}")
    print(f"{'─'*50}")
    for name, status, lp, age, decay, ew in results:
        print(f"{name:>20} {status:>6} {age:>8} {decay:.2f} {ew:>8.3f}")
    
    return results


def update_state():
    """手动更新发布状态"""
    state = load_state()
    auto_signals = auto_detect_last_publish()
    changed = False
    
    print("🔄 自动检测最新发布日期...")
    for key, date_str in auto_signals.items():
        if date_str:
            old = state['signals'].get(key, {}).get('last_publish')
            if old != date_str:
                state['signals'][key] = {'last_publish': date_str}
                print(f"  ✅ {key}: {old or '无'} → {date_str}")
                changed = True
    
    if changed:
        save_state(state)
        print("✅ 状态已更新")
    else:
        print("  无变化")


def get_freshness_multiplier(signal_name, today=None):
    """
    供外部调用的接口：返回信号的时效性衰减乘数 (0.0~1.0)。
    
    Args:
        signal_name: 信号内部名 ('usda_export', 'hog_profit', 等)
        today: 当前日期 (str, YYYY-MM-DD / date / None)
    
    Returns:
        multiplier: 0.0 ~ 1.0
    """
    config = DEFAULT_FRESHNESS.get(signal_name)
    if not config:
        return 1.0  # 未知信号不衰减
    
    state = load_state()
    lp = state['signals'].get(signal_name, {}).get('last_publish')
    if config.get('decay_days', 0) <= 0:
        return 1.0
    if not lp:
        return config.get('min_weight', 0.3)
    
    return compute_decay(config, lp, today)


if __name__ == '__main__':
    if '--update' in sys.argv:
        update_state()
    check_freshness()
    if '--update' in sys.argv:
        print("\n💡 提示：可以手动在 .data_freshness.json 中补充其他信号的最新发布日期")
        print("   格式：{'signals': {'信号名': {'last_publish': '2026-05-20'}}}")
