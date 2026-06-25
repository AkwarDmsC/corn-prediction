"""
v6 结构化契约（输入/输出 Schema）

所有模块之间通过本文件定义的 schema 交换数据。
不依赖 dict key 字符串约定；调用方和实现方都引用本文件。

变更 schema 需同时更新 config.py 中的相关常量、以及 signals/predictor/formatter/validate 的实现。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ════════════════════════════════════════════
# 信号快照（signals.py -> predictor.py）
# ════════════════════════════════════════════


@dataclass
class Indicators:
    """技术指标快照"""
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    ma_dir: int = 0               # 1=多头排列, -1=空头排列, 0=混乱
    ma60_trend: int = 0           # 1=上升, -1=下降, 0=横盘
    rsi14: Optional[float] = None
    bb_position: float = 50.0     # 0~100
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None
    macd_hist: Optional[float] = None
    vol_ratio: float = 1.0
    recent_vol: float = 20.0      # 近20日收盘价标准差
    soy_corr: Optional[float] = None


@dataclass
class SignalEntry:
    """单个信号源的状态"""
    value: float = 0.0            # 信号方向/强度
    weight: float = 1.0           # 权重
    disabled_by_trend: bool = False  # 是否被 MA60 趋势过滤禁用


@dataclass
class SignalSnapshot:
    """完整的信号快照（compute_signal_snapshot 的输出）"""
    version: str = ""
    date: str = ""
    close: float = 0.0
    change: float = 0.0
    indicators: Indicators = field(default_factory=Indicators)
    signals: Dict[str, SignalEntry] = field(default_factory=dict)       # 全部原始信号
    effective_signals: Dict[str, SignalEntry] = field(default_factory=dict)  # 趋势过滤后
    disabled_by_trend: List[str] = field(default_factory=list)
    raw_weighted_sum: float = 0.0
    weighted_sum: float = 0.0
    consistency: float = 0.0          # -1~+1 原始一致性
    filtered_consistency: float = 0.0  # -1~+1 过滤后一致性
    trend_filter: str = "none"
    confidence: str = "🔴低"
    confidence_pct: str = "0%"
    conflicts: List[str] = field(default_factory=list)
    eliminated_signals: List[str] = field(default_factory=list)
    next_trading_day: str = ""


# ════════════════════════════════════════════
# 预测结果（predictor.py -> formatter.py / validate.py）
# ════════════════════════════════════════════


@dataclass
class Scenario:
    """单一场景（日盘/夜盘）的预测"""
    base: float = 0.0    # 预测起点价格
    pred: float = 0.0    # 基准预测价
    low: float = 0.0     # 区间下界
    high: float = 0.0    # 区间上界
    change: float = 0.0
    change_pct: float = 0.0
    direction: str = "↔ 震荡"

    # 夜盘 ML 特有字段（可选）
    ridge_pred: Optional[float] = None
    rf_pred: Optional[float] = None
    ensemble_pred: Optional[float] = None
    ml_change: Optional[float] = None
    cbot_adj: Optional[float] = None


@dataclass
class HLResult:
    """高价/低价 ML 预测"""
    pred_high: float = 0.0
    pred_low: float = 0.0
    range: float = 0.0
    feature_fix: str = ""


@dataclass
class PredictionResult:
    """完整预测结果"""
    version: str = ""
    generated_at: str = ""
    input_date: str = ""
    next_trading_day: str = ""
    signal: Optional[SignalSnapshot] = None
    day: Optional[Scenario] = None
    night: Optional[Scenario] = None
    hl: Optional[HLResult] = None
    full_day_range: Dict[str, float] = field(default_factory=lambda: {"low": 0.0, "high": 0.0})
    model_errors: Dict[str, str] = field(default_factory=dict)
    output_contract: str = ""


# ════════════════════════════════════════════
# 验证记录（validate.py）
# ════════════════════════════════════════════


@dataclass
class VerificationRecord:
    """单条验证记录"""
    pred_date: str = ""           # 预测日期
    next_date: str = ""           # 验证日期（下一交易日）
    session: str = ""             # "day" 或 "night"
    pred_base: float = 0.0
    pred_close: float = 0.0
    pred_low: float = 0.0
    pred_high: float = 0.0
    actual_close: float = 0.0
    deviation: float = 0.0       # actual - pred
    deviation_pct: float = 0.0
    range_hit: bool = False      # 实际收盘是否在区间内
    direction_correct: bool = False  # 方向判断是否正确
    confidence: str = ""
    signal_consistency: float = 0.0
    attribution: str = ""        # 偏差归因文本
    source: str = ""             # 来源描述

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pred_date": self.pred_date,
            "next_date": self.next_date,
            "session": self.session,
            "pred_base": self.pred_base,
            "pred_close": self.pred_close,
            "pred_low": self.pred_low,
            "pred_high": self.pred_high,
            "actual_close": self.actual_close,
            "deviation": self.deviation,
            "deviation_pct": round(self.deviation_pct, 4),
            "range_hit": self.range_hit,
            "direction_correct": self.direction_correct,
            "confidence": self.confidence,
            "signal_consistency": round(self.signal_consistency, 4),
            "attribution": self.attribution,
            "source": self.source,
        }


# ════════════════════════════════════════════
# 错误/回退
# ════════════════════════════════════════════


@dataclass
class ModelError:
    """单个模型的错误信息"""
    name: str = ""
    action: str = ""   # 回退方式
    message: str = ""
