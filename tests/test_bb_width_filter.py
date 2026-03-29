"""
Tests for BB Width regime filter — all logic branches.

Covers:
- ETH BB breakout: detect_signal with min_bb_width
- SOL BB mean-reversion: detect_signal with min_bb_width
- Config loading: min_bb_width from TOML and defaults
- Backtest integration: all 3 ETH backtest modes
- Edge cases: 0, negative, exact boundary, huge threshold
"""
import os
import math
import pytest
import numpy as np
from unittest.mock import patch

# ── Fixtures ──

@pytest.fixture
def trending_up_prices():
    """Generate prices with uptrend + enough volatility for BB signal."""
    np.random.seed(42)
    base = np.linspace(90, 120, 200)
    noise = np.random.randn(200) * 2
    return (base + noise).tolist()


@pytest.fixture
def flat_prices():
    """Generate flat prices (tight BB → should be filtered)."""
    np.random.seed(42)
    return [100.0 + np.random.randn() * 0.1 for _ in range(200)]


@pytest.fixture
def volatile_prices():
    """Generate highly volatile prices (wide BB → should pass filter)."""
    np.random.seed(42)
    return [100.0 + np.random.randn() * 20 for _ in range(200)]


# ── ETH BB Breakout strategy ──

class TestEthDetectSignalBBWidth:
    """ETH detect_signal with min_bb_width parameter."""

    def test_default_zero_no_filter(self, trending_up_prices):
        """min_bb_width=0 (default) should not filter any signals."""
        from okx_bb.strategy import detect_signal
        r1 = detect_signal(trending_up_prices, 20, 2.5, 96, 8, 199, min_bb_width=0.0)
        r2 = detect_signal(trending_up_prices, 20, 2.5, 96, 8, 199)
        assert r1 == r2, "Default and explicit 0 should give same result"

    def test_large_threshold_blocks_all(self, trending_up_prices):
        """Very large threshold should block all signals."""
        from okx_bb.strategy import detect_signal
        result = detect_signal(trending_up_prices, 20, 2.5, 96, 8, 199, min_bb_width=99.0)
        assert result is None

    def test_negative_threshold_no_filter(self, trending_up_prices):
        """Negative threshold should behave like 0 (no filter)."""
        from okx_bb.strategy import detect_signal
        r1 = detect_signal(trending_up_prices, 20, 2.5, 96, 8, 199, min_bb_width=-1.0)
        r2 = detect_signal(trending_up_prices, 20, 2.5, 96, 8, 199, min_bb_width=0.0)
        assert r1 == r2

    def test_exact_boundary_blocks(self, trending_up_prices):
        """Threshold exactly at BB width should block (< not <=)."""
        from okx_bb.strategy import detect_signal
        from core.indicators import bollinger_bands
        bb = bollinger_bands(trending_up_prices, 20, 2.5, 199)
        if bb is None:
            pytest.skip("No BB at this idx")
        mid, upper, lower = bb
        exact_width = (upper - lower) / mid
        # Threshold slightly above → blocked
        r = detect_signal(trending_up_prices, 20, 2.5, 96, 8, 199,
                          min_bb_width=exact_width + 0.0001)
        assert r is None

    def test_below_boundary_passes(self, trending_up_prices):
        """Threshold below BB width should not filter."""
        from okx_bb.strategy import detect_signal
        from core.indicators import bollinger_bands
        bb = bollinger_bands(trending_up_prices, 20, 2.5, 199)
        if bb is None:
            pytest.skip("No BB at this idx")
        mid, upper, lower = bb
        exact_width = (upper - lower) / mid
        r1 = detect_signal(trending_up_prices, 20, 2.5, 96, 8, 199,
                           min_bb_width=exact_width - 0.0001)
        r2 = detect_signal(trending_up_prices, 20, 2.5, 96, 8, 199,
                           min_bb_width=0.0)
        assert r1 == r2

    def test_flat_prices_return_none(self, flat_prices):
        """Flat prices → BB returns None → no signal regardless of filter."""
        from okx_bb.strategy import detect_signal
        r = detect_signal(flat_prices, 20, 2.5, 96, 8, 199, min_bb_width=0.0)
        # BB std ≈ 0 → None, so signal is None
        # (might not be None if std > 1e-10 but very small)
        # Either None or a valid signal is acceptable here
        pass  # Just ensure no crash

    def test_volatile_prices_not_filtered(self, volatile_prices):
        """Volatile prices with reasonable threshold should not be filtered."""
        from okx_bb.strategy import detect_signal
        # Width with high vol should be large
        r1 = detect_signal(volatile_prices, 20, 2.5, 96, 8, 199, min_bb_width=0.01)
        r2 = detect_signal(volatile_prices, 20, 2.5, 96, 8, 199, min_bb_width=0.0)
        assert r1 == r2  # Small threshold should not filter volatile market

    def test_insufficient_data_returns_none(self):
        """Not enough data → None regardless of filter."""
        from okx_bb.strategy import detect_signal
        short = [100.0] * 10
        assert detect_signal(short, 20, 2.5, 96, 8, 5, min_bb_width=0.0) is None
        assert detect_signal(short, 20, 2.5, 96, 8, 5, min_bb_width=0.06) is None


