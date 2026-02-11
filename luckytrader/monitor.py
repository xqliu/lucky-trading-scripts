#!/usr/bin/env python3
"""
市场检查脚本 - 每30分钟运行
1. 获取价格并追加到 DECISIONS.md
2. 检测价格警报，触发时唤醒 Lucky
"""

import json
import shutil
import requests
import subprocess
from datetime import datetime
from pathlib import Path
from luckytrader.config import get_config, get_workspace_dir

_SCRIPT_DIR = Path(__file__).parent
_WORKSPACE_DIR = get_workspace_dir()
DECISIONS_FILE = _WORKSPACE_DIR / "memory/trading/DECISIONS.md"
ALERT_FLAG = _WORKSPACE_DIR / "memory/trading/.alert_triggered"

# ===== 价格警报配置 — 从 config/params.toml 加载 =====
_cfg = get_config()
ALERTS = {
    "ETH": [
        {"type": "below", "price": _cfg.alerts.eth_support, "name": f"ETH 支撑位 ${_cfg.alerts.eth_support:,.0f}"},
        {"type": "above", "price": _cfg.alerts.eth_resistance, "name": f"ETH 阻力突破 ${_cfg.alerts.eth_resistance:,.0f}"},
    ],
    "BTC": [
        {"type": "below", "price": _cfg.alerts.btc_support, "name": f"BTC 支撑位 ${_cfg.alerts.btc_support/1000:.0f}K"},
        {"type": "above", "price": _cfg.alerts.btc_resistance, "name": f"BTC 阻力突破 ${_cfg.alerts.btc_resistance/1000:.0f}K"},
    ]
}

def get_prices():
    """获取 BTC 和 ETH 价格"""
    try:
        url = "https://api.hyperliquid.xyz/info"
        payload = {"type": "allMids"}
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        btc = float(data.get("BTC", 0))
        eth = float(data.get("ETH", 0))
        # 验证价格有效性 - 防止空数据误触发警报
        if btc < 1000 or eth < 100:
            print(f"⚠️ 价格异常 (BTC={btc}, ETH={eth})，跳过本次检查")
            return None
        return {"BTC": btc, "ETH": eth}
    except Exception as e:
        print(f"Error fetching prices: {e}")
        return None

def get_account_status():
    """获取账户状态"""
    try:
        url = "https://api.hyperliquid.xyz/info"
        wallet = _cfg.exchange.main_wallet
        payload = {"type": "clearinghouseState", "user": wallet}
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        margin_summary = data.get("marginSummary", {})
        account_value = float(margin_summary.get("accountValue", 0))
        positions = data.get("assetPositions", [])
        return account_value, positions
    except Exception as e:
        print(f"Error fetching account: {e}")
        return 100.0, []

def check_alerts(prices):
    """检查价格是否触发警报"""
    triggered = []
    for coin, alerts in ALERTS.items():
        price = prices.get(coin, 0)
        for alert in alerts:
            if alert["type"] == "below" and price <= alert["price"]:
                triggered.append(f"🚨 {alert['name']}: {coin} ${price:,.2f} ≤ ${alert['price']:,}")
            elif alert["type"] == "above" and price >= alert["price"]:
                triggered.append(f"🚨 {alert['name']}: {coin} ${price:,.2f} ≥ ${alert['price']:,}")
    return triggered

def wake_lucky(alerts, prices):
    """通过 system event 唤醒 Lucky"""
    alert_text = "\n".join(alerts)
    message = f"""⚡ 价格警报触发！

{alert_text}

当前价格: BTC ${prices['BTC']:,.2f} | ETH ${prices['ETH']:,.2f}

判断流程：
1. 读 logs/trading/market_check_YYYY-MM-DD.log 看价格走势
2. 读 memory/trading/DECISIONS.md 看历史决策
3. 按 SYSTEM.md 规则判断
4. 决定后更新 DECISIONS.md"""
    
    try:
        # 使用 openclaw system event 发送消息并立即唤醒
        # 注意：使用完整路径，因为 crontab 环境没有 ~/.local/bin 在 PATH
        openclaw_path = shutil.which("openclaw") or str(Path.home() / ".local/bin/openclaw")
        result = subprocess.run(
            [openclaw_path, "system", "event", "--text", message, "--mode", "now"],
            capture_output=True,
            text=True,
            timeout=30
        )
        print(f"Wake sent: {result.stdout}")
        if result.returncode != 0:
            print(f"Wake stderr: {result.stderr}")
        # 写入标记防止重复触发
        ALERT_FLAG.write_text(datetime.now().isoformat())
    except Exception as e:
        print(f"Failed to wake Lucky: {e}")

