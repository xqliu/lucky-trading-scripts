#!/usr/bin/env python3
"""
Lucky's Hyperliquid Trading Script
"""
import sys
import os
import json
import argparse
from pathlib import Path
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

from luckytrader.config import load_secrets

# Lazy loading — secrets only loaded when needed for trading
_config_cache = None

def _get_secrets():
    """Lazy load secrets. Safe to import module without .hl_config existing."""
    global _config_cache
    if _config_cache is None:
        _config_cache = load_secrets()
    return _config_cache

# Module-level constants — lazy loaded
MAIN_WALLET = None
API_WALLET = None
API_PRIVATE_KEY = None

try:
    _c = load_secrets()
    MAIN_WALLET = _c["MAIN_WALLET"]
    API_WALLET = _c["API_WALLET"]
    API_PRIVATE_KEY = _c["API_PRIVATE_KEY"]
except (FileNotFoundError, ValueError):
    # Allow import without config for testing
    pass

def get_account_info():
    """Get account balance and positions"""
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    state = info.user_state(MAIN_WALLET)
    return {
        "account_value": state["marginSummary"]["accountValue"],
        "withdrawable": state["withdrawable"],
        "positions": state["assetPositions"]
    }

def get_market_price(coin: str):
    """Get current market price for a coin"""
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    mids = info.all_mids()
    return float(mids.get(coin, 0))

def get_meta():
    """Get exchange metadata (coin indices etc)"""
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    return info.meta()

def place_order(coin: str, is_buy: bool, size: float, price: float, reduce_only: bool = False):
    """Place a limit order"""
    account = Account.from_key(API_PRIVATE_KEY)
    exchange = Exchange(account, constants.MAINNET_API_URL, account_address=MAIN_WALLET)
    
    order_result = exchange.order(
        coin,
        is_buy,
        size,
        price,
        {"limit": {"tif": "Gtc"}},
        reduce_only=reduce_only
    )
    return order_result

def place_market_order(coin: str, is_buy: bool, size: float):
    """Place a market order"""
    account = Account.from_key(API_PRIVATE_KEY)
    exchange = Exchange(account, constants.MAINNET_API_URL, account_address=MAIN_WALLET)
    
    # Get current price and add slippage
    current_price = get_market_price(coin)
    slippage = 0.001  # 0.1%
    if is_buy:
        price = current_price * (1 + slippage)
    else:
        price = current_price * (1 - slippage)
    
    order_result = exchange.order(
        coin,
        is_buy,
        size,
        price,
        {"limit": {"tif": "Ioc"}}  # Immediate or cancel for market-like behavior
    )
    return order_result

def cancel_order(coin: str, oid: int):
    """Cancel an order"""
    account = Account.from_key(API_PRIVATE_KEY)
    exchange = Exchange(account, constants.MAINNET_API_URL, account_address=MAIN_WALLET)
    return exchange.cancel(coin, oid)

def place_stop_loss(coin: str, size: float, trigger_price: float, is_long: bool = True):
    """Place a stop loss order (trigger order)
    
    For LONG position: stop loss triggers when price DROPS to trigger_price (sell)
    For SHORT position: stop loss triggers when price RISES to trigger_price (buy)
    """
    # 验证触发价合理性
    current_price = get_market_price(coin)
    if is_long:
        if trigger_price >= current_price:
            raise ValueError(f"LONG stop-loss trigger ({trigger_price}) must be BELOW current price ({current_price})")
    else:
        if trigger_price <= current_price:
            raise ValueError(f"SHORT stop-loss trigger ({trigger_price}) must be ABOVE current price ({current_price})")
    
    account = Account.from_key(API_PRIVATE_KEY)
    exchange = Exchange(account, constants.MAINNET_API_URL, account_address=MAIN_WALLET)
    
    # For a long position, stop loss is a sell order triggered when price drops
    # For a short position, stop loss is a buy order triggered when price rises
    is_buy = not is_long
    
    order_result = exchange.order(
        coin,
        is_buy,
        size,
        trigger_price,  # Use trigger price as limit price for market-like execution
        {"trigger": {"triggerPx": trigger_price, "isMarket": True, "tpsl": "sl"}},
        reduce_only=True
    )
    return order_result

def place_take_profit(coin: str, size: float, trigger_price: float, is_long: bool = True):
    """Place a take profit order (trigger order)
    
    For LONG position: take profit triggers when price RISES to trigger_price (sell)
    For SHORT position: take profit triggers when price DROPS to trigger_price (buy)
    """
    # 验证触发价合理性
    current_price = get_market_price(coin)
    if is_long:
        if trigger_price <= current_price:
            raise ValueError(f"LONG take-profit trigger ({trigger_price}) must be ABOVE current price ({current_price})")
    else:
        if trigger_price >= current_price:
            raise ValueError(f"SHORT take-profit trigger ({trigger_price}) must be BELOW current price ({current_price})")
    
    account = Account.from_key(API_PRIVATE_KEY)
    exchange = Exchange(account, constants.MAINNET_API_URL, account_address=MAIN_WALLET)
    
    # For a long position, take profit is a sell order triggered when price rises
    # For a short position, take profit is a buy order triggered when price drops
    is_buy = not is_long
    
    order_result = exchange.order(
        coin,
        is_buy,
        size,
        trigger_price,
        {"trigger": {"triggerPx": trigger_price, "isMarket": True, "tpsl": "tp"}},
        reduce_only=True
    )
    return order_result

