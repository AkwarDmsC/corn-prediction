#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国玉米期货综合预测编排层 v6.0 — 兼容 facade

v6 Phase 1 完成后，所有核心逻辑已迁移至独立模块：
  - signals.py：信号计算
  - predictor.py：预测生成（规则+ML）
  - formatter.py：输出格式化
  - config.py：配置/路径常量
  - schemas.py：结构化契约

本文件保留为向后兼容入口，底层全部委托给新模块。
调用方 import 方式不变：
  from analysis_v6 import analyze_corn_v6, format_v51_output
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 从 v6 子模块重新导出（兼容 facade） ──
from config import (
    CORE_WEIGHTS, VERSION, ELIMINATED_SIGNALS,
    HL_FEATURE_COLS, NIGHT_BASE_COLS, NIGHT_EXT_COLS,
)
from signals import (
    prepare_corn_df, sma, rsi, macd_hist,
    compute_signal_snapshot,
    next_trading_day, _direction_label, _confidence,
)
from predictor import (
    analyze_corn_v6,
    _scenario_from_score,
    compute_hl_features_v6, predict_hl_v6,
    compute_night_features_v6, predict_night_v6,
)
from formatter import format_v51_output, format_json_output

# 所有信号计算委托给 signals.py，参见 _bb_position 在 signals 中


__all__ = [
    "analyze_corn_v6",
    "format_v51_output",
    "format_json_output",
    "compute_signal_snapshot",
    "compute_hl_features_v6", "predict_hl_v6",
    "compute_night_features_v6", "predict_night_v6",
    "prepare_corn_df",
    "sma", "rsi", "macd_hist",
    "CORE_WEIGHTS", "VERSION",
]
