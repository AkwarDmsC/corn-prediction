#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟交易模块 v1.2

核心规则：
- 从 predictions.md 解析方向信号，支持 --session day|night 参数
- 信号"偏强"→做多，"偏弱"→做空，其他→空仓
- 开仓价 = 下一交易日开盘价（日线 open）
- 平仓价 = 收盘价（日线 close）
- 按保证金交易：DCE玉米保证金率7%，全仓使用
- 初始本金10万，记录至 sim_trade.md

用法：
  python3 sim_trade.py                                    # 处理当日待处理预测（默认日盘）
  python3 sim_trade.py --session day                      # 日盘模式
  python3 sim_trade.py --session night                    # 夜盘模式
  python3 sim_trade.py --check-pending                    # 检查待执行信号
  python3 sim_trade.py --status                           # 显示当前持仓和盈亏
  python3 sim_trade.py --summary                          # 显示累计统计
"""

import json, re, statistics
from datetime import datetime
from pathlib import Path

import akshare as ak

WORKSPACE = Path(__file__).parent
PREDICTIONS = WORKSPACE / "predictions.md"
SIM_TRADE = WORKSPACE / "sim_trade.md"
STATE_JSON = WORKSPACE / "sim_trade_state.json"

# ── 模拟参数 ──
INITIAL_CAPITAL = 100000           # 初始本金 10万元
CONTRACT_MULTIPLIER = 10           # 1手=10吨
MARGIN_RATE = 0.07                 # 保证金率 7%（DCE玉米标准）
COMMISSION_RATE = 0.0001           # 手续费率 万分之一
SLIPPAGE = 0.0005                  # 滑点 0.05%


# ══════════════════════════════════════════
#  状态管理（JSON + Markdown 双写）
# ══════════════════════════════════════════

def _default_state():
    return {
        'capital': INITIAL_CAPITAL,
        'equity': float(INITIAL_CAPITAL),
        'margin_used': 0.0,
        'position': '无',
        'entry_date': '',
        'entry_price': 0.0,
        'contracts': 0,
        'latest_price': 0.0,          # 最近一次行情（用于计算浮盈/浮亏）
        'latest_date': '',            # 最近一次行情日期
        'float_pnl': 0.0,             # 当前浮盈/浮亏（持仓时为非零）
        'capital_negative_days': 0,   # v1.3: 资金为负的连续天数（减仓触发计数）
        'last_negative_date': '',     # v1.3: 上次资金为负的日期
        'pending_signals': [],        # 等待日线数据更新后再处理的信号
        'processed_signals': [],      # 已处理的信号标记列表
    }


def _signal_key(pred_date, session, direction=None):
    """生成唯一信号标记 key"""
    key = f"{pred_date}:{session}"
    if direction:
        key += f":{direction}"
    return key


def _is_signal_processed(state, pred_date, session):
    """检查某个预测的信号是否已被处理（成功开仓或明确跳过）"""
    prefix = f"{pred_date}:{session}"
    return any(s.startswith(prefix) for s in state.get('processed_signals', []))


def _add_pending_signal(state, sig_key):
    """加入待处理信号，保持列表去重。"""
    state.setdefault('pending_signals', [])
    if sig_key not in state['pending_signals']:
        state['pending_signals'].append(sig_key)


def _mark_processed_signal(state, sig_key):
    """标记信号已处理，并从待处理列表移除。"""
    state.setdefault('processed_signals', [])
    state.setdefault('pending_signals', [])
    if sig_key not in state['processed_signals']:
        state['processed_signals'].append(sig_key)
    state['pending_signals'] = [s for s in state['pending_signals'] if s != sig_key]


def _fetch_latest_price():
    """从 akshare 拉取最新一日行情（close）。失败返回 (None, None)。"""
    try:
        df = ak.futures_zh_daily_sina(symbol="C0")
        df["date"] = df["date"].astype(str)
        df = df.sort_values("date").reset_index(drop=True)
        row = df.iloc[-1]
        return float(row["close"]), str(row["date"])
    except Exception as e:
        print(f"[sim_trade] ⚠️ 拉取最新行情失败: {e}")
        return None, None


def _compute_float_pnl(state, latest_price=None):
    """根据当前 state 计算浮动盈亏。最新价可选，未提供时尝试用 state['latest_price']。"""
    if state.get('position') == '无' or state.get('contracts', 0) <= 0:
        return 0.0
    price = latest_price if latest_price is not None else state.get('latest_price', 0.0)
    if price <= 0:
        return 0.0
    if state['position'] == '多':
        return (price - state['entry_price']) * CONTRACT_MULTIPLIER * state['contracts']
    else:  # 空
        return (state['entry_price'] - price) * CONTRACT_MULTIPLIER * state['contracts']


def _refresh_equity(state, latest_price=None, latest_date=None):
    """
    用最新价重算 equity + float_pnl。
    - 无持仓：equity = capital
    - 持仓：equity = capital + margin_used + float_pnl
    这是 v1.2 → v1.3 的关键修复：之前的 equity 漏了 float_pnl，导致
    持仓期总权益和总收益率显示虚高。
    """
    if latest_price is not None:
        state['latest_price'] = latest_price
    if latest_date is not None:
        state['latest_date'] = latest_date
    float_pnl = _compute_float_pnl(state, state.get('latest_price', 0.0))
    state['float_pnl'] = float_pnl
    if state.get('position') == '无' or state.get('contracts', 0) <= 0:
        state['equity'] = state['capital']
    else:
        state['equity'] = state['capital'] + state.get('margin_used', 0.0) + float_pnl
    return float_pnl


def _check_capital_health():
    """v1.3: 资金健康检查，启动处理流程时输出告警 + 累计资本为负天数。"""
    state = load_trade_state()
    cap = state.get('capital', INITIAL_CAPITAL)
    pos = state.get('position', '无')
    today = datetime.now().strftime('%Y-%m-%d')

    if cap < 0:
        # v1.3: 累计连续负值天数（跨日才 +1）
        if state.get('last_negative_date') != today:
            state['capital_negative_days'] = state.get('capital_negative_days', 0) + 1
            state['last_negative_date'] = today
        # 这里只标记，不 save（避免在检测/状态查询路径上频繁写盘）
        neg_days = state.get('capital_negative_days', 0)
        print(f"\n🚨 [sim_trade] 资金健康警告: 可用资金为负 ({cap:,.2f}元) 连续 {neg_days} 天")
        print(f"    持仓={pos} | 手数={state.get('contracts',0)} | 开仓价={state.get('entry_price',0):.0f}")
        if neg_days >= 3:
            print(f"    ⚠️  连续 {neg_days} 天资金为负，请手动运行 --auto-reduce 减仓")
        print(f"    建议：① 等反向信号平仓  ② 调小 compute_contracts 让资金恢复  ③ 手动 reset  ④ --auto-reduce")
    else:
        if state.get('capital_negative_days', 0) > 0 or state.get('last_negative_date', ''):
            state['capital_negative_days'] = 0
            state['last_negative_date'] = ''
        if cap < 1000:
            print(f"\n⚠️  [sim_trade] 资金偏低: 可用资金 {cap:,.2f}元 (持仓={pos})")
    return state  # 返回供调用方使用


def auto_reduce(target_buffer=2000.0, force=False):
    """
    v1.3: 自动减仓。当资金为负、且连续为负天数超阈值时，强制减仓让可用资金回到目标 buffer。
    - target_buffer=2000: 减仓后可用资金至少为 2000
    - force=True: 不管几天负数都减仓

    减仓逻辑：按当前价卖出（空仓）部分手数，直到可用资金 >= target_buffer。
    """
    state = load_trade_state()
    pos = state.get('position', '无')

    if pos == '无' or state.get('contracts', 0) <= 0:
        print("[sim_trade] 无持仓，不需要减仓")
        return False

    neg_days = state.get('capital_negative_days', 0)
    if not force and neg_days < 3:
        print(f"[sim_trade] 资金为负仅 {neg_days} 天（阈值 3 天），跳过自动减仓")
        print(f"    如需强制减仓，添加 --force 参数")
        return False

    cap = state.get('capital', 0.0)
    entry_price = state['entry_price']
    contracts = state['contracts']
    margin_used = state.get('margin_used', 0.0)

    # 拉取最新价
    latest_price, latest_date = _fetch_latest_price()
    if latest_price is None:
        print("[sim_trade] ❌ 拉取最新价失败，无法减仓")
        return False

    # 计算当前盈亏（按最新价）
    if pos == '多':
        pnl_per_contract = (latest_price - entry_price) * CONTRACT_MULTIPLIER
    else:
        pnl_per_contract = (entry_price - latest_price) * CONTRACT_MULTIPLIER

    close_cost_per_contract = latest_price * CONTRACT_MULTIPLIER * (COMMISSION_RATE + SLIPPAGE)
    margin_per_contract = entry_price * CONTRACT_MULTIPLIER * MARGIN_RATE

    # 每减 1 手，能回收：margin_per_contract + pnl_per_contract - close_cost_per_contract
    recover_per_contract = margin_per_contract + pnl_per_contract - close_cost_per_contract

    # 需要的减仓手数：让 cap 达到 target_buffer
    need = target_buffer - cap
    if need <= 0:
        print(f"[sim_trade] 资金 {cap:.2f} >= {target_buffer}，不需要减仓")
        return False

    if recover_per_contract <= 0:
        print(f"[sim_trade] ❌ 减仓会亏损 (recover_per_contract={recover_per_contract:.2f})，拒绝减仓")
        print(f"    建议：手动 --reset 或等价格转好")
        return False

    reduce_contracts = int(need / recover_per_contract) + 1
    if reduce_contracts >= contracts:
        print(f"[sim_trade] ⚠️ 需要减仓 {reduce_contracts} 手 >= 持仓 {contracts} 手，将全部平仓")
        reduce_contracts = contracts

    # 执行减仓
    pnl = pnl_per_contract * reduce_contracts
    close_cost = close_cost_per_contract * reduce_contracts
    pnl_net = pnl - close_cost
    margin_released = margin_per_contract * reduce_contracts

    state['capital'] += margin_released + pnl_net
    state['margin_used'] -= margin_released
    state['contracts'] -= reduce_contracts
    state['latest_price'] = latest_price
    state['latest_date'] = latest_date

    # 记录到交易记录（先取所需字段，避免被清空）
    seq = 0
    if SIM_TRADE.exists():
        existing = SIM_TRADE.read_text()
        seq_matches = re.findall(r"\| (\d+) \|", existing)
        if seq_matches:
            seq = max(int(s) for s in seq_matches) + 1
    reduce_reason = "auto-reduce" if not force else "force-reduce"
    reduce_entry_date = state.get('entry_date', '') or today_str_for_md()
    trade_row = f"| {seq} | {reduce_entry_date} | {pos} | {entry_price:.0f} | {reduce_contracts} | {latest_date} | {latest_price:.0f} | {pnl_net:+.0f} | {pnl_net / (margin_per_contract * reduce_contracts) * 100:+.2f}% | {state['capital']:,.2f} |"
    if SIM_TRADE.exists():
        content = SIM_TRADE.read_text()
        content = re.sub(
            r"(### 📜 交易记录\n\n\| 序号.*?\| 累计权益 \|\n\|-+\|)",
            r"\1\n" + trade_row, content
        )
        SIM_TRADE.write_text(content)

    # 全部平仓后清空持仓字段
    if state['contracts'] <= 0:
        state['position'] = '无'
        state['entry_price'] = 0.0
        state['entry_date'] = ''
        state['float_pnl'] = 0.0

    state['capital_negative_days'] = 0
    state['last_negative_date'] = ''
    save_trade_state(state)

    print(f"\n🛡️  [sim_trade] 自动减仓执行完成")
    print(f"    减仓: {pos} {reduce_contracts}手 @{latest_price:.0f} ({reduce_reason})")
    print(f"    盈亏: {pnl_net:+.0f}元 (毛利{pnl:+.0f} - 成本{close_cost:.0f})")
    print(f"    资金: {cap:.2f} → {state['capital']:,.2f}元")
    print(f"    持仓: {contracts} → {state['contracts']}手")
    return True


def today_str_for_md():
    from datetime import datetime as _dt
    return _dt.now().strftime('%Y-%m-%d')


def load_trade_state():
    """从 JSON 加载交易状态；回退到 Markdown 解析（迁移兼容），再回退到默认值"""
    if STATE_JSON.exists():
        try:
            data = json.loads(STATE_JSON.read_text())
            # 确保所有键存在
            st = _default_state()
            st.update(data)
            st.setdefault('pending_signals', [])
            st.setdefault('processed_signals', [])
            st.setdefault('latest_price', 0.0)
            st.setdefault('latest_date', '')
            st.setdefault('float_pnl', 0.0)
            st.setdefault('capital_negative_days', 0)
            st.setdefault('last_negative_date', '')
            return st
        except Exception:
            pass  # JSON 损坏，回退

    # 回退：从 Markdown 解析（v1.1 迁移兼容）
    if SIM_TRADE.exists():
        content = SIM_TRADE.read_text()
        st = _default_state()
        cap_m = re.search(r"可用资金\s*\|\s*([\d,]+\.?\d*)", content)
        equity_m = re.search(r"总权益\s*\|\s*([\d,]+\.?\d*)", content)
        margin_m = re.search(r"占用保证金\s*\|\s*([\d,]+\.?\d*)", content)
        pos_m = re.search(r"方向\s*\|\s*(\S+)", content)
        entry_date_m = re.search(r"开仓日期\s*\|\s*(\S+)", content)
        entry_price_m = re.search(r"开仓价\s*\|\s*([\d.]+)", content)
        contracts_m = re.search(r"手数\s*\|\s*(\d+)", content)
        st['capital'] = float(cap_m.group(1).replace(',','')) if cap_m else INITIAL_CAPITAL
        st['equity'] = float(equity_m.group(1).replace(',','')) if equity_m else st['capital']
        st['margin_used'] = float(margin_m.group(1).replace(',','')) if margin_m else 0.0
        st['position'] = pos_m.group(1) if pos_m else '无'
        st['entry_date'] = entry_date_m.group(1) if entry_date_m else ''
        st['entry_price'] = float(entry_price_m.group(1)) if entry_price_m and entry_price_m.group(1).strip() else 0.0
        st['contracts'] = int(contracts_m.group(1)) if contracts_m else 0
        return st

    return _default_state()


def save_trade_state(state):
    """写入 JSON 状态 + 渲染 Markdown 报告"""
    # v1.3: 写入前先用最新价刷新 equity/float_pnl，保证 Markdown 报告数字一致
    latest_price, latest_date = _fetch_latest_price()
    if latest_price is not None:
        _refresh_equity(state, latest_price=latest_price, latest_date=latest_date)

    # 写 JSON
    STATE_JSON.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    # 渲染 Markdown
    has_position = state.get('position') != '无' and state.get('contracts', 0) > 0
    # 持仓市值用最新价（mark-to-market），更准确
    px = state.get('latest_price', 0.0) if has_position and state.get('latest_price', 0.0) > 0 else state.get('entry_price', 0.0)
    pos_value = state['contracts'] * CONTRACT_MULTIPLIER * px if has_position else 0
    margin_used = state.get('margin_used', 0.0)
    equity = state.get('equity', state['capital'])
    float_pnl = state.get('float_pnl', 0.0)
    latest_date = state.get('latest_date', '') or '—'

    # v1.3: 统计交易记录条数
    realized_pnl = 0.0
    trade_count = 0
    if SIM_TRADE.exists():
        existing = SIM_TRADE.read_text()
        for line in existing.split('\n'):
            if line.startswith('| ') and '|' in line and not line.startswith('|---') and not line.startswith('| 序'):
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 10:
                    try:
                        pnl_str = parts[8].replace(',', '').replace('+', '')
                        realized_pnl += float(pnl_str)
                        trade_count += 1
                    except (ValueError, IndexError):
                        pass

    pos_section = f"""### 📊 当前持仓

