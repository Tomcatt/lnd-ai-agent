"""
Tests for action executors — redesigned to drive LNDg AR/AF.
"""

import pytest
import requests as req
from unittest.mock import patch, MagicMock
from agent.actions.rebalance import RebalanceAction
from agent.actions.fee_policy import FeePolicyAction
from agent.actions.loop_swap import LoopSwapAction


class TestRebalanceAction:

    def test_dry_run_never_calls_api(self, base_config):
        action = RebalanceAction(base_config, dry_run=True)
        with patch("agent.actions.rebalance.requests.get") as mg, \
             patch("agent.actions.rebalance.requests.put") as mp, \
             patch("agent.actions.rebalance.requests.post") as mpost:
            result = action.execute({
                "chan_id": "111x1x0", "peer_alias": "TestPeer",
                "capacity_sats": 2_000_000, "local_balance_sats": 200_000,
                "local_balance_pct": 10.0, "estimated_rebalance_cost_sats": 50,
            })
            mg.assert_not_called()
            mp.assert_not_called()
            mpost.assert_not_called()
        assert result["status"] == "dry_run"

    def test_live_mode_fetches_then_puts_channel(self, base_config):
        action = RebalanceAction(base_config, dry_run=False)
        mock_channel = {"chan_id": "111x1x0", "alias": "TestPeer",
                        "auto_rebalance": False, "ar_out_target": 75,
                        "ar_in_target": 90, "ar_amt_target": 0}
        with patch("agent.actions.rebalance.requests.get") as mg, \
             patch("agent.actions.rebalance.requests.put") as mp:
            mg.return_value = MagicMock(status_code=200)
            mg.return_value.json.return_value = mock_channel
            mp.return_value = MagicMock(status_code=200)
            mp.return_value.json.return_value = mock_channel
            result = action.execute({
                "chan_id": "111x1x0", "peer_alias": "TestPeer",
                "capacity_sats": 2_000_000, "local_balance_sats": 200_000,
                "local_balance_pct": 10.0, "estimated_rebalance_cost_sats": 50,
            })
            mg.assert_called_once()
            mp.assert_called_once()
        assert result["status"] == "success"

    def test_api_failure_returns_failed_status(self, base_config):
        action = RebalanceAction(base_config, dry_run=False)
        with patch("agent.actions.rebalance.requests.get") as mg:
            mg.return_value = MagicMock()
            mg.return_value.raise_for_status.side_effect = req.HTTPError("404")
            result = action.execute({
                "chan_id": "111x1x0", "peer_alias": "TestPeer",
                "capacity_sats": 2_000_000, "local_balance_sats": 200_000,
                "local_balance_pct": 10.0, "estimated_rebalance_cost_sats": 50,
            })
        assert result["status"] == "failed"

    def test_critical_imbalance_fires_oneshot(self, base_config):
        action = RebalanceAction(base_config, dry_run=False)
        mock_ch = {"chan_id": "111x1x0", "auto_rebalance": False,
                   "ar_out_target": 75, "ar_in_target": 90, "ar_amt_target": 0}
        with patch("agent.actions.rebalance.requests.get") as mg, \
             patch("agent.actions.rebalance.requests.put") as mp, \
             patch("agent.actions.rebalance.requests.post") as mpost:
            mg.return_value = MagicMock(status_code=200)
            mg.return_value.json.return_value = mock_ch
            mp.return_value = MagicMock(status_code=200)
            mp.return_value.json.return_value = mock_ch
            mpost.return_value = MagicMock(status_code=200)
            mpost.return_value.json.return_value = {"id": 1}
            result = action.execute({
                "chan_id": "111x1x0", "peer_alias": "CriticalPeer",
                "capacity_sats": 2_000_000, "local_balance_sats": 1_980_000,
                "local_balance_pct": 99.0, "estimated_rebalance_cost_sats": 50,
            })
            mpost.assert_called_once()
        assert "oneshot" in result


class TestFeePolicyAction:

    def test_dry_run_never_calls_api(self, base_config):
        action = FeePolicyAction(base_config, dry_run=True)
        with patch("agent.actions.fee_policy.requests.put") as mp:
            result = action.execute({"congested": True})
            mp.assert_not_called()
        assert result["status"] == "dry_run"
        assert result["mode"] == "congestion"

    def test_congested_raises_af_floor(self, base_config):
        action = FeePolicyAction(base_config, dry_run=True)
        result = action.execute({"congested": True})
        assert result["af_min"] > 0
        assert result["af_max"] == 2500

    def test_normal_mode_standard_band(self, base_config):
        action = FeePolicyAction(base_config, dry_run=True)
        result = action.execute({"congested": False})
        assert result["af_min"] == 0
        assert result["af_max"] == 500
        assert result["mode"] == "normal"

    def test_live_mode_puts_three_settings(self, base_config):
        action = FeePolicyAction(base_config, dry_run=False)
        with patch("agent.actions.fee_policy.requests.put") as mp:
            mp.return_value = MagicMock(status_code=200)
            mp.return_value.json.return_value = {}
            result = action.execute({"congested": False})
            assert mp.call_count == 3
        assert result["status"] == "success"


class TestLoopSwapAction:

    def test_dry_run_never_calls_api(self, base_config):
        action = LoopSwapAction(base_config, dry_run=True)
        with patch("agent.actions.loop_swap.requests.post") as mp:
            result = action.execute({"peer_alias": "TestPeer",
                                     "capacity_sats": 2_000_000,
                                     "local_balance_sats": 100_000})
            mp.assert_not_called()
        assert result["status"] == "dry_run"

    def test_swap_skipped_when_amount_too_small(self, base_config):
        action = LoopSwapAction(base_config, dry_run=False)
        result = action.execute({"peer_alias": "TestPeer",
                                 "capacity_sats": 100_000,
                                 "local_balance_sats": 39_000})
        assert result["status"] == "skipped"
        assert result["reason"] == "amount_too_small"

    def test_swap_amount_calculated_correctly(self, base_config):
        action = LoopSwapAction(base_config, dry_run=True)
        result = action.execute({"peer_alias": "TestPeer",
                                 "capacity_sats": 2_000_000,
                                 "local_balance_sats": 100_000})
        assert result["amount_sats"] == 700_000
