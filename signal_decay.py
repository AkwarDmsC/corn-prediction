#!/usr/bin/env python3
"""
信号时间衰减机制 (过拟合修正·第1步)

功能：
1. 每个信号维持近20笔方向准确率滑动窗口
2. 准确率高于55% → 权重+0.1 (上限1.5x基准)
3. 准确率低于45% → 权重-0.1 (下限0.3x基准)
4. 准确率接近50% → 维持不变

用法（在 analysis.py 中调用）：
  from signal_decay import get_decay_factors
  
  # 在 weighted = [ ... ] 之前调用
  decay = get_decay_factors()
  # decay['MA'] = 0.95  (0.95x 基准权重)
  weighted = [
    (ma_dir, 1.6 * decay.get('MA', 1.0)),
    ...
  ]

文件: .signal_decay.json 持久化存储
"""

import json
from pathlib import Path
from datetime import datetime

WORKSPACE = Path(__file__).parent
DECAY_FILE = WORKSPACE / ".signal_decay.json"
TRACKER_STATE = WORKSPACE / ".tracker_state.json"

# 衰减参数
WINDOW = 20  # 滑动窗口大小
DECAY_STEP = 0.1  # 每次调整步长
UPPER_LIMIT = 1.5  # 最大提升倍数
LOWER_LIMIT = 0.3  # 最小降低倍数
HIGH_THRESHOLD = 55  # % — 高于此提升权重
LOW_THRESHOLD = 45   # % — 低于此降低权重

SIGNAL_NAMES = [
    "MA", "RSI", "MACD", "成交量", "布林带", "大豆",
    "季节性", "CFTC", "CFTC基金", "政策", "天气",
    "ENSO", "BCI", "持仓量", "CBOT", "新闻",
    "USDA出口", "生猪", "进口成本", "乙醇",
]


def load_data():
    if DECAY_FILE.exists():
        return json.loads(DECAY_FILE.read_text())
    return {"signal_history": {}, "last_updated": None, "decay_factors": {}}


def save_data(data):
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    DECAY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def compute_decay_factor(accuracy_pct):
    """根据近期准确率计算衰减系数"""
    if accuracy_pct is None or accuracy_pct == 50:
        return 1.0
    if accuracy_pct > HIGH_THRESHOLD:
        boost = min((accuracy_pct - HIGH_THRESHOLD) / 10 * DECAY_STEP, UPPER_LIMIT - 1.0)
        return 1.0 + boost
    elif accuracy_pct < LOW_THRESHOLD:
        penalty = min((LOW_THRESHOLD - accuracy_pct) / 10 * DECAY_STEP, 1.0 - LOWER_LIMIT)
        return 1.0 - penalty
    return 1.0


def update_from_verified(data):
    """
    从 .tracker_state.json 的最新 verified 记录更新信号历史
    每笔verified记录中包含:
      - date
      - direction_correct: True/False
      - 但没有细分到每个信号的正确性
      
    这里用近似：整体方向正确 == 全部信号正确(Naive)
    更精确的会在未来的 auto_attribution.py 增强版中实现
    """
    if not TRACKER_STATE.exists():
        return data
    
    state = json.loads(TRACKER_STATE.read_text())
    verified = state.get("verified", [])
    if not verified:
        return data
    
    for v in verified:
        date = v.get("date", "?")
        dc = v.get("direction_correct", False)
        
        # 对每个信号，整体方向正确 → 该信号也标记为正确（近似）
        for sig in SIGNAL_NAMES:
            if sig not in data["signal_history"]:
                data["signal_history"][sig] = []
            history = data["signal_history"][sig]
            # 去重：避免同一天重复
            if not history or history[-1].get("date") != date:
                history.append({"date": date, "correct": dc})
            # 只保留最近WINDOW条
            if len(history) > WINDOW:
                history.pop(0)
    
    return data


def compute_decay_factors(data):
    """计算当前所有信号的衰减系数"""
    factors = {}
    for sig in SIGNAL_NAMES:
        history = data["signal_history"].get(sig, [])
        if len(history) < 3:
            factors[sig] = 1.0  # 样本太少，不调整
            continue
        correct = sum(1 for h in history if h["correct"])
        total = len(history)
        acc = correct / total * 100
        factors[sig] = round(compute_decay_factor(acc), 3)
    
    data["decay_factors"] = factors
    return data


def get_decay_factors(force_refresh=False):
    """
    主入口：获取当前所有信号的衰减系数
    
    返回: {signal_name: decay_multiplier}
    """
    data = load_data()
    
    if force_refresh or not data.get("decay_factors"):
        data = update_from_verified(data)
        data = compute_decay_factors(data)
        save_data(data)
    
    return data.get("decay_factors", {})


def print_decay_report():
    """打印当前衰减状态"""
    data = load_data()
    data = update_from_verified(data)
    data = compute_decay_factors(data)
    save_data(data)
    
    print("=" * 60)
    print("信号时间衰减报告")
    print("=" * 60)
    
    factors = data.get("decay_factors", {})
    history = data.get("signal_history", {})
    
    print(f"\n{'信号':<12} {'衰减系数':<10} {'样本':<6} {'准确率':<10} {'调整方向':<12}")
    print("-" * 50)
    
    for sig in SIGNAL_NAMES:
        f = factors.get(sig, 1.0)
        h = history.get(sig, [])
        n = len(h)
        if n >= 3:
            correct = sum(1 for x in h if x["correct"])
            acc = correct / n * 100
        else:
            acc = None
        
        if f > 1.05:
            adj = "↑提升"
        elif f < 0.95:
            adj = "↓降低"
        else:
            adj = "—维持"
        
        acc_str = f"{acc:.0f}%" if acc else "样本不足"
        print(f"{sig:<12} {f:<10.3f} {n:<6} {acc_str:<10} {adj:<12}")
    
    print("\n说明: 衰减系数×基准权重。调整范围 0.3x ~ 1.5x")
    print(f"  样本>=3才调整, 窗口{WINDOW}笔, 步长{DECAY_STEP}")


if __name__ == "__main__":
    print_decay_report()
