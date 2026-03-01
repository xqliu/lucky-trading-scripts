"""
OKX BB Executor — Signal Detection → Order Execution
=====================================================
Atomic execution: open position → set SL → set TP.
If SL/TP fails → emergency close immediately.

Runs on 30m candle close (cron or WS trigger).
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

from core.types import Direction, Signal, Position, ExitReason, TradeResult
from core.state import load_state, save_state
from core.notify import send_discord
from okx_bb.config import load_config, OKXConfig
from okx_bb.exchange import OKXClient
from okx_bb.strategy import detect_signal

logger = logging.getLogger(__name__)

# State file
STATE_DIR = Path(__file__).parent / "state"
POSITION_STATE_FILE = STATE_DIR / "position_state.json"
TRADE_LOG_FILE = STATE_DIR / "trade_log.json"


class BBExecutor:
    """BB Breakout execution engine for OKX."""

    def __init__(self, config: Optional[OKXConfig] = None):
        self.cfg = config or load_config()
        self.client = OKXClient(
            self.cfg.api_key, self.cfg.secret_key, self.cfg.passphrase
        )
        self.instId = self.cfg.instId

    # === State Management ===

    def load_position(self) -> Optional[dict]:
        """Load persisted position state."""
        state = load_state(POSITION_STATE_FILE)
        return state.get("position")

    def save_position(self, pos: Optional[dict]):
        """Persist position state."""
        save_state(POSITION_STATE_FILE, {"position": pos})

    # === Market Data ===

    def fetch_candles(self, limit: int = 300) -> list:
        """Fetch 30m candles from OKX."""
        return self.client.get_candles(self.instId, bar="30m", limit=limit)

    def get_closes(self, candles: list) -> list:
        """Extract close prices from candles."""
        return [c["c"] for c in candles]

    # === Signal Detection ===

    def check_signal(self) -> Optional[str]:
        """Check for BB breakout signal on latest data.

        Returns 'LONG', 'SHORT', or None.
        """
        candles = self.fetch_candles(limit=300)
        if len(candles) < 120:
            logger.warning(f"Insufficient candles: {len(candles)}")
            return None

        closes = self.get_closes(candles)
        idx = len(closes) - 1

        signal = detect_signal(
            closes,
            bb_period=self.cfg.strategy.bb_period,
            bb_mult=self.cfg.strategy.bb_multiplier,
            trend_period=self.cfg.strategy.trend_ema_period,
            trend_lookback=self.cfg.strategy.trend_lookback,
            idx=idx,
        )

        if signal:
            logger.info(f"Signal detected: {signal} at price {closes[-1]:.2f}")
        return signal

    # === Position Sizing ===

    def calculate_size(self) -> Optional[str]:
        """Calculate position size based on account equity and risk params.

        Returns size as string (OKX contract units).
        """
        balance = self.client.get_balance()
        equity = balance.get("total_equity", 0)
        if equity <= 0:
            logger.error("No account equity")
            return None

        # Notional value = equity * position_ratio
        notional = equity * self.cfg.risk.position_ratio

        # Max loss check
        max_loss = notional * self.cfg.risk.stop_loss_pct
        if max_loss > self.cfg.risk.max_single_loss:
            notional = self.cfg.risk.max_single_loss / self.cfg.risk.stop_loss_pct

        # Get instrument info for contract multiplier
        inst = self.client.get_instrument(self.instId)
        if not inst:
            logger.error("Failed to get instrument info")
            return None

        ctVal = float(inst.get("ctVal", 0.01))  # contract value in coin
        lotSz = float(inst.get("lotSz", 0.01))  # minimum size increment
        minSz = float(inst.get("minSz", 0.01))  # minimum order size
        ticker = self.client.get_ticker(self.instId)
        if not ticker:
            logger.error("Failed to get ticker")
            return None

        price = ticker["last"]
        # contracts = notional / (ctVal * price), rounded down to lotSz
        contracts = notional / (ctVal * price)
        # Round down to lot size precision
        contracts = int(contracts / lotSz) * lotSz
        if contracts < minSz:
            contracts = minSz

        logger.info(f"Position sizing: equity=${equity:.2f}, "
                     f"notional=${notional:.2f}, contracts={contracts}, "
                     f"lotSz={lotSz}, ctVal={ctVal}")
        return f"{contracts:.2f}"

    # === Order Execution ===

    def open_position(self, direction: str) -> bool:
        """Atomic: market open → set SL → set TP. Abort on failure.

        Returns True if position opened successfully with SL+TP.
        """
        # Check no existing position
        existing = self.client.get_positions(self.instId)
        if existing is None:
            logger.error("Cannot check existing positions (API error), aborting open")
            return False
        if any(float(p.get("pos", 0)) != 0 for p in existing):
            logger.warning("Already have open position, skipping")
            return False

        # Calculate size
        sz = self.calculate_size()
        if not sz:
            return False

        side = "buy" if direction == "LONG" else "sell"
        close_side = "sell" if direction == "LONG" else "buy"

        # Set leverage first
        self.client.set_leverage(self.instId, "5", "isolated")

        # 1. Market order
        logger.info(f"Opening {direction} {sz} contracts on {self.instId}")
        result = self.client.place_market_order(self.instId, side, sz)

        if result.get("code") != "0":
            logger.error(f"Market order failed: {result}")
            send_discord(f"❌ OKX BB: 开仓失败\n{result.get('msg', 'unknown')}")
            return False

        if not result.get("data") or not result["data"]:
            logger.error(f"Market order returned empty data: {result}")
            send_discord(f"❌ OKX BB: 开仓返回空数据\n{result}")
            return False

        ordId = result["data"][0].get("ordId", "")
        if not ordId:
            logger.error(f"No ordId in response: {result}")
            return False

        time.sleep(2)  # wait for fill

        # Get actual fill price from order detail
        entry_price = 0
        order_detail = self.client.get_order_detail(self.instId, ordId)
        if order_detail and float(order_detail.get("accFillSz", 0)) > 0:
            entry_price = float(order_detail.get("avgPx", 0))

        if entry_price <= 0:
            # Fallback to ticker (shouldn't happen for market order)
            ticker = self.client.get_ticker(self.instId)
            entry_price = ticker["last"] if ticker else 0
            logger.warning(f"Using ticker price as fallback: {entry_price}")

        if entry_price <= 0:
            # CRITICAL: Cannot calculate SL/TP with price=0
            logger.error("CRITICAL: entry_price=0, cannot set SL/TP. Emergency close!")
            self._emergency_close(close_side, sz)
            send_discord("🚨 OKX BB: 无法获取入场价格，紧急平仓", mention=True)
            return False

        # Calculate SL/TP prices
        if direction == "LONG":
            sl_price = entry_price * (1 - self.cfg.risk.stop_loss_pct)
            tp_price = entry_price * (1 + self.cfg.risk.take_profit_pct)
        else:
            sl_price = entry_price * (1 + self.cfg.risk.stop_loss_pct)
            tp_price = entry_price * (1 - self.cfg.risk.take_profit_pct)

        # 2. Stop-loss (algo order)
        sl_result = self.client.place_stop_order(
            self.instId, close_side, sz,
            slTriggerPx=f"{sl_price:.2f}",
        )

        if sl_result.get("code") != "0" or not sl_result.get("data"):
            logger.error(f"SL order failed: {sl_result}")
            logger.error("EMERGENCY: SL failed, closing position immediately")
            self._emergency_close(close_side, sz)
            send_discord(f"🚨 OKX BB: 止损设置失败，紧急平仓\n{sl_result.get('msg')}")
            return False

        sl_algo_id = sl_result["data"][0].get("algoId", "") if sl_result["data"] else ""

        # 3. Take-profit (limit order, reduceOnly to prevent accidental opens)
        tp_result = self.client.place_limit_order(
            self.instId, close_side, sz,
            px=f"{tp_price:.2f}",
            reduceOnly=True,
        )

        if tp_result.get("code") != "0" or not tp_result.get("data"):
            logger.error(f"TP order failed: {tp_result}")
            # TP failure is less critical — warn but keep position with SL
            send_discord(f"⚠️ OKX BB: TP设置失败，仅有SL保护\n{tp_result.get('msg')}")
            tp_ord_id = ""
        else:
            tp_ord_id = tp_result["data"][0].get("ordId", "") if tp_result["data"] else ""

        # Save position state
        now = datetime.now(timezone.utc).isoformat()
        pos_state = {
            "direction": direction,
            "entry_price": entry_price,
            "size": sz,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "sl_algo_id": sl_algo_id,
            "tp_order_id": tp_ord_id,
            "entry_time": now,
            "entry_bar_count": 0,
        }
        self.save_position(pos_state)

        # Notify
        send_discord(
            f"📊 OKX BB: {direction} {self.cfg.coin}\n"
            f"入场: ${entry_price:.2f}\n"
            f"止损: ${sl_price:.2f} ({self.cfg.risk.stop_loss_pct*100:.1f}%)\n"
            f"止盈: ${tp_price:.2f} ({self.cfg.risk.take_profit_pct*100:.1f}%)\n"
            f"合约数: {sz}",
            mention=True,
        )

        logger.info(f"Position opened: {direction} @ {entry_price:.2f}, "
                     f"SL={sl_price:.2f}, TP={tp_price:.2f}")
        return True

    def _emergency_close(self, side: str, sz: str) -> bool:
        """Emergency market close — verify position actually closed."""
        for attempt in range(3):
            # Check if already closed
            positions = self.client.get_positions(self.instId)
            if positions is not None and not any(
                float(p.get("pos", 0)) != 0 for p in positions
            ):
                logger.info("Position already closed")
                self.save_position(None)
                return True

            if positions is None:
                logger.warning(f"Cannot verify position (API error), trying close anyway")

            result = self.client.place_market_order(
                self.instId, side, sz, reduceOnly=True
            )
            if result.get("code") == "0":
                time.sleep(2)
                # Verify
                positions = self.client.get_positions(self.instId)
                if positions is not None and not any(
                    float(p.get("pos", 0)) != 0 for p in positions
                ):
                    logger.info("Emergency close successful")
                    self.save_position(None)
                    return True
            logger.warning(f"Emergency close attempt {attempt + 1} failed")
            time.sleep(3)

        logger.error("CRITICAL: Emergency close failed after 3 attempts!")
        send_discord("🚨🚨 OKX BB: 紧急平仓失败！需要手动干预！", mention=True)
        return False

    # === Position Monitoring ===

    def check_position(self) -> Optional[TradeResult]:
        """Check if open position should be closed (timeout).

        SL and TP are handled by exchange orders.
        This checks: max_hold timeout + position still exists.

        Returns TradeResult if position was closed, else None.
        """
        pos = self.load_position()
        if not pos:
            return None

        # Check if position still exists on exchange
        positions = self.client.get_positions(self.instId)

        # API error → cannot determine state, skip this cycle
        if positions is None:
            logger.warning("get_positions API failed, skipping this check")
            return None

        has_position = any(float(p.get("pos", 0)) != 0 for p in positions)

        if not has_position:
            # Position closed by SL/TP order — determine which one
            exit_reason = self._determine_exit_reason(pos)
            logger.info(f"Position closed by exchange order ({exit_reason})")
            result = self._record_closed_position(pos, exit_reason)
            self.save_position(None)

            # Also clean up any remaining orders
            self._cancel_remaining_orders(pos)
            return result

        # Check timeout
        entry_time = datetime.fromisoformat(pos["entry_time"])
        now = datetime.now(timezone.utc)
        max_hold_seconds = self.cfg.risk.max_hold_bars * 30 * 60  # bars → seconds
        elapsed = (now - entry_time).total_seconds()

        if elapsed >= max_hold_seconds:
            logger.info(f"Position timeout after {elapsed/3600:.1f}h")
            close_side = "sell" if pos["direction"] == "LONG" else "buy"

            # IMPORTANT: Close position FIRST, then cancel remaining orders.
            # If we cancel SL/TP first and close fails → naked position!
            closed = self._emergency_close(close_side, pos["size"])
            if not closed:
                logger.error("Timeout close failed — position still open with SL/TP intact")
                # DO NOT cancel SL/TP — they are still protecting the position!
                return None

            # Position is closed, now clean up remaining SL/TP orders
            if pos.get("sl_algo_id"):
                self.client.cancel_algo_order(pos["sl_algo_id"], self.instId)
            if pos.get("tp_order_id"):
                self.client.cancel_order(self.instId, pos["tp_order_id"])

            result = self._record_closed_position(pos, "timeout")

            send_discord(
                f"⏰ OKX BB: {self.cfg.coin} 持仓超时平仓\n"
                f"方向: {pos['direction']}\n"
                f"持仓时间: {elapsed/3600:.1f}h",
                mention=True,
            )
            return result

        return None

    def _determine_exit_reason(self, pos: dict) -> str:
        """Check algo order history + regular order history to determine
        if exit was SL or TP.

        Returns 'sl', 'tp', or 'unknown'.
        """
        # Check if SL algo order was triggered
        if pos.get("sl_algo_id"):
            algo_history = self.client.get_algo_order_history(
                ordType="conditional", instId=self.instId, limit=5,
                state="effective",
            )
            for order in algo_history:
                if order.get("algoId") == pos["sl_algo_id"]:
                    state = order.get("state", "")
                    if state == "effective":  # triggered
                        return "sl"
                    elif state == "canceled":
                        break  # SL was cancelled → TP likely filled

        # Check if TP limit order was filled
        if pos.get("tp_order_id"):
            detail = self.client.get_order_detail(self.instId, pos["tp_order_id"])
            if detail and detail.get("state") == "filled":
                return "tp"

        # Fallback: check fills to see exit price vs SL/TP targets
        fills = self.client.get_fills(instId=self.instId, limit=5)
        if fills:
            fill_price = float(fills[0].get("fillPx", 0))
            if fill_price > 0:
                sl = pos.get("sl_price", 0)
                tp = pos.get("tp_price", 0)
                if sl and abs(fill_price - sl) / sl < 0.005:
                    return "sl"
                if tp and abs(fill_price - tp) / tp < 0.005:
                    return "tp"

        return "unknown"

    def _cancel_remaining_orders(self, pos: dict):
        """Cancel any leftover SL/TP orders after position closed."""
        try:
            if pos.get("sl_algo_id"):
                self.client.cancel_algo_order(pos["sl_algo_id"], self.instId)
        except Exception as e:
            logger.debug(f"SL cancel (may already be done): {e}")
        try:
            if pos.get("tp_order_id"):
                self.client.cancel_order(self.instId, pos["tp_order_id"])
        except Exception as e:
            logger.debug(f"TP cancel (may already be done): {e}")

    def _get_actual_exit_price(self, pos: dict) -> float:
        """Get actual exit price from fills or order history."""
        # Check recent fills
        fills = self.client.get_fills(instId=self.instId, limit=5)
        if fills:
            # Most recent fill for this instrument
            fill_price = float(fills[0].get("fillPx", 0))
            if fill_price > 0:
                return fill_price

        # Check TP order fill
        if pos.get("tp_order_id"):
            detail = self.client.get_order_detail(self.instId, pos["tp_order_id"])
            if detail and float(detail.get("avgPx", 0)) > 0:
                return float(detail["avgPx"])

        # Fallback to ticker (last resort)
        ticker = self.client.get_ticker(self.instId)
        if ticker:
            logger.warning("Using ticker price as exit price fallback")
            return ticker["last"]
        return pos["entry_price"]

    def _record_closed_position(self, pos: dict, reason: str) -> TradeResult:
        """Record a closed trade to log file.

        Args:
            reason: 'sl', 'tp', 'timeout', 'unknown'
        """
        exit_price = self._get_actual_exit_price(pos)

        if pos["direction"] == "LONG":
            pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"]
        else:
            pnl_pct = (pos["entry_price"] - exit_price) / pos["entry_price"]

        # Deduct fees
        fee_pct = self.cfg.fees.taker_fee * 2  # entry + exit
        net_pnl_pct = pnl_pct - fee_pct

        # Map reason string to ExitReason enum
        reason_map = {
            "sl": ExitReason.SL,
            "tp": ExitReason.TP,
            "timeout": ExitReason.TIMEOUT,
            "unknown": ExitReason.TIMEOUT,  # unknown exit reason — don't mislabel as TP
        }
        exit_reason = reason_map.get(reason, ExitReason.TIMEOUT)

        result = TradeResult(
            coin=self.cfg.coin,
            direction=Direction(pos["direction"]),
            entry_price=pos["entry_price"],
            exit_price=exit_price,
            size=float(pos["size"]),
            pnl_pct=net_pnl_pct,
            pnl_usd=0,  # TODO: calculate from actual fills
            entry_time=datetime.fromisoformat(pos["entry_time"]),
            exit_time=datetime.now(timezone.utc),
            exit_reason=exit_reason,
            strategy="bb_breakout",
            fees_usd=0,
        )

        # Append to trade log
        self._append_trade_log(result)
        return result

    def _append_trade_log(self, result: TradeResult):
        """Append trade result to JSON log."""
        import json
        log_path = TRADE_LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)

        log = []
        if log_path.exists():
            try:
                log = json.loads(log_path.read_text())
            except Exception:
                pass

        log.append({
            "coin": result.coin,
            "direction": result.direction.value,
            "entry_price": result.entry_price,
            "exit_price": result.exit_price,
            "pnl_pct": result.pnl_pct,
            "entry_time": result.entry_time.isoformat(),
            "exit_time": result.exit_time.isoformat(),
            "exit_reason": result.exit_reason.value,
        })

        # Write directly (save_state expects dict, we have list)
        tmp = log_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(log, indent=2, default=str))
        tmp.rename(log_path)

    # === Main Loop ===

    def run_once(self) -> str:
        """Single execution cycle (for cron).

        1. Check existing position (timeout/SL/TP hit)
        2. If no position, check for new signal
        3. If signal, open position

        Returns status string.
        """
        # Check existing position
        result = self.check_position()
        if result:
            return f"Position closed: {result.exit_reason.value} PnL={result.pnl_pct*100:+.2f}%"

        # If still in position, done
        pos = self.load_position()
        if pos:
            entry = pos.get("entry_price", 0)
            ticker = self.client.get_ticker(self.instId)
            current = ticker["last"] if ticker else entry
            if pos["direction"] == "LONG":
                unrealized = (current - entry) / entry * 100
            else:
                unrealized = (entry - current) / entry * 100
            return f"In position: {pos['direction']} {self.cfg.coin} @ {entry:.2f}, unrealized={unrealized:+.2f}%"

        # Check for new signal
        signal = self.check_signal()
        if signal:
            success = self.open_position(signal)
            if success:
                return f"Opened {signal} position"
            return f"Signal {signal} but open failed"

        return "No signal"


def main():
    """CLI entry point."""
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    parser = argparse.ArgumentParser(description="OKX BB Executor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Check signal only, don't trade")
    parser.add_argument("--status", action="store_true",
                        help="Show current position status")
    args = parser.parse_args()

    executor = BBExecutor()

    if args.status:
        pos = executor.load_position()
        if pos:
            print(f"Position: {pos['direction']} {executor.cfg.coin} @ {pos['entry_price']:.2f}")
            print(f"  SL: {pos['sl_price']:.2f}")
            print(f"  TP: {pos['tp_price']:.2f}")
            print(f"  Entry: {pos['entry_time']}")
        else:
            print("No open position")

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
