#!/usr/bin/env python3
"""
Lucky Trading Executor v5.1
信号检测 → 开仓 → 立即设SL+TP，原子化执行

规则（不可违反）：
1. 开仓后必须立即设止损和止盈，三者是原子操作
2. 如果SL或TP设置失败，立即市价平仓
3. 同一时间最多一个持仓
4. SL = 4%, TP = 7%, 持仓上限 = 72h
5. 仓位大小 = 账户净值的 30%（含杠杆后的名义价值）
"""
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from luckytrader.signal import analyze, get_recent_fills
from luckytrader.trade import (
    get_account_info, get_market_price, get_open_orders_detailed,
    place_market_order, place_stop_loss, place_take_profit, cancel_order,
    MAIN_WALLET
)
from hyperliquid.info import Info
from hyperliquid.utils import constants
from luckytrader.config import get_config

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

def load_trade_log():
    if TRADE_LOG_FILE.exists():
        try:
            return json.loads(TRADE_LOG_FILE.read_text())
        except:
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
    """通过 openclaw 发送 Discord 通知"""
    try:
        import subprocess
        import shutil
        full_msg = f"{DISCORD_MENTIONS}\n{message}"
        openclaw_path = shutil.which("openclaw") or str(Path.home() / ".local/bin/openclaw")
        subprocess.run(
            [openclaw_path, "system", "event", "--text", 
             f"发送以下消息到 Discord #投资 (channelId: {DISCORD_CHANNEL_ID}):\n\n{full_msg}",
             "--mode", "now"],
            capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        print(f"Discord通知失败: {e}")

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

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"position": None}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
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
    orders = get_open_orders_detailed()
    sl_exists = False
    tp_exists = False
    for o in orders:
        if o.get("coin") == coin:
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

