# 🌽 中国玉米期货预测系统 v6 计划

> 生成日期: 2026-05-29
> 目录: `corn/v6/` — 所有 v6 文件独立于此目录，不修改 v5.1 的任何内容

---

## 设计理念

v5.1 经过大规模回测验证和过拟合修正后，核心结论是：

> **方向准确率瓶颈在 52% 左右（OOS ~51.8%），信号精简+趋势过滤比新增信号更有效。**

v6 不追求新增更多信号，而是改进**职责分离、可维护性、透明度、以及执行/验证链路**。

旧版 `analysis.py`（1858 行）承担了数据获取、信号计算、预测、追踪触发等全部职责。v6 将其拆为独立模块，每个模块只做一件事，可以在 v6 目录内独立测试。

---

## 架构概览

```
corn/v6/
├── PLAN.md                           ← 本计划
├── config.py                         ← v6 路径常量与配置（避免意外绑定 v5.1 路径）
├── pipeline.py                       ← 管线调度（cron入口，编排各模块）
├── data.py                           ← 数据获取层（akshare, Open-Meteo, NewsAPI, USDA…）
├── signals.py                        ← 信号计算层（只做信号，不做预测）
│   ⚠️ 不叫 signal.py（会 shadow Python 标准库 signal 模块）
├── predictor.py                      ← 预测层（规则信号→日盘预测 + ML：夜盘/HL）
├── formatter.py                      ← 输出格式化（文本兼容 v5.1 + 结构化 JSON）
│   ⚠️ 不叫 format.py（语义模糊）
├── validate.py                       ← 验证层（结构化 JSON 记录，不用文本正则解析）
├── models/                           ← ML 模型副本（.pkl，v6 专用，独立于 v5.1）
│   ├── model_high.pkl
│   ├── model_low.pkl
│   └── model_night.pkl
├── tests/                            ← 测试数据与脚本
│   └── test_sample.csv
└── analysis_v6.py                    ← 已有编排层（Phase 1 后作为兼容 facade，底层 import 新模块）
    model_consolidation_report.md     ← 已有模型分析报告
```

---

## 阶段规划

### Phase 0 ✅ — 已完成

- `analysis_v6.py`：统一编排层，不主动抓取网络
- `model_consolidation_report.md`：模型分析与设计报告
- 特征口径修复（HL `range_momentum`、夜盘 `bb_position`）
- MA60 趋势过滤 + 精简 9 核心信号

### Phase 1 — 模块化拆分（P0）

将 `analysis_v6.py` 中的职责拆分为独立模块。注意：不要机械地逐行拆分代码。**先定义模块契约（输入/输出schema），再提取实现。**

#### 子任务优先级（按依赖顺序）

1. **`config.py`** — v6 路径常量（`v6/models/*.pkl` 硬编码指向），避免意外绑定 v5.1 路径
2. **定义结构化契约** — 信号快照、预测结果、模型错误、历史记录的输入/输出 schema
3. **`signals.py`** — 提取 `prepare_corn_df`、`sma`、`rsi`、`macd_hist`、`_bb_position`、`compute_signal_snapshot`
4. **`predictor.py`** — 提取特征构建 + ML 模型加载/预测 + `analyze_corn_v6` 编排逻辑
   - 分离特征构建与模型加载（无 .pkl 文件也可测试前端逻辑）
5. **`formatter.py`** — 纯输出渲染，添加 JSON 序列化作为一等目标
6. **`pipeline.py`** — 薄层 CLI 入口，只编排不包含业务逻辑
7. **`tests/`** — 用静态样本 DataFrame 添加轻量测试
8. **`validate.py`** — 结构化 JSON 记录 + 验证 + 偏差归因（最后构建，因为依赖前面所有模块）

#### 文件对照

| # | 模块 | 文件 | 职责 | 依赖 |
|---|------|------|------|------|
| 1 | 配置 | `config.py` | v6 本地路径、核心权重常量 | 无 |
| 2 | 数据获取 | `data.py` | akshare 获取 DCE/CBOT/豆粕/豆油/原油 + Open-Meteo 天气 + NewsAPI | 无 |
| 3 | 信号计算 | `signals.py` | 从 DataFrame 计算 MA/RSI/BB/MACD/成交量/大豆共振/CBOT/政策/天气 | `data.py` |
| 4 | 预测生成 | `predictor.py` | 规则方向 + 区间 + ML 模型（夜盘/HL） | `signals.py` |
| 5 | 格式输出 | `formatter.py` | 文本（兼容 v5.1）+ 结构化 JSON | `predictor.py` |
| 6 | 管线调度 | `pipeline.py` | cron 入口，编排 data/signals/predictor/formatter/validate | 全部 |
| 7 | 验证追踪 | `validate.py` | 结构化 JSON 记录 + 次日验证 + 偏差归因（不用文本正则） | 预测 JSON |

**验收标准**：
- 每个模块可以独立 import 和测试
- `pipeline.py` 能串联所有模块生成完整预测
- `analysis_v6.py` 作为兼容 facade 仍然可用（底层 import 新模块）
- 输出格式与 v5.1 `format_v51_output` 一致

### Phase 2 — 预测验证独立化（P1）

