"""
OKX BB System Configuration
============================
Loads from config.toml + secrets from .okx_config
"""
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# --- Config search ---
def _find_config_dir() -> Path:
    env_dir = os.environ.get("OKX_BB_CONFIG_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.exists():
            return p
    # Relative to this file
    pkg_config = Path(__file__).parent / "config"
    if pkg_config.exists():
        return pkg_config
    return Path(__file__).parent


@dataclass
class StrategyConfig:
    bb_period: int = 20
    bb_multiplier: float = 2.5
    trend_ema_period: int = 96
    trend_lookback: int = 8


@dataclass
class RiskConfig:
    take_profit_pct: float = 0.03
    stop_loss_pct: float = 0.02
    max_hold_bars: int = 120  # 60h on 30m candles
    position_ratio: float = 0.30
    max_single_loss: float = 10.0  # $10


@dataclass
class FeeConfig:
    taker_fee: float = 0.0005  # OKX taker 5 bps (VIP0)
    maker_fee: float = 0.0002  # OKX maker 2 bps (VIP0)


@dataclass
class OKXConfig:
    strategy: StrategyConfig
    risk: RiskConfig
    fees: FeeConfig
    # Exchange credentials (loaded from secrets)
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""
    # Coin
    coin: str = "ETH"
    instId: str = "ETH-USDT-SWAP"  # OKX instrument ID
    # Notifications
    discord_channel_id: str = "1469405365849313831"


def load_config() -> OKXConfig:
    """Load config from TOML + secrets."""
    config_dir = _find_config_dir()
    toml_path = config_dir / "config.toml"

    strategy = StrategyConfig()
    risk = RiskConfig()
    fees = FeeConfig()

    if toml_path.exists():
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)

        if "strategy" in raw:
            s = raw["strategy"]
            strategy = StrategyConfig(
                bb_period=s.get("bb_period", 20),
                bb_multiplier=s.get("bb_multiplier", 2.5),
                trend_ema_period=s.get("trend_ema_period", 96),
                trend_lookback=s.get("trend_lookback", 8),
            )
        if "risk" in raw:
            r = raw["risk"]
            risk = RiskConfig(
                take_profit_pct=r.get("take_profit_pct", 0.03),
                stop_loss_pct=r.get("stop_loss_pct", 0.02),
                max_hold_bars=r.get("max_hold_bars", 120),
                position_ratio=r.get("position_ratio", 0.30),
                max_single_loss=r.get("max_single_loss", 10.0),
            )
        if "fees" in raw:
            fe = raw["fees"]
            fees = FeeConfig(
                taker_fee=fe.get("taker_fee", 0.0005),
                maker_fee=fe.get("maker_fee", 0.0002),
            )

    cfg = OKXConfig(strategy=strategy, risk=risk, fees=fees)

    # Load secrets
    secrets_path = config_dir / ".okx_config"
    if not secrets_path.exists():
        # Try workspace-level
        ws = Path(os.environ.get("OKX_BB_CONFIG_DIR", "~/.openclaw/workspace")).expanduser()
        secrets_path = ws / ".okx_config"

    if secrets_path.exists():
        for line in secrets_path.read_text().strip().split("\n"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                key = key.strip()
                # Handle shell-style "export KEY=val"
                if key.startswith("export "):
                    key = key[7:].strip()
                val = val.strip().strip('"').strip("'")
                if key == "OKX_API_KEY":
                    cfg.api_key = val
                elif key == "OKX_SECRET_KEY":
                    cfg.secret_key = val
                elif key == "OKX_PASSPHRASE":
                    cfg.passphrase = val

    return cfg
