"""Additional execute.py tests for branch coverage."""
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from pathlib import Path


class TestMigrateState:
    def test_old_format_with_position(self, mock_hl):
        from luckytrader.execute import _migrate_state
        old = {"position": {"coin": "BTC", "size": 0.001}}
        migrated = _migrate_state(old)
        assert "BTC" in migrated
        assert migrated["BTC"]["position"]["coin"] == "BTC"

    def test_old_format_empty(self, mock_hl):
        from luckytrader.execute import _migrate_state
        old = {"position": None}
        migrated = _migrate_state(old)
        # Should create per-coin entries
        assert all(c in migrated for c in ("BTC", "ETH"))

    def test_new_format_untouched(self, mock_hl):
        from luckytrader.execute import _migrate_state, TRADING_COINS
        new = {c: {"position": None} for c in TRADING_COINS}
        result = _migrate_state(new)
        assert result is new


class TestLoadSaveState:
    def test_load_corrupt_file(self, mock_hl, tmp_path):
        from luckytrader.execute import load_state, STATE_FILE
        with patch('luckytrader.execute.STATE_FILE', tmp_path / "broken.json"):
            (tmp_path / "broken.json").write_text("{invalid json")
            state = load_state("BTC")
            assert state == {"position": None}

    def test_save_old_format_position_key(self, mock_hl, tmp_path):
        from luckytrader.execute import save_state, load_state
        with patch('luckytrader.execute.STATE_FILE', tmp_path / "state.json"):
            save_state({"position": {"coin": "ETH"}})
            # Old format triggers migration internally
            state = load_state("ETH")
            assert state.get("position") is not None

    def test_save_with_coin(self, mock_hl, tmp_path):
        from luckytrader.execute import save_state, load_state
        with patch('luckytrader.execute.STATE_FILE', tmp_path / "state.json"):
            save_state({"position": {"coin": "BTC", "entry_price": 67000}}, coin="BTC")
            state = load_state("BTC")
            assert state["position"]["entry_price"] == 67000


class TestCooldown:
    def test_cooldown_blocks_reopening(self, mock_hl, tmp_path):
        import time
        from luckytrader.execute import _check_cooldown, _set_cooldown, _cooldown_file
        with patch('luckytrader.execute.STATE_FILE', tmp_path / "state.json"):
            with patch('luckytrader.execute._cooldown_file', return_value=tmp_path / ".last_open_ts_BTC"):
                _set_cooldown.__wrapped__ if hasattr(_set_cooldown, '__wrapped__') else None
                # Write recent cooldown timestamp
                (tmp_path / ".last_open_ts_BTC").write_text(str(time.time()))
                assert _check_cooldown("BTC") is False

    def test_cooldown_expired(self, mock_hl, tmp_path):
        import time
        from luckytrader.execute import _check_cooldown
        with patch('luckytrader.execute._cooldown_file', return_value=tmp_path / ".last_open_ts_BTC"):
            (tmp_path / ".last_open_ts_BTC").write_text(str(time.time() - 3600))
            assert _check_cooldown("BTC") is True


class TestLocking:
    def test_acquire_release(self, mock_hl, tmp_path):
        from luckytrader.execute import _acquire_lock, _release_lock
        with patch('luckytrader.execute._LOCK_DIR', tmp_path):
            fd = _acquire_lock("BTC")
            assert fd is not None
            _release_lock(fd)

    def test_lock_contention(self, mock_hl, tmp_path):
        from luckytrader.execute import _acquire_lock, _release_lock
        import fcntl
        with patch('luckytrader.execute._LOCK_DIR', tmp_path):
            fd1 = _acquire_lock("BTC")
            assert fd1 is not None
            # Second attempt should fail
            fd2 = _acquire_lock("BTC")
            assert fd2 is None
            _release_lock(fd1)

    def test_release_none(self, mock_hl):
        from luckytrader.execute import _release_lock
        _release_lock(None)  # Should not crash


class TestExecuteCoin:
    def test_lock_held_skips(self, mock_hl, tmp_path):
        from luckytrader.execute import execute_coin
        with patch('luckytrader.execute._acquire_lock', return_value=None):
            result = execute_coin("BTC", dry_run=True)
        assert result["action"] == "SKIPPED"


