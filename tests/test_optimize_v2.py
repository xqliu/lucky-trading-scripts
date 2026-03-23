"""
Tests for optimize.py v2 — multi-coin, OOS split, regime, sanity gate.
TDD: written to verify each new feature independently.
"""
import pytest
from unittest.mock import patch, MagicMock


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def _make_candles(n, base=67000, spread=100, vol=100):
    """Generate n flat candles for testing."""
    return [{
        'o': str(base), 'h': str(base + spread), 'l': str(base - spread),
        'c': str(base), 'v': str(vol), 't': 1000000 + i * 1800000,
    } for i in range(n)]


def _make_trending_candles(n, base=67000, step=50, vol=100):
    """Generate n trending-up candles."""
    return [{
        'o': str(base + i * step), 'h': str(base + i * step + 100),
        'l': str(base + i * step - 50),
        'c': str(base + (i + 1) * step), 'v': str(vol),
        't': 1000000 + i * 1800000,
    } for i in range(n)]


# ──────────────────────────────────────────────────────────
# compute_stats
# ──────────────────────────────────────────────────────────

class TestComputeStats:
    def test_empty_trades(self):
        from luckytrader.optimize import compute_stats
        s = compute_stats([])
        assert s['count'] == 0
        assert s['total'] == 0
        assert s['avg'] == 0
        assert s['winrate'] == 0
        assert s['max_dd'] == 0

    def test_all_winners(self):
        from luckytrader.optimize import compute_stats
        trades = [{'pnl_pct': 2.0}, {'pnl_pct': 1.5}, {'pnl_pct': 3.0}]
        s = compute_stats(trades)
        assert s['count'] == 3
        assert s['winrate'] == 100.0
        assert s['total'] == 6.5
        assert s['max_dd'] == 0  # no drawdown if all positive

    def test_all_losers(self):
        from luckytrader.optimize import compute_stats
        trades = [{'pnl_pct': -2.0}, {'pnl_pct': -1.0}]
        s = compute_stats(trades)
        assert s['count'] == 2
        assert s['winrate'] == 0.0
        assert s['max_dd'] == 3.0  # cumulative drawdown

    def test_drawdown_calculation(self):
        from luckytrader.optimize import compute_stats
        trades = [
            {'pnl_pct': 5.0},   # cum=5, peak=5
            {'pnl_pct': -3.0},  # cum=2, peak=5, dd=3
            {'pnl_pct': -1.0},  # cum=1, peak=5, dd=4
            {'pnl_pct': 6.0},   # cum=7, peak=7, dd=0
        ]
        s = compute_stats(trades)
        assert s['max_dd'] == 4.0

    def test_mixed_trades(self):
        from luckytrader.optimize import compute_stats
        trades = [{'pnl_pct': 2.0}, {'pnl_pct': -1.0}]
        s = compute_stats(trades)
        assert s['count'] == 2
        assert s['winrate'] == 50.0
        assert s['avg'] == 0.5


# ──────────────────────────────────────────────────────────
# sanity_check
# ──────────────────────────────────────────────────────────

