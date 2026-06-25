# 🌽 DCE 玉米期货预测系统

DCE 玉米主力合约方向预测系统，多版本管线并行，信号引擎 + ML 模型 + 模拟交易。

## 目录结构

```
corn/
├── v5/                # 初代信号引擎（历史管线，归档参考）
│   ├── analysis.py           # 日线分析
│   ├── predict_hl.py         # 高低价预测
│   ├── prediction_tracker.py # 预测追踪
│   ├── sim_trade.py          # v5 模拟交易
│   ├── news.py               # 新闻信号
│   ├── weather.py            # 天气信号
│   └── ...
├── v6/                # ML 模型管线
│   ├── pipeline.py           # 管线调度入口
│   ├── signals.py            # 信号计算
│   ├── predictor.py          # ML 预测器
│   ├── data.py               # 数据获取
│   └── sim_trade.py          # v6 模拟交易
└── v7/                # 当前主版本（信号优化引擎）
    ├── pipeline.py           # cron 调度入口
    ├── signals.py            # 技术指标 + 信号快照
    ├── decider.py            # 方向决策器（含 ML 预测）
    ├── data.py               # 外部数据获取
    ├── config.py             # v7.1 优化权重表
    ├── validate.py           # 预测验证 + 日报生成
    ├── retrain_models.py     # 模型重训
    └── backtest_v71.py       # 回测
```

## 信号体系

| 信号 | 权重 | 说明 |
|------|------|------|
| **MA** | 0.5 | 均线趋势方向 |
| **RSI** | 2.0 | 超买超卖 |
| **BB** | 1.5 | 布林带位置 |
| **SOY** | 0.8 | 豆粕共振 |
| **POLICY** | 1.0 | 政策事件 |
| **CBOT** | 0.5 | 隔夜外盘 |

已淘汰：MACD、VOLUME（独立预测能力低于随机，权重归零）。

## 数据源

- **行情**：akshare（DCE 玉米/豆粕日线，CBOT 连续合约）
- **天气**：Open-Meteo API（13 个玉米产区）
- **政策新闻**：东方财富搜索 + NewsAPI 备选
- **USDA 出口**：数据分析（日志记录）

## ML 模型

- **Night (Bidirectional)**：Ridge + RF ensemble，预测夜盘变化方向
- **High/Low**：独立模型预测次日高低价区间

## 模拟交易

- 基于信号 vs ML 做多空判断
- 带保证金风控、自动减仓机制
- 每交易日 15:30 待执行信号检查 + 资金健康检查

## 运行

### v7（当前版本）

```bash
cd corn
python3 -m v7.pipeline              # 规则引擎模式
python3 -m v7.pipeline --run-ml      # 带 ML 模型
python3 -m v7.pipeline --run-ml --save  # 保存预测记录
```

### cron 定时任务（通过 OpenClaw）

- 日盘 15:05 — v7 日盘分析（微信推送）
- 夜盘 23:05 — v7 夜盘分析（微信推送）
- 15:30 — 模拟交易待执行信号处理
- 15:35 — 模拟交易资金健康检查

## 依赖

```
python3.11+
akshare
pandas, numpy
scikit-learn  (ML 模型)
requests       (天气/新闻 API)
```

## 版本

| 版本 | 状态 | 特点 |
|------|------|------|
| v5 | 归档 | 初代信号引擎 |
| v6 | 维护 | ML 模型引入 |
| v7 | **当前** | 信号优化 + 决策器 + 权重校准 |
| v7.1 | 最新 | MACD/VOLUME 淘汰，做多因子补充 |

---

🐴 — 马劭聪的玉米预测项目