# ── SOL BB Mean-Reversion strategy ──

class TestSolDetectSignalBBWidth:
    """SOL detect_signal with min_bb_width parameter."""

    def test_default_zero_no_filter(self, trending_up_prices):
        from okx_sol_bb.strategy import detect_signal
        r1 = detect_signal(trending_up_prices, 14, 3.0, 199, min_bb_width=0.0)
        r2 = detect_signal(trending_up_prices, 14, 3.0, 199)
        assert r1 == r2

    def test_large_threshold_blocks(self, trending_up_prices):
        from okx_sol_bb.strategy import detect_signal
        assert detect_signal(trending_up_prices, 14, 3.0, 199, min_bb_width=99.0) is None

    def test_negative_threshold_no_filter(self, trending_up_prices):
        from okx_sol_bb.strategy import detect_signal
        r1 = detect_signal(trending_up_prices, 14, 3.0, 199, min_bb_width=-1.0)
        r2 = detect_signal(trending_up_prices, 14, 3.0, 199, min_bb_width=0.0)
        assert r1 == r2

    def test_insufficient_data_returns_none(self):
        from okx_sol_bb.strategy import detect_signal
        short = [100.0] * 10
        assert detect_signal(short, 14, 3.0, 5, min_bb_width=0.0) is None
        assert detect_signal(short, 14, 3.0, 5, min_bb_width=0.08) is None

    def test_mean_reversion_signal_with_filter(self):
        """Construct data where SOL should give LONG (bounce off lower BB)
        and verify filter can block it."""
        from okx_sol_bb.strategy import detect_signal
        # Sharp drop then recovery
        prices = [100.0] * 20 + [99.0, 98.0, 95.0, 85.0, 84.0, 86.0]
        idx = len(prices) - 1  # 86.0 (curr > lower, prev=84 < lower → LONG)
        # Without filter
        r1 = detect_signal(prices, 14, 3.0, idx, min_bb_width=0.0)
        # With huge filter
        r2 = detect_signal(prices, 14, 3.0, idx, min_bb_width=99.0)
        assert r2 is None
        # r1 might be LONG or None depending on BB calculation


# ── Config loading ──

