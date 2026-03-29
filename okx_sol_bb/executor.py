"""
OKX SOL BB Executor — Mean-Reversion Signal → Order Execution
===============================================================
Atomic execution: open position → set SL → set TP.
If SL/TP fails → emergency close immediately.

Reuses OKXClient from okx_bb.exchange (same API wrapper).
Strategy: BB mean-reversion (opposite of ETH BB breakout).
"""
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_parent = str(Path(__file__).parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from core.types import Direction, ExitReason, TradeResult
from core.state import load_state, save_state
from core.notify import send_discord
from okx_bb.exchange import OKXClient  # Reuse exchange wrapper
from okx_sol_bb.config import load_config, OKXSolConfig
from okx_sol_bb.strategy import detect_signal

logger = logging.getLogger(__name__)

# State files — separate from ETH BB
STATE_DIR = Path(__file__).parent / "state"
POSITION_STATE_FILE = STATE_DIR / "position_state.json"
TRADE_LOG_FILE = STATE_DIR / "trade_log.json"


class SolBBExecutor:
    """BB Mean-Reversion execution engine for SOL on OKX."""

    def __init__(self, config: Optional[OKXSolConfig] = None):
        self.cfg = config or load_config()
        self.client = OKXClient(
            self.cfg.api_key, self.cfg.secret_key, self.cfg.passphrase
        )
        self.instId = self.cfg.instId

    # === State Management ===

    def load_position(self) -> Optional[dict]:
        state = load_state(POSITION_STATE_FILE)
        return state.get("position")

    def save_position(self, pos: Optional[dict]):
        """Persist position state. Safety: never clear if exchange still has position."""
        if pos is None:
            positions = self.client.get_positions(self.instId)
            if positions is None:
                logger.error("Refusing to clear local position: get_positions API failed")
                return
            if any(float(p.get("pos", 0)) != 0 for p in positions):
                logger.error("Refusing to clear local position: exchange still shows open position")
                return
        save_state(POSITION_STATE_FILE, {"position": pos})

    def _append_open_trade_log_if_missing(self, pos: dict, exchange_trade_id: Optional[str] = None,
                                          source: str = "exchange_reconcile"):
        """Ensure trade_log has an OPEN record for the current live position."""
        import json
        log_path = TRADE_LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = []
        if log_path.exists():
            try:
                loaded = json.loads(log_path.read_text())
                if isinstance(loaded, list):
                    log = loaded
            except Exception as e:
                logger.error(f"Failed to read trade log: {e}")

        direction = pos.get("direction")
        entry_price = float(pos.get("entry_price", 0) or 0)
        size = float(pos.get("size", 0) or 0)
        existing = next((x for x in log
                         if x.get("status") == "OPEN"
                         and x.get("direction") == direction
                         and abs(float(x.get("entry_price", 0) or 0) - entry_price) < 1e-9
                         and abs(float(x.get("size", 0) or 0) - size) < 1e-9), None)
        if existing:
            existing["last_sync_source"] = source
            existing["last_sync_time"] = datetime.now(timezone.utc).isoformat()
        else:
            log.append({
                "exchange_trade_id": exchange_trade_id,
                "instId": self.instId,
                "coin": self.cfg.coin,
                "direction": direction,
                "status": "OPEN",
                "entry_price": entry_price,
                "size": size,
                "entry_time": pos.get("entry_time"),
                "source": source,
                "sl_price": pos.get("sl_price"),
                "tp_price": pos.get("tp_price"),
            })

        tmp = log_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(log, indent=2, default=str))
        tmp.rename(log_path)

    def reconcile_position_from_exchange(self, source: str = "exchange_reconcile") -> Optional[dict]:
        """Rebuild local position state from exchange as source of truth."""
        positions = self.client.get_positions(self.instId)
        if positions is None:
            logger.error("reconcile: get_positions API failed; keeping local state")
            return self.load_position()

        pos_info = next((p for p in positions if float(p.get("pos", 0)) != 0), None)
        if not pos_info:
            self.save_position(None)
            return None

        pos_val = float(pos_info.get("pos", 0))
        direction = "LONG" if pos_val > 0 else "SHORT"
        avg_px = float(pos_info.get("avgPx", 0) or 0)
        pos_size = abs(pos_val)

        algos_cond = self.client.get_algo_orders(self.instId, "conditional") or []
        open_ords = self.client.get_open_orders(self.instId) or []
        close_side = "sell" if direction == "LONG" else "buy"

        sl_algo = next((a for a in algos_cond
                        if a.get("slTriggerPx") and a.get("side") == close_side), None)
        sl_price = float(sl_algo.get("slTriggerPx", 0) or 0) if sl_algo else None
        tp_order = next((o for o in open_ords
                         if o.get("side") == close_side and o.get("reduceOnly") == "true"), None)
        tp_price = float(tp_order.get("px", 0) or 0) if tp_order and tp_order.get("px") else None

        entry_time = datetime.fromtimestamp(
            int(pos_info.get("cTime", 0)) / 1000, tz=timezone.utc
        ).isoformat() if pos_info.get("cTime") else datetime.now(timezone.utc).isoformat()

        pos_state = {
            "direction": direction, "entry_price": avg_px,
            "size": f"{pos_size:.2f}",
            "sl_price": sl_price, "tp_price": tp_price,
            "sl_algo_id": sl_algo.get("algoId", "") if sl_algo else "",
            "tp_order_id": tp_order.get("ordId", "") if tp_order else "",
            "entry_time": entry_time, "entry_bar_count": 0,
            "last_sync_source": source,
            "last_sync_time": datetime.now(timezone.utc).isoformat(),
        }
        self.save_position(pos_state)
        self._append_open_trade_log_if_missing(pos_state, source=source)
        return pos_state

    # === Market Data ===

    def fetch_candles(self, limit: int = 300) -> list:
        return self.client.get_candles(self.instId, bar="30m", limit=limit)

    def get_closes(self, candles: list) -> list:
        return [c["c"] for c in candles]

    # === Signal Detection ===

    def check_signal(self) -> Optional[str]:
        candles = self.fetch_candles(limit=300)
        if len(candles) < 30:
            logger.warning(f"Insufficient candles: {len(candles)}")
            return None

        closes = self.get_closes(candles)
        idx = len(closes) - 1

        signal = detect_signal(
            closes,
            bb_period=self.cfg.strategy.bb_period,
            bb_mult=self.cfg.strategy.bb_multiplier,
            idx=idx,
            min_bb_width=self.cfg.strategy.min_bb_width,
        )

        if signal:
            logger.info(f"Signal detected: {signal} at price {closes[-1]:.2f}")
        return signal

    # === Position Sizing ===

    def calculate_size(self) -> Optional[str]:
        balance = self.client.get_balance()
        equity = balance.get("total_equity", 0)
        if equity <= 0:
            logger.error("No account equity")
            return None

        notional = equity * self.cfg.risk.position_ratio
        max_loss = notional * self.cfg.risk.stop_loss_pct
        if max_loss > self.cfg.risk.max_single_loss:
            notional = self.cfg.risk.max_single_loss / self.cfg.risk.stop_loss_pct

        inst = self.client.get_instrument(self.instId)
        if not inst:
            logger.error("Failed to get instrument info")
            return None

        ctVal_raw = inst.get("ctVal")
        if not ctVal_raw:
            logger.error(f"Instrument {self.instId} missing ctVal — refusing to size position")
            return None
        ctVal = float(ctVal_raw)  # SOL contract value
        lotSz = float(inst.get("lotSz", 1))
        minSz = float(inst.get("minSz", 1))
        ticker = self.client.get_ticker(self.instId)
        if not ticker:
            logger.error("Failed to get ticker")
            return None

        price = ticker["last"]
        contracts = notional / (ctVal * price)
        contracts = int(contracts / lotSz) * lotSz
        if contracts < minSz:
            contracts = minSz

        logger.info(f"Position sizing: equity=${equity:.2f}, notional=${notional:.2f}, "
                     f"contracts={contracts}, ctVal={ctVal}, lotSz={lotSz}")
        return f"{contracts:.2f}"

    # === Order Execution ===

    def open_position(self, direction: str) -> bool:
        """Atomic: market open → set SL → set TP."""
        existing = self.client.get_positions(self.instId)
        if existing is None:
            logger.error("Cannot check existing positions, aborting")
            return False
        if any(float(p.get("pos", 0)) != 0 for p in existing):
            logger.warning("Already have open position, skipping")
            return False

        sz = self.calculate_size()
        if not sz:
            return False

        side = "buy" if direction == "LONG" else "sell"
        close_side = "sell" if direction == "LONG" else "buy"

        logger.info(f"Opening {direction} {sz} contracts on {self.instId}")
        result = self.client.place_market_order(self.instId, side, sz)

        if result.get("code") != "0" or not result.get("data"):
            logger.error(f"Market order failed: {result}")
            send_discord(f"❌ OKX SOL BB: 开仓失败\n{result.get('msg', 'unknown')}")
            return False

        ordId = result["data"][0].get("ordId", "")
        if not ordId:
            logger.error(f"No ordId: {result}")
            return False

        time.sleep(2)

        entry_price = 0
        order_detail = self.client.get_order_detail(self.instId, ordId)
        if order_detail and float(order_detail.get("accFillSz", 0)) > 0:
            entry_price = float(order_detail.get("avgPx", 0))

        if entry_price <= 0:
            ticker = self.client.get_ticker(self.instId)
            entry_price = ticker["last"] if ticker else 0

        if entry_price <= 0:
            logger.error("CRITICAL: entry_price=0, emergency close!")
            self._emergency_close(close_side, sz)
            send_discord("🚨 OKX SOL BB: 无法获取入场价格，紧急平仓", mention=True)
            return False

        if direction == "LONG":
            sl_price = entry_price * (1 - self.cfg.risk.stop_loss_pct)
            tp_price = entry_price * (1 + self.cfg.risk.take_profit_pct)
        else:
            sl_price = entry_price * (1 + self.cfg.risk.stop_loss_pct)
            tp_price = entry_price * (1 - self.cfg.risk.take_profit_pct)

        # SL — CRITICAL
        sl_result = self.client.place_stop_order(
            self.instId, close_side, sz, slTriggerPx=f"{sl_price:.2f}")

        if sl_result.get("code") != "0" or not sl_result.get("data"):
            logger.error(f"SL failed: {sl_result} — EMERGENCY CLOSE!")
            self._emergency_close(close_side, sz)
            send_discord(f"🚨 OKX SOL BB: 止损设置失败，紧急平仓\n{sl_result.get('msg')}")
            return False

        sl_algo_id = sl_result["data"][0].get("algoId", "")

        # Verify SL is actually live on exchange (retry to handle OKX eventual consistency)
        sl_live = False
        for delay in (1, 2, 2):
            time.sleep(delay)
            algos = self.client.get_algo_orders(self.instId, "conditional")
            if any(a.get("algoId") == sl_algo_id for a in (algos or [])):
                sl_live = True
                break
            logger.warning(f"SL {sl_algo_id} not visible yet, retrying...")
        if not sl_live:
            logger.error(f"SL {sl_algo_id} not live after 3 checks — emergency close!")
            self._emergency_close(close_side, sz)
            send_discord(f"🚨 OKX SOL BB: 止损未激活，紧急平仓", mention=True)
            return False

        # TP
        tp_result = self.client.place_limit_order(
            self.instId, close_side, sz, px=f"{tp_price:.2f}", reduceOnly=True)
        tp_ord_id = ""
        if tp_result.get("code") == "0" and tp_result.get("data"):
            tp_ord_id = tp_result["data"][0].get("ordId", "")
        else:
            logger.error(f"TP failed (SL active): {tp_result}")
            send_discord(f"⚠️ OKX SOL BB: TP设置失败，仅有SL保护")

        # Get ctVal for PnL calculation at close time
        inst = self.client.get_instrument(self.instId)
        ct_val = float(inst.get("ctVal", self.CONTRACT_SIZE)) if inst else self.CONTRACT_SIZE

        now = datetime.now(timezone.utc).isoformat()
        pos_state = {
            "direction": direction, "entry_price": entry_price,
            "size": sz, "sl_price": sl_price, "tp_price": tp_price,
            "sl_algo_id": sl_algo_id, "tp_order_id": tp_ord_id,
            "entry_time": now, "entry_bar_count": 0,
            "ct_val": ct_val,
        }
        self.save_position(pos_state)

        send_discord(
            f"📊 OKX SOL BB: {direction} {self.cfg.coin}\n"
            f"入场: ${entry_price:.2f}\n"
            f"止损: ${sl_price:.2f} ({self.cfg.risk.stop_loss_pct*100:.1f}%)\n"
            f"止盈: ${tp_price:.2f} ({self.cfg.risk.take_profit_pct*100:.1f}%)\n"
            f"合约: {sz}",
            mention=True,
        )
        return True

    def _emergency_close(self, side: str, sz: str) -> bool:
        """Emergency market close — uses exchange size instead of caller sz."""
        for attempt in range(3):
            positions = self.client.get_positions(self.instId)
            if positions is not None and not any(float(p.get("pos", 0)) != 0 for p in positions):
                self.save_position(None)
                return True

            # Use exchange size if available (caller sz may be stale after partial fill)
            actual_sz = sz
            if positions:
                for p in positions:
                    pv = abs(float(p.get("pos", 0)))
                    if pv > 0:
                        actual_sz = f"{pv:.2f}"
                        if actual_sz != sz:
                            logger.warning(f"Size mismatch: caller={sz} exchange={actual_sz}, using exchange")
                        break

            result = self.client.place_market_order(self.instId, side, actual_sz, reduceOnly=True)
            if result.get("code") == "0":
                time.sleep(2)
                positions = self.client.get_positions(self.instId)
                if positions is not None and not any(float(p.get("pos", 0)) != 0 for p in positions):
                    self.save_position(None)
                    return True
            logger.warning(f"Emergency close attempt {attempt + 1} failed")
            time.sleep(3)

        logger.error("CRITICAL: Emergency close failed after 3 attempts!")
        send_discord("🚨🚨 OKX SOL BB: 紧急平仓失败！需要手动干预！", mention=True)
        return False

    # === Position Monitoring ===

    def check_position(self) -> Optional[TradeResult]:
        """Check if position should be closed (timeout). SL/TP by exchange orders."""
        pos = self.load_position()
        if not pos:
            return None

        positions = self.client.get_positions(self.instId)
        if positions is None:
            logger.warning("get_positions API failed, skipping")
            return None

        has_position = any(float(p.get("pos", 0)) != 0 for p in positions)

        if not has_position:
            exit_reason = self._determine_exit_reason(pos)
            result = self._record_closed_position(pos, exit_reason)
            self.save_position(None)
            self._cancel_remaining_orders(pos)
            return result

        entry_time = datetime.fromisoformat(pos["entry_time"])
        now = datetime.now(timezone.utc)
        max_hold_seconds = self.cfg.risk.max_hold_bars * 30 * 60
        elapsed = (now - entry_time).total_seconds()

        if elapsed >= max_hold_seconds:
            logger.info(f"Position timeout after {elapsed/3600:.1f}h")
            close_side = "sell" if pos["direction"] == "LONG" else "buy"
            closed = self._emergency_close(close_side, pos["size"])
            if not closed:
                return None

            if pos.get("sl_algo_id"):
                self.client.cancel_algo_order(pos["sl_algo_id"], self.instId)
            if pos.get("tp_order_id"):
                self.client.cancel_order(self.instId, pos["tp_order_id"])

            result = self._record_closed_position(pos, "timeout")
            send_discord(
                f"⏰ OKX SOL BB: 持仓超时平仓\n方向: {pos['direction']}\n持仓: {elapsed/3600:.1f}h",
                mention=True)
            return result

        return None

    def _determine_exit_reason(self, pos: dict) -> str:
        if pos.get("sl_algo_id"):
            algo_history = self.client.get_algo_order_history(
                ordType="conditional", instId=self.instId, limit=5, state="effective")
            for order in algo_history:
                if order.get("algoId") == pos["sl_algo_id"]:
                    if order.get("state") == "effective":
                        return "sl"

        if pos.get("tp_order_id"):
            detail = self.client.get_order_detail(self.instId, pos["tp_order_id"])
            if detail and detail.get("state") == "filled":
                return "tp"

        fills = self.client.get_fills(instId=self.instId, limit=5)
        if fills:
            fill_price = float(fills[0].get("fillPx", 0))
            if fill_price > 0:
                sl = pos.get("sl_price", 0)
                tp = pos.get("tp_price", 0)
                if sl and abs(fill_price - sl) / sl < 0.015:
                    return "sl"
                if tp and abs(fill_price - tp) / tp < 0.015:
                    return "tp"

        return "unknown"

    # SOL-USDT-SWAP: 1 contract = 1 SOL
    CONTRACT_SIZE = 1

    def position_status(self, pos: Optional[dict] = None) -> str:
        if pos is None:
            pos = self.load_position()
        if not pos:
            return "No position"

        entry = float(pos.get("entry_price", 0) or 0)
        size_contracts = float(pos.get("size", 0) or 0)
        size_coin = size_contracts * self.CONTRACT_SIZE
        ticker = self.client.get_ticker(self.instId)
        current = ticker["last"] if ticker else entry

        if pos["direction"] == "LONG":
            pnl_usd = (current - entry) * size_coin
        else:
            pnl_usd = (entry - current) * size_coin

        balance = self.client.get_balance()
        account_equity = balance.get("total_equity", 0)
        pnl_pct = pnl_usd / account_equity * 100 if account_equity > 0 else 0

        sl_text = f"${float(pos['sl_price']):.2f}" if pos.get("sl_price") is not None else "None"
        tp_text = f"${float(pos['tp_price']):.2f}" if pos.get("tp_price") is not None else "None"

        return (
            f"In position: {pos['direction']} {self.cfg.coin} @ ${entry:.2f}\n"
            f"  Size: {size_contracts} contracts ({size_coin:.2f} {self.cfg.coin})\n"
            f"  PnL: ${pnl_usd:+.2f} ({pnl_pct:+.2f}% of account)\n"
            f"  SL: {sl_text} | TP: {tp_text}\n"
            f"  Entry: {pos.get('entry_time', 'unknown')}"
        )

    def _cancel_remaining_orders(self, pos: dict):
        try:
            if pos.get("sl_algo_id"):
                self.client.cancel_algo_order(pos["sl_algo_id"], self.instId)
        except Exception as e:
            logger.debug(f"SL cancel: {e}")
        try:
            if pos.get("tp_order_id"):
                self.client.cancel_order(self.instId, pos["tp_order_id"])
        except Exception as e:
            logger.debug(f"TP cancel: {e}")

    def _get_actual_exit_info(self, pos: dict) -> tuple:
        now = datetime.now(timezone.utc)
        fills = self.client.get_fills(instId=self.instId, limit=5)
        if fills:
            fill = fills[0]
            fill_price = float(fill.get("fillPx", 0))
            if fill_price > 0:
                fill_ts = int(fill.get("ts", 0))
                fill_time = datetime.fromtimestamp(fill_ts / 1000, tz=timezone.utc) if fill_ts else now
                return fill_price, fill_time

        if pos.get("tp_order_id"):
            detail = self.client.get_order_detail(self.instId, pos["tp_order_id"])
            if detail and float(detail.get("avgPx", 0)) > 0:
                detail_ts = int(detail.get("uTime", 0))
                detail_time = datetime.fromtimestamp(detail_ts / 1000, tz=timezone.utc) if detail_ts else now
                return float(detail["avgPx"]), detail_time

        ticker = self.client.get_ticker(self.instId)
        if ticker:
            return ticker["last"], now
        return pos["entry_price"], now

    def _record_closed_position(self, pos: dict, reason: str) -> TradeResult:
        exit_price, exit_time = self._get_actual_exit_info(pos)

        if pos["direction"] == "LONG":
            pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"]
        else:
            pnl_pct = (pos["entry_price"] - exit_price) / pos["entry_price"]

        fee_pct = self.cfg.fees.taker_fee * 2
        net_pnl_pct = pnl_pct - fee_pct

        reason_map = {"sl": ExitReason.SL, "tp": ExitReason.TP,
                       "timeout": ExitReason.TIMEOUT, "unknown": ExitReason.TIMEOUT}
        exit_reason = reason_map.get(reason, ExitReason.TP)

        # Calculate USD PnL and fees
        ct_val = float(pos.get("ct_val", self.CONTRACT_SIZE))
        size = float(pos["size"])
        notional = pos["entry_price"] * size * ct_val
        pnl_usd = net_pnl_pct * notional

        # Get actual fees from fills if available
        fees_usd = 0.0
        try:
            fills = self.client.get_fills(instId=self.instId, limit=10)
            for f in fills:
                fee_val = float(f.get("fee", 0))
                fees_usd += abs(fee_val)
        except Exception:
            fees_usd = fee_pct * notional

        result = TradeResult(
            coin=self.cfg.coin, direction=Direction(pos["direction"]),
            entry_price=pos["entry_price"], exit_price=exit_price,
            size=size, pnl_pct=net_pnl_pct, pnl_usd=round(pnl_usd, 4),
            entry_time=datetime.fromisoformat(pos["entry_time"]),
            exit_time=exit_time, exit_reason=exit_reason,
            strategy="bb_mean_reversion", fees_usd=round(fees_usd, 4))

        self._append_trade_log(result)
        return result

    def _append_trade_log(self, result: TradeResult):
        import json
        log_path = TRADE_LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)

        log = []
        if log_path.exists():
            try:
                log = json.loads(log_path.read_text())
            except Exception:
                pass

        entry_time_str = result.entry_time.isoformat()
        direction_str = result.direction.value

        existing_close = next((x for x in log
                               if x.get("entry_time") == entry_time_str
                               and x.get("direction") == direction_str
                               and x.get("exit_price") is not None
                               and x.get("status") != "OPEN"), None)
        if existing_close:
            logger.warning(f"Skipping duplicate close: {direction_str} entry={entry_time_str}")
            return

        for row in log:
            if (row.get("status") == "OPEN"
                    and row.get("direction") == direction_str
                    and row.get("entry_time") == entry_time_str):
                row["status"] = "CLOSED"

        log.append({
            "coin": result.coin, "direction": direction_str,
            "entry_price": result.entry_price, "exit_price": result.exit_price,
            "size": result.size,
            "pnl_pct": result.pnl_pct, "pnl_usd": result.pnl_usd,
            "fees_usd": result.fees_usd,
            "entry_time": entry_time_str, "exit_time": result.exit_time.isoformat(),
            "exit_reason": result.exit_reason.value,
        })

        tmp = log_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(log, indent=2, default=str))
        tmp.rename(log_path)

    # === Main Loop ===

    def run_once(self) -> str:
        result = self.check_position()
        if result:
            return f"Position closed: {result.exit_reason.value} PnL={result.pnl_pct*100:+.2f}%"

        pos = self.load_position()
        if pos:
            return self.position_status(pos)

        signal = self.check_signal()
        if signal:
            success = self.open_position(signal)
            if success:
                return f"Opened {signal} position"
            return f"Signal {signal} but open failed"

        return "No signal"


def main():
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    parser = argparse.ArgumentParser(description="OKX SOL BB Executor")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    executor = SolBBExecutor()

    if args.status:
        print(executor.position_status())
        balance = executor.client.get_balance()
        print(f"Account: ${balance.get('total_equity', 0):.2f}")
        return

    if args.dry_run:
        signal = executor.check_signal()
        print(f"Signal: {signal or 'None'}")
        return

    status = executor.run_once()
    print(status)


if __name__ == "__main__":
    main()
