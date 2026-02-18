#!/usr/bin/env python3
"""
Lucky's Trailing Stop Manager

移动止损策略：
- Trailing 比例：5%（止损跟随最高价的 95%）
- 激活条件：涨 3%+ 后启动
- 最低保护：止损不低于入场价（保本线）
- 只上不下：价格回调时止损不动
"""

import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 添加脚本目录到 path
sys.path.insert(0, str(Path(__file__).parent))

from luckytrader.trade import (
    get_market_price, 
    get_account_info, 
    get_open_orders,
    get_open_orders_detailed,
    place_stop_loss,
    cancel_order,
    MAIN_WALLET
)
from luckytrader.config import get_config, get_workspace_dir

# 配置 — 从 config/params.toml 加载
_cfg = get_config()
INITIAL_STOP_PCT = _cfg.trailing.initial_stop_pct
TRAILING_PCT = _cfg.trailing.trailing_pct
ACTIVATION_PCT = _cfg.trailing.activation_pct
STATE_FILE = get_workspace_dir() / "memory/trading/trailing_state.json"

def load_state():
    """加载持仓状态"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            print(f"⚠️ trailing_state.json 损坏，重置为空状态")
            return {}
    return {}

def save_state(state):
    """保存持仓状态（原子写入，防止 crash 损坏文件）"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)

def get_positions():
    """获取当前持仓"""
    info = get_account_info()
    positions = []
    for pos in info.get("positions", []):
        p = pos.get("position", {})
        if float(p.get("szi", 0)) != 0:
            positions.append({
                "coin": p.get("coin"),
                "size": abs(float(p.get("szi", 0))),
                "entry_price": float(p.get("entryPx", 0)),
                "is_long": float(p.get("szi", 0)) > 0,
                "unrealized_pnl": float(p.get("unrealizedPnl", 0))
            })
    return positions

def get_current_stop_order(coin: str, is_long: bool):
    """获取当前止损触发单（只匹配真正的 trigger order，不匹配 limit order）"""
    orders = get_open_orders_detailed()
    
    for order in orders:
        # 必须是指定币种
        if order.get("coin") != coin:
            continue
        
        # 必须是 trigger order（止损/止盈触发单）
        if not order.get("isTrigger"):
            continue
            
        # 必须是 reduce only（平仓单）
        if not order.get("reduceOnly"):
            continue
        
        trigger_price = float(order.get("triggerPx", 0))
        side = order.get("side")
        order_type = order.get("orderType", "")
        
        # 多头止损：触发卖单 (side=A)
        # 空头止损：触发买单 (side=B)
        if is_long and side == "A":
            return {
                "oid": order.get("oid"),
                "trigger_price": trigger_price,
                "order_type": order_type,
                "is_trigger": True
            }
        elif not is_long and side == "B":
            return {
                "oid": order.get("oid"),
                "trigger_price": trigger_price,
                "order_type": order_type,
                "is_trigger": True
            }
    
    return None

def check_and_update_trailing_stop(coin: str, position: dict, state: dict):
    """检查并更新移动止损"""
    
    entry_price = position["entry_price"]
    current_price = get_market_price(coin)
    size = position["size"]
    is_long = position["is_long"]
    
    # 获取或初始化状态
    pos_state = state.get(coin, {
        "entry_price": entry_price,
        "high_water_mark": entry_price,
        "trailing_active": False,
        "last_stop_price": None
    })
    
    high_water_mark = pos_state.get("high_water_mark", entry_price)
    trailing_active = pos_state.get("trailing_active", False)
    
    # 更新最高价（多头）或最低价（空头）
    if is_long:
        if current_price > high_water_mark:
            high_water_mark = current_price
            pos_state["high_water_mark"] = high_water_mark
    else:
        if current_price < high_water_mark:
            high_water_mark = current_price
            pos_state["high_water_mark"] = high_water_mark
    
    # 计算涨幅
    if is_long:
        gain_pct = (high_water_mark - entry_price) / entry_price
    else:
        gain_pct = (entry_price - high_water_mark) / entry_price
    
    # 检查是否激活
    if gain_pct >= ACTIVATION_PCT and not trailing_active:
        trailing_active = True
        pos_state["trailing_active"] = True
        print(f"🔔 Trailing stop ACTIVATED for {coin}! Gain: {gain_pct*100:.1f}%")
    
    # 计算止损位
    # 1) 未激活时：初始止损 = 入场价 * (1 - INITIAL_STOP_PCT)
    # 2) 激活后：移动止损 = 最高价 * (1 - TRAILING_PCT)，但不低于入场价
    if is_long:
        initial_stop = entry_price * (1 - INITIAL_STOP_PCT)
        if trailing_active:
            trailing_stop = high_water_mark * (1 - TRAILING_PCT)
            # 移动止损不低于入场价（保本线）
            new_stop = max(trailing_stop, entry_price)
        else:
            new_stop = initial_stop
    else:
        initial_stop = entry_price * (1 + INITIAL_STOP_PCT)
        if trailing_active:
            trailing_stop = high_water_mark * (1 + TRAILING_PCT)
            # 移动止损不高于入场价（保本线）
            new_stop = min(trailing_stop, entry_price)
        else:
            new_stop = initial_stop
    
    # 获取当前止损单
    current_stop = get_current_stop_order(coin, is_long)
    current_stop_price = current_stop["trigger_price"] if current_stop else None
    
    # 判断是否需要更新止损
    should_update = False
    if current_stop_price is None:
        # 没有止损单 → 必须设置！
        should_update = True
        print(f"   ⚠️ NO STOP ORDER! Setting initial stop @ ${new_stop:,.2f}")
    elif trailing_active:
        # 移动止损模式：只有新止损更优时才更新
        if is_long and new_stop > current_stop_price:
            should_update = True
        elif not is_long and new_stop < current_stop_price:
            should_update = True
    # 未激活时，已有止损单就不动
    
    if should_update:
        # 取消旧止损单
        if current_stop:
            print(f"❌ Canceling old stop @ ${current_stop_price:,.2f}")
            cancel_result = cancel_order(coin, current_stop["oid"])
            print(f"   Cancel result: {cancel_result}")
        
        # 下新止损单
        print(f"✅ Setting new stop @ ${new_stop:,.2f}")
        result = place_stop_loss(coin, size, new_stop, is_long)
        print(f"   Order result: {result}")
        
        # 🔒 验证止损单确实设置成功
        import time
        time.sleep(1)  # 等待订单上链
        verify_stop = get_current_stop_order(coin, is_long)
        if verify_stop:
            print(f"   ✅ VERIFIED: Stop order active @ ${verify_stop['trigger_price']:,.2f}")
            pos_state["last_stop_price"] = verify_stop['trigger_price']
            pos_state["verified"] = True
        else:
            print(f"   ⚠️ WARNING: Stop order NOT FOUND after placement!")
            print(f"   ⚠️ MANUAL CHECK REQUIRED!")
            pos_state["verified"] = False
            # 返回错误状态
            return {
                "action": "error",
                "coin": coin,
                "error": "Stop order not verified after placement",
                "result": result
            }
        
        return {
            "action": "updated",
            "coin": coin,
            "old_stop": current_stop_price,
            "new_stop": verify_stop['trigger_price'],
            "high_water_mark": high_water_mark,
            "trailing_active": trailing_active,
            "verified": True,
            "result": result
        }
    else:
        # 已有止损单，无需更新
        return {
            "action": "no_change",
            "coin": coin,
            "current_stop": current_stop_price,
            "calculated_stop": new_stop,
            "high_water_mark": high_water_mark,
            "trailing_active": trailing_active,
            "gain_pct": gain_pct * 100
        }

