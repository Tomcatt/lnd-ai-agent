"""
Tests for LNDgMonitor.
Field names updated to match real LNDg API responses confirmed on Umbrel.
"""

import pytest
from unittest.mock import patch, MagicMock
from agent.monitors.lndg import LNDgMonitor


def make_monitor(base_config):
    return LNDgMonitor(base_config)


class TestLNDgImbalanceDetection:

    def test_balanced_channel_not_flagged(self, base_config, healthy_channel):
        monitor = make_monitor(base_config)
        assert monitor._find_imbalanced([healthy_channel]) == []

    def test_depleted_channel_flagged(self, base_config, depleted_channel):
        monitor = make_monitor(base_config)
        result = monitor._find_imbalanced([depleted_channel])
        assert len(result) == 1
        assert result[0]["peer_alias"] == "DepletedPeer"
        assert result[0]["local_balance_pct"] == 10.0

    def test_oversaturated_channel_flagged(self, base_config):
        monitor = make_monitor(base_config)
        channel = {
            "chan_id": "444x1x0", "alias": "FullPeer", "remote_pubkey": "02ddd",
            "capacity": 2_000_000, "local_balance": 1_900_000, "remote_balance": 100_000,
            "is_active": True, "is_open": True,
            "total_sent": 100, "total_received": 0,
            "fees_updated": "2025-01-01T00:00:00",
        }
        result = monitor._find_imbalanced([channel])
        assert len(result) == 1
        assert result[0]["local_balance_pct"] == 95.0

    def test_exactly_at_threshold_not_flagged(self, base_config):
        monitor = make_monitor(base_config)
        channel = {
            "chan_id": "555x1x0", "alias": "BoundaryPeer", "remote_pubkey": "02eee",
            "capacity": 1_000_000, "local_balance": 200_000, "remote_balance": 800_000,
            "is_active": True, "is_open": True,
            "total_sent": 0, "total_received": 0,
            "fees_updated": "2025-01-01T00:00:00",
        }
        assert monitor._find_imbalanced([channel]) == []

    def test_rebalance_cost_estimate_is_positive(self, base_config, depleted_channel):
        monitor = make_monitor(base_config)
        result = monitor._find_imbalanced([depleted_channel])
        assert result[0]["estimated_rebalance_cost_sats"] > 0


class TestLNDgZombieDetection:

    def test_active_channel_not_zombie(self, base_config, healthy_channel):
        monitor = make_monitor(base_config)
        assert monitor._find_dead([healthy_channel]) == []

    def test_old_never_forwarded_channel_flagged(self, base_config, zombie_channel):
        monitor = make_monitor(base_config)
        result = monitor._find_dead([zombie_channel])
        assert len(result) == 1
        assert result[0]["peer_alias"] == "ZombiePeer"

    def test_new_never_forwarded_channel_not_flagged(self, base_config):
        from datetime import datetime, timedelta
        recent_date = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        monitor = make_monitor(base_config)
        channel = {
            "chan_id": "666x1x0", "alias": "NewPeer", "remote_pubkey": "02fff",
            "capacity": 1_000_000, "local_balance": 500_000, "remote_balance": 500_000,
            "is_active": True, "is_open": True,
            "total_sent": 0, "total_received": 0,
            "fees_updated": recent_date,
        }
        assert monitor._find_dead([channel]) == []


class TestLNDgRevenue:

    def test_revenue_calculated_correctly(self, base_config):
        from datetime import datetime, timedelta
        monitor = make_monitor(base_config)
        recent = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        old = (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S")
        payments = [
            {"rebal_chan": None, "fee": 100.0, "chan_out": "111x1x0", "creation_date": recent, "status": 2},
            {"rebal_chan": None, "fee": 50.0,  "chan_out": "111x1x0", "creation_date": recent, "status": 2},
            {"rebal_chan": None, "fee": 200.0, "chan_out": "111x1x0", "creation_date": old,    "status": 2},
            {"rebal_chan": "222x1x0", "fee": 999.0, "chan_out": "111x1x0", "creation_date": recent, "status": 2},
        ]
        revenue = monitor._calc_revenue_7d(payments)
        assert revenue.get("111x1x0") == 150.0

    def test_rebalance_cost_calculated_correctly(self, base_config):
        from datetime import datetime, timedelta
        monitor = make_monitor(base_config)
        recent = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        payments = [
            {"rebal_chan": "111x1x0", "fee": 25.0, "chan_out": "999x1x0", "creation_date": recent, "status": 2},
            {"rebal_chan": "111x1x0", "fee": 15.0, "chan_out": "999x1x0", "creation_date": recent, "status": 2},
            {"rebal_chan": None,       "fee": 100.0, "chan_out": "111x1x0", "creation_date": recent, "status": 2},
        ]
        costs = monitor._calc_rebalance_cost_7d(payments)
        assert costs.get("111x1x0") == 40.0

    def test_incomplete_payments_not_counted(self, base_config):
        from datetime import datetime, timedelta
        monitor = make_monitor(base_config)
        recent = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        payments = [
            {"rebal_chan": None, "fee": 100.0, "chan_out": "111x1x0", "creation_date": recent, "status": 1},
            {"rebal_chan": None, "fee": 100.0, "chan_out": "111x1x0", "creation_date": recent, "status": 3},
        ]
        revenue = monitor._calc_revenue_7d(payments)
        assert revenue.get("111x1x0") is None

    def test_empty_payments_returns_empty_dict(self, base_config):
        monitor = make_monitor(base_config)
        assert monitor._calc_revenue_7d([]) == {}


class TestLNDgAPIFailure:

    def test_channel_api_failure_returns_empty_lists(self, base_config):
        monitor = make_monitor(base_config)
        with patch("agent.monitors.lndg.requests.get") as mock_get:
            mock_get.side_effect = Exception("LNDg unreachable")
            signals = monitor.collect()
        assert signals["imbalanced_channels"] == []
        assert signals["dead_channels"] == []

    def test_payment_api_failure_returns_empty_dicts(self, base_config):
        monitor = make_monitor(base_config)
        channel_response = MagicMock(status_code=200)
        channel_response.json.return_value = {"results": [], "next": None}
        with patch("agent.monitors.lndg.requests.get") as mock_get:
            mock_get.side_effect = [channel_response, Exception("payment API down")]
            signals = monitor.collect()
        assert signals["routing_revenue_7d"] == {}
        assert signals["rebalance_cost_7d"] == {}