class TestDryRunOpen:
    def test_dry_run_long(self, mock_hl):
        from luckytrader.execute import dry_run_open, get_account_info, get_coin_info
        mock_hl.get_account_info.return_value = {"account_value": "200"}
        analysis = {"price": 67000}
        with patch('luckytrader.execute.get_candles', return_value=[{"c": "67000"}] * 30), \
             patch('luckytrader.execute.compute_de', return_value=0.05), \
             patch('luckytrader.execute.get_regime_params', return_value={
                 'sl_pct': 0.04, 'tp_pct': 0.07, 'regime': 'normal'
             }), \
             patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5}):
            result = dry_run_open("LONG", analysis, "BTC")
        assert result["action"] == "DRY_RUN_WOULD_OPEN"
        assert result["direction"] == "LONG"
        assert result["dry_run"] is True

    def test_dry_run_short(self, mock_hl):
        from luckytrader.execute import dry_run_open
        mock_hl.get_account_info.return_value = {"account_value": "200"}
        analysis = {"price": 67000}
        with patch('luckytrader.execute.get_candles', return_value=[{"c": "67000"}] * 30), \
             patch('luckytrader.execute.compute_de', side_effect=Exception("no data")), \
             patch('luckytrader.execute.get_regime_params', return_value={
                 'sl_pct': 0.04, 'tp_pct': 0.07, 'regime': 'normal'
             }), \
             patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5}):
            result = dry_run_open("SHORT", analysis, "BTC")
        assert result["action"] == "DRY_RUN_WOULD_OPEN"
        assert result["direction"] == "SHORT"


class TestCheckExistingOrders:
    def test_detects_sl_and_tp(self, mock_hl):
        from luckytrader.execute import check_existing_orders
        mock_hl.get_open_orders_detailed.return_value = [
            {"orderType": "Stop Market", "isTrigger": True},
            {"orderType": "Take Profit", "isTrigger": True},
        ]
        sl, tp = check_existing_orders("BTC")
        assert sl is True
        assert tp is True


class TestCheckSlTpOrders:
    def test_both_exist(self, mock_hl):
        from luckytrader.execute import check_sl_tp_orders
        mock_hl.get_open_orders_detailed.return_value = [
            {"orderType": "Stop Market", "isTrigger": True, "reduceOnly": True,
             "coin": "BTC", "side": "A", "triggerPx": "65000", "oid": 1},
            {"orderType": "Take Profit Market", "isTrigger": True, "reduceOnly": True,
             "coin": "BTC", "side": "A", "triggerPx": "72000", "oid": 2},
        ]
        position = {"direction": "LONG", "coin": "BTC"}
        sl, tp = check_sl_tp_orders("BTC", position)
        assert sl is True
        assert tp is True


class TestRecordTradeResult:
    def test_under_threshold_no_optimization(self, mock_hl, tmp_path):
        from luckytrader.execute import record_trade_result
        with patch('luckytrader.execute.TRADE_LOG_FILE', tmp_path / "trade_log.json"):
            record_trade_result(1.5, "LONG", "BTC", "TP")
            # Should not trigger optimization with just 1 trade