- `validate.py` 独立管理预测记录（JSON 格式，不依赖旧 `predictions.md` 和 `prediction_tracker.py`）
- 记录格式：`corn/v6/history/v6_predictions.json`
- 验证流程：次日自动读取 DCE 收盘 → 匹配预测 → 计算偏差 → 归因

**验收标准**：
- 独立跑通一次"预测→记录→验证→归因"全链路
- 输出一份 v6 的历史方向准确率和 MAE 统计

### Phase 3 — cron 对接（P2）

- v6 的 cron 任务独立于 v5.1 的 cron（并行运行）
- `pipeline.py` 可作为 cron isolated session 的入口：`python3 v6/pipeline.py`
- 输出：一条 v6 格式的文本预测 + v6_predictions.json 追加

**验收标准**：
- cron isolated session 能成功运行 pipeline.py 并输出预测
- v5.1 的 cron 不受影响

### Phase 4 — 独立 ML 模型（P3，可选）

- 在 `corn/v6/models/` 下保留 v6 专用的 ML 模型副本
- v5.1 的模型训练脚本可以产出一份副本放入此目录
- 用途：v6 不依赖旧目录的 `.pkl`，完全独立

### Phase 5 — 策略交易模块（P4，可选）

- 在 v6 目录内实现独立的模拟交易引擎（参考 `backtest_strategy.py` 但更轻量）
- 与 `validate.py` 集成：用实际验证结果驱动交易信号

---

## v6 与 v5.1 的差异总结

| 维度 | v5.1 | v6 |
|------|------|----|
| 文件结构 | 单体 `analysis.py` + 分散的工具脚本 | 模块化：data/signal/predictor/format/validate |
| 数据获取 | 内嵌在 `analysis.py` 中 | `data.py` 独立模块 |
| 信号计算 | 内嵌 | `signal.py` 独立模块 |
| ML 模型 | 引用旧目录 `.pkl` | v6/models/ 下独立副本 |
| 预测追踪 | `prediction_tracker.py` + `predictions.md` | `validate.py` + `v6_predictions.json` |
| ML 输出 | 仅在文本中嵌入 | 结构化字典，可序列化为 JSON |
| 输出兼容性 | 自成一派 | 输出兼容 v5.1 格式 + 额外 JSON |
| 运行方式 | cron 直调 `analysis.py` | cron 调 `pipeline.py` |
| 与旧版关联 | 依赖旧版工具脚本 | 完全独立（模型副本+独立追踪） |

---

## 当前状态

| 文件 | 状态 | 说明 |
|------|------|------|
| `config.py` | ✅ Phase 1 | 路径常量 + 权重表 + 特征列表 |
| `schemas.py` | ✅ Phase 1 | 结构化 dataclass 契约 |
| `signals.py` | ✅ Phase 1 | 信号计算（277 行） |
| `predictor.py` | ✅ Phase 1 | 预测编排 + ML（346 行） |
| `formatter.py` | ✅ Phase 1 | 文本/JSON 输出（146 行） |
| `pipeline.py` | ✅ Ph1+2+3 | CLI 管线入口，已集成 data.py |
| `validate.py` | ✅ Phase 1 | 结构化 JSON 验证追踪 |
| `analysis_v6.py` | ✅ Phase 1 | 兼容 facade（65 行） |
| `tests/test_basic.py` | ✅ Phase 1 | 6 项冒烟测试 |
| `models/*.pkl` | ✅ Phase 1 | 3 个 v6 专用模型副本 |
| `data.py` | ✅ Phase 2 | 数据获取层（Codex 编写） |
| cron（日盘 15:05） | ✅ Phase 3 | 已注册 `玉米v6日盘分析` |
| cron（夜盘 23:05） | ✅ Phase 3 | 已注册 `玉米v6夜盘分析` |

Phase 1 Codex 审查（2026-05-29）：4 项发现已修复（`_bb_position` 重复代码删除、validate 置信度路径修正、测试退出码）

---

## Codex Review 反馈（2026-05-29）

Codex 已对 v6 计划进行了代码级审查。完整评价见：`../.codex-queue/results/review-v6-plan.final.md`

### 核心反馈（已纳入计划）

| 反馈项 | 处理 |
|--------|------|
| `signal.py` 会 shadow Python 标准库 `signal` 模块 | 已改名为 `signals.py` |
| `format.py` 语义模糊 | 已改名为 `formatter.py` |
| 缺少 `config.py`/`paths.py`，v6 模型路径可能意外绑定 v5.1 | 已加入 Phase 1 子任务首项 |
| 先定义契约再提取代码，不要机械拆分行 | 已体现在子任务优先级中 |
| `pipeline.py` vs `analysis_v6.py` 可能成为两个竞争编排器 | `analysis_v6.py` 作为兼容 facade，底层 import 新模块 |
| `validate.py` 应用结构化 JSON 记录，不用文本正则解析 | 已体现在 `validate.py` 设计中 |
| Phase 1 应保护行为等价性（regression tests），不试图改进预测逻辑 | 已体现在测试阶段和 OOS 边缘认知中 |
| `analysis_v6.py` 中 `SessionSnapshot` 和 `math` 未使用 | 清理留待 Phase 1 实施时处理 |
| 信号快照的输出 schema 应显式定义 | 已列入子任务 #2 |

### 依赖确认

`numpy`, `pandas`, `scikit-learn`, `akshare`, `requests`, `bs4`, `lxml`, `joblib` 均已安装。
