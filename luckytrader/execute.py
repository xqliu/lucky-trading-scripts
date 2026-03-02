#!/usr/bin/env python3
"""
Lucky Trading Executor v5.1
信号检测 → 开仓 → 立即设SL+TP，原子化执行

规则（不可违反）：
1. 开仓后必须立即设止损和止盈，三者是原子操作
2. 如果SL或TP设置失败，立即市价平仓
3. 同一时间最多一个持仓
4. SL = 4%, TP = 7%, 持仓上限 = 60h
5. 仓位大小 = 账户净值的 30%（含杠杆后的名义价值）
"""
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)
from luckytrader.signal import analyze, get_recent_fills, get_candles
from luckytrader.regime import compute_de, get_regime_params
from luckytrader.strategy import should_tighten_tp, compute_tp_price, compute_pnl_pct
from luckytrader.trade import (
    get_account_info, get_market_price, get_open_orders_detailed,
    place_market_order, place_stop_loss, place_take_profit, cancel_order,
    MAIN_WALLET
)
from hyperliquid.info import Info
from hyperliquid.utils import constants
from luckytrader.config import get_config, get_coin_config, TRADING_COINS

# === 系统参数 — 从 config/params.toml 加载 ===
_cfg = get_config()
STOP_LOSS_PCT = _cfg.risk.stop_loss_pct
TAKE_PROFIT_PCT = _cfg.risk.take_profit_pct
MAX_HOLD_HOURS = _cfg.risk.max_hold_hours
POSITION_RATIO = _cfg.risk.position_ratio
MAX_SINGLE_LOSS = _cfg.risk.max_single_loss
DISCORD_CHANNEL_ID = _cfg.notifications.discord_channel_id
DISCORD_MENTIONS = _cfg.notifications.discord_mentions

from luckytrader.config import get_workspace_dir

_WORKSPACE_DIR = get_workspace_dir()
STATE_FILE = _WORKSPACE_DIR / "memory" / "trading" / "position_state.json"
TRADES_FILE = _WORKSPACE_DIR / "memory" / "trading" / "TRADES.md"
TRADE_LOG_FILE = _WORKSPACE_DIR / "memory" / "trading" / "trade_results.json"
CONSEC_LOSS_THRESHOLD = _cfg.optimization.consec_loss_threshold


def _get_coin_params(coin: str):
    """Get per-coin risk/strategy params (convenience wrapper)."""
    cc = get_coin_config(coin)
    return {
        'stop_loss_pct': cc.stop_loss_pct,
        'take_profit_pct': cc.take_profit_pct,
        'max_hold_hours': cc.max_hold_hours,
        'position_ratio': cc.position_ratio,
        'max_single_loss': cc.max_single_loss,
    }

def load_trade_log():
    if TRADE_LOG_FILE.exists():
        try:
            return json.loads(TRADE_LOG_FILE.read_text())
        except Exception as e:
            print(f"⚠️ Failed to parse trade log: {e}")
            return []
    return []

def save_trade_log(log):
    TRADE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = TRADE_LOG_FILE.with_suffix('.tmp')
    tmp_file.write_text(json.dumps(log, indent=2, default=str))
    tmp_file.rename(TRADE_LOG_FILE)

def record_trade_result(pnl_pct, direction, coin, reason):
    """记录交易结果并检查连亏"""
    log = load_trade_log()
    log.append({
        "time": datetime.now(timezone.utc).isoformat(),
        "coin": coin,
        "direction": direction,
        "pnl_pct": pnl_pct,
        "reason": reason,
    })
    save_trade_log(log)
    
    # 检查最近N笔是否全亏
    recent = log[-CONSEC_LOSS_THRESHOLD:]
    if len(recent) >= CONSEC_LOSS_THRESHOLD and all(t["pnl_pct"] <= 0 for t in recent):
        print(f"🚨 连亏{CONSEC_LOSS_THRESHOLD}笔！触发自动优化")
        trigger_optimization()

def notify_discord(message):
    """通过 core.notify.send_discord 发送 Discord 通知"""
    from core.notify import send_discord
    send_discord(message, channel_id=DISCORD_CHANNEL_ID, mention=True)

def trigger_optimization():
    """连亏触发优化"""
    try:
        import subprocess
        import shutil
        openclaw_path = shutil.which("openclaw") or str(Path.home() / ".local/bin/openclaw")
        msg = f"🚨 连亏{CONSEC_LOSS_THRESHOLD}笔触发自动优化。运行 `python monthly_optimize.py`"
        subprocess.run(
            [openclaw_path, "system", "event", "--text", msg, "--mode", "now"],
            capture_output=True, text=True, timeout=30
        )
        print("已唤醒 Lucky 执行优化")
    except Exception as e:
        print(f"触发优化失败: {e}")

def _migrate_state(data: dict) -> dict:
    """Migrate old single-coin format to multi-coin format.
    
    Old: {"position": {...}}
    New: {"BTC": {"position": ...}, "ETH": {"position": None}}
    """
    if "position" in data and not any(c in data for c in TRADING_COINS):
        old_pos = data.get("position")
        migrated = {c: {"position": None} for c in TRADING_COINS}
        if old_pos:
            coin = old_pos.get("coin", "BTC")
            migrated[coin] = {"position": old_pos}
        return migrated
    return data


def load_state(coin: str = None):
    """Load position state. If coin is specified, return that coin's state only.
    
    For backward compatibility: if coin is None, returns the full multi-coin dict.
    """
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
        except (json.JSONDecodeError, ValueError):
            print(f"⚠️ position_state.json 损坏，重置为空状态")
            data = {}
    else:
        data = {}

    data = _migrate_state(data)
    
    # Ensure all coins exist
    for c in TRADING_COINS:
        if c not in data:
            data[c] = {"position": None}

    if coin:
        return data.get(coin, {"position": None})
    return data


