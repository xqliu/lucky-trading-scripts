"""
Core data types shared across all trading systems.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ExitReason(str, Enum):
    TP = "TP"
    SL = "SL"
    TIMEOUT = "TIMEOUT"
    EARLY_EXIT = "EARLY_EXIT"
    EMERGENCY = "EMERGENCY"
    MANUAL = "MANUAL"


@dataclass
class Signal:
    """Trading signal from any strategy."""
    coin: str
    direction: Direction
    price: float  # price at signal time
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    strategy: str = ""  # e.g. "momentum", "bb_breakout"
    confidence: float = 1.0
    metadata: dict = field(default_factory=dict)


@dataclass
class Position:
    """Open position tracking."""
    coin: str
    direction: Direction
    entry_price: float
    size: float  # in coin units
    entry_time: datetime
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    strategy: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class TradeResult:
    """Completed trade record."""
    coin: str
    direction: Direction
    entry_price: float
    exit_price: float
    size: float
    pnl_pct: float
    pnl_usd: float
    entry_time: datetime
    exit_time: datetime
    exit_reason: ExitReason
    strategy: str = ""
    fees_usd: float = 0.0
    metadata: dict = field(default_factory=dict)
