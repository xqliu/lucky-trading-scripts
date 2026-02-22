"""
Centralized configuration loader for Lucky Trading System.
Reads config.toml from the first found config directory:
  1. $LUCKYTRADER_CONFIG_DIR environment variable
  2. ./config/  (standalone repo usage)
  3. ../config/ (submodule / workspace usage)
"""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# --- Config search ---
def _find_config_dir() -> Path:
    """Find config directory by priority."""
    # 1. Environment variable
    env_dir = os.environ.get("LUCKYTRADER_CONFIG_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.exists():
            return p

    # 2. ./config/ (standalone usage — user cloned repo directly)
    local = Path.cwd() / "config"
    if local.exists() and (local / "config.toml").exists():
        return local

    # 3. Relative to this file: package_dir/../config/
    pkg_config = Path(__file__).parent.parent / "config"
    if pkg_config.exists() and (pkg_config / "config.toml").exists():
        return pkg_config

    # 4. ../config/ from cwd (submodule in workspace)
    parent_config = Path.cwd().parent / "config"
    if parent_config.exists() and (parent_config / "config.toml").exists():
        return parent_config

    # Fallback: ./config/ (will use defaults if missing)
    return Path(__file__).parent.parent / "config"


def get_workspace_dir() -> Path:
    """Get workspace root directory.

    Resolution order:
    1. $LUCKYTRADER_CONFIG_DIR → grandparent (config_dir/../../)
       e.g. .../workspace/trading/config → .../workspace
    2. Fallback to ~/.openclaw/workspace
    """
    env_dir = os.environ.get("LUCKYTRADER_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).parent.parent
    return Path.home() / ".openclaw" / "workspace"


# --- Data classes ---
@dataclass(frozen=True)
class StrategyConfig:
    vol_threshold: float = 2.0
    lookback_bars: int = 48
    range_bars: int = 48
    de_threshold: float = 0.25      # Directional Efficiency threshold (trend vs range)
    de_lookback_days: int = 7       # Days window for DE calculation

@dataclass(frozen=True)
class RiskConfig:
    stop_loss_pct: float = 0.03
    take_profit_pct: float = 0.05
    max_hold_hours: int = 48
    position_ratio: float = 0.20
    max_single_loss: float = 5.0

@dataclass(frozen=True)
class TrailingConfig:
    initial_stop_pct: float = 0.03
    trailing_pct: float = 0.04
    activation_pct: float = 0.02

@dataclass(frozen=True)
class OptimizationConfig:
    min_trades: int = 30
    improvement_threshold: float = 0.30
    consec_loss_threshold: int = 3

@dataclass(frozen=True)
class NotificationConfig:
    discord_channel_id: str = ""
    discord_mention_1: str = ""
    discord_mention_2: str = ""

    @property
    def discord_mentions(self) -> str:
        return f"{self.discord_mention_1} {self.discord_mention_2}"

@dataclass(frozen=True)
class ExchangeConfig:
    main_wallet: str = ""
    api_wallet: str = ""
    api_private_key: str = ""

@dataclass(frozen=True)
class AlertConfig:
    btc_support: float = 65000
    btc_resistance: float = 70000
    eth_support: float = 1850
    eth_resistance: float = 2100

@dataclass(frozen=True)
class TokenConfig:
    address: str = ""

@dataclass(frozen=True)
class TradingConfig:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    trailing: TrailingConfig = field(default_factory=TrailingConfig)
    optimization: OptimizationConfig = field(default_factory=OptimizationConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    token: TokenConfig = field(default_factory=TokenConfig)


# --- Loader ---
def _load_toml(path: Path) -> dict:
    """Load TOML file, return empty dict if missing."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


_CONFIG = None

def get_config() -> TradingConfig:
    """Load and cache trading config from config.toml."""
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG

    config_dir = _find_config_dir()
    data = _load_toml(config_dir / "config.toml")

    _CONFIG = TradingConfig(
        strategy=StrategyConfig(**data.get("strategy", {})),
        risk=RiskConfig(**data.get("risk", {})),
        trailing=TrailingConfig(**data.get("trailing", {})),
        optimization=OptimizationConfig(**data.get("optimization", {})),
        notifications=NotificationConfig(**data.get("notifications", {})),
        exchange=ExchangeConfig(**data.get("exchange", {})),
        alerts=AlertConfig(**data.get("alerts", {})),
        token=TokenConfig(**data.get("token", {})),
    )
    return _CONFIG


def reload_config() -> TradingConfig:
    """Force reload config (useful for tests)."""
    global _CONFIG
    _CONFIG = None
    return get_config()


def load_secrets() -> dict:
    """Load exchange credentials from config.toml [exchange] section.
    Returns dict with MAIN_WALLET, API_WALLET, API_PRIVATE_KEY."""
    cfg = get_config()
    result = {
        "MAIN_WALLET": cfg.exchange.main_wallet,
        "API_WALLET": cfg.exchange.api_wallet,
        "API_PRIVATE_KEY": cfg.exchange.api_private_key,
    }
    missing = [k for k, v in result.items() if not v]
    if missing:
        raise ValueError(
            f"Missing keys in config.toml [exchange]: {missing}\n"
            "Set main_wallet, api_wallet, api_private_key in [exchange] section"
        )
    return result
