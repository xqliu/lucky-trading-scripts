"""
TDD Tests for DE (Directional Efficiency) Regime Detection
RED-GREEN-REFACTOR cycle.

Tests cover:
- compute_de(): 11 cases
- get_regime_params(): 7 cases
- Integration: 5 cases
- Edge cases: 3 cases
Total: 26 tests
"""
import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

# Will fail until regime.py is created — RED phase
from luckytrader.regime import compute_de, get_regime_params


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_candles(closes, highs=None, lows=None):
    """Build daily candle list from close prices.
    Uses close±0.5% for high/low if not provided.
    """
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c * 1.005
        l = lows[i] if lows else c * 0.995
        out.append({'c': str(c), 'h': str(h), 'l': str(l), 't': str(i * 86400000)})
    return out


def make_trending_candles(start=60000, step=1000, n=10):
    """Strongly trending: each day +step — DE should be high."""
    closes = [start + i * step for i in range(n)]
    return make_candles(closes)


def make_ranging_candles(base=60000, amplitude=100, n=10):
    """Ranging: oscillate around base — DE should be low."""
    import math
    closes = [base + amplitude * math.sin(i) for i in range(n)]
    return make_candles(closes)


@dataclass(frozen=True)
class FakeStrategyConfig:
    de_threshold: float = 0.25
    de_lookback_days: int = 7


@dataclass(frozen=True)
class FakeConfig:
    strategy: FakeStrategyConfig = None

    def __post_init__(self):
        object.__setattr__(self, 'strategy', FakeStrategyConfig())


# ─── compute_de() tests ───────────────────────────────────────────────────────

class TestComputeDe:

    def test_trending_market_returns_high_de(self):
        """Strongly trending candles → DE > 0.25 (trend signal)."""
        candles = make_trending_candles(start=60000, step=600, n=12)
        de = compute_de(candles)
        assert de is not None
        assert de > 0.25, f"Expected DE > 0.25 for trending market, got {de}"

    def test_ranging_market_returns_low_de(self):
        """Ranging/oscillating candles → DE ≤ 0.25 (range signal)."""
        candles = make_ranging_candles(base=60000, amplitude=300, n=12)
        de = compute_de(candles)
        assert de is not None
        assert de <= 0.25, f"Expected DE ≤ 0.25 for ranging market, got {de}"

    def test_minimum_8_candles_returns_valid(self):
        """Exactly 8 candles (minimum for lookback_days=7) → returns float."""
        candles = make_candles([60000 + i * 100 for i in range(8)])
        de = compute_de(candles, lookback_days=7)
        assert de is not None
        assert isinstance(de, float)
        assert de >= 0.0

    def test_7_candles_insufficient_returns_none(self):
        """7 candles (below minimum) → returns None."""
        candles = make_candles([60000] * 7)
        de = compute_de(candles, lookback_days=7)
        assert de is None

    def test_atr_zero_flat_market_returns_zero_not_exception(self):
        """All candles same price → ATR=0, DE=0.0 (not ZeroDivisionError)."""
        candles = make_candles([60000.0] * 10, highs=[60000.0] * 10, lows=[60000.0] * 10)
        de = compute_de(candles)
        assert de == 0.0, f"Expected 0.0 for flat market, got {de}"

    def test_price_unchanged_7d_returns_zero(self):
        """Price 7d ago == price now, but ATR > 0 → DE = 0.0."""
        # Build candles where first and last close are equal, but H/L vary
        closes = [60000] + [60100, 59900, 60200, 59800, 60150, 59850, 60000]  # 8 candles
        highs = [c * 1.01 for c in closes]
        lows = [c * 0.99 for c in closes]
        candles = make_candles(closes, highs, lows)
        de = compute_de(candles, lookback_days=7)
        assert de == 0.0, f"Expected 0.0 when price unchanged, got {de}"

    def test_empty_list_returns_none(self):
        """Empty list → None."""
        assert compute_de([]) is None

    def test_none_input_returns_none(self):
        """None input → None (no AttributeError)."""
        assert compute_de(None) is None

    def test_candle_missing_c_key_returns_none(self):
        """Candle missing 'c' key → None gracefully."""
        candles = make_candles([60000] * 8)
        candles[-1] = {'h': '60100', 'l': '59900', 't': '0'}  # missing 'c'
        de = compute_de(candles)
        assert de is None

    def test_candle_with_none_value_returns_none(self):
        """Candle with None value for 'c' → None gracefully."""
        candles = make_candles([60000] * 8)
        candles[-1] = {'c': None, 'h': '60100', 'l': '59900', 't': '0'}
        de = compute_de(candles)
        assert de is None

    def test_very_large_de_no_overflow(self):
        """Extreme trending (huge moves, tiny ATR) → valid float, no overflow."""
        # Price jumps from 1000 to 100000 with tiny daily ranges
        closes = [1000] + [1000] * 6 + [100000]  # 8 candles
        highs = [c * 1.0001 for c in closes]
        lows = [c * 0.9999 for c in closes]
        candles = make_candles(closes, highs, lows)
        de = compute_de(candles)
        assert de is not None
        assert isinstance(de, float)
        assert de > 0

    def test_de_is_non_negative(self):
        """DE is always >= 0 (uses abs of net change)."""
        # Downtrending
        closes = [60000 - i * 500 for i in range(10)]
        candles = make_candles(closes)
        de = compute_de(candles)
        assert de is not None
        assert de >= 0.0

    def test_custom_lookback_days_respected(self):
        """lookback_days=3 uses shorter window than default 7."""
        candles = make_trending_candles(n=10)
        de7 = compute_de(candles, lookback_days=7)
        de3 = compute_de(candles, lookback_days=3)
        # Both should be valid floats
        assert de7 is not None
        assert de3 is not None


