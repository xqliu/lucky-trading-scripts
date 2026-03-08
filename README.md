# 🍀 Lucky Trading Scripts

Open-source trading infrastructure for [Hyperliquid](https://hyperliquid.xyz). Built by [Lucky](https://luckyclaw.win) — an AI trader running a $100→$217 experiment.

[![Tests](https://img.shields.io/badge/tests-130%20passed-brightgreen)](tests/)
[![Coverage](https://img.shields.io/badge/coverage-90%25%20core-blue)](tests/)
[![Journal](https://img.shields.io/badge/journal-luckyclaw.win-orange)](https://luckyclaw.win)

## What's This?

A complete crypto trading system with:
- **Signal detection** — Volume breakout strategy with configurable parameters
- **Atomic execution** — Open + SL + TP as one unit, rollback on failure
- **Trailing stop** — Activates after configurable gain, follows high water mark
- **Emergency close** — 3x retry with exponential backoff + persistent alerting
- **Monthly optimization** — Auto-scans parameter space, suggests updates
- **Dry run mode** — Full pipeline without placing real orders
- **130 unit tests** — Every money-touching path is tested

## Quick Start

```bash
# Clone
git clone https://github.com/xqliu/lucky-trading-scripts.git
cd lucky-trading-scripts

# Setup
python -m venv .venv && source .venv/bin/activate
pip install hyperliquid-python-sdk eth-account requests pytest pytest-cov

# Configure
cp config/config.example.toml config/config.toml  # edit with your params

# Run tests (no real money touched!)
cd scripts && python -m pytest tests/ -v

# Dry run
python execute_signal.py --dry-run

# Check signal
python signal_check.py BTC
```

## Architecture

```
config/
  __init__.py           # Typed config loader (dataclass + TOML)
  config.example.toml   # Example config (copy to config.toml)
scripts/
  signal_check.py       # Signal detection + report formatting
  execute_signal.py     # Order execution (atomic open + SL + TP)
  trailing_stop.py      # Trailing stop management
  hl_trade.py           # Hyperliquid API wrapper + CLI
  market_check.py       # Cron job: price alerts + signal executor
  monthly_optimize.py   # Parameter space scanner
  backtest_30m_v2.py    # Backtesting engine
  luckytrader_monitor.py # Token monitoring
tests/
  conftest.py           # Mock framework (isolates from real exchange)
  test_*.py × 12        # 130 tests covering all core paths
```

## Config System

All parameters live in `config/config.toml` (gitignored). Every script reads from the same source — change once, applies everywhere.

```toml
[risk]
stop_loss_pct = 0.03       # your stop loss %
take_profit_pct = 0.05     # your take profit %
max_hold_hours = 48        # max hours before timeout close

[strategy]
vol_threshold = 2.0        # volume breakout multiplier

[notifications]
discord_channel_id = "1234567890123456789"
discord_mention_1 = "<@111111111111111111>"
discord_mention_2 = "<@222222222222222222>"
```

For OKX BB, copy `okx_bb/config/config.toml.sample` to `okx_bb/config/config.toml` and set `notifications.discord_channel_id` there as well.

## Notifications

Discord notifications intentionally avoid hardcoded IDs in the repo. Configure them locally instead:

1. Put your channel and mention targets in `config/config.toml` or `okx_bb/config/config.toml`.
2. Keep your bot token in `~/.openclaw/openclaw.json` under `channels.discord.token`.
3. If you use a Discord-compatible gateway other than `discord.com`, set:

```bash
export OPENCLAW_DISCORD_API_BASE="https://your-gateway.example/api/v10"
```

4. Optional overrides if you want to drive notifications entirely from env vars:

```bash
export OPENCLAW_DISCORD_CHANNEL_ID="1234567890123456789"
export OPENCLAW_DISCORD_MENTIONS="<@111111111111111111> <@222222222222222222>"
```

Do not commit real channel IDs, mention IDs, bot tokens, or private gateway URLs.

## Safety Features

- **Atomic execution**: If SL or TP placement fails after opening, emergency close fires immediately
- **Emergency close retry**: 3 attempts with exponential backoff. If all fail, writes `DANGER_UNPROTECTED.json` and sends alert
- **SL/TP monitoring**: Every heartbeat checks if protection orders are still active
- **Position timeout**: Auto-closes after max hold hours
- **Dry run**: `--dry-run` flag runs the full decision pipeline without placing any orders

## Testing

```bash
# All tests
python -m pytest tests/ -v

# With coverage
python -m pytest tests/ --cov=execute_signal --cov=signal_check --cov=trailing_stop --cov-report=term-missing

# Single file
python -m pytest tests/test_trailing_stop.py -v
```

## The Experiment

Lucky (an AI) was given $100 on 2025-02-01 to learn trading. Current balance: **$217.76** (+117.8%). Full journey documented at [luckyclaw.win](https://luckyclaw.win).

## License

MIT — Use it, learn from it, make money with it. Not financial advice.
