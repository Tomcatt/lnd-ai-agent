"""
Tests for action executors.
Critical: verify dry_run=True never makes real API calls.
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
        with patch("agent.actions.rebalance.requests.post") as mock_post:
            result = action.execute({"chan_id": "111x1x0", "peer_alias": "TestPeer",
                                     "capacity_sats": 2_000_000, "local_balance_sats": 200_000,
                                     "estimated_rebalance_cost_sats": 50})
            mock_post.assert_not_called()
        assert result["status"] == "dry_run"

    def test_live_mode_calls_lndg_api(self, base_config):
        action = RebalanceAction(base_config, dry_run=False)
        with patch("agent.actions.rebalance.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.json.return_value = {"status": "ok"}
            result = action.execute({"chan_id": "111x1x0", "peer_alias": "TestPeer",
                                     "capacity_sats": 2_000_000, "local_balance_sats": 200_000,
                                     "estimated_rebalance_cost_sats": 50})
            mock_post.assert_called_once()
        assert result["status"] == "success"

    def test_api_failure_returns_failed_status(self, base_config):
        action = RebalanceAction(base_config, dry_run=False)
        with patch("agent.actions.rebalance.requests.post") as mock_post:
            mock_post.return_value = MagicMock()
            mock_post.return_value.raise_for_status.side_effect = req.HTTPError("500 error")
            result = action.execute({"chan_id": "111x1x0", "peer_alias": "TestPeer",
                                     "capacity_sats": 2_000_000, "local_balance_sats": 200_000,
                                     "estimated_rebalance_cost_sats": 50})
        assert result["status"] == "failed"


class TestFeePolicyAction:

    def test_dry_run_never_calls_api(self, base_config):
        action = FeePolicyAction(base_config, dry_run=True)
        with patch("agent.actions.fee_policy.requests.post") as mock_post:
            result = action.execute({"target_ppm": 150, "congested": True})
            mock_post.assert_not_called()
        assert result["status"] == "dry_run"
        assert result["target_ppm"] == 150

    def test_live_mode_posts_correct_ppm(self, base_config):
        action = FeePolicyAction(base_config, dry_run=False)
        with patch("agent.actions.fee_policy.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.json.return_value = {}
            action.execute({"target_ppm": 150, "congested": True})
            assert mock_post.call_args[1]["json"]["fee_rate"] == 150


class TestLoopSwapAction:

    def test_dry_run_never_calls_api(self, base_config):
        action = LoopSwapAction(base_config, dry_run=True)
        with patch("agent.actions.loop_swap.requests.post") as mock_post:
            result = action.execute({"peer_alias": "TestPeer",
                                     "capacity_sats": 2_000_000, "local_balance_sats": 100_000})
            mock_post.assert_not_called()
        assert result["status"] == "dry_run"

    def test_swap_skipped_when_amount_too_small(self, base_config):
        action = LoopSwapAction(base_config, dry_run=False)
        result = action.execute({"peer_alias": "TestPeer",
                                 "capacity_sats": 100_000, "local_balance_sats": 39_000})
        assert result["status"] == "skipped"
        assert result["reason"] == "amount_too_small"

    def test_swap_amount_calculated_correctly(self, base_config):
        action = LoopSwapAction(base_config, dry_run=True)
        result = action.execute({"peer_alias": "TestPeer",
                                 "capacity_sats": 2_000_000, "local_balance_sats": 100_000})
        assert result["amount_sats"] == 700_000
