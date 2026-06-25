# 玉米预测模型汇总与优化报告

生成日期: 2026-05-28

## 1. 阅读范围

本次只做本地静态代码分析和设计落地，未发送网络请求，未重训模型，未执行需要加载大数据或模型推理的流程。

已重点阅读:

- `framework.md` v5.1
- `analysis.py`
- `predict_hl.py`
- `predict_night.py`
- `train_hl_predictor.py`
- `retrain_night.py`
- `prediction_tracker.py`
- `backtest_weights.py`
- `oos_validation_report.md`
- `strategy_backtest_report.md`
- `optimization_report.md`

输出文件:

- `analysis_v6.py`
- `model_consolidation_report.md`

## 2. 当前模型架构汇总

| 模型/脚本 | 定位 | 输入 | 输出 | 优点 | 主要问题 |
|---|---|---|---|---|---|
| `analysis.py` | 日盘主分析脚本 | DCE日线、外盘、天气、新闻、辅助基本面 | v5.1 文本预测、日/夜盘区间 | 指标完整、可解释性强、与预测记录链路兼容 | 入口脚本承担数据获取、信号计算、展示、追踪触发，职责过重；部分低效信号仍有展示噪音；基准/预测语义容易混淆 |
| `predict_hl.py` | 次日高低价 ML | DCE日线特征 | 次高、次低、元数据 | 直接给交易区间边界；特征与训练管道大体一致 | `range_momentum` 当前为 `daily_range.diff()`，与训练脚本 `rolling(5).mean()` 不一致，会造成训练/推理口径漂移 |
| `predict_night.py` | 夜盘变化 ML | 日线衍生特征 | 夜盘变化、方向、置信度 | Ridge 负责方向、RF/ensemble 负责幅度，结构合理；支持扩展特征 | `bb_position` 当前为 0~1，而 `retrain_night.py --extended` 训练口径是 0~100；真实夜盘样本仅约14条，代理目标仍占主导 |
| `train_hl_predictor.py` | 高低价训练 | `dce_data_full.json` | `model_high.pkl`、`model_low.pkl` | 特征清晰，保存训练基线 | 与线上 `predict_hl.py` 存在 `range_momentum` 口径差异 |
| `retrain_night.py` | 夜盘重训 | 日线特征、5分钟真实夜盘样本 | `model_night.pkl` | 有真实样本门槛、方向不对称模型思路、扩展特征 | 当前真实样本不足，重训结论不稳定；文档描述15维，实际扩展为22维附近，需统一说明 |
| `prediction_tracker.py` | 预测追踪验证 | `analysis.py` 输出、DCE日线/分钟线 | pending/verified 状态、归因数据 | 能把日盘和夜盘预测落地验证 | 解析依赖文本格式，输出格式变化会影响追踪，需要保留 v5.1 样式字段 |
| `backtest_weights.py` / `backtest_oos.py` | 权重和样本外验证 | 长周期日线和辅助信号 | 权重表现、OOS准确率 | 提供过拟合约束和长期稳定性判断 | OOS 方向准确率约51.8%-51.9%，说明方向优势很薄，不能过度复杂化 |
| `backtest_strategy.py` / `optimize_strategy.py` | 交易策略验证 | 方向信号、费用滑点 | 年化、夏普、回撤、盈亏比 | 直接暴露实盘可用性问题 | 全段基准策略表现差；优化后主要改善回撤和大波幅亏损，不是显著提升方向胜率 |

## 3. 日盘、夜盘、高低价模型差异

### 日盘预测

日盘模型以规则信号为核心，主要依赖 MA、RSI、布林带、MACD、成交量、豆粕共振、政策、CBOT、天气。优势是可解释、可手工审计、能接入政策和天气等非价格变量。缺点是方向准确率只有轻微统计优势，且外部信号质量不稳定。

v5.1 的关键改进不是增加信号，而是减少信号并加入趋势过滤。优化报告显示，精简信号+趋势过滤牺牲部分年化，但显著降低回撤，并把大波幅交易的亏损转为小幅正收益。

### 夜盘预测

夜盘模型是独立 ML 管道，使用 Ridge + RF ensemble。Ridge 用于方向，RF/ensemble 用于幅度。它适合捕捉日线状态对夜盘的短期延续或反转，但目前真实夜盘样本少，仍高度依赖代理目标，因此不能直接替代日盘规则模型。

夜盘模型最适合在综合层作为独立子预测输出，并叠加 CBOT 传导修正，而不是与日盘信号简单平均。

### 高价/低价预测

高低价模型关注次日区间边界，不直接负责方向判断。它的价值是给日盘/夜盘预测提供可交易的风险边界，尤其用于修正规则模型区间过窄或过宽的问题。

当前最大问题是推理脚本和训练脚本的 `range_momentum` 口径不一致。v6 已在新编排层中修正为训练口径 `daily_range.rolling(5).mean()`。

