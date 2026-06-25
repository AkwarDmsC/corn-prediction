# v6 Phase 4+5 完成报告

生成时间：2026-05-29 09:31 Asia/Shanghai

## Phase 4：独立 ML 模型

训练脚本：

- `v6/train_hl_predictor.py`
- `v6/train_night_predictor.py`

模型输出：

- `v6/models/model_high.pkl`
- `v6/models/model_low.pkl`
- `v6/models/model_night.pkl`

最后一次训练：

- Night：2026-05-29T09:30:46
- HL：2026-05-29T09:31:01

模型参数摘要：

- HL：`GradientBoostingRegressor(n_estimators=200, max_depth=4, learning_rate=0.05, min_samples_leaf=10, random_state=42)`，23 个 `HL_FEATURE_COLS` 特征，5195 个有效样本；High MAE 17.37，Low MAE 21.10。
- Night：`Ridge(alpha=1.0)` + `RandomForestRegressor(n_estimators=200, max_depth=6, min_samples_leaf=5, random_state=42)`，22 个 `NIGHT_EXT_COLS` 特征，5195 个有效样本；Ensemble MAE 3.45，Ridge 方向准确率 35.4%，RF 方向准确率 37.7%。

备注：训练脚本先通过 `v6/data.py` 获取 DCE 数据；本次执行时外部数据返回为空，因此使用本地 `dce_data_full.json` 归档完成兼容性训练。

## Phase 5：轻量模拟交易引擎

新增脚本：

- `v6/sim_trade.py`

交易日志：

- `v6/history/trading_log.json`

模拟账户初始化状态：

- 初始资金：100,000.00
- 当前权益：100,000.00
- 现金：100,000.00
- 持仓：0.0 手

交易记录数量：0

账户盈亏概述：当前 `v6/history/v6_predictions.json` 尚无已验证记录，因此回填交易日志为空，累计盈亏为 0.00。

## 验收清单

- ✅ `python3 v6/train_night_predictor.py` 成功训练并输出到 `v6/models/model_night.pkl`
- ✅ `python3 v6/train_hl_predictor.py` 成功训练并输出到 `v6/models/model_high.pkl` / `v6/models/model_low.pkl`
- ✅ `python3 v6/sim_trade.py --backfill` 成功生成 `v6/history/trading_log.json`
- ✅ `python3 v6/sim_trade.py --status` 成功显示账户摘要
- ✅ `python3 v6/tests/test_basic.py` 全部通过
- ✅ v6 预测模型加载逻辑保持兼容
