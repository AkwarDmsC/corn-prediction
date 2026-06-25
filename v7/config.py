"""
v7 路径常量与配置
独立于 v5/v6，所有路径指向 v7 本地。
"""

from pathlib import Path

# 根目录
ROOT = Path(__file__).parent

# 模型目录（软链到 v6/models/）
MODELS_DIR = ROOT / "models"

# ML 模型路径
MODEL_HIGH = MODELS_DIR / "model_high.pkl"
MODEL_LOW = MODELS_DIR / "model_low.pkl"
MODEL_NIGHT = MODELS_DIR / "model_night.pkl"

# 预测历史
HISTORY_DIR = ROOT / "history"
PREDICTIONS_JSON = HISTORY_DIR / "v7_predictions.json"

# ── 核心权重表（v7.1 优化版，2026-06-25 回溯校准） ──
# 优化方法：signal_optimization.json + signal_regime_analysis.json
# 核心结论：
#   1. 只有 RSI/BB 接近 50% 独立预测能力（布林 50.3%、RSI 50.2%）
#   2. MACD(47.9%) / VOL(46.7%) 任何区间均低于随机，淘汰
#   3. MA(48.1%) 略低于随机，只在均线多头/空头排列时有用，大幅降权
#   4. 做多因子（布林下轨反弹/连跌/跳空低开）在 2023-2026 近 3 年全部失效，
#      做多准确率 44.5%/44.4%/55.6%，低于自然上涨概率 48.0%
#      根源：玉米 2023-2026 为震荡偏空行情，均值回归不成立
#   5. 方向 bullish/bearish 偏差是市场状态导致的，非信号体系问题
CORE_WEIGHTS: dict = {
    "MA": 0.5,                # 1.6→0.5（大幅降权，48.1% 低于随机）
    "RSI": 2.0,               # 2.2→2.0（50.2%，保留最优权重）
    "BB": 1.5,                # 1.8→1.5（50.3%，微调）
    "SOY": 0.8,               # 不变
    "POLICY": 1.0,            # 不变（事件驱动）
    "CBOT": 0.5,              # 不变
    "MACD": 0.0,              # 0.5→0.0（47.9%，淘汰）
    "VOLUME": 0.0,            # 0.3→0.0（46.7%，淘汰）
    "BB_LOWER_TOUCH": 0.3,    # 新增但低权重（近 3 年失效，仅做尾部补充）
    "CONSECUTIVE_DOWN": 0.3,  # 同上
    "GAP_DOWN": 0.3,          # 同上
    "WEATHER_NORMAL": 1.0,
    "WEATHER_EXTREME": 2.0,
}

# ── 方向决策参数（v7.1 优化版，2026-06-25 校准） ──
# 核心观察：2023-2026 中位数 filtered_consistency = 0.0
# 活跃信号平均 2-3 个（MA/RSI/BB + 做多因子），多数情况下一致性为 0
# 震荡熔断阈值保持适当严格，宁愿不做方向判断也不要错误信号。
# 关键改进：delta_threshold 控制信号必须在特定容忍窗口内才能出方向。
DIRECTION_THRESHOLD = 0.15       # 保持不变（避免在震荡市强出方向）
LOW_AGREEMENT_CUTOFF = 2         # 4→2（同步活跃信号减少）
LOW_CONSISTENCY_CUTOFF = 0.10    # 0.20→0.10（微调放宽）

# 夜盘 CBOT 权重（vs v5: 0.4, 提升到 1.5）
NIGHT_CBOT_WEIGHT = 1.5

# ── 已淘汰信号 ──
ELIMINATED_SIGNALS = [
    "seasonality", "cftc", "enso", "bci", "open_interest",
    "news_score", "usda_export", "hog", "import_cost", "ethanol",
    "port_basis",
]

VERSION = "v7.1"

# HL 特征列
HL_FEATURE_COLS = [
    "open", "high", "low", "close", "volume",
    "ma5", "ma10", "ma20", "ma60",
    "price_vs_ma5", "price_vs_ma20",
    "bb_position", "rsi14", "atr14",
    "macd_hist", "macd_signal",
    "vol_ratio", "daily_range", "daily_range_pct",
    "chg_pct", "open_close_spread", "range_momentum",
    "month",
]

# 夜盘特征列
NIGHT_BASE_COLS = [
    "price_chg", "rsi", "bb_position", "macd_hist",
    "vol_ratio", "seasonal", "close", "ma5", "ma10", "ma20",
]

NIGHT_EXT_COLS = NIGHT_BASE_COLS + [
    "price_chg_abs", "bb_width_ratio", "bb_squeeze",
    "range_pct", "vol_surge", "consecutive_up", "consecutive_down",
    "ma5_slope", "ma20_slope", "candle_body_pct",
    "upper_shadow", "lower_shadow",
]
