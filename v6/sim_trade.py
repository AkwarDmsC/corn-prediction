#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v6 轻量模拟交易引擎。

用法:
  python3 v6/sim_trade.py --backfill
  python3 v6/sim_trade.py --status
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import HISTORY_DIR, PREDICTIONS_JSON

TRADING_LOG = HISTORY_DIR / "trading_log.json"
CONTRACT_TONS = 10.0
DEFAULT_QTY = 1.0


@dataclass
class TradeRecord:
    date: str
    side: str  # "long" | "short" | "close"
    price: float
    qty: float
    pnl: float
    reason: str


@dataclass
class SimAccount:
    initial_capital: float = 100_000.0
    cash: float = 100_000.0
    position: float = 0.0
    avg_price: float = 0.0
    trades: List[TradeRecord] = field(default_factory=list)
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)

    def mark_equity(self, date: str, price: float) -> float:
        unrealized = _position_pnl(self.position, self.avg_price, price)
        equity = self.cash + unrealized
        self.equity_curve.append({
            "date": date,
            "price": round(price, 2),
            "cash": round(self.cash, 2),
            "position": self.position,
            "avg_price": round(self.avg_price, 2),
            "equity": round(equity, 2),
            "unrealized_pnl": round(unrealized, 2),
        })
        return equity

    def open_position(self, date: str, side: str, price: float, qty: float, reason: str) -> TradeRecord:
        signed_qty = qty if side == "long" else -qty
        self.position = signed_qty
        self.avg_price = price
        trade = TradeRecord(date=date, side=side, price=price, qty=qty, pnl=0.0, reason=reason)
        self.trades.append(trade)
        return trade

    def close_position(self, date: str, price: float, reason: str) -> Optional[TradeRecord]:
        if self.position == 0:
            return None
        qty = abs(self.position)
        pnl = _position_pnl(self.position, self.avg_price, price)
        self.cash += pnl
        trade = TradeRecord(date=date, side="close", price=price, qty=qty, pnl=round(pnl, 2), reason=reason)
        self.trades.append(trade)
        self.position = 0.0
        self.avg_price = 0.0
        return trade


def _position_pnl(position: float, entry_price: float, price: float) -> float:
    if position == 0:
        return 0.0
    if position > 0:
        return (price - entry_price) * abs(position) * CONTRACT_TONS
    return (entry_price - price) * abs(position) * CONTRACT_TONS


def _load_predictions() -> List[Dict[str, Any]]:
    if not PREDICTIONS_JSON.exists():
        return []
    return json.loads(PREDICTIONS_JSON.read_text(encoding="utf-8"))


def _load_log() -> Dict[str, Any]:
    if not TRADING_LOG.exists():
        return {}
    return json.loads(TRADING_LOG.read_text(encoding="utf-8"))


