"""
Tests for DecisionEngine rules and safety guarantees.
"""

import pytest
from unittest.mock import MagicMock
from agent.core.decision_engine import DecisionEngine


def make_engine(base_config, signals_override=None):
    signals = {
        "mempool": {"fee_rate_sat_vbyte": 8, "mempool_congested": False, "mempool_size_mb": 18.0},
        "lndg": {
            "imbalanced_channels": [], "dead_channels": [], "stuck_funding_channels": [],
            "total_channels": 14, "routing_revenue_7d": {}, "rebalance_cost_7d": {},
            "db_corruption_warning": False,
        },
        "thunderhub": {"peer_uptime": {}, "inactive_channels": [], "scb_age_hours": 2.0},
        "faraday": {"channel_revenue_per_sat": {}, "underperforming_channels": [], "faraday_close_recommendations": []},
    }
    if signals_override:
        for k, v in signals_override.items():
            signals[k].update(v)
    monitors = {name: MagicMock() for name in ["mempool", "lndg", "thunderhub", "faraday"]}
    for name, mock in monitors.items():
        mock.collect.return_value = signals[name]
    actions = {name: MagicMock() for name in ["rebalance", "fee_policy", "loop_swap"]}
    for action in actions.values():
        action.execute.return_value = {"status": "dry_run"}
    approval_gate = MagicMock()
    engine = DecisionEngine(base_config, monitors, actions, approval_gate)
    return engine, actions, approval_gate


class TestSCBFreshnessRule:

    def test_fresh_scb_no_alert(self, base_config):
        engine, _, approval = make_engine(base_config, {"thunderhub": {"scb_age_hours": 2.0}})
        engine.run_cycle()
        sent = [call[0][0] for call in approval.send_request.call_args_list]
        assert not any(d.rule == "scb_freshness" for d in sent)

    def test_stale_scb_triggers_alert(self, base_config):
        engine, _, approval = make_engine(base_config, {"thunderhub": {"scb_age_hours": 30.0}})
        engine.run_cycle()
        sent = [call[0][0] for call in approval.send_request.call_args_list]
        alerts = [d for d in sent if d.rule == "scb_freshness"]
        assert len(alerts) == 1
        assert alerts[0].priority == "critical"


class TestDBCorruptionRule:

    def test_no_corruption_no_alert(self, base_config):
        engine, _, _ = make_engine(base_config, {"lndg": {"db_corruption_warning": False}})
        decisions = engine._evaluate_rules(engine._collect_signals())
        assert not any(d.rule == "db_corruption" for d in decisions)

    def test_corruption_generates_human_instruction(self, base_config):
        engine, _, _ = make_engine(base_config, {"lndg": {"db_corruption_warning": True}})
        decisions = engine._evaluate_rules(engine._collect_signals())
        corruption = [d for d in decisions if d.rule == "db_corruption"]
        assert len(corruption) == 1
        assert corruption[0].action_type == "instruct_human"
        assert corruption[0].priority == "critical"
        assert len(corruption[0].data["instructions"]) > 0


class TestRebalancingRule:

    def test_no_rebalancing_when_fee_too_high(self, base_config):
        imbalanced = [{"chan_id": "111x1x0", "peer_alias": "TestPeer", "peer_pubkey": "02aaa",
                       "local_balance_pct": 10.0, "local_balance_sats": 200_000,
                       "capacity_sats": 2_000_000, "estimated_rebalance_cost_sats": 50}]
        engine, actions, _ = make_engine(base_config, {
            "mempool": {"fee_rate_sat_vbyte": 50},
            "lndg": {"imbalanced_channels": imbalanced},
        })
        engine.run_cycle()
        actions["rebalance"].execute.assert_not_called()

    def test_rebalancing_triggered_when_economic(self, base_config):
        imbalanced = [{"chan_id": "111x1x0", "peer_alias": "TestPeer", "peer_pubkey": "02aaa",
                       "local_balance_pct": 10.0, "local_balance_sats": 200_000,
                       "capacity_sats": 2_000_000, "estimated_rebalance_cost_sats": 50}]
        engine, actions, _ = make_engine(base_config, {
            "mempool": {"fee_rate_sat_vbyte": 8},
            "lndg": {"imbalanced_channels": imbalanced, "routing_revenue_7d": {"111x1x0": 500}},
        })
        engine.run_cycle()
        actions["rebalance"].execute.assert_called_once()

    def test_rebalancing_skipped_when_uneconomic(self, base_config):
        imbalanced = [{"chan_id": "111x1x0", "peer_alias": "ExpensivePeer", "peer_pubkey": "02aaa",
                       "local_balance_pct": 10.0, "local_balance_sats": 200_000,
                       "capacity_sats": 2_000_000, "estimated_rebalance_cost_sats": 400}]
        engine, actions, _ = make_engine(base_config, {
            "mempool": {"fee_rate_sat_vbyte": 8},
            "lndg": {"imbalanced_channels": imbalanced, "routing_revenue_7d": {"111x1x0": 500}},
        })
        engine.run_cycle()
        actions["rebalance"].execute.assert_not_called()

    def test_zero_revenue_channel_not_rebalanced(self, base_config):
        imbalanced = [{"chan_id": "111x1x0", "peer_alias": "DeadEarner", "peer_pubkey": "02aaa",
                       "local_balance_pct": 10.0, "local_balance_sats": 200_000,
                       "capacity_sats": 2_000_000, "estimated_rebalance_cost_sats": 1}]
        engine, actions, _ = make_engine(base_config, {
            "mempool": {"fee_rate_sat_vbyte": 8},
            "lndg": {"imbalanced_channels": imbalanced, "routing_revenue_7d": {}},
        })
        engine.run_cycle()
        actions["rebalance"].execute.assert_not_called()