def execute(dry_run=False):
    """主执行流程。dry_run=True 时只分析不下单。"""
    mode = "🧪 DRY RUN" if dry_run else "🔴 LIVE"
    _CST = timezone(timedelta(hours=8))
    print(f"[{datetime.now(_CST).strftime('%H:%M:%S CST')}] {mode} 执行信号检查...")
    
    # 1. 检查是否有持仓
    position = get_position("BTC")
    state = load_state()
    
    # 检查：state里有持仓但链上没了 → SL/TP被触发了
    if not position and state.get("position"):
        sp = state["position"]
        print(f"⚡ 持仓已被平仓（SL/TP触发）: {sp['direction']} {sp['coin']}")
        # 计算盈亏（优先使用实际成交价，回退到市场价）
        entry = sp["entry_price"]
        fills = get_recent_fills(limit=1)
        if fills and fills[0].get("coin") == sp["coin"]:
            current_price = float(fills[0]["price"])
        else:
            current_price = get_market_price(sp["coin"])
        if sp["direction"] == "LONG":
            pnl_pct = (current_price - entry) / entry * 100
        else:
            pnl_pct = (entry - current_price) / entry * 100
        
        # 判断是SL还是TP
        sl = sp.get("sl_price", 0)
        tp = sp.get("tp_price", 0)
        if sp["direction"] == "LONG":
            reason = "TP" if current_price >= tp * 0.99 else "SL" if current_price <= sl * 1.01 else "UNKNOWN"
        else:
            reason = "TP" if current_price <= tp * 1.01 else "SL" if current_price >= sl * 0.99 else "UNKNOWN"
        
        record_trade_result(pnl_pct, sp["direction"], sp["coin"], reason)
        log_trade("CLOSED_BY_TRIGGER", sp["coin"], sp["direction"], sp["size"],
                  current_price, reason=f"{reason} 触发, PnL {pnl_pct:+.2f}%")
        save_state({"position": None})
        print(f"  估算PnL: {pnl_pct:+.2f}%, 原因: {reason}")
        
        emoji = "🎯" if reason == "TP" else "🛑"
        notify_discord(f"{emoji} **平仓** {sp['direction']} {sp['coin']} — {reason}触发\n💰 入场: ${sp['entry_price']:,.2f} → 平仓: ~${current_price:,.2f}\n📊 盈亏: {pnl_pct:+.2f}%")
        return {"action": "CLOSED_BY_TRIGGER", "reason": reason, "pnl_pct": pnl_pct}
    
    if position:
        print(f"当前持仓: {position['direction']} {abs(position['size'])} BTC @ ${position['entry_price']:,.2f}")
        print(f"未实现盈亏: ${position['unrealized_pnl']:,.2f}")
        
        # 检查超时平仓
        if state.get("position") and state["position"].get("entry_time"):
            entry_time = datetime.fromisoformat(state["position"]["entry_time"])
            elapsed = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            print(f"持仓时间: {elapsed:.1f}h / {MAX_HOLD_HOURS}h")
            
            if elapsed >= MAX_HOLD_HOURS:
                pnl_pct = position["unrealized_pnl"] / (abs(position["size"]) * position["entry_price"]) * 100
                if dry_run:
                    print(f"🧪 DRY RUN: 超时 {elapsed:.1f}h，WOULD 平仓 (PnL {pnl_pct:+.2f}%)")
                    return {"action": "DRY_RUN_WOULD_TIMEOUT_CLOSE", "elapsed": elapsed, "pnl_pct": pnl_pct, "dry_run": True}
                print(f"⏰ 超时平仓！已持仓 {elapsed:.1f}h")
                try:
                    result = close_position(position)
                    if result is None:
                        # close_position 发现链上无仓位（SL/TP 已触发），仍需记录交易结果
                        record_trade_result(pnl_pct, position["direction"], position["coin"], "SL_TP_AUTO")
                        return {"action": "STALE_STATE_CLEANED", "elapsed": elapsed}
                except RuntimeError as e:
                    return {"action": "CLOSE_FAILED", "error": str(e)}
                record_trade_result(pnl_pct, position["direction"], position["coin"], "TIMEOUT")
                notify_discord(f"⏰ **超时平仓** {position['direction']} {position['coin']}\n💰 入场: ${position['entry_price']:,.2f}\n📊 盈亏: {pnl_pct:+.2f}% | 持仓 {elapsed:.1f}h")
                return {"action": "TIMEOUT_CLOSE", "elapsed": elapsed, "pnl_pct": pnl_pct}
        
        # 检查SL/TP是否还在
        if not dry_run:
            sl_exists, tp_exists = check_sl_tp_orders("BTC", position)
            if not sl_exists or not tp_exists:
                print(f"⚠️ SL/TP 缺失! SL={sl_exists}, TP={tp_exists}")
                print("紧急修复中...")
                fix_sl_tp(position)
        
        return {"action": "HOLD", "position": position, "dry_run": dry_run}
    
    # 2. 无持仓，检查信号
    result = analyze("BTC")
    if "error" in result:
        print(f"信号检查失败: {result['error']}")
        return {"action": "ERROR", "error": result["error"]}
    
    signal = result["signal"]
    print(f"信号: {signal}")
    
    if signal == "HOLD":
        print("无信号，继续等待")
        return {"action": "HOLD", "signal": result, "dry_run": dry_run}
    
    # 3. 有信号，执行开仓
    if dry_run:
        return dry_run_open(signal, result)
    return open_position(signal, result)

