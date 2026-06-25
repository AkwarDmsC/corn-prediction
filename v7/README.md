# v7 — 玉米预测系统重构

## 目标

解决 v5 方向准确率持续低迷（21%）的根本问题，建立基于分钟 K 线实时验证的震荡市友好系统。

## 架构（继承 v6 模块化设计 + 关键改进）

```
v7/
├── pipeline.py          ← cron 入口（调度管线）
├── data.py              ← 数据获取（分钟K线优先，akshare fallback）
├── signals.py           ← 信号计算层（9个独立信号，同v6）
├── decider.py           ← 方向决策器 ← **核心新模块**
│   ├── day_pipeline()   → 日盘决策
│   └── night_pipeline() → 夜盘决策（CBOT权重提升）
├── formatter.py         ← 输出格式化
├── validate.py          ← 验证器（分钟K线优先）
├── config.py            ← 常量配置
└── REFACTOR_PLAN.md
```

## 核心改进

### 1. 方向阈值提升（decider.py）
- 信号一致性阈值 `0.05 → 0.15`
- 新增震荡市熔断：同向信号 < 4 个且一致性 < 0.2 时强制输出震荡/中性

### 2. 日盘/夜盘管线分离
- 日盘：规则引擎 + 震荡市熔断 + 均线强制方向
- 夜盘：CBOT 传导权重提升，ML 模型（predict_night.py）优先

### 3. 分钟K线优先验证
- `validate.py` 优先从分钟 K 线（`ak.futures_zh_minute_sina`）获取夜盘收盘
- 仅当分钟 K 线无数据时回退 akshare 日 K 线

## 实施顺序

1. `config.py` — 常量定义
2. `signals.py` — 从 v6 复制，不变
3. `decider.py` — 新写，核心改进
4. `data.py` — 从 v6 复制，分钟K线增强
5. `formatter.py` — 从 v6 复制
6. `validate.py` — 从 v6 复制，分钟K线优先
7. `pipeline.py` — cron 入口

## 与 v5/v6 的关系

- v7 是**独立目录**，不依赖 v5 代码
- v7 的模型文件软链到 v6 的 `v6/models/`
- v7 的预测记录写入 `v7/history/v7_predictions.json`
- v5 cron 继续运行不受影响（并行验证期）

## 准确率验证
- 并行运行 v5 + v7 至少 1 周
- 对比方向准确率、区间命中率
- v7 确认优于 v5 后切换 cron