## 4. 方向准确率瓶颈

1. 长周期 OOS 方向准确率约51.8%-51.9%，优势很薄，说明大部分新增信号只会带来噪音或过拟合。
2. 大波幅行情是主要亏损来源。优化报告显示，基准模型在大波幅交易中亏损严重，趋势过滤和精简信号能显著改善。
3. 因子有效性随时间变化明显。OOS报告中多数信号权重跨窗口波动较大，固定全局权重只能保守使用。
4. 外部基本面信号的时效和可验证性不一致。季节性、CFTC、ENSO、BCI、持仓量等在 v5.1 已被降权或淘汰，应保留展示但不参与方向加权。
5. 夜盘真实样本不足。14条真实样本不足以支持可靠的方向不对称模型重训，需要至少30条以上再启用重训结论。

## 5. 过拟合评估

`oos_validation_report.md` 显示:

- OOS 方向准确率: 51.8%
- 全量回测方向准确率: 53.3%
- IS-OOS 差距: -1.5pp
- 判断: 轻微过拟合

这意味着 v6 不应继续增加复杂信号，也不应把辅助基本面全部重新纳入方向加权。更合理的方案是:

- 保留 v5.1 精简核心信号。
- 使用 MA60 趋势过滤禁止逆势方向贡献。
- 保留所有可观测指标为展示字段和归因字段。
- 让高低价模型只负责区间边界，让夜盘 ML 只负责夜盘子预测。

## 6. v6 综合版本设计

`analysis_v6.py` 定位为新综合编排层，不替换旧脚本，不主动抓取网络数据。调用方传入 `corn_df`，可选传入 `soy_df`、`cbot_chg`、`weather_score`、`policy_signal`、日盘/夜盘当前快照。

核心函数:

- `analyze_corn_v6(corn_df, ...)`
- `format_v51_output(result)`
- `compute_signal_snapshot(...)`
- `compute_hl_features_v6(...)`
- `compute_night_features_v6(...)`
- `predict_hl_v6(...)`
- `predict_night_v6(...)`

### v6 已实现的修正

1. 统一入口  
   `analyze_corn_v6` 返回结构化字典，包含 `day`、`night`、`hl`、`signal`、`full_day_range`、`model_errors`。

2. 不主动抓取网络  
   v6 编排层只消费调用方传入的数据。`run_ml=True` 时会加载本地 pickle 模型，但不会抓取外部数据。

3. 精简信号和最优权重  
   保留 v5.1 核心权重:
   - MA 1.6
   - RSI 2.2
   - BB 1.8
   - 大豆共振 0.8
   - 政策 1.0动态
   - CBOT 0.5
   - MACD 0.5
   - 成交量 0.3
   - 天气 1.0/2.0动态

4. MA60趋势过滤  
   v6 将逆 MA60 趋势的有效信号贡献归零，并保留原始信号和 `disabled_by_trend`，便于归因。它不再使用“逆势减半”。

5. `predict_hl` 特征口径修复  
   v6 的 `compute_hl_features_v6` 使用 `range_momentum = daily_range.rolling(5).mean()`，与 `train_hl_predictor.py` 对齐。

6. `predict_night` 特征口径修复  
   v6 的 `compute_night_features_v6` 使用 `bb_position` 0~100，和 `retrain_night.py` 训练口径对齐。

7. 基准输出语义修复  
   v6 输出中 `base` 明确表示预测起点价格，`pred` 表示基准预测价；格式化文本中写作“起点 / 基准 / 乐观 / 悲观”。

8. v5.1 输出兼容  
   `format_v51_output` 保留日盘、夜盘、全日区间、置信度、信号冲突、关键价位、季节性淘汰、偏差归因占位等字段，便于 `prediction_tracker.py` 后续解析。

## 7. 建议的后续优化

1. 将 `analysis.py` 的数据获取层与信号层逐步解耦，未来可让旧入口调用 `analysis_v6.py`。
2. 夜盘真实样本达到30条后，再运行 `retrain_night.py --extended --split` 并更新本报告中的结论。
3. `prediction_tracker.py` 可增加结构化 JSON 输入支持，减少对中文文本正则的依赖。
4. 对 MA60 过滤做一次小规模 walk-forward 验证，比较“分量归零”和“总分归零”的差异。
5. 保留被淘汰信号的展示和归因，但不要重新纳入方向权重，除非 OOS 能证明新增收益。

## 8. 结论

当前最佳综合版本不是更复杂的模型，而是一个职责清晰的编排层:

- 日盘方向由 v5.1 精简规则信号决定。
- MA60 过滤负责降低大趋势逆向风险。
- 夜盘由独立 ML 子模型输出，并叠加 CBOT 调整。
- 高低价 ML 只负责区间边界。
- 低稳定性信号保留观测，不参与方向加权。

这就是 `analysis_v6.py` 的设计原则。
