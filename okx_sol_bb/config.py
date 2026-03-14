"""
OKX SOL BB System Configuration
=================================
Loads from config.toml + secrets from .okx_config
Independent from ETH BB config — separate config dir, separate keys.
"""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _find_config_dir() -> Path:
    env_dir = os.environ.get("OKX_SOL_BB_CONFIG_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.exists():
            return p
    pkg_config = Path(__file__).parent / "config"
    if pkg_config.exists():
        return pkg_config
    return Path(__file__).parent


@dataclass
class StrategyConfig:
    bb_period: int = 14
    bb_multiplier: float = 3.0


@dataclass
class RiskConfig:
    take_profit_pct: float = 0.02    # 2% — backtest validated
    stop_loss_pct: float = 0.05      # 5% — backtest validated
    max_hold_bars: int = 96          # 48h on 30m candles
    position_ratio: float = 0.30
    max_single_loss: float = 10.0    # $10
    leverage: int = 5


@dataclass
class FeeConfig:
    taker_fee: float = 0.0005  # OKX taker 5 bps (VIP0)
    maker_fee: float = 0.0002  # OKX maker 2 bps (VIP0)


@dataclass
class OKXSolConfig:
    strategy: StrategyConfig
    risk: RiskConfig
    fees: FeeConfig
    # Exchange credentials (loaded from secrets)
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""
    # Coin
    coin: str = "SOL"
    instId: str = "SOL-USDT-SWAP"
    # Notifications
    discord_channel_id: str = ""


def load_config() -> OKXSolConfig:
    """Load config from TOML + secrets."""
    config_dir = _find_config_dir()
    toml_path = config_dir / "config.toml"

    strategy = StrategyConfig()
    risk = RiskConfig()
    fees = FeeConfig()
    raw = {}

    if toml_path.exists():
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)

        if "strategy" in raw:
            s = raw["strategy"]
            strategy = StrategyConfig(
                bb_period=s.get("bb_period", 14),
                bb_multiplier=s.get("bb_multiplier", 3.0),
            )
        if "risk" in raw:
            r = raw["risk"]
            risk = RiskConfig(
                take_profit_pct=r.get("take_profit_pct", 0.02),
                stop_loss_pct=r.get("stop_loss_pct", 0.05),
                max_hold_bars=r.get("max_hold_bars", 96),
                position_ratio=r.get("position_ratio", 0.30),
                max_single_loss=r.get("max_single_loss", 10.0),
                leverage=r.get("leverage", 5),
            )
        if "fees" in raw:
            fe = raw["fees"]
            fees = FeeConfig(
                taker_fee=fe.get("taker_fee", 0.0005),
                maker_fee=fe.get("maker_fee", 0.0002),
            )

    coin = "SOL"
    instId = "SOL-USDT-SWAP"
    discord_channel_id = ""
    if "exchange" in raw:
        ex = raw["exchange"]
        coin = ex.get("coin", coin)
        instId = ex.get("instId", instId)
    if "notifications" in raw:
        discord_channel_id = raw["notifications"].get("discord_channel_id", "")

    cfg = OKXSolConfig(strategy=strategy, risk=risk, fees=fees,
                       coin=coin, instId=instId, discord_channel_id=discord_channel_id)

    # Load secrets
    secrets_path = config_dir / ".okx_config"
    if secrets_path.exists():
        for line in secrets_path.read_text().strip().split("\n"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                key = key.strip()
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