def dry_run_open(signal, analysis):
    """Dry run: 计算开仓参数但不下单"""
    coin = "BTC"
    price = analysis["price"]
    is_long = signal == "LONG"
    
    account = get_account_info()
    account_value = float(account["account_value"])
    position_value = account_value * POSITION_RATIO
    
    max_loss_at_sl = position_value * STOP_LOSS_PCT
    if max_loss_at_sl > MAX_SINGLE_LOSS:
        position_value = MAX_SINGLE_LOSS / STOP_LOSS_PCT
    
    coin_info = get_coin_info(coin)
    sz_decimals = coin_info.get("szDecimals", 5) if coin_info else 5
    size = round(position_value / price, sz_decimals)
    
    if is_long:
        sl_price = round(price * (1 - STOP_LOSS_PCT))
        tp_price = round(price * (1 + TAKE_PROFIT_PCT))
    else:
        sl_price = round(price * (1 + STOP_LOSS_PCT))
        tp_price = round(price * (1 - TAKE_PROFIT_PCT))
    
    print(f"\n{'='*50}")
    print(f"🧪 DRY RUN — WOULD OPEN: {signal} {coin}")
    print(f"   账户: ${account_value:.2f}")
    print(f"   数量: {size} ({position_value:.2f} USD)")
    print(f"   价格: ~${price:,.2f}")
    print(f"   止损: ${sl_price:,.2f} ({'-' if is_long else '+'}{STOP_LOSS_PCT*100:.0f}%)")
    print(f"   止盈: ${tp_price:,.2f} ({'+' if is_long else '-'}{TAKE_PROFIT_PCT*100:.0f}%)")
    print(f"   最大亏损: ${position_value * STOP_LOSS_PCT:.2f}")
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
        "max_loss": position_value * STOP_LOSS_PCT,
        "reasons": analysis.get("signal_reasons", []),
    }

