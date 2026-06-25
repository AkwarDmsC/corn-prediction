#!/usr/bin/env python3
"""
滚动窗口回测框架 (过拟合修正·第3步)

功能：
  每30个交易日自动触发的滚动训练：
  1. 加载最近90-120个交易日数据
  2. 运行 backtest_weights.py 的网格搜索找最优权重
  3. 将新权重写入 analysis.py 的 weighted = [...] 段
  4. 记录训练结果日志

用法：
  python3 rolling_retrain.py               # 检查是否到训练日
  python3 rolling_retrain.py --force        # 强制重训
  python3 rolling_retrain.py --check-only   # 仅检查状态不训练

通过 cron 每日调用：
  PYTHONUNBUFFERED=1 python3 rolling_retrain.py   # 自动判断是否到30个交易日
"""

import json
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta

WORKSPACE = Path(__file__).parent
ANALYSIS = WORKSPACE / "analysis.py"
RETRAIN_FILE = WORKSPACE / ".rolling_retrain.json"
TRAIN_INTERVAL = 30  # 交易日数

DEFAULT_STATE = {
    "last_train_date": None,
    "last_train_samples": 0,
    "next_due_count": 0,
    "trading_days_since_last": 0,
    "current_weights": {},
    "history": []
}


def load_state():
    if RETRAIN_FILE.exists():
        return json.loads(RETRAIN_FILE.read_text())
    return dict(DEFAULT_STATE)


def save_state(state):
    RETRAIN_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def count_trading_days_since(start_date):
    """统计从start_date到现在的交易日数（DCE日历）"""
    import akshare as ak
    try:
        df = ak.futures_zh_daily_sina(symbol="C0")
        df['date'] = df['date'].astype(str)
        df = df.sort_values('date')
        
        if start_date:
            mask = df['date'] > start_date
        else:
            mask = pd.Series([True] * len(df))
        
        return int(mask.sum())
    except Exception as e:
        print(f"  ⚠️ 交易日统计失败: {e}")
        return 0


def scan_current_weights():
    """从 analysis.py 中提取当前权重值"""
    if not ANALYSIS.exists():
        return {}
    
    content = ANALYSIS.read_text()
    weights = {}
    
    # 找 weighted = [ 中的 _dw() 调用
    # 格式: (ma_dir, _dw(1.6, "MA"))
    for line in content.split('\n'):
        m = re.search(r'_dw\(([\d.]+),\s*"([^"]+)"\)', line)
        if m:
            w = float(m.group(1))
            name = m.group(2)
            weights[name] = w
    
    return weights


def update_weights_in_analysis(new_weights):
    """用新权重更新 analysis.py 中的 _dw() 调用的基值"""
    content = ANALYSIS.read_text()
    
    for name, new_w in new_weights.items():
        old_pattern = f'_dw([\\d.]+,\\s*"{name}"'
        match = re.search(old_pattern, content)
        if match:
            old_w = match.group(1)
            content = content.replace(f'_dw({old_w}, "{name}")', f'_dw({new_w:.1f}, "{name}")', 1)
            print(f"  {name}: {old_w} → {new_w:.1f}")
    
    ANALYSIS.write_text(content)
    print(f"\n✅ 权重已更新至 {ANALYSIS}")


def run_backtest():
    """执行回测搜索最优权重"""
    import subprocess
    
    print("\n  运行 backtest_weights.py...")
    result = subprocess.run(
        [sys.executable, "backtest_weights.py"],
        cwd=str(WORKSPACE),
        capture_output=True, text=True, timeout=120
    )
    
    print(result.stdout[-500:] if len(result.stdout) > 500 else result.stdout)
    if result.returncode != 0:
        print(f"  ❌ 回测失败: {result.stderr[:200]}")
        return None
    
    # 从输出中提取推荐权重
    output = result.stdout
    new_weights = {}
    in_recommend = False
    for line in output.split('\n'):
        if "推荐权重配置" in line:
            in_recommend = True
            continue
        if in_recommend:
            m = re.search(r"'?(\w+)'?:\s*([\d.]+)", line)
            if m:
                new_weights[m.group(1)] = float(m.group(2))
    
    return new_weights


def map_backtest_to_analysis(backtest_weights):
    """将backtest的权重名映射到analysis.py的信号名"""
    mapping = {
        'ma': 'MA', 'rsi': 'RSI', 'macd': 'MACD',
        'vol': '成交量', 'bb': '布林带', 'season': '季节性',
        'soy': '大豆', 'policy': '政策', 'cftc': 'CFTC',
        'enso': 'ENSO', 'bci': 'BCI', 'hold': '持仓量',
        'cbot': 'CBOT',
    }
    mapped = {}
    for bk, bv in backtest_weights.items():
        if bk in mapping:
            mapped[mapping[bk]] = bv
        else:
            mapped[bk] = bv  # 保留不匹配的（如wheat, div, gap）
    return mapped


def main():
    force = '--force' in sys.argv
    check_only = '--check-only' in sys.argv
    
    print("=" * 60)
    print("滚动窗口回测")
    print("=" * 60)
    
    state = load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    last_train = state.get("last_train_date")
    
    # 统计交易日
    trading_days = count_trading_days_since(last_train)
    state["trading_days_since_last"] = trading_days
    state["current_weights"] = scan_current_weights()
    
    print(f"\n上次训练: {last_train or '从未'}")
    print(f"距上次训练: {trading_days}个交易日")
    print(f"训练间隔: {TRAIN_INTERVAL}个交易日")
    
    should_train = force or (last_train is None and trading_days > 0) or trading_days >= TRAIN_INTERVAL
    
    if check_only:
        print(f"\n检查模式: {'✅ 需要重训' if should_train else '⏸️ 未到重训日'}")
        save_state(state)
        return
    
    if not should_train:
        print(f"\n⏸️  未到重训日 (还需 {TRAIN_INTERVAL - trading_days}个交易日)")
        save_state(state)
        return
    
    print(f"\n{'='*60}")
    print("开始滚动重训...")
    
    # 运行回测
    bt_weights = run_backtest()
    if bt_weights is None:
        print("  ❌ 回测失败，跳过权重更新")
        return
    
    # 映射并更新
    new_weights = map_backtest_to_analysis(bt_weights)
    print("\n回测完成，共 " + str(len(new_weights)) + " 个信号")
    
    update_weights_in_analysis(new_weights)
    
    # 更新状态
    state["last_train_date"] = today
    state["trading_days_since_last"] = 0
    state["history"].append({
        "date": today,
        "weights": new_weights,
        "samples": trading_days,
    })
    save_state(state)
    
    print(f"\n✅ 滚动重训完成 ({today})")
    print(f"  下次预计: ~{TRAIN_INTERVAL}个交易日后")


if __name__ == "__main__":
    main()