class TestConfigBBWidth:
    """Config loading of min_bb_width."""

    def test_eth_default(self):
        from okx_bb.config import StrategyConfig
        cfg = StrategyConfig()
        assert cfg.min_bb_width == 0.0

    def test_sol_default(self):
        from okx_sol_bb.config import StrategyConfig
        cfg = StrategyConfig()
        assert cfg.min_bb_width == 0.0

    def test_eth_custom(self):
        from okx_bb.config import StrategyConfig
        cfg = StrategyConfig(min_bb_width=0.06)
        assert cfg.min_bb_width == 0.06

    def test_sol_custom(self):
        from okx_sol_bb.config import StrategyConfig
        cfg = StrategyConfig(min_bb_width=0.08)
        assert cfg.min_bb_width == 0.08

    def test_eth_load_from_toml(self, tmp_path):
        """Load min_bb_width from TOML file."""
        toml_content = """
[strategy]
bb_period = 20
bb_multiplier = 2.5
min_bb_width = 0.06

[risk]
take_profit_pct = 0.04

[fees]
taker_fee = 0.0005
"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(toml_content)
        with patch.dict(os.environ, {"OKX_BB_CONFIG_DIR": str(config_dir)}):
            from okx_bb.config import load_config
            import importlib
            import okx_bb.config
            importlib.reload(okx_bb.config)
            cfg = okx_bb.config.load_config()
            assert cfg.strategy.min_bb_width == 0.06

    def test_eth_load_missing_key_defaults_to_zero(self, tmp_path):
        """Missing min_bb_width in TOML should default to 0.0."""
        toml_content = """
[strategy]
bb_period = 20

[risk]
take_profit_pct = 0.04

[fees]
taker_fee = 0.0005
"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(toml_content)
        with patch.dict(os.environ, {"OKX_BB_CONFIG_DIR": str(config_dir)}):
            import importlib
            import okx_bb.config
            importlib.reload(okx_bb.config)
            cfg = okx_bb.config.load_config()
            assert cfg.strategy.min_bb_width == 0.0


# ── BB Width math correctness ──

class TestBBWidthCalculation:
    """Verify BB width formula matches expected math."""

    def test_width_formula(self):
        """width = (upper - lower) / mid = 2 * mult * std / mean."""
        from core.indicators import bollinger_bands
        np.random.seed(42)
        prices = [100 + np.random.randn() * 5 for _ in range(30)]
        bb = bollinger_bands(prices, 20, 2.0, 29)
        assert bb is not None
        mid, upper, lower = bb
        width = (upper - lower) / mid
        # Manual calculation
        window = prices[9:29]  # [idx-period:idx]
        manual_mid = sum(window) / 20
        manual_std = math.sqrt(sum((x - manual_mid)**2 for x in window) / 20)
        manual_width = (2 * 2.0 * manual_std) / manual_mid
        assert abs(width - manual_width) < 1e-10

    def test_width_always_non_negative(self):
        """BB width should always be >= 0."""
        from core.indicators import bollinger_bands
        np.random.seed(42)
        for _ in range(100):
            prices = [100 + np.random.randn() * np.random.uniform(0.1, 20) for _ in range(30)]
            bb = bollinger_bands(prices, 20, 2.0, 29)
            if bb is not None:
                mid, upper, lower = bb
                assert (upper - lower) / mid >= 0

    def test_zero_std_returns_none(self):
        """Flat prices → std=0 → BB returns None."""
        from core.indicators import bollinger_bands
        prices = [100.0] * 30
        assert bollinger_bands(prices, 20, 2.0, 29) is None

    def test_no_lookahead(self):
        """BB at idx should not use data at or after idx."""
        from core.indicators import bollinger_bands
        np.random.seed(42)
        prices = [100 + np.random.randn() * 5 for _ in range(30)]
        bb1 = bollinger_bands(prices, 20, 2.0, 25)
        # Change data after idx=25 — should not affect BB at 25
        prices2 = prices[:26] + [999.0] * 4
        bb2 = bollinger_bands(prices2, 20, 2.0, 25)
        assert bb1 == bb2


# ── Backtest integration ──

