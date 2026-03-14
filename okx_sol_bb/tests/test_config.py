"""Tests for SOL BB config loading."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_sol_bb.config import load_config, StrategyConfig, RiskConfig


class TestConfig:
    def test_default_values(self):
        """Defaults match backtest-validated parameters."""
        s = StrategyConfig()
        assert s.bb_period == 14
        assert s.bb_multiplier == 3.0

    def test_risk_defaults(self):
        r = RiskConfig()
        assert r.take_profit_pct == 0.02
        assert r.stop_loss_pct == 0.05
        assert r.max_hold_bars == 96  # 48h

    def test_env_var_config_dir(self, tmp_path, monkeypatch):
        """Config loads from OKX_SOL_BB_CONFIG_DIR."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("""
[strategy]
bb_period = 20
bb_multiplier = 2.5

[risk]
take_profit_pct = 0.03
stop_loss_pct = 0.04

[exchange]
coin = "SOL"
instId = "SOL-USDT-SWAP"
""")
        monkeypatch.setenv("OKX_SOL_BB_CONFIG_DIR", str(config_dir))
        cfg = load_config()
        assert cfg.strategy.bb_period == 20
        assert cfg.risk.take_profit_pct == 0.03
        assert cfg.coin == "SOL"
        assert cfg.instId == "SOL-USDT-SWAP"