class TestTimeoutClose:
    def test_dry_run_timeout(self, mock_hl):
        """Cover dry_run + timeout branch."""
        from luckytrader.execute import _execute_inner
        from datetime import datetime, timezone
        mock_hl.get_account_info.return_value = {
            "account_value": "200", "withdrawable": "100", "positions": []
        }
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000, "unrealized_pnl": -50,
            "liquidation_price": 0,
        }
        state_data = {"position": {
            "coin": "BTC", "entry_time": (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat(),
            "entry_price": 67000,
        }}
        with patch('luckytrader.execute.get_position', return_value=position), \
             patch('luckytrader.execute.load_state', return_value=state_data):
            result = _execute_inner(dry_run=True, mode="🧪 DRY RUN",
                                   _CST=timezone(timedelta(hours=8)), coin="BTC")
        assert result["action"] == "DRY_RUN_WOULD_TIMEOUT_CLOSE"

    def test_close_returns_none_stale(self, mock_hl):
        """When close_position returns None → STALE_STATE_CLEANED."""
        from luckytrader.execute import _execute_inner
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000, "unrealized_pnl": -50,
            "liquidation_price": 0,
        }
        state_data = {"position": {
            "coin": "BTC", "entry_time": (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat(),
            "entry_price": 67000,
        }}
        with patch('luckytrader.execute.get_position', return_value=position), \
             patch('luckytrader.execute.load_state', return_value=state_data), \
             patch('luckytrader.execute.close_position', return_value=None):
            result = _execute_inner(dry_run=False, mode="🔴 LIVE",
                                   _CST=timezone(timedelta(hours=8)), coin="BTC")
        assert result["action"] == "STALE_STATE_CLEANED"

    def test_close_raises_runtime_error(self, mock_hl):
        """When close_position raises RuntimeError → CLOSE_FAILED."""
        from luckytrader.execute import _execute_inner
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000, "unrealized_pnl": -50,
            "liquidation_price": 0,
        }
        state_data = {"position": {
            "coin": "BTC", "entry_time": (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat(),
            "entry_price": 67000,
        }}
        with patch('luckytrader.execute.get_position', return_value=position), \
             patch('luckytrader.execute.load_state', return_value=state_data), \
             patch('luckytrader.execute.close_position', side_effect=RuntimeError("API failure")):
            result = _execute_inner(dry_run=False, mode="🔴 LIVE",
                                   _CST=timezone(timedelta(hours=8)), coin="BTC")
        assert result["action"] == "CLOSE_FAILED"


class TestSlTpTriggerBranches:
    """Cover the SL/TP trigger detection in _execute_inner when position gone."""

    def test_distance_fallback_sl(self, mock_hl):
        """Cover distance fallback when price is ambiguous."""
        from luckytrader.execute import _execute_inner
        # State has position but no live position (was closed)
        state_data = {"position": {
            "coin": "BTC", "direction": "LONG", "size": 0.001,
            "entry_price": 67000, "sl_price": 64000, "tp_price": 72000,
            "entry_time": datetime.now(timezone.utc).isoformat(),
        }}
        # Fill shows close at a price near SL
        mock_hl.get_market_price.return_value = 64500.0
        with patch('luckytrader.execute.get_position', return_value=None), \
             patch('luckytrader.execute.load_state', return_value=state_data), \
             patch('luckytrader.execute.get_recent_fills', return_value=[
                 {"coin": "BTC", "side": "SELL", "price": "64500"}
             ]), \
             patch('luckytrader.execute.save_state'), \
             patch('luckytrader.execute.log_trade'):
            result = _execute_inner(dry_run=False, mode="🔴 LIVE",
                                   _CST=timezone(timedelta(hours=8)), coin="BTC")
        assert result["action"] == "CLOSED_BY_TRIGGER"
        assert result["reason"] == "SL"

    def test_short_tp_trigger(self, mock_hl):
        """SHORT position closed by TP."""
        from luckytrader.execute import _execute_inner
        state_data = {"position": {
            "coin": "BTC", "direction": "SHORT", "size": 0.001,
            "entry_price": 67000, "sl_price": 70000, "tp_price": 62000,
            "entry_time": datetime.now(timezone.utc).isoformat(),
        }}
        mock_hl.get_market_price.return_value = 61800.0
        with patch('luckytrader.execute.get_position', return_value=None), \
             patch('luckytrader.execute.load_state', return_value=state_data), \
             patch('luckytrader.execute.get_recent_fills', return_value=[
                 {"coin": "BTC", "side": "BUY", "price": "61800"}
             ]), \
             patch('luckytrader.execute.save_state'), \
             patch('luckytrader.execute.log_trade'):
            result = _execute_inner(dry_run=False, mode="🔴 LIVE",
                                   _CST=timezone(timedelta(hours=8)), coin="BTC")
        assert result["action"] == "CLOSED_BY_TRIGGER"
        assert result["reason"] == "TP"


class TestReevalRegimeTp:
    def test_de_api_failure(self, mock_hl):
        from luckytrader.execute import reeval_regime_tp
        position = {"coin": "BTC", "entry_price": 67000, "size": 0.001,
                     "direction": "LONG", "regime_tp_pct": 0.07, "regime": "normal"}
        with patch('luckytrader.execute.Info', side_effect=Exception("API down")):
            result = reeval_regime_tp(position)
        assert result is None

    def test_de_none_skips(self, mock_hl):
        from luckytrader.execute import reeval_regime_tp
        position = {"coin": "BTC", "entry_price": 67000, "size": 0.001,
                     "direction": "LONG", "regime_tp_pct": 0.07, "regime": "normal"}
        mock_info = MagicMock()
        mock_info.candles_snapshot.return_value = []
        with patch('luckytrader.execute.Info', return_value=mock_info), \
             patch('luckytrader.execute.compute_de', return_value=None):
            result = reeval_regime_tp(position)
        assert result is None

    def test_no_tighten_needed(self, mock_hl):
        from luckytrader.execute import reeval_regime_tp
        position = {"coin": "BTC", "entry_price": 67000, "size": 0.001,
                     "direction": "LONG", "regime_tp_pct": 0.02, "regime": "range"}
        mock_info = MagicMock()
        mock_info.candles_snapshot.return_value = [{"c": "67000"}] * 15
        with patch('luckytrader.execute.Info', return_value=mock_info), \
             patch('luckytrader.execute.compute_de', return_value=0.03), \
             patch('luckytrader.execute.should_tighten_tp', return_value=None):
            result = reeval_regime_tp(position)
        assert result is None

    def test_tp_tighten_success(self, mock_hl):
        from luckytrader.execute import reeval_regime_tp
        position = {"coin": "BTC", "entry_price": 67000, "size": 0.001,
                     "direction": "LONG", "regime_tp_pct": 0.07, "regime": "normal"}
        mock_info = MagicMock()
        mock_info.candles_snapshot.return_value = [{"c": "67000"}] * 15
        mock_hl.get_market_price.return_value = 68000.0
        mock_hl.get_open_orders_detailed.return_value = [
            {"isTrigger": True, "orderType": "Take Profit Market", "oid": 123}
        ]
        with patch('luckytrader.execute.Info', return_value=mock_info), \
             patch('luckytrader.execute.compute_de', return_value=0.01), \
             patch('luckytrader.execute.should_tighten_tp', return_value=0.02), \
             patch('luckytrader.execute.get_regime_params', return_value={
                 'sl_pct': 0.04, 'tp_pct': 0.02, 'regime': 'range'
             }), \
             patch('luckytrader.execute.compute_tp_price', return_value=68340), \
             patch('luckytrader.execute.load_state', return_value={"position": position.copy()}), \
             patch('luckytrader.execute.save_state'):
            result = reeval_regime_tp(position)
        assert result is not None
        assert result["action"] == "TP_TIGHTENED"

    def test_tp_tighten_close_when_price_exceeds(self, mock_hl):
        """Price already above new TP → market close."""
        from luckytrader.execute import reeval_regime_tp
        position = {"coin": "BTC", "entry_price": 67000, "size": 0.001,
                     "direction": "LONG", "regime_tp_pct": 0.07, "regime": "normal"}
        mock_info = MagicMock()
        mock_info.candles_snapshot.return_value = [{"c": "67000"}] * 15
        mock_hl.get_market_price.return_value = 70000.0  # above new TP
        with patch('luckytrader.execute.Info', return_value=mock_info), \
             patch('luckytrader.execute.compute_de', return_value=0.01), \
             patch('luckytrader.execute.should_tighten_tp', return_value=0.02), \
             patch('luckytrader.execute.get_regime_params', return_value={
                 'sl_pct': 0.04, 'tp_pct': 0.02, 'regime': 'range'
             }), \
             patch('luckytrader.execute.compute_tp_price', return_value=68340), \
             patch('luckytrader.execute.compute_pnl_pct', return_value=4.5), \
             patch('luckytrader.execute.close_and_cleanup', return_value={"close_price": 70000}):
            result = reeval_regime_tp(position)
        assert result is not None
        assert result["action"] == "CLOSED_BY_REGIME"


class TestFixSlTp:
    def test_fix_replaces_missing_sl(self, mock_hl):
        from luckytrader.execute import fix_sl_tp, load_state
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000, "unrealized_pnl": 0,
            "liquidation_price": 0,
        }
        mock_hl.get_open_orders_detailed.return_value = []  # nothing exists
        with patch('luckytrader.execute.load_state', return_value={
            "position": {"sl_price": 64320, "tp_price": 71690, "regime_sl_pct": 0.04}
        }):
            fix_sl_tp(position, coin="BTC")
        # Should have placed both SL and TP
        assert mock_hl.place_stop_loss.called
        assert mock_hl.place_take_profit.called