def main():
    """主函数：检查所有持仓的移动止损"""
    print(f"\n{'='*50}")
    _CST = timezone(timedelta(hours=8))
    print(f"🔄 Trailing Stop Check - {datetime.now(_CST).strftime('%Y-%m-%d %H:%M:%S CST')}")
    print(f"{'='*50}\n")
    
    positions = get_positions()
    
    if not positions:
        print("📭 No open positions")
        # 清理残留的 trailing state（防止与链上不一致）
        state = load_state()
        if state:
            print("🧹 Cleaning stale trailing state")
            from luckytrader.execute import notify_discord
            notify_discord(f"⚠️ **State 不一致** — 链上无持仓但 trailing_state 有残留: {list(state.keys())}，已自动清理")
            save_state({})
        return
    
    state = load_state()
    alerts = []  # 收集需要告警的问题
    
    for pos in positions:
        coin = pos["coin"]
        current_price = get_market_price(coin)
        print(f"\n📊 {coin} {'LONG' if pos['is_long'] else 'SHORT'}")
        print(f"   Entry: ${pos['entry_price']:,.2f}")
        print(f"   Size: {pos['size']}")
        print(f"   Current: ${current_price:,.2f}")
        print(f"   P&L: ${pos['unrealized_pnl']:,.2f}")
        
        # 🔒 首先检查是否有止损单存在
        existing_stop = get_current_stop_order(coin, pos['is_long'])
        if existing_stop:
            print(f"   🛡️ Stop order active @ ${existing_stop['trigger_price']:,.2f}")
        else:
            print(f"   ⚠️ NO STOP ORDER FOUND!")
            alerts.append(f"⚠️ {coin}: No stop order! Position unprotected!")
        
        result = check_and_update_trailing_stop(coin, pos, state)
        
        if result["action"] == "updated":
            print(f"   ⬆️ Stop updated: ${result.get('old_stop', 'N/A')} → ${result['new_stop']:,.2f}")
        elif result["action"] == "error":
            print(f"   ❌ ERROR: {result.get('error')}")
            alerts.append(f"❌ {coin}: Stop order failed to set!")
        elif result["action"] == "no_change":
            print(f"   ✓ Stop unchanged @ ${result['current_stop']:,.2f}")
        
        state[coin] = state.get(coin, {})
        state[coin].update({
            "entry_price": pos["entry_price"],
            "high_water_mark": result.get("high_water_mark", pos["entry_price"]),
            "trailing_active": result.get("trailing_active", False),
            "last_check": datetime.now().isoformat(),
            "has_stop": existing_stop is not None or result["action"] == "updated"
        })
    
    save_state(state)
    
    # 输出告警
    if alerts:
        print(f"\n{'!'*50}")
        print("⚠️ ALERTS:")
        for alert in alerts:
            print(f"   {alert}")
        print(f"{'!'*50}")
    
    print(f"\n{'='*50}\n")
    
    return alerts  # 返回告警列表供外部使用

if __name__ == "__main__":
    main()
