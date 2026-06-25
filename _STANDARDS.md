# 🌽 corn 项目统一规范

## 命名规范

| 类别 | 规范 | 示例 |
|------|------|------|
| Python脚本 | 小写+下划线，功能名词 | `news.py`, `weather.py` |
| 文档文件 | 小写+下划线，`.md` | `framework.md`, `predictions.md` |
| 数据文件 | 扩展名清晰 | `dce_data_full.json`, `night_session_features.csv` |
| 模型文件 | `model_<用途>.pkl` | `model_high.pkl`, `model_low.pkl` |
| 缓存/状态 | `.name.json` 点文件 | `.news_impact_db.json`, `.tracker_state.json` |
| 日志文件 | `*_log.md` | `news_log.md` |

## 文件头注释模板

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
<模块名称> v<主版本>.<次版本>
<一行用途说明>
"""
```

## 版本号规范

- **主版本**: 不兼容变更（数据源切换、框架重构）
- **次版本**: 向下兼容（功能新增、优化）
- 所有文件的版本号统一写在文件头的 docstring 中
- 内部注释不再标注版本号（由 git 历史管理）

### 当前版本映射
| 文件 | 当前版本 |
|------|---------|
| `analysis.py` | v5.0 |
| `news.py` | v3.0 |
| `weather.py` | v1.0 |
| `predict_hl.py` | v1.0 |
| `predict_night.py` | v1.0 |
| `prediction_tracker.py` | v4.0 |
| `train_hl_predictor.py` | v1.0 |
| `update_dashboard.py` | v1.0 |
| `news_impact.py` | v1.0 |
| `framework.md` | v5.0 |

## 日志输出规范

格式：`[模块名] 内容`

```
[分析] 正在获取DCE玉米数据...
[新闻] 找到 20 条相关新闻
[天气] 产区天气评分: -0.4 (轻微利空)
[预测] 日盘区间: 2320-2360 元/吨
[影响] 新增 5 条新闻影响记录 (共 30 条)
⚠️  获取超时: fetch_dce_corn (30秒)
✅  预测记录已保存
```

前缀对照表：
| 前缀 | 含义 |
|------|------|
| `[分析]` | 主分析流程 |
| `[新闻]` | 政策新闻模块 |
| `[天气]` | 产区天气模块 |
| `[预测]` | 预测生成 |
| `[验证]` | 预测验证/回测 |
| `[影响]` | 新闻影响学习 |
| `[模型]` | 模型训练 |
| `[数据]` | 数据获取 |
| `[校准]` | 权重校准 |
| `⚠️`   | 警告/异常 |
| `✅`   | 成功 |
| `→`    | 结果/指向 |

## 代码风格

- 缩进: 4空格
- 行尾: 无空格
- 文件末尾: 保留空行
- 中文注释: 使用中文标点
- 代码内: 使用英文标点
- import顺序: 标准库 → 第三方 → 本地模块