def append_check(prices, account_value, positions, alerts):
    """追加检查记录到 DECISIONS.md"""
    now = datetime.now()
    time_str = now.strftime("%H:%M SGT")
    date_str = now.strftime("%Y-%m-%d")
    
    content = DECISIONS_FILE.read_text() if DECISIONS_FILE.exists() else ""
    
    if f"## {date_str}" not in content:
        content += f"\n## {date_str}\n"
    
    if positions:
        pos_str = ", ".join([f"{p['position']['coin']} {p['position']['szi']}" for p in positions])
    else:
        pos_str = "无"
    
    alert_str = " | ".join(alerts) if alerts else ""
    alert_line = f"\n- **⚡ 警报**: {alert_str}" if alerts else ""
    
    # 无警报时自动标记 HOLD，有警报时待分析
    if alerts:
        decision = "[待分析 - 警报触发]"
        reason = "Lucky 被唤醒，需判断是否入场"
    else:
        decision = "HOLD ⏸️"
        reason = "未触发警报，继续观望"
    
    entry = f"""
### {time_str}
- **BTC**: ${prices['BTC']:,.2f} | **ETH**: ${prices['ETH']:,.2f}
- **账户**: ${account_value:.2f} | **持仓**: {pos_str}{alert_line}
- **决策**: {decision}
- **理由**: {reason}

"""
    content += entry
    DECISIONS_FILE.write_text(content)
    
    status = "🚨 ALERT" if alerts else "✅"
    print(f"{status} {time_str} - BTC: ${prices['BTC']:,.2f}, ETH: ${prices['ETH']:,.2f}")

def run_signal_executor():
    """运行信号执行器"""
    try:
        from execute_signal import execute
        print("\n--- 信号执行器 ---")
        result = execute()
        action = result.get("action", "UNKNOWN")
        print(f"执行结果: {action}")
        
        # 如果开仓或平仓，唤醒 Lucky 通知
        if action in ("OPENED", "TIMEOUT_CLOSE", "SL_FAILED_CLOSED", "TP_FAILED_CLOSED"):
            message = f"⚡ 交易执行: {action}\n{json.dumps(result, default=str)}"
            try:
                openclaw_path = shutil.which("openclaw") or str(Path.home() / ".local/bin/openclaw")
                subprocess.run(
                    [openclaw_path, "system", "event", "--text", message, "--mode", "now"],
                    capture_output=True, text=True, timeout=30
                )
            except Exception as e:
                print(f"通知失败: {e}")
        
        return result
    except Exception as e:
        print(f"信号执行器错误: {e}")
        import traceback
        traceback.print_exc()
        return None

def main():
    prices = get_prices()
    if prices is None:
        print("❌ 无法获取价格")
        return
    
    account_value, positions = get_account_status()
    alerts = check_alerts(prices)
    
    append_check(prices, account_value, positions, alerts)
    
    # 运行信号执行器（每次都跑，检查信号+管理持仓）
    run_signal_executor()
    
    # 如果有警报且最近1小时内没触发过，唤醒 Lucky
    if alerts:
        if ALERT_FLAG.exists():
            last_alert = datetime.fromisoformat(ALERT_FLAG.read_text())
            hours_since = (datetime.now() - last_alert).total_seconds() / 3600
            if hours_since < 1:
                print("⏳ 警报已在1小时内触发过，跳过")
                return
        wake_lucky(alerts, prices)

if __name__ == "__main__":
    main()