class TestLoadTradeLog:
    def test_corrupt_file(self, mock_hl, tmp_path):
        from luckytrader.execute import load_trade_log
        with patch('luckytrader.execute.TRADE_LOG_FILE', tmp_path / "log.json"):
            (tmp_path / "log.json").write_text("not json{")
            result = load_trade_log()
            assert result == []

    def test_no_file(self, mock_hl, tmp_path):
        from luckytrader.execute import load_trade_log
        with patch('luckytrader.execute.TRADE_LOG_FILE', tmp_path / "nonexistent.json"):
            result = load_trade_log()
            assert result == []


class TestSaveStateOldFormat:
    def test_save_old_format_none_position(self, mock_hl, tmp_path):
        """Old format save_state({"position": None}) preserves existing."""
        from luckytrader.execute import save_state, load_state
        with patch('luckytrader.execute.STATE_FILE', tmp_path / "state.json"):
            # First, save a normal state
            save_state({"position": {"coin": "BTC"}}, coin="BTC")
            # Then save old format with None
            save_state({"position": None})
            # Should preserve BTC state
            state = load_state("BTC")
            assert state.get("position") is not None


class TestOpenPositionBranches:
    def test_recheck_position_exists(self, mock_hl):
        """If position appears on recheck, skip opening."""
        from luckytrader.execute import _execute_inner
        recheck_pos = {"coin": "BTC", "size": 0.001, "direction": "LONG",
                        "entry_price": 67000, "unrealized_pnl": 0}
        with patch('luckytrader.execute.get_position', side_effect=[None, recheck_pos]), \
             patch('luckytrader.execute.load_state', return_value={"position": None}), \
             patch('luckytrader.execute.analyze', return_value={
                 "signal": "LONG", "price": 67000, "signal_reasons": ["test"],
             }):
            result = _execute_inner(dry_run=False, mode="🔴 LIVE",
                                   _CST=timezone(timedelta(hours=8)), coin="BTC")
        assert result["action"] == "HOLD"
        assert result["reason"] == "position_exists_on_recheck"