def open_position(signal, analysis):
    """开仓 + SL + TP 原子操作"""
    coin = "BTC"
    price = analysis["price"]
    is_long = signal == "LONG"
    
    # 计算仓位大小
    account = get_account_info()
    account_value = float(account["account_value"])
    position_value = account_value * POSITION_RATIO
    
    # 检查单笔最大亏损限制
    max_loss_at_sl = position_value * STOP_LOSS_PCT
    if max_loss_at_sl > MAX_SINGLE_LOSS:
        position_value = MAX_SINGLE_LOSS / STOP_LOSS_PCT
        print(f"仓位受限于最大单笔亏损 ${MAX_SINGLE_LOSS}: 仓位 ${position_value:.2f}")
    
    # 获取精度
    coin_info = get_coin_info(coin)
    sz_decimals = coin_info.get("szDecimals", 5) if coin_info else 5
    
    size = round(position_value / price, sz_decimals)
    if size <= 0:
        print("仓位太小，跳过")
        return {"action": "SKIP", "reason": "size_too_small"}
    
    # 计算 SL/TP 价格
    if is_long:
        sl_price = round(price * (1 - STOP_LOSS_PCT))
        tp_price = round(price * (1 + TAKE_PROFIT_PCT))
    else:
        sl_price = round(price * (1 + STOP_LOSS_PCT))
        tp_price = round(price * (1 - TAKE_PROFIT_PCT))
    
    print(f"\n{'='*50}")
    print(f"🚀 开仓: {signal} {coin}")
    print(f"   数量: {size} ({position_value:.2f} USD)")
    print(f"   价格: ~${price:,.2f}")
    print(f"   止损: ${sl_price:,.2f} ({'-' if is_long else '+'}{STOP_LOSS_PCT*100:.0f}%)")
    print(f"   止盈: ${tp_price:,.2f} ({'+' if is_long else '-'}{TAKE_PROFIT_PCT*100:.0f}%)")
    print(f"   最大亏损: ${position_value * STOP_LOSS_PCT:.2f}")
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
        sl_price = round(actual_entry * (1 - STOP_LOSS_PCT))
        tp_price = round(actual_entry * (1 + TAKE_PROFIT_PCT))
    else:
        sl_price = round(actual_entry * (1 + STOP_LOSS_PCT))
        tp_price = round(actual_entry * (1 - TAKE_PROFIT_PCT))
    
    # Step 2: 设止损
    print(f"\n[2/3] 设止损 ${sl_price:,.2f}...")
    try:
        sl_result = place_stop_loss(coin, actual_size, sl_price, is_long)
        print(f"止损结果: {json.dumps(sl_result, indent=2)}")
        if sl_result.get("status") == "err":
            raise Exception(f"SL failed: {sl_result}")
    except Exception as e:
        print(f"❌ 止损设置失败: {e}")
        print("🚨 紧急平仓！")
        try:
            emergency_close(coin, actual_size, is_long)
        except RuntimeError as close_err:
            return {"action": "EMERGENCY_CLOSE_FAILED", "error": str(close_err)}
        return {"action": "SL_FAILED_CLOSED", "error": str(e)}
    
    # Step 3: 设止盈
    print(f"\n[3/3] 设止盈 ${tp_price:,.2f}...")
    try:
        tp_result = place_take_profit(coin, actual_size, tp_price, is_long)
        print(f"止盈结果: {json.dumps(tp_result, indent=2)}")
        if tp_result.get("status") == "err":
            raise Exception(f"TP failed: {tp_result}")
    except Exception as e:
        print(f"❌ 止盈设置失败: {e}")
        print("🚨 紧急平仓！")
        # 先取消已设的SL
        try:
            orders = get_open_orders_detailed()
            for o in orders:
                if o.get("coin") == coin:
                    cancel_order(coin, o["oid"])
        except:
            pass
        try:
            emergency_close(coin, actual_size, is_long)
        except RuntimeError as close_err:
            return {"action": "EMERGENCY_CLOSE_FAILED", "error": str(close_err)}
        return {"action": "TP_FAILED_CLOSED", "error": str(e)}
    
    # 全部成功，保存状态
    state = {
        "position": {
            "coin": coin,
            "direction": signal,
            "size": actual_size,
            "entry_price": actual_entry,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "sl_price": sl_price,
            "tp_price": tp_price,
            "max_hold_hours": MAX_HOLD_HOURS,
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=MAX_HOLD_HOURS)).isoformat(),
        }
    }
    save_state(state)
    
    log_trade("OPEN", coin, signal, actual_size, actual_entry, sl_price, tp_price,
              "; ".join(analysis.get("signal_reasons", [])))
    
    print(f"\n✅ 开仓完成！SL=${sl_price:,.2f} TP=${tp_price:,.2f}")
    print(f"⏰ 超时平仓时间: {state['position']['deadline']}")
    
    notify_discord(f"🚀 **开仓** {signal} {coin}\n💰 入场: ${actual_entry:,.2f} | 数量: {actual_size}\n🛑 止损: ${sl_price:,.2f} (-{STOP_LOSS_PCT*100:.0f}%) | 🎯 止盈: ${tp_price:,.2f} (+{TAKE_PROFIT_PCT*100:.0f}%)\n⏰ 最长持仓: {MAX_HOLD_HOURS}h")
    
    return {
        "action": "OPENED",
        "direction": signal,
        "size": actual_size,
        "entry": actual_entry,
        "sl": sl_price,
        "tp": tp_price,
        "deadline": state["position"]["deadline"],
    }