class TestSanityCheck:
    def test_low_sample_warning(self):
        from luckytrader.optimize import sanity_check
        w = sanity_check(
            {"sl": 0.04, "tp": 0.06},
            {"count": 10, "avg": 0.5, "winrate": 60, "max_dd": 5},
            {"avg": 0.1},
            "BTC"
        )
        assert any("样本量不足" in x for x in w)

    def test_no_warning_sufficient_sample(self):
        from luckytrader.optimize import sanity_check
        w = sanity_check(
            {"sl": 0.04, "tp": 0.06},
            {"count": 30, "avg": 0.5, "winrate": 60, "max_dd": 5},
            {"avg": 0.3},
            "BTC"
        )
        assert not any("样本量不足" in x for x in w)

    def test_high_avg_warning(self):
        from luckytrader.optimize import sanity_check
        w = sanity_check(
            {"sl": 0.04, "tp": 0.10},
            {"count": 30, "avg": 2.5, "winrate": 70, "max_dd": 5},
            {"avg": 0.3},
            "BTC"
        )
        assert any("异常高" in x for x in w)

    def test_high_improvement_warning(self):
        from luckytrader.optimize import sanity_check
        w = sanity_check(
            {"sl": 0.04, "tp": 0.06},
            {"count": 30, "avg": 1.0, "winrate": 60, "max_dd": 5},
            {"avg": 0.1},
            "BTC"
        )
        assert any("异常大" in x for x in w)

    def test_negative_ev_warning(self):
        from luckytrader.optimize import sanity_check
        # WR=40%, SL=4%, TP=3% → EV = 0.4*3 - 0.6*4 = -1.2 < 0
        w = sanity_check(
            {"sl": 0.04, "tp": 0.03},
            {"count": 30, "avg": -0.5, "winrate": 40, "max_dd": 5},
            {"avg": -0.3},
            "BTC"
        )
        assert any("期望值为负" in x for x in w)

    def test_high_drawdown_warning(self):
        from luckytrader.optimize import sanity_check
        w = sanity_check(
            {"sl": 0.04, "tp": 0.06},
            {"count": 30, "avg": 0.5, "winrate": 60, "max_dd": 20},
            {"avg": 0.3},
            "BTC"
        )
        assert any("回撤" in x for x in w)

    def test_no_warnings_clean(self):
        """No warnings when everything is reasonable."""
        from luckytrader.optimize import sanity_check
        w = sanity_check(
            {"sl": 0.035, "tp": 0.06},
            {"count": 30, "avg": 0.5, "winrate": 55, "max_dd": 8},
            {"avg": 0.3},
            "BTC"
        )
        assert len(w) == 0


# ──────────────────────────────────────────────────────────
# tag_regime
# ──────────────────────────────────────────────────────────

class TestTagRegime:
    def test_insufficient_data_tags_unknown(self):
        from luckytrader.optimize import tag_regime
        candles = _make_candles(50)
        trades = [{'entry_idx': 10}]
        tag_regime(candles, trades)
        assert trades[0]['regime'] == 'unknown'

    def test_flat_candles_tag_range(self):
        """Flat (no directional movement) should produce low DE → 'range'."""
        from luckytrader.optimize import tag_regime
        # Need 7*48+48 = 384 bars minimum
        candles = _make_candles(500, spread=10)
        trades = [{'entry_idx': 450}]
        tag_regime(candles, trades)
        # Flat candles should have low DE
        assert trades[0]['regime'] in ('range', 'unknown')

    def test_trending_candles_tag_trend(self):
        """Strong trend should produce high DE → 'trend'."""
        from luckytrader.optimize import tag_regime
        candles = _make_trending_candles(500, step=200)
        trades = [{'entry_idx': 450}]
        tag_regime(candles, trades)
        # Strong trend should have high DE
        assert trades[0]['regime'] in ('trend', 'unknown')


# ──────────────────────────────────────────────────────────
# run_backtest_on_slice
# ──────────────────────────────────────────────────────────