class TestFixSlTpEdge:
    def test_fix_sl_exception(self, mock_hl):
        """When SL placement fails in fix_sl_tp."""
        from luckytrader.execute import fix_sl_tp
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000, "unrealized_pnl": 0,
            "liquidation_price": 0,
        }
        mock_hl.get_open_orders_detailed.return_value = []
        mock_hl.place_stop_loss.side_effect = Exception("api error")
        with patch('luckytrader.execute.load_state', return_value={
            "position": {"sl_price": 64320, "tp_price": 71690, "regime_sl_pct": 0.04}
        }):
            # Should not crash
            fix_sl_tp(position, coin="BTC")


class TestExecuteMultiCoin:
    def test_all_coins(self, mock_hl):
        from luckytrader.execute import execute
        with patch('luckytrader.execute.execute_coin', return_value={"action": "HOLD"}) as mock_exec:
            results = execute(dry_run=True)
        # Should call execute_coin for each trading coin
        assert isinstance(results, dict)
        assert mock_exec.call_count >= 2  # at least BTC and ETH

    def test_single_coin(self, mock_hl):
        from luckytrader.execute import execute
        with patch('luckytrader.execute.execute_coin', return_value={"action": "HOLD"}):
            result = execute(dry_run=True, coin="BTC")
        assert result == {"action": "HOLD"}