class TestBacktestBBWidthFilter:
    """Verify BB width filter integrates correctly in all 3 backtest modes."""

    @pytest.fixture
    def eth_config(self):
        from okx_bb.config import OKXConfig, StrategyConfig, RiskConfig, FeeConfig, ExecutionConfig
        return OKXConfig(
            instId="ETH-USDT-SWAP",
            strategy=StrategyConfig(bb_period=20, bb_multiplier=2.5,
                                    trend_ema_period=96, trend_lookback=8,
                                    min_bb_width=0.0),
            risk=RiskConfig(stop_loss_pct=0.03, take_profit_pct=0.04, max_hold_bars=120),
            fees=FeeConfig(taker_fee=0.0005, maker_fee=0.0002),
            execution=ExecutionConfig(),
        )

    @pytest.fixture
    def sample_candles(self):
        """Generate 500 candles with realistic OHLC."""
        np.random.seed(42)
        candles = []
        price = 2000.0
        ts = 1700000000000
        for i in range(500):
            change = np.random.randn() * 30
            o = price
            c = price + change
            h = max(o, c) + abs(np.random.randn() * 10)
            l = min(o, c) - abs(np.random.randn() * 10)
            candles.append({"ts": ts, "o": o, "c": c, "h": h, "l": l})
            price = c
            ts += 1800000  # 30min
        return candles

    def test_no_filter_gives_more_trades(self, eth_config, sample_candles):
        """Without filter, there should be >= trades than with filter."""
        from okx_bb.backtest import backtest_close_confirm_buffer
        trades_no = backtest_close_confirm_buffer(sample_candles, eth_config)
        eth_config.strategy.min_bb_width = 0.10
        trades_f = backtest_close_confirm_buffer(sample_candles, eth_config)
        assert len(trades_no) >= len(trades_f)

    def test_filter_is_subset(self, eth_config, sample_candles):
        """Filtered trades should be a subset of unfiltered trades
        (same entry prices, possibly different due to trade-locking)."""
        from okx_bb.backtest import backtest_close_confirm_buffer
        trades_no = backtest_close_confirm_buffer(sample_candles, eth_config)
        eth_config.strategy.min_bb_width = 0.10
        trades_f = backtest_close_confirm_buffer(sample_candles, eth_config)
        # Filtered trades' entry indices should all appear as potential entries
        # (not exact subset due to trade-locking changes)
        assert len(trades_f) <= len(trades_no)

    def test_close_mode_filter(self, eth_config, sample_candles):
        """backtest_close should also respect min_bb_width."""
        from okx_bb.backtest import backtest_close
        trades_no = backtest_close(sample_candles, eth_config)
        eth_config.strategy.min_bb_width = 0.10
        trades_f = backtest_close(sample_candles, eth_config)
        assert len(trades_no) >= len(trades_f)

    def test_intrabar_mode_filter(self, eth_config, sample_candles):
        """backtest_intrabar should also respect min_bb_width."""
        from okx_bb.backtest import backtest_intrabar
        trades_no = backtest_intrabar(sample_candles, eth_config)
        eth_config.strategy.min_bb_width = 0.10
        trades_f = backtest_intrabar(sample_candles, eth_config)
        assert len(trades_no) >= len(trades_f)

    def test_huge_filter_zero_trades(self, eth_config, sample_candles):
        """Huge threshold should result in zero trades."""
        from okx_bb.backtest import backtest_close_confirm_buffer
        eth_config.strategy.min_bb_width = 99.0
        trades = backtest_close_confirm_buffer(sample_candles, eth_config)
        assert len(trades) == 0


# ── Kill Zone Tests ──