def emergency_close(coin, size, is_long, max_retries=3):
    """紧急市价平仓 — 带重试和持久化告警"""
    print(f"🚨 紧急平仓 {coin} size={size}")
    
    for attempt in range(1, max_retries + 1):
        try:
            result = place_market_order(coin, not is_long, size)
            print(f"平仓结果 (attempt {attempt}): {json.dumps(result, indent=2)}")
            if result.get("status") == "err":
                raise Exception(f"Order error: {result}")
            save_state({"position": None})
            log_trade("EMERGENCY_CLOSE", coin, "LONG" if is_long else "SHORT", size, 
                      get_market_price(coin), reason=f"SL/TP设置失败紧急平仓 (attempt {attempt})")
            return  # success
        except Exception as e:
            print(f"❌ 紧急平仓 attempt {attempt}/{max_retries} 失败: {e}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)  # exponential backoff
    
    # All retries failed — persist danger state, alert, and RAISE
    print("❌❌ 紧急平仓全部失败！持久化告警...")
    danger_file = _WORKSPACE_DIR / "memory" / "trading" / "DANGER_UNPROTECTED.json"
    danger_file.parent.mkdir(parents=True, exist_ok=True)
    danger_file.write_text(json.dumps({
        "time": datetime.now(timezone.utc).isoformat(),
        "coin": coin,
        "size": size,
        "is_long": is_long,
        "reason": "emergency_close failed after all retries",
    }, indent=2))
    notify_discord(f"🚨🚨🚨 **紧急平仓失败** — {coin} 仓位无保护！需要人工干预！")
    raise RuntimeError(f"紧急平仓失败: {coin} size={size} — 仓位无保护！")

def close_position(position):
    """正常平仓（超时等原因）"""
    coin = position["coin"]
    size = abs(position["size"])
    is_long = position["direction"] == "LONG"
    
    # 先验证链上是否真的有仓位（防止 state 与链上不一致）
    real_pos = get_position(coin)
    if not real_pos:
        print(f"⚠️ 链上无 {coin} 持仓，state 残留。清理 state。")
        save_state({"position": None})
        notify_discord(f"ℹ️ {coin} 超时平仓跳过 — 链上已无仓位（可能 SL/TP 已触发）")
        return
    # 用链上真实数据覆盖，防止 size 不一致
    size = abs(real_pos["size"])
    is_long = real_pos["direction"] == "LONG"
    
    # 先取消所有挂单
    try:
        orders = get_open_orders_detailed()
        for o in orders:
            if o.get("coin") == coin:
                cancel_order(coin, o["oid"])
                print(f"已取消订单 {o['oid']}")
    except Exception as e:
        print(f"取消挂单失败: {e}")
    
    # 市价平仓
    result = place_market_order(coin, not is_long, size)
    print(f"平仓结果: {json.dumps(result, indent=2)}")

    if result.get("status") == "err":
        notify_discord(f"🚨 **超时平仓失败** — {coin} 仓位可能仍存在！需要人工干预！\n错误: {result}")
        raise RuntimeError(f"平仓失败: {coin} size={size} — {result}")

    save_state({"position": None})
    log_trade("CLOSE", coin, real_pos["direction"], size,
              get_market_price(coin), reason="超时平仓")
    return True

def check_sl_tp_orders(coin, position):
    """检查SL/TP订单是否存在"""
    orders = get_open_orders_detailed()
    sl_exists = False
    tp_exists = False
    for o in orders:
        if o.get("coin") == coin and o.get("isTrigger"):
            order_type = o.get("orderType", "")
            if "Stop" in order_type:
                sl_exists = True
            elif "Take" in order_type:
                tp_exists = True
    return sl_exists, tp_exists

def fix_sl_tp(position):
    """修复缺失的SL/TP"""
    coin = position["coin"]
    size = abs(position["size"])
    entry = position["entry_price"]
    is_long = position["direction"] == "LONG"
    
    if is_long:
        sl_price = round(entry * (1 - STOP_LOSS_PCT))
        tp_price = round(entry * (1 + TAKE_PROFIT_PCT))
    else:
        sl_price = round(entry * (1 + STOP_LOSS_PCT))
        tp_price = round(entry * (1 - TAKE_PROFIT_PCT))
    
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
            except RuntimeError:
                pass  # already persisted danger state and notified
            return
    
    if not tp_exists:
        print(f"补设止盈 ${tp_price:,.2f}...")
        try:
            place_take_profit(coin, size, tp_price, is_long)
            print("✅ 止盈已补设")
        except Exception as e:
            print(f"❌ 止盈补设失败: {e}, 止损已在，继续持仓")

if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    result = execute(dry_run=dry)
    print(f"\n最终结果: {json.dumps(result, default=str, indent=2)}")