class TestFeePolicyRule:

    def test_normal_fee_when_not_congested(self, base_config):
        engine, actions, _ = make_engine(base_config, {"mempool": {"mempool_congested": False}})
        engine.run_cycle()
        call_data = actions["fee_policy"].execute.call_args[0][0]
        assert call_data["target_ppm"] == 100
        assert call_data["congested"] is False

    def test_elevated_fee_when_congested(self, base_config):
        engine, actions, _ = make_engine(base_config, {"mempool": {"mempool_congested": True}})
        engine.run_cycle()
        call_data = actions["fee_policy"].execute.call_args[0][0]
        assert call_data["target_ppm"] == 150
        assert call_data["congested"] is True


class TestZombieChannelRule:

    def test_zombie_with_low_uptime_queued_for_approval(self, base_config):
        dead = [{"chan_id": "333x1x0", "peer_alias": "ZombiePeer", "peer_pubkey": "02ccc",
                 "capacity_sats": 1_000_000, "local_balance_sats": 500_000, "days_no_routing": 30}]
        engine, _, approval = make_engine(base_config, {
            "lndg": {"dead_channels": dead},
            "thunderhub": {"peer_uptime": {"02ccc": 8.0}},
        })
        engine.run_cycle()
        sent = [call[0][0] for call in approval.send_request.call_args_list]
        zombie_recs = [d for d in sent if d.rule == "zombie_channel"]
        assert len(zombie_recs) == 1
        assert zombie_recs[0].action_type == "approval_required"

    def test_zombie_with_good_uptime_not_flagged(self, base_config):
        dead = [{"chan_id": "333x1x0", "peer_alias": "QuietButReliable", "peer_pubkey": "02ccc",
                 "capacity_sats": 1_000_000, "local_balance_sats": 500_000, "days_no_routing": 30}]
        engine, _, approval = make_engine(base_config, {
            "lndg": {"dead_channels": dead},
            "thunderhub": {"peer_uptime": {"02ccc": 95.0}},
        })
        engine.run_cycle()
        sent = [call[0][0] for call in approval.send_request.call_args_list]
        assert not any(d.rule == "zombie_channel" for d in sent)


class TestSafetyGuarantees:

    def test_channel_close_never_autonomous(self, base_config):
        dead = [{"chan_id": "333x1x0", "peer_alias": "ZombiePeer", "peer_pubkey": "02ccc",
                 "capacity_sats": 1_000_000, "local_balance_sats": 500_000, "days_no_routing": 30}]
        engine, actions, approval = make_engine(base_config, {
            "lndg": {"dead_channels": dead},
            "thunderhub": {"peer_uptime": {"02ccc": 5.0}},
        })
        engine.run_cycle()
        assert approval.send_request.called

    def test_engine_continues_after_monitor_failure(self, base_config):
        engine, _, _ = make_engine(base_config)
        engine.monitors["mempool"].collect.side_effect = Exception("API down")
        engine.run_cycle()  # must not raise

    def test_engine_continues_after_action_failure(self, base_config):
        imbalanced = [{"chan_id": "111x1x0", "peer_alias": "TestPeer", "peer_pubkey": "02aaa",
                       "local_balance_pct": 10.0, "local_balance_sats": 200_000,
                       "capacity_sats": 2_000_000, "estimated_rebalance_cost_sats": 50}]
        engine, actions, _ = make_engine(base_config, {
            "lndg": {"imbalanced_channels": imbalanced, "routing_revenue_7d": {"111x1x0": 500}},
        })
        actions["rebalance"].execute.side_effect = Exception("LNDg API timeout")
        engine.run_cycle()  # must not raise