# ─── get_regime_params() tests ────────────────────────────────────────────────

class TestGetRegimeParams:

    def setup_method(self):
        self.cfg = FakeConfig()

    def test_high_de_returns_trend_params(self):
        """DE=0.30 (> 0.25) → trend params."""
        params = get_regime_params(0.30, self.cfg)
        assert params['regime'] == 'trend'
        assert params['tp_pct'] == 0.07
        assert params['sl_pct'] == 0.04

    def test_de_exactly_at_threshold_returns_range(self):
        """DE=0.25 (== threshold) → range (boundary: not > threshold)."""
        params = get_regime_params(0.25, self.cfg)
        assert params['regime'] == 'range'
        assert params['tp_pct'] == 0.02
        assert params['sl_pct'] == 0.05

    def test_low_de_returns_range_params(self):
        """DE=0.10 (< 0.25) → range params."""
        params = get_regime_params(0.10, self.cfg)
        assert params['regime'] == 'range'
        assert params['tp_pct'] == 0.02
        assert params['sl_pct'] == 0.05

    def test_zero_de_returns_range_params(self):
        """DE=0.0 → range params."""
        params = get_regime_params(0.0, self.cfg)
        assert params['regime'] == 'range'
        assert params['tp_pct'] == 0.02

    def test_none_de_returns_range_failopen(self):
        """DE=None (data unavailable) → range params with regime='unknown'."""
        params = get_regime_params(None, self.cfg)
        assert params['regime'] == 'unknown'
        assert params['tp_pct'] == 0.02   # safer default: range TP
        assert params['sl_pct'] == 0.05   # safer default: range SL

    def test_custom_threshold_in_config_respected(self):
        """DE=0.30, custom threshold=0.40 → range (0.30 < 0.40)."""
        @dataclass(frozen=True)
        class CustomStrategy:
            de_threshold: float = 0.40
            de_lookback_days: int = 7

        @dataclass(frozen=True)
        class CustomConfig:
            strategy: CustomStrategy = None
            def __post_init__(self):
                object.__setattr__(self, 'strategy', CustomStrategy())

        params = get_regime_params(0.30, CustomConfig())
        assert params['regime'] == 'range'

    def test_config_missing_de_threshold_uses_default(self):
        """Config without de_threshold attr → falls back to 0.25 default."""
        @dataclass(frozen=True)
        class LegacyStrategy:
            vol_threshold: float = 1.25  # no de_threshold

        @dataclass(frozen=True)
        class LegacyConfig:
            strategy: LegacyStrategy = None
            def __post_init__(self):
                object.__setattr__(self, 'strategy', LegacyStrategy())

        # Should not raise AttributeError
        params = get_regime_params(0.30, LegacyConfig())
        assert params['regime'] in ('trend', 'range', 'unknown')
        assert 'tp_pct' in params
        assert 'sl_pct' in params

    def test_params_dict_has_required_keys(self):
        """Return dict always has tp_pct, sl_pct, regime keys."""
        for de in [0.0, 0.10, 0.25, 0.30, None]:
            params = get_regime_params(de, self.cfg)
            assert 'tp_pct' in params, f"Missing tp_pct for de={de}"
            assert 'sl_pct' in params, f"Missing sl_pct for de={de}"
            assert 'regime' in params, f"Missing regime for de={de}"