def save_state(state, coin: str = None):
    """Save position state. If coin is specified, only update that coin's state.
    
    For backward compatibility: if coin is None and state has old format {"position": ...},
    auto-detect and handle.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    if coin:
        # Update only the specified coin
        full_state = load_state()
        full_state[coin] = state
        state = full_state
    elif "position" in state and not any(c in state for c in TRADING_COINS):
        # Old-format call: save_state({"position": None}) — migrate on the fly
        # Detect which coin this is about from the position data
        pos = state.get("position")
        full_state = load_state()
        if pos:
            c = pos.get("coin", "BTC")
            full_state[c] = state
        else:
            # Clearing position — we don't know which coin, so this is a legacy call
            # Keep existing state (caller should use save_state(state, coin) instead)
            pass
        state = full_state
    
    tmp_file = STATE_FILE.with_suffix('.tmp')
    with open(tmp_file, 'w') as f:
        json.dump(state, f, indent=2)
    tmp_file.rename(STATE_FILE)

def get_position(coin):
    """获取当前持仓"""
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    state = info.user_state(MAIN_WALLET)
    for pos in state["assetPositions"]:
        if pos["position"]["coin"] == coin:
            size = float(pos["position"]["szi"])
            if size != 0:
                return {
                    "coin": coin,
                    "size": size,
                    "direction": "LONG" if size > 0 else "SHORT",
                    "entry_price": float(pos["position"]["entryPx"]),
                    "unrealized_pnl": float(pos["position"]["unrealizedPnl"]),
                    "liquidation_price": float(pos["position"].get("liquidationPx", 0) or 0),
                }
    return None

def get_coin_info(coin):
    """获取币种精度信息"""
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    meta = info.meta()
    for asset in meta["universe"]:
        if asset["name"] == coin:
            return asset
    return None

def check_existing_orders(coin):
    """检查是否已有SL/TP挂单"""
    orders = get_open_orders_detailed(coin)
    sl_exists = False
    tp_exists = False
    for o in orders:
        ot = o.get("orderType", "")
        if "Stop" in ot or o.get("isTrigger") and "sl" in str(o).lower():
            sl_exists = True
        if "Take" in ot or o.get("isTrigger") and "tp" in str(o).lower():
            tp_exists = True
    return sl_exists, tp_exists

def log_trade(action, coin, direction, size, price, sl=None, tp=None, reason=""):
    """记录交易到 TRADES.md"""
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M SGT")
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    entry = f"\n### {now} — {action} {direction} {coin}\n"
    entry += f"- 数量: {size}\n"
    entry += f"- 价格: ${price:,.2f}\n"
    if sl: entry += f"- 止损: ${sl:,.2f} (-{STOP_LOSS_PCT*100:.0f}%)\n"
    if tp: entry += f"- 止盈: ${tp:,.2f} (+{TAKE_PROFIT_PCT*100:.0f}%)\n"
    if reason: entry += f"- 原因: {reason}\n"
    
    with open(TRADES_FILE, 'a') as f:
        f.write(entry)

_LOCK_DIR = STATE_FILE.parent

def _acquire_lock(coin: str = "BTC"):
    """Per-coin file lock: prevent concurrent execution for the same coin."""
    import fcntl
    lock_file = _LOCK_DIR / f".execute_{coin}.lock"
    fd = open(lock_file, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd._lock_path = lock_file  # stash path for cleanup
        return fd
    except OSError as e:
        logger.debug(f"Lock acquisition failed for {coin} (another instance running): {e}")
        fd.close()
        return None

def _release_lock(fd):
    if fd:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        lock_path = getattr(fd, '_lock_path', None)
        fd.close()
        if lock_path:
            try:
                lock_path.unlink(missing_ok=True)
            except Exception as e:
                logger.debug(f"Lock file cleanup failed: {e}")

def execute(dry_run=False, coin=None):
    """主执行流程。dry_run=True 时只分析不下单。
    
    Args:
        dry_run: 只分析不下单
        coin: 指定币种。None = 遍历所有 TRADING_COINS。
    """
    coins = [coin] if coin else TRADING_COINS
    results = {}
    
    for c in coins:
        result = execute_coin(c, dry_run)
        results[c] = result
    
    # For backward compatibility: if single coin, return its result directly
    if coin:
        return results[coin]
    return results


def execute_coin(coin: str, dry_run=False):
    """Execute signal check for a single coin."""
    mode = "🧪 DRY RUN" if dry_run else "🔴 LIVE"
    _CST = timezone(timedelta(hours=8))
    print(f"[{datetime.now(_CST).strftime('%H:%M:%S CST')}] {mode} {coin} 执行信号检查...")

    # 防并发：per-coin 文件锁
    lock_fd = _acquire_lock(coin)
    if lock_fd is None:
        print(f"⚠️ 另一个 {coin} execute 进程正在运行，跳过")
        return {"action": "SKIPPED", "reason": "lock_held"}
    
    try:
        return _execute_inner(dry_run, mode, _CST, coin)
    finally:
        _release_lock(lock_fd)

_COOLDOWN_SECONDS = 1800  # 30 分钟内不允许重复开仓

def _cooldown_file(coin: str) -> Path:
    return STATE_FILE.parent / f".last_open_ts_{coin}"

def _check_cooldown(coin: str = "BTC"):
    """开仓后 30 分钟内禁止再次开仓，防止 cron+手动重复。Per-coin."""
    cf = _cooldown_file(coin)
    if cf.exists():
        try:
            last_ts = float(cf.read_text().strip())
            elapsed = time.time() - last_ts
            if elapsed < _COOLDOWN_SECONDS:
                remaining = _COOLDOWN_SECONDS - elapsed
                print(f"⚠️ {coin} 冷却中：上次开仓 {elapsed:.0f}s 前，还需等待 {remaining:.0f}s")
                return False
        except Exception as e:
            logger.debug(f"Cooldown check failed (treating as no cooldown): {e}")
    return True

def _set_cooldown(coin: str = "BTC"):
    """记录开仓时间戳。"""
    _cooldown_file(coin).write_text(str(time.time()))

def _execute_inner(dry_run, mode, _CST, coin="BTC"):
    """Execute for a single coin."""
    coin_params = _get_coin_params(coin)
    max_hold_hours = coin_params['max_hold_hours']
    
    # 1. 检查是否有持仓
    position = get_position(coin)
    state = load_state(coin)
    
    # 检查：state里有持仓但链上没了 → SL/TP被触发了
    if not position and state.get("position"):
        sp = state["position"]
        print(f"⚡ {coin} 持仓已被平仓（SL/TP触发）: {sp['direction']}")
        entry = sp["entry_price"]
        expected_close_side = "SELL" if sp["direction"] == "LONG" else "BUY"
        fills = get_recent_fills(limit=5)
        close_fill = next(
            (f for f in fills if f.get("coin") == sp.get("coin", coin) and f.get("side") == expected_close_side),
            None
        )
        if close_fill:
            current_price = float(close_fill["price"])
        else:
            current_price = get_market_price(coin)
        if sp["direction"] == "LONG":
            pnl_pct = (current_price - entry) / entry * 100
        else:
            pnl_pct = (entry - current_price) / entry * 100
        
        sl = sp.get("sl_price", 0)
        tp = sp.get("tp_price", 0)
        if sp["direction"] == "LONG":
            reason = "TP" if current_price >= tp * 0.99 else "SL" if current_price <= sl * 1.01 else None
        else:
            reason = "TP" if current_price <= tp * 1.01 else "SL" if current_price >= sl * 0.99 else None

        if reason is None:
            # Distance fallback (handles trailing stop moving SL to breakeven+)
            dist_sl = abs(current_price - sl) if sl else float('inf')
            dist_tp = abs(current_price - tp) if tp else float('inf')
            reason = "SL" if dist_sl < dist_tp else "TP" if dist_tp < dist_sl else ("TP" if pnl_pct > 0 else "SL")
            logger.info(f"Exit classified by distance fallback: {reason} (close={current_price}, sl={sl}, tp={tp})")
        
        record_trade_result(pnl_pct, sp["direction"], coin, reason)
        log_trade("CLOSED_BY_TRIGGER", coin, sp["direction"], sp["size"],
                  current_price, reason=f"{reason} 触发, PnL {pnl_pct:+.2f}%")
        save_state({"position": None}, coin)
        print(f"  估算PnL: {pnl_pct:+.2f}%, 原因: {reason}")
        
        emoji = "🎯" if reason == "TP" else "🛑"
        notify_discord(f"{emoji} **平仓** {sp['direction']} {coin} — {reason}触发\n💰 入场: ${sp['entry_price']:,.2f} → 平仓: ~${current_price:,.2f}\n📊 盈亏: {pnl_pct:+.2f}%")
        return {"action": "CLOSED_BY_TRIGGER", "reason": reason, "pnl_pct": pnl_pct}
    
    if position:
        print(f"当前持仓: {position['direction']} {abs(position['size'])} {coin} @ ${position['entry_price']:,.2f}")
        print(f"未实现盈亏: ${position['unrealized_pnl']:,.2f}")
        
        # 检查超时平仓
        if state.get("position") and state["position"].get("entry_time"):
            entry_time = datetime.fromisoformat(state["position"]["entry_time"])
            elapsed = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            print(f"持仓时间: {elapsed:.1f}h / {max_hold_hours}h")
            
            if elapsed >= max_hold_hours:
                pnl_pct = position["unrealized_pnl"] / (abs(position["size"]) * position["entry_price"]) * 100
                if dry_run:
                    print(f"🧪 DRY RUN: {coin} 超时 {elapsed:.1f}h，WOULD 平仓 (PnL {pnl_pct:+.2f}%)")
                    return {"action": "DRY_RUN_WOULD_TIMEOUT_CLOSE", "elapsed": elapsed, "pnl_pct": pnl_pct, "dry_run": True}
                print(f"⏰ {coin} 超时平仓！已持仓 {elapsed:.1f}h")
                try:
                    result = close_position(position, coin=coin)
                    if result is None:
                        record_trade_result(pnl_pct, position["direction"], coin, "SL_TP_AUTO")
                        return {"action": "STALE_STATE_CLEANED", "elapsed": elapsed}
                except RuntimeError as e:
                    logger.error(f"Timeout close failed for {coin}: {e}")
                    return {"action": "CLOSE_FAILED", "error": str(e)}
                record_trade_result(pnl_pct, position["direction"], coin, "TIMEOUT")
                notify_discord(f"⏰ **超时平仓** {position['direction']} {coin}\n💰 入场: ${position['entry_price']:,.2f}\n📊 盈亏: {pnl_pct:+.2f}% | 持仓 {elapsed:.1f}h")
                return {"action": "TIMEOUT_CLOSE", "elapsed": elapsed, "pnl_pct": pnl_pct}
        
        # 检查SL/TP是否还在
        if not dry_run:
            sl_exists, tp_exists = check_sl_tp_orders(coin, position)
            if not sl_exists or not tp_exists:
                print(f"⚠️ {coin} SL/TP 缺失! SL={sl_exists}, TP={tp_exists}")
                print("紧急修复中...")
                fix_sl_tp(position, coin=coin)
        
        return {"action": "HOLD", "position": position, "dry_run": dry_run}
    
    # 2. 无持仓，检查信号
    result = analyze(coin)
    if "error" in result:
        print(f"{coin} 信号检查失败: {result['error']}")
        return {"action": "ERROR", "error": result["error"]}
    
    signal = result["signal"]
    print(f"{coin} 信号: {signal}")
    
    if signal == "HOLD":
        print(f"{coin} 无信号，继续等待")
        return {"action": "HOLD", "signal": result, "dry_run": dry_run}
    
    # 3. 有信号，执行开仓
    if dry_run:
        return dry_run_open(signal, result, coin)

    # 开仓前再次确认链上无持仓（防重复开仓）
    position_recheck = get_position(coin)
    if position_recheck:
        print(f"⚠️ {coin} 开仓前二次检查发现已有持仓，跳过")
        return {"action": "HOLD", "reason": "position_exists_on_recheck"}

    open_result = open_position(signal, result, coin)

    if open_result.get("action") == "OPENED":
        ev_min = _cfg.strategy.early_validation_bars * 30
        print(f"⚠️  Early validation 将在 {ev_min} 分钟后由 ws_monitor 自动执行")
        print(f"   确认 ws_monitor 正在运行: systemctl is-active ws-monitor")

    return open_result

def dry_run_open(signal, analysis, coin="BTC"):
    """Dry run: 计算开仓参数但不下单"""
    price = analysis["price"]
    is_long = signal == "LONG"
    cp = _get_coin_params(coin)

    # Compute DE regime to select adaptive TP/SL
    try:
        cc = get_coin_config(coin)
        candles_1d = get_candles(coin, "1d", (cc.de_lookback_days + 3) * 24)
        de = compute_de(candles_1d, lookback_days=cc.de_lookback_days)
    except Exception as e:
        print(f"⚠️ DE计算失败，降级为默认区间参数: {e}")
        de = None
    regime_params = get_regime_params(de, _cfg)
    sl_pct = regime_params['sl_pct']
    tp_pct = regime_params['tp_pct']
    regime = regime_params['regime']
    de_str = f"{de:.3f}" if de is not None else "None"
    print(f"🔍 {coin} Regime={regime} DE={de_str} → TP={tp_pct*100:.0f}% SL={sl_pct*100:.0f}%")
    
    account = get_account_info()
    account_value = float(account["account_value"])
    position_value = account_value * cp['position_ratio']
    
    max_loss_at_sl = position_value * sl_pct
    if max_loss_at_sl > cp['max_single_loss']:
        position_value = cp['max_single_loss'] / sl_pct
    
    coin_info = get_coin_info(coin)
    sz_decimals = coin_info.get("szDecimals", 5) if coin_info else 5
    size = round(position_value / price, sz_decimals)
    
    if is_long:
        sl_price = round(price * (1 - sl_pct))
        tp_price = round(price * (1 + tp_pct))
    else:
        sl_price = round(price * (1 + sl_pct))
        tp_price = round(price * (1 - tp_pct))
    
    print(f"\n{'='*50}")
    print(f"🧪 DRY RUN — WOULD OPEN: {signal} {coin}")
    print(f"   账户: ${account_value:.2f}")
    print(f"   数量: {size} ({position_value:.2f} USD)")
    print(f"   价格: ~${price:,.2f}")
    print(f"   止损: ${sl_price:,.2f} ({'-' if is_long else '+'}{sl_pct*100:.0f}%)")
    print(f"   止盈: ${tp_price:,.2f} ({'+' if is_long else '-'}{tp_pct*100:.0f}%)")
    print(f"   最大亏损: ${position_value * sl_pct:.2f}")
    print(f"   信号理由: {'; '.join(analysis.get('signal_reasons', []))}")
    print(f"{'='*50}")
    print(f"⚠️  DRY RUN — 未下单！")
    
    return {
        "action": "DRY_RUN_WOULD_OPEN",
        "dry_run": True,
        "direction": signal,
        "size": size,
        "entry": price,
        "sl": sl_price,
        "tp": tp_price,
        "position_value": position_value,
        "max_loss": position_value * sl_pct,
        "regime": regime,
        "de": de,
        "regime_tp_pct": tp_pct,
        "regime_sl_pct": sl_pct,
        "reasons": analysis.get("signal_reasons", []),
    }

def open_position(signal, analysis, coin="BTC"):
    """开仓 + SL + TP 原子操作"""
    price = analysis["price"]
    is_long = signal == "LONG"
    cp = _get_coin_params(coin)

    # Compute DE regime to select adaptive TP/SL
    try:
        cc = get_coin_config(coin)
        candles_1d = get_candles(coin, "1d", (cc.de_lookback_days + 3) * 24)
        de = compute_de(candles_1d, lookback_days=cc.de_lookback_days)
    except Exception as e:
        print(f"⚠️ DE计算失败，降级为默认区间参数: {e}")
        de = None
    regime_params = get_regime_params(de, _cfg)
    sl_pct = regime_params['sl_pct']
    tp_pct = regime_params['tp_pct']
    regime = regime_params['regime']
    de_str = f"{de:.3f}" if de is not None else "None"
    print(f"🔍 {coin} Regime={regime} DE={de_str} → TP={tp_pct*100:.0f}% SL={sl_pct*100:.0f}%")
    
    # 计算仓位大小
    account = get_account_info()
    account_value = float(account["account_value"])
    position_value = account_value * cp['position_ratio']
    
    # 检查单笔最大亏损限制
    max_loss_at_sl = position_value * sl_pct
    if max_loss_at_sl > cp['max_single_loss']:
        position_value = cp['max_single_loss'] / sl_pct
        print(f"仓位受限于最大单笔亏损 ${cp['max_single_loss']}: 仓位 ${position_value:.2f}")
    
    # 获取精度
    coin_info = get_coin_info(coin)
    sz_decimals = coin_info.get("szDecimals", 5) if coin_info else 5
    
    size = round(position_value / price, sz_decimals)
    if size <= 0:
        print("仓位太小，跳过")
        return {"action": "SKIP", "reason": "size_too_small"}
    
    # 计算 SL/TP 价格
    if is_long:
        sl_price = round(price * (1 - sl_pct))
        tp_price = round(price * (1 + tp_pct))
    else:
        sl_price = round(price * (1 + sl_pct))
        tp_price = round(price * (1 - tp_pct))
    
    print(f"\n{'='*50}")
    print(f"🚀 开仓: {signal} {coin}")
    print(f"   数量: {size} ({position_value:.2f} USD)")
    print(f"   价格: ~${price:,.2f}")
    print(f"   止损: ${sl_price:,.2f} ({'-' if is_long else '+'}{sl_pct*100:.0f}%)")
    print(f"   止盈: ${tp_price:,.2f} ({'+' if is_long else '-'}{tp_pct*100:.0f}%)")
    print(f"   最大亏损: ${position_value * sl_pct:.2f}")
    print(f"{'='*50}")
    
    # Step 1: 市价开仓
    print("\n[1/3] 市价开仓...")
    order_result = place_market_order(coin, is_long, size)
    print(f"开仓结果: {json.dumps(order_result, indent=2)}")
    
    # 验证开仓成功
    if order_result.get("status") == "err":
        print(f"❌ 开仓失败: {order_result}")
        return {"action": "OPEN_FAILED", "error": order_result}
    
    # 等待成交
    time.sleep(1)
    
    # 确认持仓
    position = get_position(coin)
    if not position:
        print("❌ 开仓后未找到持仓，可能未成交")
        return {"action": "OPEN_FAILED", "error": "no_position_after_order"}
    
    actual_size = abs(position["size"])
    actual_entry = position["entry_price"]
    print(f"✅ 持仓确认: {position['direction']} {actual_size} @ ${actual_entry:,.2f}")
    
    # 用实际入场价重新计算SL/TP
    if is_long:
        sl_price = round(actual_entry * (1 - sl_pct))
        tp_price = round(actual_entry * (1 + tp_pct))
    else:
        sl_price = round(actual_entry * (1 + sl_pct))
        tp_price = round(actual_entry * (1 - tp_pct))
    
    # API 冷却：连续调用间等待，防 429 rate limit
    time.sleep(1)
    
    # Step 2: 设止损（带重试，429 rate limit 常见）
    print(f"\n[2/3] 设止损 ${sl_price:,.2f}...")
    sl_set = False
    for sl_attempt in range(3):
        try:
            sl_result = place_stop_loss(coin, actual_size, sl_price, is_long)
            print(f"止损结果: {json.dumps(sl_result, indent=2)}")
            if sl_result.get("status") == "err":
                raise Exception(f"SL failed: {sl_result}")
            sl_set = True
            break
        except Exception as e:
            print(f"❌ 止损设置 attempt {sl_attempt+1}/3 失败: {e}")
            if sl_attempt < 2:
                wait = 3 * (sl_attempt + 1)
                print(f"⏳ 等待 {wait}s 后重试...")
                time.sleep(wait)
    
    if not sl_set:
        print("🚨 止损 3 次重试全部失败，紧急平仓！")
        try:
            emergency_close(coin, actual_size, is_long)
        except RuntimeError as close_err:
            logger.error(f"Emergency close failed after SL setup failure: {close_err}")
            return {"action": "EMERGENCY_CLOSE_FAILED", "error": str(close_err)}
        return {"action": "SL_FAILED_CLOSED", "error": "SL setup failed after 3 retries"}
    
    time.sleep(1)  # API 冷却
    
    # Step 3: 设止盈（带重试）
    print(f"\n[3/3] 设止盈 ${tp_price:,.2f}...")
    tp_set = False
    for tp_attempt in range(3):
        try:
            tp_result = place_take_profit(coin, actual_size, tp_price, is_long)
            print(f"止盈结果: {json.dumps(tp_result, indent=2)}")
            if tp_result.get("status") == "err":
                raise Exception(f"TP failed: {tp_result}")
            tp_set = True
            break
        except Exception as e:
            print(f"❌ 止盈设置 attempt {tp_attempt+1}/3 失败: {e}")
            if tp_attempt < 2:
                wait = 3 * (tp_attempt + 1)
                print(f"⏳ 等待 {wait}s 后重试...")
                time.sleep(wait)
    
    if not tp_set:
        print("🚨 止盈 3 次重试全部失败，紧急平仓！")
        # 先取消已设的SL
        try:
            orders = get_open_orders_detailed(coin)
            for o in orders:
                cancel_order(coin, o["oid"])
        except Exception as e2:
            print(f"⚠️ Failed to cancel orders before emergency close: {e2}")
        try:
            emergency_close(coin, actual_size, is_long)
        except RuntimeError as close_err:
            logger.error(f"Emergency close failed after TP setup failure: {close_err}")
            return {"action": "EMERGENCY_CLOSE_FAILED", "error": str(close_err)}
        return {"action": "TP_FAILED_CLOSED", "error": "TP setup failed after 3 retries"}
    
    # 全部成功，保存状态 (per-coin)
    coin_state = {
        "position": {
            "coin": coin,
            "direction": signal,
            "size": actual_size,
            "entry_price": actual_entry,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "sl_price": sl_price,
            "tp_price": tp_price,
            "max_hold_hours": cp['max_hold_hours'],
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=cp['max_hold_hours'])).isoformat(),
            "regime": regime,
            "de": de,
            "regime_tp_pct": tp_pct,
            "regime_sl_pct": sl_pct,
        }
    }
    save_state(coin_state, coin)

    regime_reason = f"regime={regime} de={de_str} tp={tp_pct*100:.0f}% sl={sl_pct*100:.0f}%"
    reasons = analysis.get("signal_reasons", [])
    reason_text = "; ".join(reasons + [regime_reason]) if reasons else regime_reason
    log_trade("OPEN", coin, signal, actual_size, actual_entry, sl_price, tp_price, reason_text)
    
    print(f"\n✅ 开仓完成！SL=${sl_price:,.2f} TP=${tp_price:,.2f}")
    print(f"⏰ 超时平仓时间: {coin_state['position']['deadline']}")
    
    notify_discord(
        f"🚀 **开仓** {signal} {coin}\n"
        f"💰 入场: ${actual_entry:,.2f} | 数量: {actual_size}\n"
        f"🛑 止损: ${sl_price:,.2f} (-{sl_pct*100:.0f}%) | 🎯 止盈: ${tp_price:,.2f} (+{tp_pct*100:.0f}%)\n"
        f"🔍 Regime: {regime} (DE={de_str})\n"
        f"⏰ 最长持仓: {cp['max_hold_hours']}h"
    )
    
    return {
        "action": "OPENED",
        "direction": signal,
        "size": actual_size,
        "entry": actual_entry,
        "sl": sl_price,
        "tp": tp_price,
        "deadline": coin_state["position"]["deadline"],
        "regime": regime,
        "de": de,
        "regime_tp_pct": tp_pct,
        "regime_sl_pct": sl_pct,
    }

def close_and_cleanup(coin: str, is_long: bool, size: float, reason: str,
                      pnl_pct: float = None, extra_msg: str = ""):
    """通用平仓 + 清理函数 — 所有平仓逻辑的单一入口。

    执行：市价平仓 → 取消挂单 → 记录交易 → 清理 state → Discord 通知。

    Args:
        coin: 币种
        is_long: 仓位方向
        size: 仓位大小
        reason: 平仓原因（用于 log_trade action 和通知）
        pnl_pct: 已计算的盈亏百分比（None 则自动从链上计算）
        extra_msg: 附加到 Discord 通知的额外信息
    
    Returns:
        dict with close_price, pnl_pct, reason
    
    Raises:
        Exception: 如果市价平仓失败
    """
    import logging
    logger = logging.getLogger(__name__)
    
    direction = "LONG" if is_long else "SHORT"
    
    # 1. 市价平仓
    result = place_market_order(coin, not is_long, size)
    if result.get("status") == "err":
        raise Exception(f"close_and_cleanup order error: {result}")
    
    close_price = get_market_price(coin)
    
    # 2. 计算盈亏（如未提供）
    if pnl_pct is None:
        state = load_state(coin)
        entry = state.get("position", {}).get("entry_price")
        if entry:
            pnl_pct = compute_pnl_pct(direction, entry, close_price)
        else:
            pnl_pct = 0.0
            logger.warning("close_and_cleanup: no entry_price in state, pnl_pct=0")
    
    # 3. 取消所有挂单
    try:
        for o in get_open_orders_detailed(coin):
            if o.get("isTrigger"):
                cancel_order(coin, o["oid"])
    except Exception as e:
        logger.error(f"close_and_cleanup: failed to cancel orders: {e}")
    
    # 4. 记录交易
    record_trade_result(pnl_pct, direction, coin, reason)
    log_trade(reason, coin, direction, size, close_price, None, None,
              f"{reason}: PnL {pnl_pct:+.2f}% {extra_msg}")
    
    # 5. 清理 state
    save_state({"position": None}, coin)
    
    # 6. Discord 通知
    notify_discord(
        f"{'❌' if pnl_pct < 0 else '✅'} **{reason}** {direction} {coin}\n"
        f"💰 平仓价: ~${close_price:,.2f} | 盈亏: {pnl_pct:+.2f}%\n"
        f"{extra_msg}\n"
        f"<@1469390967256703013> <@1469405440289821357>"
    )
    
    logger.info(f"close_and_cleanup: {reason} {direction} {coin} pnl={pnl_pct:+.2f}%")
    return {"close_price": close_price, "pnl_pct": pnl_pct, "reason": reason}


def emergency_close(coin, size, is_long, max_retries=3):
    """紧急市价平仓 — 带重试和持久化告警
    
    关键安全措施：每次重试前检查链上仓位是否还在。
    防止 "平仓成功但 SDK 抛异常" → 再次 SELL → 开反向仓位。
    """
    print(f"🚨 紧急平仓 {coin} size={size}")
    
    for attempt in range(1, max_retries + 1):
        # 🔒 每次重试前必须确认链上仓位还在（防止反向开仓）
        if attempt > 1:
            wait_secs = max(5, 2 ** attempt)  # 至少等 5 秒（429 cooldown）
            print(f"⏳ 等待 {wait_secs}s 后检查链上仓位...")
            time.sleep(wait_secs)
            
            # 仓位检查本身也可能 429，重试 3 次
            check_succeeded = False
            real_pos = "UNKNOWN"
            for check_attempt in range(3):
                try:
                    real_pos = get_position(coin)
                    check_succeeded = True
                    break
                except Exception as e:
                    print(f"⚠️ 仓位检查 attempt {check_attempt+1}/3 失败: {e}")
                    time.sleep(3)
            
            if not check_succeeded:
                # 3 次都查不到 → 大概率 429 还在，不能盲目重试平仓
                print(f"🚨 仓位检查连续 3 次失败（API 限速），中止重试（防止开反向仓）")
                logger.error(f"emergency_close aborted: position check failed 3 times for {coin}, refusing to retry (risk of opening reverse position)")
                break  # 跳出 → 走到 danger 告警
            
            if real_pos is None:
                # get_position 返回 None = 链上无仓位，平仓已生效
                print(f"✅ 链上 {coin} 已无仓位（前次平仓已生效），跳过重试")
                save_state({"position": None}, coin)
                log_trade("EMERGENCY_CLOSE", coin, "LONG" if is_long else "SHORT", size,
                          get_market_price(coin), reason=f"SL/TP设置失败紧急平仓 (confirmed after attempt {attempt-1})")
                return
        
        try:
            result = place_market_order(coin, not is_long, size)
            print(f"平仓结果 (attempt {attempt}): {json.dumps(result, indent=2)}")
            if result.get("status") == "err":
                raise Exception(f"Order error: {result}")
            # 平仓成功后验证链上状态
            time.sleep(1)
            verify_pos = get_position(coin)
            if verify_pos is not None:
                print(f"⚠️ 平仓指令成功但链上仍有仓位，可能部分成交，继续重试")
                continue
            save_state({"position": None}, coin)
            log_trade("EMERGENCY_CLOSE", coin, "LONG" if is_long else "SHORT", size, 
                      get_market_price(coin), reason=f"SL/TP设置失败紧急平仓 (attempt {attempt})")
            return  # success
        except Exception as e:
            print(f"❌ 紧急平仓 attempt {attempt}/{max_retries} 失败: {e}")
    
    # All retries failed — persist danger state, alert, and RAISE
    print("❌❌ 紧急平仓全部失败！持久化告警...")
    danger_file = _WORKSPACE_DIR / "memory" / "trading" / "DANGER_UNPROTECTED.json"
    try:
        danger_file.parent.mkdir(parents=True, exist_ok=True)
        danger_file.write_text(json.dumps({
            "time": datetime.now(timezone.utc).isoformat(),
            "coin": coin,
            "size": size,
            "is_long": is_long,
            "reason": "emergency_close failed after all retries",
        }, indent=2))
    except Exception as e:
        # Persistence failure must not mask the critical RuntimeError path.
        print(f"⚠️ 持久化告警文件失败: {e}")
    notify_discord(f"🚨🚨🚨 **紧急平仓失败** — {coin} 仓位无保护！需要人工干预！")
    raise RuntimeError(f"紧急平仓失败: {coin} size={size} — 仓位无保护！")

def close_position(position, max_retries=3, backoff_seconds=5, coin=None):
    """正常平仓（超时等原因），带指数退避重试

    Args:
        position: 本地 state 中的仓位信息
        max_retries: 失败后最多重试次数（总尝试 = 1 + max_retries）
        backoff_seconds: 首次重试等待秒数，后续指数增长（0 = 不等待，用于测试）
        coin: 币种（可选，默认从 position 中读取）
    """
    coin = coin or position["coin"]
    
    # 先验证链上是否真的有仓位（防止 state 与链上不一致）
    real_pos = get_position(coin)
    if not real_pos:
        print(f"⚠️ 链上无 {coin} 持仓，state 残留。清理 state。")
        save_state({"position": None}, coin)
        notify_discord(f"ℹ️ {coin} 超时平仓跳过 — 链上已无仓位（可能 SL/TP 已触发）")
        return None
    # 用链上真实数据覆盖，防止 size 不一致
    size = abs(real_pos["size"])
    is_long = real_pos["direction"] == "LONG"

    # 先取消所有挂单
    try:
        orders = get_open_orders_detailed(coin)
        for o in orders:
            cancel_order(coin, o["oid"])
            print(f"已取消订单 {o['oid']}")
    except Exception as e:
        print(f"取消挂单失败: {e}")

    # 市价平仓 — 带指数退避重试
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            result = place_market_order(coin, not is_long, size)
            print(f"平仓结果 (attempt {attempt + 1}): {json.dumps(result, indent=2)}")
            if result.get("status") != "err":
                # 成功
                save_state({"position": None}, coin)
                log_trade("CLOSE", coin, real_pos["direction"], size,
                          get_market_price(coin), reason="超时平仓")
                return True
            last_error = f"status=err: {result}"
        except Exception as e:
            last_error = str(e)
            print(f"❌ 平仓异常 (attempt {attempt + 1}/{max_retries + 1}): {e}")

        if attempt < max_retries:
            wait = backoff_seconds * (2 ** attempt) if backoff_seconds > 0 else 0
            if wait > 0:
                print(f"⏳ {wait}s 后重试...")
                time.sleep(wait)
            else:
                print(f"🔄 重试 ({attempt + 2}/{max_retries + 1})...")

    # 全部重试失败
    notify_discord(
        f"🚨 **超时平仓失败** — {coin} 仓位可能仍存在！需要人工干预！\n"
        f"重试 {max_retries} 次后仍失败: {last_error}"
    )
    raise RuntimeError(f"平仓失败: {coin} size={size} — {last_error}")

def check_sl_tp_orders(coin, position):
    """检查SL/TP订单是否存在"""
    orders = get_open_orders_detailed(coin)
    sl_exists = False
    tp_exists = False
    for o in orders:
        if o.get("isTrigger"):
            order_type = o.get("orderType", "")
            if "Stop" in order_type:
                sl_exists = True
            elif "Take" in order_type:
                tp_exists = True
    return sl_exists, tp_exists

def reeval_regime_tp(position):
    """动态重估 regime，如果 TP 需要收紧则调整链上订单。
    
    核心逻辑：趋势市开仓 (TP=7%) → 持仓期间变横盘 (TP=2%) → 收紧 TP。
    只收紧不放松：横盘开仓后变趋势不放大 TP（已锚定的 range 参数更安全）。
    SL 不动：只调 TP，SL 由 trailing stop 管理。
    
    Returns: dict with action taken, or None if no change.
    """
    coin = position["coin"]
    entry = position["entry_price"]
    size = abs(position["size"])
    is_long = position["direction"] == "LONG"
    old_tp_pct = position.get("regime_tp_pct", TAKE_PROFIT_PCT)
    old_regime = position.get("regime", "unknown")
    
    # 重新计算 DE
    try:
        info = Info(skip_ws=True)
        import time as _time
        end = int(_time.time() * 1000)
        start = end - 15 * 24 * 3600 * 1000
        candles_1d = info.candles_snapshot(coin, "1d", start, end)
        de = compute_de(candles_1d, _cfg.strategy.de_lookback_days)
    except Exception as e:
        print(f"⚠️ Regime re-eval failed (API error): {e}")
        return None
    
    # DE 无法计算时（API 数据不足/异常）→ 不调整，保持原参数
    if de is None:
        print(f"⚠️ DE unavailable, skipping regime re-eval (keeping entry params)")
        return None
    
    # 用 strategy.should_tighten_tp() — 和回测共用同一判断逻辑
    new_tp_pct = should_tighten_tp(old_tp_pct, de, _cfg)
    if new_tp_pct is None:
        return None
    
    new_params = get_regime_params(de, _cfg)
    new_regime = new_params['regime']
    
    # TP 需要收紧
    new_tp_price = compute_tp_price(entry, new_tp_pct, is_long)
    
    print(f"🔄 Regime 变化: {old_regime}→{new_regime} (DE={de:.3f}), TP 收紧 {old_tp_pct*100:.0f}%→{new_tp_pct*100:.0f}%")
    print(f"   新 TP: ${new_tp_price:,.2f}")
    
    # 检查当前价是否已经超过新 TP（浮盈已超额）
    try:
        current_price = get_market_price(coin)
        should_close = (is_long and current_price >= new_tp_price) or \
                       (not is_long and current_price <= new_tp_price)
        if should_close:
            print(f"   💰 当前价 ${current_price:,.0f} 已超过新 TP ${new_tp_price:,.0f}，市价平仓")
            pnl_pct = compute_pnl_pct("LONG" if is_long else "SHORT", entry, current_price)
            result = close_and_cleanup(
                coin, is_long, size, reason="CLOSED_BY_REGIME",
                pnl_pct=pnl_pct,
                extra_msg=f"Regime {old_regime}→{new_regime}, TP收紧触发平仓"
            )
            return {"action": "CLOSED_BY_REGIME", "old_regime": old_regime, "new_regime": new_regime,
                    "old_tp_pct": old_tp_pct, "new_tp_pct": new_tp_pct,
                    "close_price": result["close_price"], "de": de}
    except Exception as e:
        print(f"⚠️ 价格检查/市价平仓失败: {e}")
        return None
    
    # 取消旧 TP 单
    try:
        orders = get_open_orders_detailed(coin)
        for o in orders:
            if o.get("isTrigger") and "Take" in o.get("orderType", ""):
                cancel_order(coin, o["oid"])
                print(f"   取消旧 TP 单: oid={o['oid']}")
    except Exception as e:
        print(f"⚠️ 取消旧 TP 失败: {e}")
        return None
    
    # 挂新 TP 单
    try:
        place_take_profit(coin, size, new_tp_price, is_long)
        print(f"   ✅ 新 TP 已挂: ${new_tp_price:,.2f}")
    except Exception as e:
        print(f"❌ 新 TP 挂单失败: {e}，尝试恢复旧 TP")
        old_tp_price = round(entry * (1 - old_tp_pct)) if not is_long else round(entry * (1 + old_tp_pct))
        try:
            place_take_profit(coin, size, old_tp_price, is_long)
            print(f"   ✅ 旧 TP 已恢复: ${old_tp_price:,.2f}")
        except Exception as e2:
            print(f"   🚨 旧 TP 恢复也失败: {e2}，下次 fix_sl_tp 会补")
        return None
    
    # 更新 position_state（per-coin）
    state = load_state(coin)
    if state.get("position"):
        state["position"]["regime"] = new_regime
        state["position"]["regime_tp_pct"] = new_tp_pct
        state["position"]["tp_price"] = new_tp_price
        save_state(state, coin)
    
    return {
        "action": "TP_TIGHTENED",
        "old_regime": old_regime,
        "new_regime": new_regime,
        "old_tp_pct": old_tp_pct,
        "new_tp_pct": new_tp_pct,
        "new_tp_price": new_tp_price,
        "de": de,
    }


def fix_sl_tp(position, coin=None):
    """修复缺失的SL/TP — 使用开仓时的 regime 参数（不用硬编码常量）"""
    coin = coin or position["coin"]
    size = abs(position["size"])
    entry = position["entry_price"]
    is_long = position["direction"] == "LONG"

    # 优先使用开仓时保存的 regime SL/TP，回退到 config 默认值
    sl_pct = position.get("regime_sl_pct", STOP_LOSS_PCT)
    tp_pct = position.get("regime_tp_pct", TAKE_PROFIT_PCT)
    print(f"fix_sl_tp: 使用 sl_pct={sl_pct*100:.0f}% tp_pct={tp_pct*100:.0f}% "
          f"(regime={position.get('regime', 'unknown')}, 来源={'state' if 'regime_sl_pct' in position else 'config默认'})")

    if is_long:
        sl_price = round(entry * (1 - sl_pct))
        tp_price = round(entry * (1 + tp_pct))
    else:
        sl_price = round(entry * (1 + sl_pct))
        tp_price = round(entry * (1 - tp_pct))
    
    sl_exists, tp_exists = check_sl_tp_orders(coin, position)
    
    if not sl_exists:
        print(f"补设止损 ${sl_price:,.2f}...")
        try:
            place_stop_loss(coin, size, sl_price, is_long)
            print("✅ 止损已补设")
        except Exception as e:
            print(f"❌ 止损补设失败: {e}")
            print("🚨 紧急平仓！")
            try:
                emergency_close(coin, size, is_long)
            except RuntimeError as e:
                print(f"🚨 Emergency close also failed in fix_sl_tp: {e}")
            return
    
    if not tp_exists:
        print(f"补设止盈 ${tp_price:,.2f}...")
        try:
            place_take_profit(coin, size, tp_price, is_long)
            print("✅ 止盈已补设")
        except Exception as e:
            print(f"❌ 止盈补设失败: {e}, 止损已在，继续持仓")

def reconcile_orphan_positions():
    """检测链上孤儿仓位（链上有仓位但 state 为空）并自动修复。
    
    场景：emergency_close 竞态导致反向开仓，state 被清空但链上有仓。
    修复：重建 state + 设置 SL/TP，避免裸仓运行。
    
    Returns: list of reconciled positions, empty if all consistent.
    """
    reconciled = []
    for coin in TRADING_COINS:
        try:
            chain_pos = get_position(coin)
            local_state = load_state(coin)
            local_pos = local_state.get("position")
            
            if chain_pos is not None and local_pos is None:
                # 🚨 孤儿仓位！
                direction = chain_pos["direction"]
                entry = float(chain_pos["entry_price"])
                size = abs(chain_pos["size"])
                is_long = direction == "LONG"
                
                logger.warning(f"🚨 Orphan position detected: {direction} {coin} {size} @ ${entry:,.2f}")
                
                # 用 config 默认参数设 SL/TP
                sl_pct = STOP_LOSS_PCT
                tp_pct = TAKE_PROFIT_PCT
                
                if is_long:
                    sl_price = round(entry * (1 - sl_pct))
                    tp_price = round(entry * (1 + tp_pct))
                else:
                    sl_price = round(entry * (1 + sl_pct))
                    tp_price = round(entry * (1 - tp_pct))
                
                # 检查是否已有 SL
                sl_exists, tp_exists = check_sl_tp_orders(coin, {"coin": coin, "direction": direction})
                
                if not sl_exists:
                    try:
                        place_stop_loss(coin, size, sl_price, is_long)
                        print(f"  ✅ 补设止损 ${sl_price:,.2f}")
                    except Exception as e:
                        logger.error(f"  ❌ 补设止损失败: {e}")
                
                if not tp_exists:
                    try:
                        place_take_profit(coin, size, tp_price, is_long)
                        print(f"  ✅ 补设止盈 ${tp_price:,.2f}")
                    except Exception as e:
                        logger.error(f"  ❌ 补设止盈失败: {e}")
                
                # 重建 state
                new_state = {
                    "position": {
                        "coin": coin,
                        "direction": direction,
                        "entry_price": entry,
                        "size": size if is_long else -size,
                        "entry_time": datetime.now(timezone.utc).isoformat(),
                        "regime": "unknown",
                        "regime_tp_pct": tp_pct,
                        "regime_sl_pct": sl_pct,
                        "orphan_reconciled": True,
                        "deadline": (datetime.now(timezone.utc) + timedelta(hours=MAX_HOLD_HOURS)).isoformat(),
                    }
                }
                save_state(new_state, coin)
                
                notify_discord(
                    f"🚨 **孤儿仓位修复** {direction} {coin}\n"
                    f"💰 入场: ${entry:,.2f} | 数量: {size}\n"
                    f"🛑 SL: ${sl_price:,.2f} | 🎯 TP: ${tp_price:,.2f}\n"
                    f"⚠️ State 已重建，使用默认参数\n"
                    f"<@1469390967256703013> <@1469405440289821357>"
                )
                
                reconciled.append({"coin": coin, "direction": direction, "size": size, "entry": entry})
                
            elif chain_pos is None and local_pos is not None:
                # State 有仓位但链上没有 → SL/TP 已触发，清理 state
                logger.info(f"Stale state for {coin}: local has position but chain empty, cleaning up")
                save_state({"position": None}, coin)
                
        except Exception as e:
            logger.error(f"reconcile_orphan_positions error for {coin}: {e}")
    
    return reconciled


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    # Support --coin BTC/ETH or positional
    coin_arg = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--coin" and i + 1 < len(sys.argv):
            coin_arg = sys.argv[i + 1].upper()
        elif arg.upper() in TRADING_COINS and sys.argv[i - 1] != "--coin":
            coin_arg = arg.upper()
    result = execute(dry_run=dry, coin=coin_arg)
    print(f"\n最终结果: {json.dumps(result, default=str, indent=2)}")