class TestReleaseLockCleanup:
    def test_unlink_exception_suppressed(self, mock_hl, tmp_path):
        from luckytrader.execute import _release_lock
        import io
        fd = open(tmp_path / "test.lock", "w")
        fd._lock_path = tmp_path / "test.lock"
        # Make unlink fail
        with patch.object(Path, 'unlink', side_effect=PermissionError("no perm")):
            _release_lock(fd)  # Should not crash

    def test_release_no_lock_path(self, mock_hl, tmp_path):
        from luckytrader.execute import _release_lock
        fd = open(tmp_path / "test2.lock", "w")
        # No _lock_path attribute
        _release_lock(fd)  # Should not crash


class TestExecuteInnerOpenedPath:
    def test_opened_shows_early_validation(self, mock_hl, capsys):
        """When open_position returns OPENED → prints early validation info."""
        from luckytrader.execute import _execute_inner
        with patch('luckytrader.execute.get_position', side_effect=[None, None]), \
             patch('luckytrader.execute.load_state', return_value={"position": None}), \
             patch('luckytrader.execute.analyze', return_value={
                 "signal": "LONG", "price": 67000, "signal_reasons": ["test"],
             }), \
             patch('luckytrader.execute.open_position', return_value={"action": "OPENED"}):
            result = _execute_inner(dry_run=False, mode="🔴 LIVE",
                                   _CST=timezone(timedelta(hours=8)), coin="BTC")
        assert result["action"] == "OPENED"
        captured = capsys.readouterr()
        assert "Early validation" in captured.out

    def test_dry_run_dispatches_to_dry_run_open(self, mock_hl):
        """Dry run mode → calls dry_run_open."""
        from luckytrader.execute import _execute_inner
        with patch('luckytrader.execute.get_position', return_value=None), \
             patch('luckytrader.execute.load_state', return_value={"position": None}), \
             patch('luckytrader.execute.analyze', return_value={
                 "signal": "LONG", "price": 67000, "signal_reasons": ["test"],
             }), \
             patch('luckytrader.execute.dry_run_open', return_value={"action": "DRY_RUN_WOULD_OPEN"}) as mock_dry:
            result = _execute_inner(dry_run=True, mode="🧪 DRY RUN",
                                   _CST=timezone(timedelta(hours=8)), coin="BTC")
        assert result["action"] == "DRY_RUN_WOULD_OPEN"
        mock_dry.assert_called_once()


class TestCooldownEdge:
    def test_cooldown_corrupt_file(self, mock_hl, tmp_path):
        """Corrupt cooldown file → treat as no cooldown."""
        from luckytrader.execute import _check_cooldown
        with patch('luckytrader.execute._cooldown_file', return_value=tmp_path / ".ts_BTC"):
            (tmp_path / ".ts_BTC").write_text("not-a-number")
            assert _check_cooldown("BTC") is True

    def test_set_cooldown(self, mock_hl, tmp_path):
        from luckytrader.execute import _set_cooldown
        with patch('luckytrader.execute._cooldown_file', return_value=tmp_path / ".ts_BTC"):
            _set_cooldown("BTC")
            assert (tmp_path / ".ts_BTC").exists()


class TestDryRunOpenShortBranch:
    def test_dry_run_open_de_failure(self, mock_hl):
        """DE calculation failure falls back to defaults."""
        from luckytrader.execute import dry_run_open
        mock_hl.get_account_info.return_value = {"account_value": "200"}
        analysis = {"price": 67000}
        with patch('luckytrader.execute.get_candles', side_effect=Exception("no data")), \
             patch('luckytrader.execute.get_regime_params', return_value={
                 'sl_pct': 0.04, 'tp_pct': 0.07, 'regime': 'normal'
             }), \
             patch('luckytrader.execute.get_coin_info', return_value=None):
            result = dry_run_open("SHORT", analysis, "BTC")
        assert result["direction"] == "SHORT"


class TestGetCoinInfo:
    def test_found(self, mock_hl):
        from luckytrader.execute import get_coin_info
        result = get_coin_info("BTC")
        assert result is not None

    def test_not_found(self, mock_hl):
        from luckytrader.execute import get_coin_info
        result = get_coin_info("UNKNOWN")
        assert result is None
