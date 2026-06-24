"""
Tests for LNDgMonitor.
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
            "capacity": 2_000_000, "local_balance": 1_900_000,
            "is_active": True, "open_date": "2024-01-01T00:00:00Z",
            "last_forward": "2025-01-01T00:00:00Z",
        }
        result = monitor._find_imbalanced([channel])
        assert len(result) == 1
        assert result[0]["local_balance_pct"] == 95.0

    def test_exactly_at_threshold_not_flagged(self, base_config):
        monitor = make_monitor(base_config)
        channel = {
            "chan_id": "555x1x0", "alias": "BoundaryPeer", "remote_pubkey": "02eee",
            "capacity": 1_000_000, "local_balance": 200_000,
            "is_active": True, "open_date": "2024-01-01T00:00:00Z",
            "last_forward": "2025-01-01T00:00:00Z",
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
        recent_date = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        monitor = make_monitor(base_config)
        channel = {
            "chan_id": "666x1x0", "alias": "NewPeer", "remote_pubkey": "02fff",
            "capacity": 1_000_000, "local_balance": 500_000,
            "is_active": True, "open_date": recent_date, "last_forward": None,
        }
        assert monitor._find_dead([channel]) == []


class TestLNDgRevenue:

    def test_revenue_calculated_correctly(self, base_config):
        from datetime import datetime, timedelta
        monitor = make_monitor(base_config)
        recent = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payments = [
            {"is_routing": True, "fee_sat": 100, "chan_id_out": "111x1x0", "created_at": recent},
            {"is_routing": True, "fee_sat": 50,  "chan_id_out": "111x1x0", "created_at": recent},
            {"is_routing": True, "fee_sat": 200, "chan_id_out": "111x1x0", "created_at": old},
            {"is_routing": False, "fee_sat": 999, "chan_id_out": "111x1x0", "created_at": recent},
        ]
        revenue = monitor._calc_revenue_7d(payments)
        assert revenue.get("111x1x0") == 150

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
        channel_response.json.return_value = {"results": []}
        with patch("agent.monitors.lndg.requests.get") as mock_get:
            mock_get.side_effect = [channel_response, Exception("payment API down")]
            signals = monitor.collect()
        assert signals["routing_revenue_7d"] == {}
        assert signals["rebalance_cost_7d"] == {}
