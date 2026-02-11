"""
Tests for centralized config system.
"""
import pytest
import os
from pathlib import Path


class TestConfigLoader:
    """Config loading and validation."""
    
    def test_load_default_config(self):
        from luckytrader.config import get_config
        cfg = get_config()
        assert cfg.risk.stop_loss_pct == 0.04
        assert cfg.risk.take_profit_pct == 0.07
        assert cfg.risk.max_hold_hours == 72
        assert cfg.strategy.vol_threshold == 1.25
        assert cfg.trailing.initial_stop_pct == 0.035
    
    def test_load_missing_file_uses_defaults(self, tmp_path):
        from luckytrader.config import reload_config
        # Point to empty dir → should use defaults
        old = os.environ.get("LUCKYTRADER_CONFIG_DIR")
        os.environ["LUCKYTRADER_CONFIG_DIR"] = str(tmp_path)
        try:
            cfg = reload_config()
            assert cfg.risk.stop_loss_pct == 0.03  # dataclass default
            assert cfg.strategy.vol_threshold == 2.0  # dataclass default
        finally:
            if old:
                os.environ["LUCKYTRADER_CONFIG_DIR"] = old
            else:
                del os.environ["LUCKYTRADER_CONFIG_DIR"]
            reload_config()  # restore
    
    def test_discord_mentions(self):
        from luckytrader.config import get_config
        cfg = get_config()
        mentions = cfg.notifications.discord_mentions
        assert mentions is not None
    
    def test_frozen_config(self):
        from luckytrader.config import get_config
        cfg = get_config()
        with pytest.raises(Exception):
            cfg.risk.stop_loss_pct = 0.10
    
    def test_get_config_singleton(self):
        from luckytrader.config import get_config
        c1 = get_config()
        c2 = get_config()
        assert c1 is c2
    
    def test_reload_config(self, tmp_path):
        from luckytrader.config import reload_config
        
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[risk]\nstop_loss_pct = 0.05\n')
        
        old = os.environ.get("LUCKYTRADER_CONFIG_DIR")
        os.environ["LUCKYTRADER_CONFIG_DIR"] = str(config_dir)
        try:
            cfg = reload_config()
            assert cfg.risk.stop_loss_pct == 0.05
            assert cfg.risk.take_profit_pct == 0.05  # dataclass default
        finally:
            if old:
                os.environ["LUCKYTRADER_CONFIG_DIR"] = old
            else:
                del os.environ["LUCKYTRADER_CONFIG_DIR"]
            reload_config()  # restore


class TestParameterConsistencyAcrossFiles:
    """P2 #9: All files must use the same config source."""
    
    def test_execute_signal_uses_config(self):
        from luckytrader.execute import STOP_LOSS_PCT, TAKE_PROFIT_PCT, MAX_HOLD_HOURS
        from luckytrader.config import get_config
        cfg = get_config()
        assert STOP_LOSS_PCT == cfg.risk.stop_loss_pct
        assert TAKE_PROFIT_PCT == cfg.risk.take_profit_pct
        assert MAX_HOLD_HOURS == cfg.risk.max_hold_hours
    
    def test_trailing_stop_uses_config(self):
        from luckytrader.trailing import INITIAL_STOP_PCT, TRAILING_PCT, ACTIVATION_PCT
        from luckytrader.config import get_config
        cfg = get_config()
        assert INITIAL_STOP_PCT == cfg.trailing.initial_stop_pct
        assert TRAILING_PCT == cfg.trailing.trailing_pct
        assert ACTIVATION_PCT == cfg.trailing.activation_pct
