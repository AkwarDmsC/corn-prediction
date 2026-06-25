#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目全局常量 v1.0
所有文件路径、模型路径、状态缓存、分析参数统一管理
"""
from pathlib import Path

# ── 根目录 ──
ROOT = Path(__file__).parent

# ═══════════════════════════════════════════════
# 数据文件
# ═══════════════════════════════════════════════
DCE_DATA = ROOT / "dce_data_full.json"
NIGHT_FEATURES = ROOT / "night_session_features.csv"
WEATHER_CACHE = ROOT / "weather_cache.json"

# ═══════════════════════════════════════════════
# 模型文件
# ═══════════════════════════════════════════════
MODEL_HIGH = ROOT / "model_high.pkl"
MODEL_LOW = ROOT / "model_low.pkl"
MODEL_NIGHT = ROOT / "model_night.pkl"
MODEL_BASELINE = ROOT / ".model_baseline.json"

# ═══════════════════════════════════════════════
# 输出/日志文件
# ═══════════════════════════════════════════════
PREDICTIONS = ROOT / "predictions.md"
NEWS_LOG = ROOT / "news_log.md"
DASHBOARD_DATA = ROOT / "_archived_corn" / "dashboard_data.json"

# ═══════════════════════════════════════════════
# 状态缓存（点文件）
# ═══════════════════════════════════════════════
NEWS_DEDUP_CACHE = ROOT / ".news_dedup_cache.txt"
NEWS_IMPACT_DB = ROOT / ".news_impact_db.json"
TRACKER_STATE = ROOT / ".tracker_state.json"
CALIBRATION = ROOT / ".weight_calibration.json"

# ═══════════════════════════════════════════════
# 分析参数
# ═══════════════════════════════════════════════
TIMEOUT_SECONDS = 30
DAYS_BACK_NEWS = 10       # 新闻搜索回溯天数（主分析）
DAYS_BACK_NEWS_SHORT = 7  # 新闻搜索回溯天数（图表用）

# ═══════════════════════════════════════════════
# 主分析脚本路径（prediction_tracker调用的外部脚本）
# ═══════════════════════════════════════════════
ANALYSIS_SCRIPT = str(ROOT / "analysis.py")