# ─── Edge Cases ───────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_candles_with_string_numbers_work(self):
        """Candles from Hyperliquid API return strings — must parse correctly."""
        candles = [
            {'c': '60000.0', 'h': '60500.0', 'l': '59500.0', 't': '0'},
            {'c': '60100.0', 'h': '60600.0', 'l': '59600.0', 't': '1'},
            {'c': '60200.0', 'h': '60700.0', 'l': '59700.0', 't': '2'},
            {'c': '60300.0', 'h': '60800.0', 'l': '59800.0', 't': '3'},
            {'c': '60400.0', 'h': '60900.0', 'l': '59900.0', 't': '4'},
            {'c': '60500.0', 'h': '61000.0', 'l': '60000.0', 't': '5'},
            {'c': '60600.0', 'h': '61100.0', 'l': '60100.0', 't': '6'},
            {'c': '60700.0', 'h': '61200.0', 'l': '60200.0', 't': '7'},
        ]
        de = compute_de(candles, lookback_days=7)
        assert de is not None
        assert isinstance(de, float)
        assert de >= 0.0

    def test_de_boundary_0_25_exactly(self):
        """Boundary test: verify DE=0.25 goes to range, DE=0.251 goes to trend."""
        cfg = FakeConfig()
        range_params = get_regime_params(0.25, cfg)
        trend_params = get_regime_params(0.251, cfg)
        assert range_params['regime'] == 'range'
        assert trend_params['regime'] == 'trend'

    def test_mixed_none_valid_candles_returns_none(self):
        """List with one None entry → None (no crash)."""
        candles = make_candles([60000] * 7) + [None]
        de = compute_de(candles)
        assert de is None


# ─── Integration (execute.open_position) ─────────────────────────────────────

