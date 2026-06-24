"""
Tests for MempoolMonitor.
"""

import pytest
from unittest.mock import patch, MagicMock
from agent.monitors.mempool import MempoolMonitor


class TestMempoolMonitor:

    def test_collect_returns_fee_rate(self, base_config):
        monitor = MempoolMonitor(base_config)
        mock_fees = {"economyFee": 8, "fastestFee": 22, "hourFee": 12, "halfHourFee": 15}
        mock_mempool = {"vsize": 18_000_000, "count": 4200}
        with patch("agent.monitors.mempool.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.side_effect = [mock_fees, mock_mempool]
            signals = monitor.collect()
        assert signals["fee_rate_sat_vbyte"] == 8
        assert signals["fee_rate_fastest"] == 22

    def test_not_congested_below_threshold(self, base_config):
        monitor = MempoolMonitor(base_config)
        mock_fees = {"economyFee": 8, "fastestFee": 22, "hourFee": 12}
        mock_mempool = {"vsize": 18_000_000, "count": 4200}
        with patch("agent.monitors.mempool.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.side_effect = [mock_fees, mock_mempool]
            signals = monitor.collect()
        assert signals["mempool_congested"] is False
        assert signals["mempool_size_mb"] == 18.0

    def test_congested_above_threshold(self, base_config):
        monitor = MempoolMonitor(base_config)
        mock_fees = {"economyFee": 65, "fastestFee": 90, "hourFee": 70}
        mock_mempool = {"vsize": 82_000_000, "count": 18000}
        with patch("agent.monitors.mempool.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.side_effect = [mock_fees, mock_mempool]
            signals = monitor.collect()
        assert signals["mempool_congested"] is True

    def test_fee_api_failure_returns_safe_default(self, base_config):
        monitor = MempoolMonitor(base_config)
        with patch("agent.monitors.mempool.requests.get") as mock_get:
            mock_get.side_effect = Exception("Connection refused")
            signals = monitor.collect()
        assert signals["fee_rate_sat_vbyte"] == 999
        assert "mempool_congested" in signals

    def test_mempool_api_failure_defaults_not_congested(self, base_config):
        monitor = MempoolMonitor(base_config)
        mock_fees = {"economyFee": 8, "fastestFee": 22, "hourFee": 12}
        with patch("agent.monitors.mempool.requests.get") as mock_get:
            success = MagicMock(status_code=200)
            success.json.return_value = mock_fees
            fail = MagicMock()
            fail.raise_for_status.side_effect = Exception("timeout")
            mock_get.side_effect = [success, fail]
            signals = monitor.collect()
        assert signals["fee_rate_sat_vbyte"] == 8
        assert signals["mempool_congested"] is False