def _save_log(payload: Dict[str, Any]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    TRADING_LOG.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _direction_from_text(text: str) -> Optional[str]:
    if "偏强" in text or "看涨" in text:
        return "long"
    if "偏弱" in text or "看跌" in text:
        return "short"
    return None


def _risk_worsened(record: Dict[str, Any], wanted_side: str) -> bool:
    indicators = record.get("indicators") or record.get("signal", {}).get("indicators", {})
    trend_filter = str(indicators.get("trend_filter", ""))
    ma_dir = indicators.get("ma_dir")
    if trend_filter.startswith("counter_trend_blocked"):
        return True
    if wanted_side == "long" and ma_dir is not None and float(ma_dir) < 0:
        return True
    if wanted_side == "short" and ma_dir is not None and float(ma_dir) > 0:
        return True
    return False


def _iter_verified_signals(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    signals = []
    for rec in records:
        verified = rec.get("verified") or {}
        for session in ("day", "night"):
            result = verified.get(session)
            if not result:
                continue
            signals.append({
                "record": rec,
                "session": session,
                "date": result.get("input_date") or rec.get("next_trading_day") or rec.get("input_date", ""),
                "price": float(result.get("actual_close", 0.0) or 0.0),
                "direction_correct": bool(result.get("direction_correct")),
                "range_hit": bool(result.get("range_hit")),
                "direction": (rec.get(session) or {}).get("direction", ""),
                "confidence": result.get("confidence", ""),
            })
    return sorted(signals, key=lambda x: (x["date"], x["session"]))


def _snapshot(account: SimAccount, last_trade: Optional[TradeRecord], price: float, date: str) -> Dict[str, Any]:
    equity = account.cash + _position_pnl(account.position, account.avg_price, price)
    return {
        "date": date,
        "cash": round(account.cash, 2),
        "position": account.position,
        "avg_price": round(account.avg_price, 2),
        "mark_price": round(price, 2),
        "equity": round(equity, 2),
        "pnl": round(equity - account.initial_capital, 2),
        "last_trade": asdict(last_trade) if last_trade else None,
    }


def backfill_trading_log() -> Dict[str, Any]:
    records = _load_predictions()
    account = SimAccount()
    log_records: List[Dict[str, Any]] = []
    last_trade: Optional[TradeRecord] = None

    for item in _iter_verified_signals(records):
        rec = item["record"]
        date = item["date"]
        price = item["price"]
        wanted_side = _direction_from_text(item["direction"])

        if account.position != 0:
            current_side = "long" if account.position > 0 else "short"
            should_close = False
            reasons = []
            if wanted_side and wanted_side != current_side:
                should_close = True
                reasons.append("signal_reversal")
            if _risk_worsened(rec, current_side):
                should_close = True
                reasons.append("ma60_trend_filter_worsened")
            if not item["range_hit"]:
                reasons.append("range_missed")
            if should_close:
                last_trade = account.close_position(date, price, ",".join(reasons))

        if account.position == 0 and item["direction_correct"] and wanted_side and not _risk_worsened(rec, wanted_side):
            last_trade = account.open_position(
                date,
                wanted_side,
                price,
                DEFAULT_QTY,
                f"verified_{item['session']}_direction_correct",
            )

        account.mark_equity(date, price)
        copied = json.loads(json.dumps(rec, ensure_ascii=False))
        copied["sim_state"] = _snapshot(account, last_trade, price, date)
        log_records.append(copied)

    final_price = account.equity_curve[-1]["price"] if account.equity_curve else 0.0
    final_equity = account.equity_curve[-1]["equity"] if account.equity_curve else account.cash
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(PREDICTIONS_JSON),
        "initial_capital": account.initial_capital,
        "cash": round(account.cash, 2),
        "position": account.position,
        "avg_price": round(account.avg_price, 2),
        "mark_price": final_price,
        "final_equity": round(final_equity, 2),
        "total_pnl": round(final_equity - account.initial_capital, 2),
        "trade_count": len(account.trades),
        "trades": [asdict(t) for t in account.trades],
        "equity_curve": account.equity_curve,
        "records": log_records,
    }
    _save_log(payload)
    return payload


def print_status() -> None:
    payload = _load_log() or backfill_trading_log()
    print("=== v6 模拟账户状态 ===")
    print(f"更新时间: {payload.get('generated_at', '')}")
    print(f"初始资金: {payload.get('initial_capital', 100000.0):,.2f}")
    print(f"当前权益: {payload.get('final_equity', 100000.0):,.2f}")
    print(f"累计盈亏: {payload.get('total_pnl', 0.0):+,.2f}")
    print(f"现金: {payload.get('cash', 100000.0):,.2f}")
    print(f"持仓: {payload.get('position', 0.0)} 手 @ {payload.get('avg_price', 0.0)}")
    print(f"交易数: {payload.get('trade_count', 0)}")
    trades = payload.get("trades") or []
    if trades:
        last = trades[-1]
        print(f"最后交易: {last['date']} {last['side']} {last['qty']}手 @ {last['price']} pnl={last['pnl']:+.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="v6 simulated trading account")
    parser.add_argument("--backfill", action="store_true", help="rebuild trading log from verified v6 predictions")
    parser.add_argument("--status", action="store_true", help="show current simulated account summary")
    args = parser.parse_args()

    if args.backfill:
        payload = backfill_trading_log()
        print(f"✅ trading log: {TRADING_LOG}")
        print(f"records={len(payload['records'])}, trades={payload['trade_count']}, pnl={payload['total_pnl']:+.2f}")
    elif args.status:
        print_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
