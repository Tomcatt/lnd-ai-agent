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
    priority: str
    action_type: str
    summary: str
    data: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class DecisionEngine:
    def __init__(self, config: dict, monitors: dict, actions: dict, approval_gate,
                 notifier=None):
        self.config = config
        self.monitors = monitors
        self.actions = actions
        self.approval_gate = approval_gate
        self.notifier = notifier
        self.cfg = config
        self._rebalance_failures: dict = {}

    def run_cycle(self):
        log.info("Collecting signals from all monitors...")
        signals = self._collect_signals()
        log.info("Running decision rules...")
        decisions = self._evaluate_rules(signals)
        log.info(f"{len(decisions)} decisions produced. Processing...")
        self._process_decisions(decisions)

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

    def _evaluate_rules(self, signals: dict) -> list:
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

    def _rule_scb_freshness(self, signals: dict) -> Optional[Decision]:
        scb_age = signals.get("thunderhub", {}).get("scb_age_hours", 0)
        max_age = self.cfg["safety"]["scb_max_age_hours"]
        if scb_age > max_age:
            return Decision(
                rule="scb_freshness",
                priority="critical",
                action_type="approval_required",
                summary=f"SCB is {scb_age:.1f}h old (max: {max_age}h). Immediate attention required.",
                data={"scb_age_hours": scb_age},
            )
        return None

    def _rule_db_corruption(self, signals: dict) -> Optional[Decision]:
        if signals.get("lndg", {}).get("db_corruption_warning", False):
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

    def _rule_stuck_funding(self, signals: dict) -> Optional[list]:
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

    def _rule_zombie_channels(self, signals: dict) -> Optional[list]:
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

    def _rule_rebalancing(self, signals: dict) -> Optional[list]:
        fee_rate = signals.get("mempool", {}).get("fee_rate_sat_vbyte", 999)
        max_fee = self.cfg["rebalancing"]["max_fee_rate_sat_vbyte"]
        if fee_rate > max_fee:
            log.info(f"Rebalancing skipped — fee rate {fee_rate} > max {max_fee} sat/vbyte")
            return None

        imbalanced = signals.get("lndg", {}).get("imbalanced_channels", [])
        revenue = signals.get("lndg", {}).get("routing_revenue_7d", {})
        ratio_max = self.cfg["rebalancing"]["cost_revenue_ratio_max"]
        new_channel_cap = 500  # max sats to spend rebalancing a channel with no revenue history

        log.info(f"Rebalancing: {len(imbalanced)} imbalanced channels, "
                 f"revenue on {len(revenue)} channels")

        skip_pubkeys = set(self.cfg["rebalancing"].get("skip_pubkeys", []))

        decisions = []
        for ch in imbalanced:
            if ch.get("peer_pubkey") in skip_pubkeys:
                log.info(f"Skipping {ch.get('peer_alias')} — in skip_pubkeys list")
                continue

            ch_id = str(ch.get("chan_id", ""))
            ch_revenue = revenue.get(ch_id, 0)
            projected_cost = ch.get("estimated_rebalance_cost_sats", 0)

            log.info(f"Rebalance check {ch.get('peer_alias')}: "
                     f"local {ch.get('local_balance_pct')}%, "
                     f"7d revenue {ch_revenue:.1f} sats, "
                     f"est cost {projected_cost} sats")

            if ch_revenue == 0:
                if projected_cost > new_channel_cap:
                    log.info(f"Skipping {ch.get('peer_alias')} — no revenue, "
                             f"cost {projected_cost} > {new_channel_cap} sat new-channel cap")
                    continue
                log.info(f"Rebalancing {ch.get('peer_alias')} — new channel, "
                         f"cost {projected_cost} sats within {new_channel_cap} sat cap")
            else:
                if projected_cost > ch_revenue * ratio_max:
                    log.info(f"Skipping {ch.get('peer_alias')} — uneconomic, "
                             f"cost {projected_cost} > {ratio_max*100:.0f}% of {ch_revenue:.1f} revenue")
                    continue

            decisions.append(Decision(
                rule="rebalancing",
                priority="low",
                action_type="autonomous",
                summary=f"Rebalance {ch.get('peer_alias')} — local {ch.get('local_balance_pct'):.0f}%",
                data=ch,
            ))
        return decisions if decisions else None

    def _rule_fee_policy(self, signals: dict) -> Decision:
        congested = signals.get("mempool", {}).get("mempool_congested", False)
        bump = self.cfg["fees"]["congestion_fee_bump_ppm"]
        base_ppm = self.cfg["fees"]["base_fee_rate_ppm"]
        target_ppm = base_ppm + bump if congested else base_ppm
        return Decision(
            rule="fee_policy",
            priority="low",
            action_type="autonomous",
            summary=f"Set fee rate to {target_ppm} PPM ({'congestion' if congested else 'normal'} mode)",
            data={"target_ppm": target_ppm, "congested": congested},
        )

    def _rule_loop_swap(self, signals: dict) -> Optional[list]:
        fee_rate = signals.get("mempool", {}).get("fee_rate_sat_vbyte", 999)
        max_fee = self.cfg["loop"]["max_fee_rate_sat_vbyte"]
        if fee_rate > max_fee:
            return None

        trigger_pct = self.cfg["loop"]["trigger_below_local_pct"]
        fallback_threshold = self.cfg["rebalancing"]["loop_fallback_after_failures"]
        imbalanced = signals.get("lndg", {}).get("imbalanced_channels", [])

        skip_pubkeys = set(self.cfg["rebalancing"].get("skip_pubkeys", []))

        decisions = []
        for ch in imbalanced:
            if ch.get("peer_pubkey") in skip_pubkeys:
                continue
            if ch.get("local_balance_pct", 100) > trigger_pct:
                continue
            ch_id = ch.get("chan_id")
            failures = self._rebalance_failures.get(ch_id, 0)
            if failures >= fallback_threshold:
                decisions.append(Decision(
                    rule="loop_swap",
                    priority="medium",
                    action_type="instruct_human",
                    summary=f"Loop In needed: {ch.get('peer_alias')} — rebalancing failed {failures}x, local {ch.get('local_balance_pct'):.0f}%",
                    data={**ch, "failures": failures},
                ))
        return decisions if decisions else None

    def _process_decisions(self, decisions: list):
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
            log.warning(f"No action handler for rule: {decision.rule}")

    def _send_for_approval(self, decision: Decision):
        log.info(f"Sending approval request: {decision.summary}")
        try:
            self.approval_gate.send_request(decision)
        except Exception as e:
            log.error(f"Failed to send approval request: {e}")

    def _instruct_human(self, decision: Decision):
        instructions = decision.data.get("instructions", [])
        log.warning(f"HUMAN ACTION REQUIRED: {decision.summary}")
        for i, step in enumerate(instructions, 1):
            log.warning(f"  Step {i}: {step}")

        if decision.rule == "loop_swap" and self.notifier:
            ch = decision.data
            alias = ch.get("peer_alias", "unknown")
            cap = ch.get("capacity_sats", 0)
            local_pct = ch.get("local_balance_pct", 0)
            local_sats = ch.get("local_balance_sats", 0)
            swap_est = max(0, int(cap * 0.4) - local_sats)
            # Fee estimates: Loop charges ~0.5% swap fee + ~0.2% miner fee + ~0.5% routing
            fee_swap = int(swap_est * 0.005)
            fee_miner = int(swap_est * 0.002)
            fee_routing = int(swap_est * 0.005)
            fee_total = fee_swap + fee_miner + fee_routing
            fee_pct = (fee_total / swap_est * 100) if swap_est else 0
            self.notifier.send(
                title=f"Loop In needed — {alias}",
                message=(
                    f"Channel: {alias}\n"
                    f"Balance: {local_pct:.0f}% local ({local_sats:,} / {cap:,} sat)\n"
                    f"Rebalance failures: {ch.get('failures', '?')}x\n"
                    f"\n"
                    f"Swap amount: ~{swap_est:,} sat\n"
                    f"Est. fees:   ~{fee_total:,} sat ({fee_pct:.1f}%)\n"
                    f"  Swap fee:    {fee_swap:,} sat\n"
                    f"  Miner fee:   {fee_miner:,} sat\n"
                    f"  Routing fee: {fee_routing:,} sat\n"
                    f"\n"
                    f"Action: Lightning Terminal → Loop → Loop In"
                ),
                priority="high",
                tags="zap,warning",
                dedup_key=f"loop_swap_{ch.get('chan_id', alias)}",
                cooldown_hours=24.0,
            )