def get_open_orders():
    """Get open orders (basic info only)"""
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    return info.open_orders(MAIN_WALLET)

def get_open_orders_detailed():
    """Get open orders with full details (including isTrigger, orderType, etc.)"""
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    return info.frontend_open_orders(MAIN_WALLET)

def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Trading CLI")
    parser.add_argument("action", choices=["status", "price", "meta", "orders", "buy", "sell", "cancel", "stop-loss", "take-profit"])
    parser.add_argument("--coin", default="BTC", help="Coin to trade")
    parser.add_argument("--size", type=float, help="Order size")
    parser.add_argument("--price", type=float, help="Limit price (optional for market)")
    parser.add_argument("--trigger", type=float, help="Trigger price for stop-loss/take-profit")
    parser.add_argument("--oid", type=int, help="Order ID for cancel")
    parser.add_argument("--reduce", action="store_true", help="Reduce only")
    parser.add_argument("--short", action="store_true", help="For short position (default is long)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without executing")
    
    args = parser.parse_args()
    
    if args.action == "status":
        print(json.dumps(get_account_info(), indent=2))
    
    elif args.action == "price":
        price = get_market_price(args.coin)
        print(f"{args.coin}: ${price:,.2f}")
    
    elif args.action == "meta":
        meta = get_meta()
        print(json.dumps(meta, indent=2))
    
    elif args.action == "orders":
        orders = get_open_orders()
        print(json.dumps(orders, indent=2))
    
    elif args.action in ["buy", "sell"]:
        if not args.size:
            print("Error: --size required")
            sys.exit(1)
        
        is_buy = args.action == "buy"
        current_price = get_market_price(args.coin)
        
        if args.dry_run:
            order_type = "limit" if args.price else "market"
            price = args.price or current_price
            value = args.size * price
            print(f"🧪 DRY RUN - Would execute:")
            print(f"   Action: {args.action.upper()}")
            print(f"   Coin: {args.coin}")
            print(f"   Size: {args.size}")
            print(f"   Type: {order_type}")
            print(f"   Price: ${price:,.2f}")
            print(f"   Value: ${value:,.2f}")
            print(f"   Current market: ${current_price:,.2f}")
        else:
            if args.price:
                result = place_order(args.coin, is_buy, args.size, args.price, args.reduce)
            else:
                result = place_market_order(args.coin, is_buy, args.size)
            print(json.dumps(result, indent=2))
    
    elif args.action == "cancel":
        if not args.oid:
            print("Error: --oid required")
            sys.exit(1)
        if args.dry_run:
            print(f"🧪 DRY RUN - Would cancel:")
            print(f"   Coin: {args.coin}")
            print(f"   Order ID: {args.oid}")
        else:
            result = cancel_order(args.coin, args.oid)
            print(json.dumps(result, indent=2))
    
    elif args.action == "stop-loss":
        if not args.size or not args.trigger:
            print("Error: --size and --trigger required")
            sys.exit(1)
        is_long = not args.short
        current_price = get_market_price(args.coin)
        
        if args.dry_run:
            position_type = "SHORT" if args.short else "LONG"
            direction = "BUY" if args.short else "SELL"
            distance = ((args.trigger - current_price) / current_price) * 100
            print(f"🧪 DRY RUN - Would set stop-loss:")
            print(f"   Position: {position_type}")
            print(f"   Coin: {args.coin}")
            print(f"   Size: {args.size}")
            print(f"   Trigger: ${args.trigger:,.2f}")
            print(f"   Action when triggered: {direction}")
            print(f"   Current price: ${current_price:,.2f}")
            print(f"   Distance: {distance:+.2f}%")
        else:
            result = place_stop_loss(args.coin, args.size, args.trigger, is_long)
            print(json.dumps(result, indent=2))
    
    elif args.action == "take-profit":
        if not args.size or not args.trigger:
            print("Error: --size and --trigger required")
            sys.exit(1)
        is_long = not args.short
        current_price = get_market_price(args.coin)
        
        if args.dry_run:
            position_type = "SHORT" if args.short else "LONG"
            direction = "BUY" if args.short else "SELL"
            distance = ((args.trigger - current_price) / current_price) * 100
            print(f"🧪 DRY RUN - Would set take-profit:")
            print(f"   Position: {position_type}")
            print(f"   Coin: {args.coin}")
            print(f"   Size: {args.size}")
            print(f"   Trigger: ${args.trigger:,.2f}")
            print(f"   Action when triggered: {direction}")
            print(f"   Current price: ${current_price:,.2f}")
            print(f"   Distance: {distance:+.2f}%")
        else:
            result = place_take_profit(args.coin, args.size, args.trigger, is_long)
            print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