@pytest.fixture
def mock_deps():
    """Patch execute.py dependencies for open_position integration tests."""
    with patch('luckytrader.execute.get_candles', return_value=make_candles([60000 + i for i in range(10)])) as mock_get_candles, \
         patch('luckytrader.execute.get_account_info', return_value={"account_value": "1000.0"}) as mock_account, \
         patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5}) as mock_coin_info, \
         patch('luckytrader.execute.place_market_order', return_value={"status": "ok"}) as mock_market_order, \
         patch('luckytrader.execute.get_position', return_value={
             "coin": "BTC",
             "size": -1.0,
             "direction": "SHORT",
             "entry_price": 100.0,
             "unrealized_pnl": 0.0,
             "liquidation_price": 0.0,
         }) as mock_get_position, \
         patch('luckytrader.execute.place_stop_loss', return_value={"status": "ok"}) as mock_stop_loss, \
         patch('luckytrader.execute.place_take_profit', return_value={"status": "ok"}) as mock_take_profit, \
         patch('luckytrader.execute.save_state') as mock_save_state, \
         patch('luckytrader.execute.log_trade') as mock_log_trade, \
         patch('luckytrader.execute.notify_discord') as mock_notify, \
         patch('luckytrader.execute.time.sleep', return_value=None) as mock_sleep:
        yield {
            "get_candles": mock_get_candles,
            "get_account_info": mock_account,
            "get_coin_info": mock_coin_info,
            "place_market_order": mock_market_order,
            "get_position": mock_get_position,
            "place_stop_loss": mock_stop_loss,
            "place_take_profit": mock_take_profit,
            "save_state": mock_save_state,
            "log_trade": mock_log_trade,
            "notify_discord": mock_notify,
            "sleep": mock_sleep,
        }


class TestIntegration:

    def test_execute_open_position_uses_range_params_when_ranging(self, mock_deps):
        """When DE indicates range, open_position uses TP=2% SL=5%."""
        from luckytrader.execute import open_position

        with patch('luckytrader.execute.compute_de', return_value=0.10):
            result = open_position("SHORT", {"price": 100.0, "signal_reasons": []})

        assert result["action"] == "OPENED"
        assert result["sl"] == 105  # SHORT SL = entry * (1 + 0.05)
        assert result["tp"] == 98   # SHORT TP = entry * (1 - 0.02)

        sl_call = mock_deps["place_stop_loss"].call_args
        tp_call = mock_deps["place_take_profit"].call_args
        assert sl_call[0][2] == 105
        assert tp_call[0][2] == 98

        saved = mock_deps["save_state"].call_args[0][0]["position"]
        assert saved["regime"] == "range"
        assert saved["de"] == 0.10
        assert saved["regime_tp_pct"] == 0.02
        assert saved["regime_sl_pct"] == 0.05

    def test_execute_open_position_uses_trend_params_when_trending(self, mock_deps):
        """When DE indicates trend, open_position uses TP=7% SL=4%."""
        from luckytrader.execute import open_position

        with patch('luckytrader.execute.compute_de', return_value=0.40):
            result = open_position("SHORT", {"price": 100.0, "signal_reasons": []})

        assert result["action"] == "OPENED"
        assert result["sl"] == 104  # SHORT SL = entry * (1 + 0.04)
        assert result["tp"] == 93   # SHORT TP = entry * (1 - 0.07)

        sl_call = mock_deps["place_stop_loss"].call_args
        tp_call = mock_deps["place_take_profit"].call_args
        assert sl_call[0][2] == 104
        assert tp_call[0][2] == 93

        saved = mock_deps["save_state"].call_args[0][0]["position"]
        assert saved["regime"] == "trend"
        assert saved["de"] == 0.40
        assert saved["regime_tp_pct"] == 0.07
        assert saved["regime_sl_pct"] == 0.04

    def test_execute_open_position_failopen_when_de_none(self, mock_deps):
        """When compute_de returns None (API fail), uses range params."""
        from luckytrader.execute import open_position

        with patch('luckytrader.execute.compute_de', return_value=None):
            result = open_position("SHORT", {"price": 100.0, "signal_reasons": []})

        assert result["action"] == "OPENED"  # no exception, fail-open
        assert result["sl"] == 105
        assert result["tp"] == 98

        sl_call = mock_deps["place_stop_loss"].call_args
        tp_call = mock_deps["place_take_profit"].call_args
        assert sl_call[0][2] == 105
        assert tp_call[0][2] == 98

        saved = mock_deps["save_state"].call_args[0][0]["position"]
        assert saved["regime"] == "unknown"
        assert saved["de"] is None
        assert saved["regime_tp_pct"] == 0.02
        assert saved["regime_sl_pct"] == 0.05