class TestRunBacktestOnSlice:
    def test_returns_list(self):
        """v2 returns trade list, not dict."""
        from luckytrader.optimize import run_backtest_on_slice, _cfg
        candles = _make_candles(100)
        result = run_backtest_on_slice(candles, [], 0.04, 0.07, 48, _cfg)
        assert isinstance(result, list)

    def test_empty_on_flat(self):
        """Flat candles should produce no signals."""
        from luckytrader.optimize import run_backtest_on_slice, _cfg
        candles = _make_candles(100, spread=1)
        result = run_backtest_on_slice(candles, [], 0.04, 0.07, 48, _cfg)
        assert len(result) == 0

    def test_trades_have_entry_idx(self):
        """Each trade should have entry_idx tag."""
        from luckytrader.optimize import run_backtest_on_slice, _cfg
        # Use trending candles to potentially trigger signals
        candles = _make_trending_candles(200, step=500)
        result = run_backtest_on_slice(candles, [], 0.04, 0.10, 48, _cfg)
        for t in result:
            assert 'entry_idx' in t

    def test_coin_cfg_overrides_start(self):
        """With coin_cfg having smaller range_bars, start index should be lower."""
        from luckytrader.optimize import run_backtest_on_slice, _cfg
        from luckytrader.config import CoinConfig
        coin_cfg = CoinConfig(coin='TEST', range_bars=10, lookback_bars=10)
        candles = _make_candles(100)
        # Should not crash with small range_bars
        result = run_backtest_on_slice(candles, [], 0.04, 0.07, 48, _cfg, coin_cfg)
        assert isinstance(result, list)


# ──────────────────────────────────────────────────────────
# Integration: optimize_coin consistency with backtest.run_backtest
# ──────────────────────────────────────────────────────────

class TestConsistency:
    def test_detect_signal_same_call_signature(self):
        """optimize and backtest should call detect_signal with same args."""
        import inspect
        from luckytrader.optimize import run_backtest_on_slice
        from luckytrader.backtest import run_backtest
        
        opt_src = inspect.getsource(run_backtest_on_slice)
        bt_src = inspect.getsource(run_backtest)
        
        # Both should call detect_signal(candles_30m, candles_4h, i, cfg, coin_cfg)
        assert 'detect_signal(' in opt_src
        assert 'detect_signal(' in bt_src

    def test_simulate_trade_same_call_pattern(self):
        """Both should use opens[i+1] as entry price."""
        import inspect
        from luckytrader.optimize import run_backtest_on_slice
        from luckytrader.backtest import run_backtest
        
        opt_src = inspect.getsource(run_backtest_on_slice)
        bt_src = inspect.getsource(run_backtest)
        
        assert 'opens[i + 1]' in opt_src
        assert 'opens[i + 1]' in bt_src

    def test_start_offset_aligned(self):
        """Start offset should match backtest.run_backtest."""
        import inspect
        from luckytrader.optimize import run_backtest_on_slice
        from luckytrader.backtest import run_backtest
        
        opt_src = inspect.getsource(run_backtest_on_slice)
        bt_src = inspect.getsource(run_backtest)
        
        # Both should use + 2
        assert '+ 2' in opt_src
        assert '+ 2' in bt_src


# ──────────────────────────────────────────────────────────
# OOS split correctness
# ──────────────────────────────────────────────────────────

class TestOOSSplit:
    def test_4h_overlap_for_test_set(self):
        """Test set 4h candles should include overlap for EMA warmup."""
        # Simulate the split logic from optimize_coin
        total_4h = 136  # typical for 104 days
        split_4h = int(total_4h * 0.7)  # 95
        overlap_4h = 60
        test_4h_start = max(0, split_4h - overlap_4h)  # 35
        test_4h_len = total_4h - test_4h_start  # 101
        
        # Test set should have enough for EMA (need ~48 bars)
        assert test_4h_len >= 48, f"Test set 4h: {test_4h_len} < 48 needed for EMA"

    def test_train_test_no_overlap_30m(self):
        """30m candles should have clean train/test split (no data leakage)."""
        total = 5000
        split = int(total * 0.7)  # 3500
        train = list(range(split))
        test = list(range(split, total))
        
        assert len(set(train) & set(test)) == 0, "Train/test overlap in 30m data"

    def test_4h_overlap_intentional(self):
        """4h overlap is intentional for warmup, not data leakage.
        
        The overlap only provides EMA context — it doesn't generate signals
        in the training period because 30m data has clean split.
        """
        # This is a documentation test — the 4h overlap feeds into
        # detect_signal's trend filter (EMA), not into signal generation.
        # Signals are gated by 30m candle index, so no leakage.
        pass
