"""
v6 路径常量与配置
所有模型/数据路径硬编码为 v6 本地，避免意外绑定 v5.1 路径。
"""

from pathlib import Path
from typing import Dict

# 根目录：corn/v6/
ROOT = Path(__file__).parent

# v6 模型副本目录
MODELS_DIR = ROOT / "models"

# v6 专用 ML 模型路径（独立于 v5.1）
MODEL_HIGH = MODELS_DIR / "model_high.pkl"
MODEL_LOW = MODELS_DIR / "model_low.pkl"
MODEL_NIGHT = MODELS_DIR / "model_night.pkl"

# v6 预测历史记录
HISTORY_DIR = ROOT / "history"
PREDICTIONS_JSON = HISTORY_DIR / "v6_predictions.json"

# v6 测试数据
TESTS_DIR = ROOT / "tests"

# ── 核心权重表（与 v5.1 一致） ──
CORE_WEIGHTS: Dict[str, float] = {
    "MA": 1.6,
    "RSI": 2.2,
    "BB": 1.8,
    "SOY": 0.8,
    "POLICY": 1.0,
    "CBOT": 0.5,
    "MACD": 0.5,
    "VOLUME": 0.3,
    "WEATHER_NORMAL": 1.0,
    "WEATHER_EXTREME": 2.0,
}

# 已淘汰信号（保留展示但不参与方向加权）
ELIMINATED_SIGNALS = [
    "seasonality", "cftc", "enso", "bci", "open_interest",
    "news_score", "usda_export", "hog", "import_cost", "ethanol",
    "port_basis",
]

# ML 模型版本标识
VERSION = "v6.0"

# HL 特征列顺序（与 train_hl_predictor.py 对齐）
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

# 夜盘基础特征列
NIGHT_BASE_COLS = [
    "price_chg", "rsi", "bb_position", "macd_hist",
    "vol_ratio", "seasonal", "close", "ma5", "ma10", "ma20",
]

# 夜盘扩展特征列（--extended 管道使用）
NIGHT_EXT_COLS = NIGHT_BASE_COLS + [
    "price_chg_abs", "bb_width_ratio", "bb_squeeze",
    "range_pct", "vol_surge", "consecutive_up", "consecutive_down",
    "ma5_slope", "ma20_slope", "candle_body_pct",
    "upper_shadow", "lower_shadow",
]