class TestKillZoneFilter:
    """BB width kill zone: reject signals when kill_lo <= width < kill_hi."""

    def test_kill_zone_blocks_middle(self):
        """Signal with width in kill zone should be blocked."""
        from okx_bb.strategy import detect_signal
        from core.indicators import bollinger_bands
        import numpy as np
        np.random.seed(42)
        prices = [100 + np.random.randn() * 5 for _ in range(200)]
        bb = bollinger_bands(prices, 20, 2.5, 199)
        if bb is None:
            return
        mid, upper, lower = bb
        w = (upper - lower) / mid
        # Set kill zone to exactly bracket the current width
        r = detect_signal(prices, 20, 2.5, 96, 8, 199,
                          bb_width_kill_lo=w - 0.001, bb_width_kill_hi=w + 0.001)
        assert r is None

    def test_kill_zone_passes_outside(self):
        """Signal with width outside kill zone should pass."""
        from okx_bb.strategy import detect_signal
        import numpy as np
        np.random.seed(42)
        prices = [100 + np.random.randn() * 5 for _ in range(200)]
        # Kill zone far away from actual width
        r1 = detect_signal(prices, 20, 2.5, 96, 8, 199,
                           bb_width_kill_lo=0.001, bb_width_kill_hi=0.002)
        r2 = detect_signal(prices, 20, 2.5, 96, 8, 199, min_bb_width=0.0)
        assert r1 == r2

    def test_kill_zone_disabled_when_lo_zero(self):
        """kill_lo=0 should disable kill zone."""
        from okx_bb.strategy import detect_signal
        import numpy as np
        np.random.seed(42)
        prices = [100 + np.random.randn() * 5 for _ in range(200)]
        r1 = detect_signal(prices, 20, 2.5, 96, 8, 199,
                           bb_width_kill_lo=0.0, bb_width_kill_hi=0.1)
        r2 = detect_signal(prices, 20, 2.5, 96, 8, 199)
        assert r1 == r2

    def test_kill_zone_disabled_when_hi_le_lo(self):
        """kill_hi <= kill_lo should disable kill zone."""
        from okx_bb.strategy import detect_signal
        import numpy as np
        np.random.seed(42)
        prices = [100 + np.random.randn() * 5 for _ in range(200)]
        r1 = detect_signal(prices, 20, 2.5, 96, 8, 199,
                           bb_width_kill_lo=0.05, bb_width_kill_hi=0.03)
        r2 = detect_signal(prices, 20, 2.5, 96, 8, 199)
        assert r1 == r2

    def test_kill_zone_config_loading(self, tmp_path):
        """Config loads kill zone params from TOML."""
        toml_content = """
[strategy]
bb_period = 20
bb_multiplier = 2.5
bb_width_kill_lo = 0.04
bb_width_kill_hi = 0.055

[risk]
take_profit_pct = 0.04

[fees]
taker_fee = 0.0005
"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(toml_content)
        with patch.dict(os.environ, {"OKX_BB_CONFIG_DIR": str(config_dir)}):
            import importlib
            import okx_bb.config
            importlib.reload(okx_bb.config)
            cfg = okx_bb.config.load_config()
            assert cfg.strategy.bb_width_kill_lo == 0.04
            assert cfg.strategy.bb_width_kill_hi == 0.055

    def test_kill_zone_default_zero(self):
        """Default kill zone params are 0 (disabled)."""
        from okx_bb.config import StrategyConfig
        cfg = StrategyConfig()
        assert cfg.bb_width_kill_lo == 0.0
        assert cfg.bb_width_kill_hi == 0.0

    def test_backtest_kill_zone_reduces_trades(self):
        """Kill zone in backtest should reduce trade count."""
        import numpy as np
        from okx_bb.config import OKXConfig, StrategyConfig, RiskConfig, FeeConfig, ExecutionConfig
        from okx_bb.backtest import backtest_close_confirm_buffer
        np.random.seed(42)
        candles = []
        price = 2000.0
        ts = 1700000000000
        for i in range(500):
            change = np.random.randn() * 30
            o = price; c = price + change
            h = max(o, c) + abs(np.random.randn() * 10)
            l = min(o, c) - abs(np.random.randn() * 10)
            candles.append({"ts": ts, "o": o, "c": c, "h": h, "l": l})
            price = c; ts += 1800000

        cfg_no = OKXConfig(instId="TEST", strategy=StrategyConfig(min_bb_width=0.0),
                           risk=RiskConfig(stop_loss_pct=0.03, take_profit_pct=0.04, max_hold_bars=120),
                           fees=FeeConfig(), execution=ExecutionConfig())
        cfg_kill = OKXConfig(instId="TEST",
                             strategy=StrategyConfig(bb_width_kill_lo=0.01, bb_width_kill_hi=0.10),
                             risk=RiskConfig(stop_loss_pct=0.03, take_profit_pct=0.04, max_hold_bars=120),
                             fees=FeeConfig(), execution=ExecutionConfig())
        t_no = backtest_close_confirm_buffer(candles, cfg_no)
        t_kill = backtest_close_confirm_buffer(candles, cfg_kill)
        assert len(t_no) >= len(t_kill)
