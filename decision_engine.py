"""
Decision Engine — applies rules to collected signals and produces actions.
Rules are evaluated in priority order. Each rule returns a Decision object.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

log = logging.getLogger("decision_engine")


@dataclass
class Decision:
    rule: str
    priority: str          # critical / high / medium / low
    action_type: str       # autonomous / approval_required / instruct_human
    summary: str
    data: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class DecisionEngine:
    def __init__(self, config: dict, monitors: dict, actions: dict, approval_gate):
        self.config = config
        self.monitors = monitors
        self.actions = actions
        self.approval_gate = approval_gate
        self.cfg = config  # shorthand

        # Track consecutive rebalance failures per channel
        self._rebalance_failures: dict = {}

    def run_cycle(self):
        log.info("Collecting signals from all monitors...")
        signals = self._collect_signals()

        log.info("Running decision rules...")
        decisions = self._evaluate_rules(signals)

        log.info(f"{len(decisions)} decisions produced. Processing...")
        self._process_decisions(decisions)

    # ─────────────────────────────────────────
    # Signal collection
    # ─────────────────────────────────────────

    def _collect_signals(self) -> dict:
        signals = {}
        for name, monitor in self.monitors.items():
            try:
                signals[name] = monitor.collect()
                log.debug(f"Monitor [{name}] collected {len(signals[name])} signals")
            except Exception as e:
                log.warning(f"Monitor [{name}] failed: {e}. Using empty signals.")
                signals[name] = {}
        return signals

    # ─────────────────────────────────────────
    # Rule evaluation
    # ─────────────────────────────────────────

    def _evaluate_rules(self, signals: dict) -> list[Decision]:
        decisions = []
        rules = [
            self._rule_scb_freshness,
            self._rule_db_corruption,
            self._rule_stuck_funding,
            self._rule_zombie_channels,
            self._rule_rebalancing,
            self._rule_fee_policy,
            self._rule_loop_swap,
        ]
        for rule in rules:
            try:
                result = rule(signals)
                if result:
                    if isinstance(result, list):
                        decisions.extend(result)
                    else:
                        decisions.append(result)
            except Exception as e:
                log.error(f"Rule [{rule.__name__}] raised exception: {e}", exc_info=True)
        return decisions

    # ─────────────────────────────────────────
    # Rules
    # ─────────────────────────────────────────

    def _rule_scb_freshness(self, signals: dict) -> Optional[Decision]:
        scb_age = signals.get("thunderhub", {}).get("scb_age_hours", 0)
        max_age = self.cfg["safety"]["scb_max_age_hours"]
        if scb_age > max_age:
            return Decision(
                rule="scb_freshness",
                priority="critical",
                action_type="approval_required",
                summary=f"Static Channel Backup is {scb_age:.1f} hours old (max: {max_age}h). Immediate attention required.",
                data={"scb_age_hours": scb_age},
            )
        return None

    def _rule_db_corruption(self, signals: dict) -> Optional[Decision]:
        corruption_detected = signals.get("lndg", {}).get("db_corruption_warning", False)
        if corruption_detected:
            return Decision(
                rule="db_corruption",
                priority="critical",
                action_type="instruct_human",
                summary="LND database corruption warning detected. Manual ChanTools intervention required.",
                data={
                    "instructions": [
                        "SSH into Umbrel: ssh umbrel@umbrel.local",
                        "Enter ChanTools: docker exec -it chantools_chantools_1 bash",
                        "Run: chantools compactdb --channeldb /data/.lnd/data/graph/mainnet/channel.db",
                        "If NO ERRORS: docker restart lightning_lnd_1",
                        "If ERRORS: do NOT restart LND — contact support",
                    ]
                },
            )
        return None

    def _rule_stuck_funding(self, signals: dict) -> Optional[Decision]:
        stuck = signals.get("lndg", {}).get("stuck_funding_channels", [])
        decisions = []
        for ch in stuck:
            decisions.append(Decision(
                rule="stuck_funding",
                priority="high",
                action_type="instruct_human",
                summary=f"Channel funding tx stuck for {ch.get('days_pending', '?')} days with peer {ch.get('peer_alias', 'unknown')}",
                data=ch,
            ))
        return decisions if decisions else None

    def _rule_zombie_channels(self, signals: dict) -> Optional[Decision]:
        zombie_days = self.cfg["channel_health"]["zombie_routing_days"]
        uptime_thresh = self.cfg["channel_health"]["close_uptime_threshold_pct"]
        dead = signals.get("lndg", {}).get("dead_channels", [])
        peer_uptimes = signals.get("thunderhub", {}).get("peer_uptime", {})

        decisions = []
        for ch in dead:
            peer_uptime = peer_uptimes.get(ch.get("peer_pubkey"), 100)
            if peer_uptime < uptime_thresh:
                decisions.append(Decision(
                    rule="zombie_channel",
                    priority="medium",
                    action_type="approval_required",
                    summary=f"Zombie channel: {ch.get('peer_alias')} — {zombie_days}d zero routing, {peer_uptime:.0f}% uptime",
                    data={**ch, "peer_uptime_pct": peer_uptime},
                ))
        return decisions if decisions else None

    def _rule_rebalancing(self, signals: dict) -> Optional[Decision]:
        fee_rate = signals.get("mempool", {}).get("fee_rate_sat_vbyte", 999)
        max_fee = self.cfg["rebalancing"]["max_fee_rate_sat_vbyte"]
        if fee_rate > max_fee:
            log.info(f"Rebalancing skipped — fee rate {fee_rate} > max {max_fee} sat/vbyte")
            return None

        imbalanced = signals.get("lndg", {}).get("imbalanced_channels", [])
        revenue = signals.get("lndg", {}).get("routing_revenue_7d", {})
        rebal_cost = signals.get("lndg", {}).get("rebalance_cost_7d", {})
        ratio_max = self.cfg["rebalancing"]["cost_revenue_ratio_max"]

        decisions = []
        for ch in imbalanced:
            ch_id = ch.get("chan_id")
            ch_revenue = revenue.get(ch_id, 0)
            ch_cost = rebal_cost.get(ch_id, 0)

            if ch_revenue == 0:
                log.debug(f"Skipping rebalance for {ch.get('peer_alias')} — zero revenue this week")
                continue

            projected_cost = ch.get("estimated_rebalance_cost_sats", 0)
            if projected_cost > ch_revenue * ratio_max:
                log.info(f"Rebalancing {ch.get('peer_alias')} uneconomic — cost {projected_cost} > {ratio_max * 100:.0f}% of revenue {ch_revenue}")
                continue

            decisions.append(Decision(
                rule="rebalancing",
                priority="low",
                action_type="autonomous",
                summary=f"Rebalance {ch.get('peer_alias')} — local {ch.get('local_balance_pct'):.0f}%",
                data=ch,
            ))
        return decisions if decisions else None

    def _rule_fee_policy(self, signals: dict) -> Optional[Decision]:
        congested = signals.get("mempool", {}).get("mempool_congested", False)
        bump = self.cfg["fees"]["congestion_fee_bump_ppm"]
        base_ppm = self.cfg["fees"]["base_fee_rate_ppm"]

        target_ppm = base_ppm + bump if congested else base_ppm
        return Decision(
            rule="fee_policy",
            priority="low",
            action_type="autonomous",
            summary=f"Set fee rate to {target_ppm} PPM ({'congestion mode' if congested else 'normal mode'})",
            data={"target_ppm": target_ppm, "congested": congested},
        )

    def _rule_loop_swap(self, signals: dict) -> Optional[Decision]:
        fee_rate = signals.get("mempool", {}).get("fee_rate_sat_vbyte", 999)
        max_fee = self.cfg["loop"]["max_fee_rate_sat_vbyte"]
        if fee_rate > max_fee:
            return None

        trigger_pct = self.cfg["loop"]["trigger_below_local_pct"]
        fallback_threshold = self.cfg["rebalancing"]["loop_fallback_after_failures"]
        imbalanced = signals.get("lndg", {}).get("imbalanced_channels", [])

        decisions = []
        for ch in imbalanced:
            if ch.get("local_balance_pct", 100) > trigger_pct:
                continue
            ch_id = ch.get("chan_id")
            failures = self._rebalance_failures.get(ch_id, 0)
            if failures >= fallback_threshold:
                decisions.append(Decision(
                    rule="loop_swap",
                    priority="medium",
                    action_type="autonomous",
                    summary=f"Loop In for {ch.get('peer_alias')} — rebalancing failed {failures}x, local balance {ch.get('local_balance_pct'):.0f}%",
                    data=ch,
                ))
        return decisions if decisions else None

    # ─────────────────────────────────────────
    # Decision processing
    # ─────────────────────────────────────────

    def _process_decisions(self, decisions: list[Decision]):
        for decision in decisions:
            log.info(f"[{decision.priority.upper()}] {decision.rule}: {decision.summary}")

            if decision.action_type == "autonomous":
                self._execute_autonomous(decision)
            elif decision.action_type == "approval_required":
                self._send_for_approval(decision)
            elif decision.action_type == "instruct_human":
                self._instruct_human(decision)

    def _execute_autonomous(self, decision: Decision):
        action_map = {
            "rebalancing": "rebalance",
            "fee_policy": "fee_policy",
            "loop_swap": "loop_swap",
        }
        action_name = action_map.get(decision.rule)
        if action_name and action_name in self.actions:
            try:
                result = self.actions[action_name].execute(decision.data)
                log.info(f"Autonomous action [{decision.rule}] result: {result}")
            except Exception as e:
                log.error(f"Autonomous action [{decision.rule}] failed: {e}")
        else:
            log.warning(f"No action handler for autonomous rule: {decision.rule}")

    def _send_for_approval(self, decision: Decision):
        log.info(f"Sending approval request to Alby Hub: {decision.summary}")
        try:
            self.approval_gate.send_request(decision)
        except Exception as e:
            log.error(f"Failed to send approval request: {e}")

    def _instruct_human(self, decision: Decision):
        instructions = decision.data.get("instructions", [])
        log.warning(f"HUMAN ACTION REQUIRED: {decision.summary}")
        for i, step in enumerate(instructions, 1):
            log.warning(f"  Step {i}: {step}")
