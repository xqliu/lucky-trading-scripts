"""
TDD: close_position retry logic — 超时平仓失败时自动重试

Root cause: 2026-02-22 16:35 UTC，交易所短暂 down，close_position 无重试
直接抛 RuntimeError，触发 ws_monitor 误判状态不一致并清除 trailing_state。

目标行为：
- exchange 短暂故障 → 重试 → 成功
- 多次失败 → 告警 Discord → 仍然 raise RuntimeError
- 指数退避 backoff（防止暴打 API）
- 成功时 save_state 和 log_trade 各执行一次
- 失败时不得清除 state（仓位还在）
"""
import pytest
from unittest.mock import patch, MagicMock, call

# close_position 使用的仓位对象（来自 load_state）
POSITION = {
    "coin": "BTC",
    "direction": "SHORT",
    "size": 0.00096,
    "entry_price": 67615.0,
    "unrealized_pnl": 0.05,
}

# get_position 返回的链上仓位
CHAIN_POSITION = {
    "coin": "BTC",
    "size": -0.00096,
    "direction": "SHORT",
    "entry_price": 67615.0,
}


class TestClosePositionRetryOnErrStatus:
    """place_market_order 返回 {'status': 'err'} 时触发重试"""

    @patch('luckytrader.execute.log_trade')
    @patch('luckytrader.execute.save_state')
    @patch('luckytrader.execute.get_market_price', return_value=67500.0)
    @patch('luckytrader.execute.get_open_orders_detailed', return_value=[])
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_position', return_value=CHAIN_POSITION)
    def test_succeeds_on_second_attempt(self, mock_gp, mock_notify, mock_orders,
                                        mock_price, mock_save, mock_log, mock_hl):
        """第一次返回 err，第二次成功 → 平仓完成"""
        from luckytrader.execute import close_position

        mock_hl.place_market_order.side_effect = [
            {"status": "err", "response": "exchange temporarily unavailable"},
            {"status": "ok"},
        ]

        result = close_position(POSITION, max_retries=3, backoff_seconds=0)

        assert result is True
        assert mock_hl.place_market_order.call_count == 2
        mock_save.assert_called_once_with({"position": None}, "BTC")
        mock_log.assert_called_once()
        mock_hl.place_market_order.side_effect = None

    @patch('luckytrader.execute.log_trade')
    @patch('luckytrader.execute.save_state')
    @patch('luckytrader.execute.get_market_price', return_value=67500.0)
    @patch('luckytrader.execute.get_open_orders_detailed', return_value=[])
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_position', return_value=CHAIN_POSITION)
    def test_succeeds_on_first_attempt(self, mock_gp, mock_notify, mock_orders,
                                       mock_price, mock_save, mock_log, mock_hl):
        """正常情况：第一次成功，不重试"""
        from luckytrader.execute import close_position

        mock_hl.place_market_order.return_value = {"status": "ok"}

        result = close_position(POSITION, max_retries=3, backoff_seconds=0)

        assert result is True
        assert mock_hl.place_market_order.call_count == 1
        mock_save.assert_called_once_with({"position": None}, "BTC")


class TestClosePositionRetryOnException:
    """place_market_order 抛出异常（网络错误）时触发重试"""

    @patch('luckytrader.execute.log_trade')
    @patch('luckytrader.execute.save_state')
    @patch('luckytrader.execute.get_market_price', return_value=67500.0)
    @patch('luckytrader.execute.get_open_orders_detailed', return_value=[])
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_position', return_value=CHAIN_POSITION)
    def test_recovers_after_exception(self, mock_gp, mock_notify, mock_orders,
                                      mock_price, mock_save, mock_log, mock_hl):
        """API 抛异常两次，第三次成功"""
        from luckytrader.execute import close_position

        mock_hl.place_market_order.side_effect = [
            ConnectionError("exchange down"),
            TimeoutError("timeout"),
            {"status": "ok"},
        ]

        result = close_position(POSITION, max_retries=3, backoff_seconds=0)

        assert result is True
        assert mock_hl.place_market_order.call_count == 3
        mock_hl.place_market_order.side_effect = None


class TestClosePositionAllRetriesFail:
    """全部重试失败 → 通知 Discord + raise RuntimeError"""

    @patch('luckytrader.execute.get_open_orders_detailed', return_value=[])
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_position', return_value=CHAIN_POSITION)
    def test_raises_after_all_retries(self, mock_gp, mock_notify, mock_orders, mock_hl):
        """max_retries=3 → 总共尝试 4 次（1 initial + 3 retries）"""
        from luckytrader.execute import close_position

        mock_hl.place_market_order.return_value = {"status": "err", "response": "down"}

        with pytest.raises(RuntimeError, match="平仓失败"):
            close_position(POSITION, max_retries=3, backoff_seconds=0)

        assert mock_hl.place_market_order.call_count == 4  # 1 + 3 retries

    @patch('luckytrader.execute.get_open_orders_detailed', return_value=[])
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_position', return_value=CHAIN_POSITION)
    def test_notifies_discord_on_failure(self, mock_gp, mock_notify, mock_orders, mock_hl):
        """全部失败时必须通知 Discord"""
        from luckytrader.execute import close_position

        mock_hl.place_market_order.return_value = {"status": "err", "response": "down"}

        with pytest.raises(RuntimeError):
            close_position(POSITION, max_retries=2, backoff_seconds=0)

        assert mock_notify.called
        alert_text = " ".join(str(c) for c in mock_notify.call_args_list)
        assert "失败" in alert_text

    @patch('luckytrader.execute.get_open_orders_detailed', return_value=[])
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_position', return_value=CHAIN_POSITION)
    def test_save_state_not_called_on_failure(self, mock_gp, mock_notify, mock_orders, mock_hl):
        """平仓失败时不得清除 state（仓位仍存在）"""
        from luckytrader.execute import close_position

        mock_hl.place_market_order.return_value = {"status": "err", "response": "down"}

        with patch('luckytrader.execute.save_state') as mock_save:
            with pytest.raises(RuntimeError):
                close_position(POSITION, max_retries=2, backoff_seconds=0)
            mock_save.assert_not_called()


class TestClosePositionBackoff:
    """指数退避：重试间隔正确"""

    @patch('luckytrader.execute.get_open_orders_detailed', return_value=[])
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_position', return_value=CHAIN_POSITION)
    def test_exponential_backoff_called(self, mock_gp, mock_notify, mock_orders, mock_hl):
        """重试之间应调用 time.sleep，且时间递增"""
        from luckytrader.execute import close_position

        mock_hl.place_market_order.return_value = {"status": "err", "response": "down"}

        with patch('luckytrader.execute.time') as mock_time:
            with pytest.raises(RuntimeError):
                close_position(POSITION, max_retries=3, backoff_seconds=2)

        sleep_calls = [c[0][0] for c in mock_time.sleep.call_args_list]
        assert len(sleep_calls) >= 2, f"应有至少2次sleep，实际: {sleep_calls}"
        assert sleep_calls[1] >= sleep_calls[0], f"退避应递增: {sleep_calls}"


class TestClosePositionNoPositionOnChain:
    """链上已无仓位 → 直接清理 state，不尝试平仓"""

    @patch('luckytrader.execute.save_state')
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_position', return_value=None)
    def test_cleans_stale_state(self, mock_gp, mock_notify, mock_save, mock_hl):
        """链上无仓位时不调用 place_market_order"""
        from luckytrader.execute import close_position

        result = close_position(POSITION, max_retries=3, backoff_seconds=0)

        assert result is None
        mock_hl.place_market_order.assert_not_called()
        mock_save.assert_called_once_with({"position": None}, "BTC")