| 项目 | 值 |
|------|-----|
| 方向 | {state.get('position', '无')} |
| 开仓日期 | {state.get('entry_date') if state.get('entry_date') else '—'} |
| 开仓价 | {state.get('entry_price', 0):.0f} 元/吨 |
| 手数 | {state.get('contracts', 0)} 手 |
| 最新价({latest_date}) | {px:,.0f} 元/吨 |
| 持仓市值 | {pos_value:,.0f} 元 |
| 占用保证金 | {margin_used:,.0f} 元 |

### 💰 账户资金

| 项目 | 值 |
|------|-----|
| 初始本金 | {INITIAL_CAPITAL:,} 元 |
| 总权益 | {equity:,.2f} 元 |
| 占用保证金 | {margin_used:,.0f} 元 |
| 可用资金 | {state['capital']:,.2f} 元 |
| 浮动盈亏（未实现） | {float_pnl:+,.0f} 元 |
| 已实现盈亏 | {realized_pnl:+,.0f} 元（{trade_count} 笔） |
| 总收益率 | {(equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100:+.2f}% |
"""

    if SIM_TRADE.exists():
        content = SIM_TRADE.read_text()
        content, pos_count = re.subn(
            r"### 📊 当前持仓.*?(?=\n### 💰 账户资金|\n### 📜 交易记录|\Z)",
            pos_section, content, flags=re.DOTALL
        )
        content, capital_count = re.subn(
            r"### 💰 账户资金.*?(?=\n### 📜 交易记录|\Z)",
            f"### 💰 账户资金\n\n| 项目 | 值 |\n|------|-----|\n| 初始本金 | {INITIAL_CAPITAL:,} 元 |\n| 总权益 | {equity:,.2f} 元 |\n| 占用保证金 | {margin_used:,.0f} 元 |\n| 可用资金 | {state['capital']:,.2f} 元 |\n| 浮动盈亏（未实现） | {float_pnl:+,.0f} 元 |\n| 已实现盈亏 | {realized_pnl:+,.0f} 元（{trade_count} 笔） |\n| 总收益率 | {(equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100:+.2f}% |\n\n",
            content, flags=re.DOTALL
        )
        # 如果没有被任何一个替换匹配到，说明文件格式异常，完整重建
        if pos_count == 0 and capital_count == 0:
            content = None
    else:
        content = None

    if content is None:
        content = f"""# 🌽 DCE玉米模拟交易记录

> 初始本金: {INITIAL_CAPITAL:,} 元
> 合约乘数: 10吨/手 | 保证金率: {MARGIN_RATE*100:.0f}%
> 交易成本: 手续费{COMMISSION_RATE*100:.2f}% + 滑点{SLIPPAGE*100:.2f}%

---

{pos_section}

### 📜 交易记录

| 序号 | 开仓日期 | 方向 | 开仓价 | 手数 | 平仓日期 | 平仓价 | 盈亏(元) | 盈亏率 | 累计权益 |
|------|---------|------|-------|------|---------|-------|---------|-------|---------|
"""

    SIM_TRADE.write_text(content)


# ══════════════════════════════════════════
#  预测解析（session 感知）
# ══════════════════════════════════════════

def _normalize_direction(raw: str):
    """统一方向字符串为 '多'/'空'/'无'"""
    d = raw.strip().lower()
    if any(k in d for k in ['偏强', '多头', '上涨', '↑', '↗', '做多']):
        return '多'
    if any(k in d for k in ['偏弱', '空头', '下跌', '↓', '↘', '做空']):
        return '空'
    return '无'


def parse_direction(pred_block: str, session: str = 'day') -> str:
    """
    从预测区块中按 session 解析方向。
    session='day' → 解析 ### 🌞 日盘 小节
    session='night' → 解析 ### 🌙 夜盘 小节
    找不到对应小节 → 返回 '无'
    """
    if session == 'day':
        section = re.search(
            r"### 🌞 日盘[^\n]*?预测[^\n]*?下一交易日日盘.*?\n\n\| 项目 \| 值 \|.*?方向 \| ([^\|]+)",
            pred_block, re.DOTALL
        )
    else:
        section = re.search(
            r"### 🌙 夜盘[^\n]*?预测[^\n]*?下一交易日夜盘.*?\n\n\| 项目 \| 值 \|.*?方向 \| ([^\|]+)",
            pred_block, re.DOTALL
        )

    if section:
        return _normalize_direction(section.group(1))

    # 备用：行首匹配方向行（夜盘可能没有完整表格，用简化）
    if session == 'night':
        fallback = re.search(r"方向\s*[：:]\s*([^\n]+)", pred_block)
    else:
        # 日盘备用：只匹配非表格行（行首方向）
        fallback = re.search(r"(?m)^方向\s*[：:]\s*([^\n]+)", pred_block)

    if fallback:
        return _normalize_direction(fallback.group(1))

    return '无'


# 备用方向源文件
LATEST_ANALYSIS_PATHS = [
    Path("/tmp/corn_analysis_output_day.txt"),
    Path("/tmp/corn_analysis_output_night.txt"),
    Path("/tmp/corn_analysis_output.txt"),
]


def get_direction_from_analysis(session: str = 'day'):
    """
    从最新的分析输出文件解析方向（备用来源）。
    日盘优先检查 day 文件，夜盘优先检查 night 文件。
    """
    if session == 'night':
        priority = [Path("/tmp/corn_analysis_output_night.txt"),
                    Path("/tmp/corn_analysis_output.txt"),
                    Path("/tmp/corn_analysis_output_day.txt")]
    else:
        priority = LATEST_ANALYSIS_PATHS

    for path in priority:
        if not path.exists():
            continue
        content = path.read_text()
        m = re.search(r'方向[：:]\s*([^\n]+)', content)
        if m:
            d = _normalize_direction(m.group(1))
            if d != '无':
                return d, m.group(1).strip()
    return None


def get_latest_prediction(session: str = 'day'):
    """
    从 predictions.md 获取最新的预测日期及对应 session 的区块。
    跳过校准报告等非预测区块。
    返回 {'date': str, 'block': str, 'direction': str} 或 None
    """
    if not PREDICTIONS.exists():
        print("[sim_trade] predictions.md 不存在")
        return None

    content = PREDICTIONS.read_text()
    # 用预测日期 + 完整区块做临时文件指纹，避免读取期间被写入
    blocks = re.findall(r"## 预测 (\d{4}-\d{2}-\d{2})\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not blocks:
        print("[sim_trade] 未找到预测记录")
        return None

    # 从最后往前找，跳过校准报告等
    for date_str, block in reversed(blocks):
        # 检查是否包含当前 session 的区块
        session_label = "### 🌞 日盘" if session == 'day' else "### 🌙 夜盘"
        if session_label not in block:
            continue
        direction = parse_direction(block, session)
        if direction != '无':
            return {'date': date_str, 'block': block, 'direction': direction}

    # 所有区块都无效，尝试分析输出
    result = get_direction_from_analysis(session)
    if result:
        signal, raw = result
        print(f"[sim_trade] 从分析输出获取方向: {raw} → {signal}")
        return {'date': 'today', 'direction': signal, 'block': ''}

    print(f"[sim_trade] 未找到{session}盘有效方向")
    return None


def get_prediction_by_date(pred_date: str, session: str = 'day'):
    """按日期和 session 读取历史预测，用于 pending 信号补处理。"""
    if not PREDICTIONS.exists():
        print("[sim_trade] predictions.md 不存在")
        return None

    content = PREDICTIONS.read_text()
    blocks = re.findall(r"## 预测 (\d{4}-\d{2}-\d{2})\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    session_label = "### 🌞 日盘" if session == 'day' else "### 🌙 夜盘"

    for date_str, block in reversed(blocks):
        if date_str != pred_date or session_label not in block:
            continue
        direction = parse_direction(block, session)
        if direction != '无':
            return {'date': date_str, 'block': block, 'direction': direction}

    print(f"[sim_trade] pending 信号 {pred_date}:{session} 未找到有效预测方向")
    return None


def get_trade_prices(pred_date):
    """
    获取根据预测日期（做预测的交易日）确定的下一个交易日的开盘和收盘
    返回 (open, close) 或 (None, None)
    """
    try:
        df = ak.futures_zh_daily_sina(symbol="C0")
        df["date"] = df["date"].astype(str)
        df = df.sort_values("date").reset_index(drop=True)
        idx_list = df[df["date"] == pred_date].index
        if len(idx_list) == 0:
            return None, None
        idx = idx_list[0]
        if idx + 1 < len(df):
            row = df.iloc[idx + 1]
            return float(row["open"]), float(row["close"])
        return None, None
    except Exception as e:
        print(f"[sim_trade] 获取数据失败: {e}")
        return None, None


def compute_contracts(money, price, reserve_buffer=200.0):
    """
    按保证金交易计算可开手数。
    v1.3 优化：预留 reserve_buffer 元余额，保证开仓后可用资金不为负。

    公式：可开手数 = floor((money - reserve_buffer) / (margin_per_contract + open_cost_per_contract))
    margin_per_contract = price * 10 * MARGIN_RATE
    open_cost_per_contract = price * 10 * (COMMISSION_RATE + SLIPPAGE)
    """
    if price <= 0:
        return 0
    margin_per_contract = price * CONTRACT_MULTIPLIER * MARGIN_RATE
    open_cost_per_contract = price * CONTRACT_MULTIPLIER * (COMMISSION_RATE + SLIPPAGE)
    cost_per_contract = margin_per_contract + open_cost_per_contract
    if cost_per_contract <= 0:
        return 0
    usable = max(0.0, money - reserve_buffer)
    n = int(usable / cost_per_contract)
    return n  # 不足一手则返回 0（修复 v1.1 max(1, ...) 透支风险）


# ══════════════════════════════════════════
#  交易执行
# ══════════════════════════════════════════

def process(session: str = 'day'):
    """处理今日预测：检查是否需要开仓或平仓"""
    pred = get_latest_prediction(session)
    if not pred:
        print(f"[sim_trade] 无{session}盘预测数据，跳过")
        return
    process_prediction(pred, session, add_pending_on_missing=True)


def process_prediction(pred, session: str = 'day', add_pending_on_missing: bool = True):
    """按指定预测执行交易；行情未就绪时按需保留为 pending。"""
    state = load_trade_state()
    pred_date = pred.get('date', 'unknown')
    direction = pred.get('direction', '无')
    print(f"[sim_trade] [{session}] 最新预测日期: {pred_date} | 方向: {direction}")

    # 检查是否已处理过
    sig_key = _signal_key(pred_date, session)
    state.setdefault('processed_signals', [])
    state.setdefault('pending_signals', [])
    if _is_signal_processed(state, pred_date, session):
        print(f"[sim_trade] [{session}] 预测 {sig_key} 已处理过，跳过")
        return True

    # 获取下一交易日的开盘收盘价
    trade_open, trade_close = get_trade_prices(pred_date)

    # ── 检查平仓 ──
    if state['position'] != '无' and state['contracts'] > 0:
        should_close = False
        close_reason = ""

        if direction == '无':
            should_close = True
            close_reason = "信号转为震荡/中性，平仓"
        elif (state['position'] == '多' and direction == '空') or \
             (state['position'] == '空' and direction == '多'):
            should_close = True
            close_reason = f"信号反转({state['position']}→{direction})，平仓换向"

        if should_close and trade_close is None:
            print(f"[sim_trade] [{session}] ⏳ 平仓所需下一交易日收盘数据未就绪，加入 pending 等待重试")
            print(f"[sim_trade]    原因: {close_reason}")
            if add_pending_on_missing:
                _add_pending_signal(state, sig_key)
            save_trade_state(state)
            return False

        if should_close and trade_close is not None:
            close_price = trade_close
            entry_price = state['entry_price']
            contracts = state['contracts']

            if state['position'] == '多':
                pnl = (close_price - entry_price) * CONTRACT_MULTIPLIER * contracts
            else:
                pnl = (entry_price - close_price) * CONTRACT_MULTIPLIER * contracts

            # 平仓：只扣平仓侧成本（修复 v1.1 双扣 Bug）
            close_cost = close_price * CONTRACT_MULTIPLIER * contracts * (COMMISSION_RATE + SLIPPAGE)
            pnl_net = pnl - close_cost

            margin_per_contract_open = entry_price * CONTRACT_MULTIPLIER * MARGIN_RATE
            pnl_rate = pnl_net / (margin_per_contract_open * contracts) * 100 if margin_per_contract_open * contracts > 0 else 0.0

            # 更新资金和权益
            frozen_margin = state.get('margin_used', 0.0)
            state['capital'] += frozen_margin + pnl_net
            state['equity'] = state['capital']
            state['margin_used'] = 0.0

            # 添加到交易记录
            content = SIM_TRADE.read_text() if SIM_TRADE.exists() else ""
            seq = 0
            seq_matches = re.findall(r"\| (\d+) \|", content)
            if seq_matches:
                seq = max(int(s) for s in seq_matches) + 1

            trade_row = f"| {seq} | {state['entry_date']} | {state['position']} | {entry_price:.0f} | {contracts} | {pred_date} | {close_price:.0f} | {pnl_net:+.0f} | {pnl_rate:+.2f}% | {state['capital']:,.2f} |"
            content = re.sub(
                r"(### 📜 交易记录\n\n\| 序号.*?\| 累计权益 \|\n\|-+\|)",
                r"\1\n" + trade_row, content
            )
            SIM_TRADE.write_text(content)

            print(f"[sim_trade] [{session}] 🔒 平仓: {state['position']} {contracts}手 @{close_price:.0f}, 盈亏{pnl_net:+.0f}元, 权益{state['capital']:,.2f}")
            print(f"[sim_trade]    原因: {close_reason}")

            # 清空持仓
            state['position'] = '无'
            state['entry_price'] = 0.0
            state['contracts'] = 0
            state['entry_date'] = ''
            state['float_pnl'] = 0.0  # v1.3: 平仓后浮盈/浮亏清零
            state['latest_price'] = close_price  # 记录平仓价
            state['latest_date'] = pred_date

            # 平仓成功 → 标记已处理
            _mark_processed_signal(state, sig_key)
            save_trade_state(state)
            return True
        return False  # 平仓后不再开新仓（等待下次调用）

    # ── 开新仓 ──
    if direction != '无' and state['position'] == '无':
        if trade_open is not None:
            entry_price = trade_open
            margin_per_contract = entry_price * CONTRACT_MULTIPLIER * MARGIN_RATE
            contracts = compute_contracts(state['capital'], entry_price)

            if contracts > 0:
                margin_total = margin_per_contract * contracts
                # 只扣开仓侧成本
                open_cost = entry_price * CONTRACT_MULTIPLIER * contracts * (COMMISSION_RATE + SLIPPAGE)

                # v1.3 风控：开仓后可用资金不得为负（允许极少负值的警告阈值设为 -1 元）
                capital_after_open = state['capital'] - margin_total - open_cost
                if capital_after_open < -1.0:
                    print(f"[sim_trade] [{session}] 🛑 风控拦截：开仓后可用资金为负 ({capital_after_open:,.2f}元)，拒绝开仓")
                    print(f"[sim_trade]    需求资金={margin_total+open_cost:,.0f}，可用={state['capital']:,.2f}")
                    _mark_processed_signal(state, sig_key)
                    save_trade_state(state)
                    return True
                elif capital_after_open < 0:
                    print(f"[sim_trade] [{session}] ⚠️ 警告：开仓后可用资金轻微为负 ({capital_after_open:,.2f}元)，成本超高")

                state['position'] = direction
                state['entry_date'] = pred_date
                state['entry_price'] = entry_price
                state['contracts'] = contracts
                state['margin_used'] = margin_total
                state['capital'] = capital_after_open
                # 记录开仓价作为临时 latest_price，save_trade_state 会拉取真实最新价
                state['latest_price'] = entry_price
                state['latest_date'] = pred_date
                state['float_pnl'] = 0.0
                # 写盘前 refresh，equity = capital + margin + float
                _refresh_equity(state, latest_price=entry_price, latest_date=pred_date)

                # 开仓成功 → 标记已处理
                _mark_processed_signal(state, sig_key)

                save_trade_state(state)

                print(f"[sim_trade] [{session}] 🚀 开仓: {direction} {contracts}手 @{entry_price:.0f}元")
                print(f"[sim_trade]    保证金: {margin_total:,.0f}元, 可用: {state['capital']:,.2f}元, 总权益: {state['equity']:,.2f}")
                return True
            else:
                print(f"[sim_trade] [{session}] ⚠️ 资金不足开仓")
                _mark_processed_signal(state, sig_key)
                save_trade_state(state)
                return True
        else:
            print(f"[sim_trade] [{session}] ⏳ 下一交易日数据未就绪，加入 pending 等待重试")

        if add_pending_on_missing:
            _add_pending_signal(state, sig_key)
        save_trade_state(state)
        return False

    elif state['position'] != '无':
        print(f"[sim_trade] [{session}] 已有持仓({state['position']}), 等待平仓或反转信号")
        return False

    return False


def process_today():
    """兼容旧调用：默认日盘"""
    process('day')


def check_pending_signals():
    """扫描 pending 信号；行情可用才处理并移入 processed。"""
    state = load_trade_state()
    pending = list(state.get('pending_signals', []))
    if not pending:
        print("[sim_trade] 无 pending 信号")
        return

    print(f"[sim_trade] 待检查 pending 信号: {len(pending)} 条")
    for sig_key in pending:
        parts = sig_key.split(':')
        if len(parts) < 2:
            print(f"[sim_trade] pending key 格式异常，保留: {sig_key}")
            continue

        pred_date, session = parts[0], parts[1]
        if session not in ('day', 'night'):
            print(f"[sim_trade] pending key session 异常，保留: {sig_key}")
            continue

        if _is_signal_processed(load_trade_state(), pred_date, session):
            state = load_trade_state()
            _mark_processed_signal(state, sig_key)
            save_trade_state(state)
            print(f"[sim_trade] pending 信号已处理过，移除: {sig_key}")
            continue

        trade_open, trade_close = get_trade_prices(pred_date)
        if trade_open is None and trade_close is None:
            print(f"[sim_trade] {sig_key} 行情仍未就绪，保持 pending")
            continue

        pred = get_prediction_by_date(pred_date, session)
        if not pred:
            print(f"[sim_trade] {sig_key} 无法恢复预测方向，保持 pending")
            continue

        process_prediction(pred, session, add_pending_on_missing=True)


# ══════════════════════════════════════════
#  CLI 工具
# ══════════════════════════════════════════

def show_status():
    """显示当前持仓和盈亏"""
    state = load_trade_state()
    print(f"\n=== 模拟交易状态 ===")
    print(f"总权益: {state.get('equity', state['capital']):,.2f} 元")
    print(f"可用资金: {state['capital']:,.2f} 元")
    print(f"当前持仓: {state.get('position', '无')}")
    print(f"待处理信号: {len(state.get('pending_signals', []))} 条")
    print(f"已处理信号: {len(state.get('processed_signals', []))} 条")

    if state['position'] != '无' and state.get('contracts', 0) > 0:
        try:
            df = ak.futures_zh_daily_sina(symbol="C0")
            df["date"] = df["date"].astype(str)
            df = df.sort_values("date").reset_index(drop=True)
            latest_row = df.iloc[-1]
            latest_price = float(latest_row["close"])
            latest_date = latest_row["date"]
            # v1.3: 用统一函数算浮盈/浮亏
            if state['position'] == '多':
                float_pnl = (latest_price - state['entry_price']) * CONTRACT_MULTIPLIER * state['contracts']
            else:
                float_pnl = (state['entry_price'] - latest_price) * CONTRACT_MULTIPLIER * state['contracts']
            true_equity = state['capital'] + state['margin_used'] + float_pnl
            print(f"  开仓价: {state['entry_price']:.0f} → 最新({latest_date}): {latest_price:.0f} 元/吨")
            print(f"  浮动盈亏: {float_pnl:+,.0f} 元")
            print(f"  预估总权益(实): {true_equity:,.2f} 元")
            print(f"  真实收益率: {(true_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100:+.2f}%")
        except Exception as e:
            print(f"  (计算浮动盈亏失败: {e})")


def show_summary():
    """显示累计统计（含 v1.3 浮亏 vs 已实现）"""
    content = SIM_TRADE.read_text() if SIM_TRADE.exists() else ""
    state = load_trade_state()

    # v1.3: 实时拉取最新价，算浮盈/浮亏
    latest_price, latest_date = _fetch_latest_price()
    float_pnl = 0.0
    if latest_price is not None and state.get('position') != '无':
        _refresh_equity(state, latest_price=latest_price, latest_date=latest_date)
        float_pnl = state.get('float_pnl', 0.0)

    # v1.3: 总是输出持仓状态
    if state.get('position') != '无' and state.get('contracts', 0) > 0:
        print(f"\n=== 当前持仓 ===")
        print(f"方向: {state['position']} | 手数: {state['contracts']} | 开仓价: {state['entry_price']:.0f}")
        if latest_price is not None:
            print(f"最新价({latest_date}): {latest_price:.0f} | 浮动盈亏: {float_pnl:+,.0f} 元")
        print(f"总权益: {state.get('equity', 0):,.2f} 元 | 真实收益率: {(state.get('equity', INITIAL_CAPITAL) - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100:+.2f}%")

    if not content:
        print("无交易记录")
        return

    trades = []
    for line in content.split('\n'):
        if not line.startswith('| '):
            continue
        cols = [c.strip() for c in line.split('|')]
        cols = [c for c in cols if c]
        if len(cols) >= 9 and cols[0].isdigit() and re.match(r'^[\d.]+$', cols[3]):
            try:
                trades.append({
                    'seq': int(cols[0]),
                    'direction': cols[2],
                    'pnl': float(cols[7].replace(',', '')),
                })
            except (ValueError, IndexError):
                continue

    if not trades:
        print("\n=== 已实现盈亏 ===")
        print("0 笔（未平仓）")
        return

    total_trades = len(trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] < 0]
    win_rate = len(wins) / total_trades * 100
    total_pnl = sum(t['pnl'] for t in trades)

    cumulative = []
    cap = INITIAL_CAPITAL
    for t in trades:
        cap += t['pnl']
        cumulative.append(cap)
    max_drawdown = 0
    peak = INITIAL_CAPITAL
    for v in cumulative:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_drawdown:
            max_drawdown = dd

    print(f"\n=== 模拟交易累计统计 ===")
    print(f"总交易次数: {total_trades}")
    print(f"胜率: {win_rate:.1f}% ({len(wins)}/{total_trades})")
    print(f"已实现盈亏: {total_pnl:+.0f} 元")
    if state.get('position') != '无':
        print(f"浮动盈亏（未平仓）: {float_pnl:+,.0f} 元")
        print(f"总盈亏（已实现+浮亏）: {total_pnl + float_pnl:+,.0f} 元")
    print(f"已实现收益率: {total_pnl / INITIAL_CAPITAL * 100:+.2f}%")
    if state.get('position') != '无':
        true_eq = state.get('equity', cap)
        print(f"真实总收益率: {(true_eq - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100:+.2f}%")
    print(f"最新权益: {cap:,.2f} 元")
    print(f"最大回撤: {max_drawdown:.2f}%")
    if wins:
        avg_win = statistics.mean([t['pnl'] for t in wins])
        print(f"平均盈利: {avg_win:+.0f} 元")
    if losses:
        avg_loss = statistics.mean([t['pnl'] for t in losses])
        print(f"平均亏损: {avg_loss:+.0f} 元")
    if losses and abs(sum(t['pnl'] for t in losses)) > 1:
        profit_factor = abs(sum(t['pnl'] for t in wins) / sum(t['pnl'] for t in losses))
        print(f"盈亏比(Profit Factor): {profit_factor:.2f}")


def main():
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith('--session=')]
    session = 'day'
    for a in sys.argv[1:]:
        if a.startswith('--session='):
            session = a.split('=', 1)[1]
        elif a == '--session' and len(sys.argv) > sys.argv.index(a) + 1:
            session = sys.argv[sys.argv.index(a) + 1]

    if session not in ('day', 'night'):
        print(f"[sim_trade] 错误: --session 只能是 day 或 night，当前值: {session}", file=sys.stderr)
        sys.exit(2)

    # v1.3: 资金健康检查（处理/检查 pending 时告警）
    if not (('--status' in args) or ('--summary' in args) or ('--help' in args)):
        updated = _check_capital_health()
        # 如果资本健康检查更新了状态（负天数累计），轻量写盘
        if updated is not None:
            STATE_JSON.write_text(json.dumps(updated, ensure_ascii=False, indent=2))

    if '--status' in args:
        show_status()
    elif '--check-pending' in args:
        check_pending_signals()
    elif '--summary' in args:
        show_summary()
    elif '--auto-reduce' in args:
        force = '--force' in args
        auto_reduce(force=force)
    elif '--help' in args:
        print(__doc__)
    else:
        process(session)


if __name__ == "__main__":
    main()
