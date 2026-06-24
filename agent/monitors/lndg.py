"""
LNDg Monitor — queries LNDg REST API for channel state, routing revenue,
rebalance history, and health indicators.
"""

import logging
import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger("monitor.lndg")


class LNDgMonitor:
    def __init__(self, config: dict):
        self.base_url = config["endpoints"]["lndg_api"]
        self.auth = HTTPBasicAuth(
            config["credentials"]["lndg_user"],
            config["credentials"]["lndg_pass"],
        )
        self.min_local_pct = config["rebalancing"]["min_local_balance_pct"]
        self.max_local_pct = config["rebalancing"]["max_local_balance_pct"]
        self.zombie_days = config["channel_health"]["zombie_routing_days"]

    def collect(self) -> dict:
        signals = {}
        try:
            channels = self._get_channels()
            signals["imbalanced_channels"] = self._find_imbalanced(channels)
            signals["dead_channels"] = self._find_dead(channels)
            signals["stuck_funding_channels"] = self._find_stuck_funding(channels)
            signals["total_channels"] = len(channels)
        except Exception as e:
            log.warning(f"Failed to fetch channels: {e}")
            signals["imbalanced_channels"] = []
            signals["dead_channels"] = []
        try:
            payments = self._get_payments()
            signals["routing_revenue_7d"] = self._calc_revenue_7d(payments)
            signals["rebalance_cost_7d"] = self._calc_rebalance_cost_7d(payments)
        except Exception as e:
            log.warning(f"Failed to fetch payment data: {e}")
            signals["routing_revenue_7d"] = {}
            signals["rebalance_cost_7d"] = {}
        signals["db_corruption_warning"] = False
        return signals

    def _get_channels(self) -> list:
        r = requests.get(f"{self.base_url}/api/channels/", auth=self.auth, timeout=15)
        r.raise_for_status()
        return r.json().get("results", [])

    def _get_payments(self) -> list:
        r = requests.get(f"{self.base_url}/api/payments/", auth=self.auth, timeout=15)
        r.raise_for_status()
        return r.json().get("results", [])

    def _find_imbalanced(self, channels: list) -> list:
        imbalanced = []
        for ch in channels:
            capacity = ch.get("capacity", 1)
            local = ch.get("local_balance", 0)
            local_pct = (local / capacity) * 100 if capacity else 50
            if local_pct < self.min_local_pct or local_pct > self.max_local_pct:
                imbalanced.append({
                    "chan_id": ch.get("chan_id"),
                    "peer_alias": ch.get("alias", "unknown"),
                    "peer_pubkey": ch.get("remote_pubkey"),
                    "local_balance_pct": round(local_pct, 1),
                    "local_balance_sats": local,
                    "capacity_sats": capacity,
                    "estimated_rebalance_cost_sats": int(abs(local - capacity // 2) * 0.001),
                })
        return imbalanced

    def _find_dead(self, channels: list) -> list:
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(days=self.zombie_days)
        dead = []
        for ch in channels:
            if ch.get("last_forward") is None:
                opened = ch.get("open_date")
                if opened:
                    try:
                        open_dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                        if open_dt.replace(tzinfo=None) < cutoff:
                            dead.append({
                                "chan_id": ch.get("chan_id"),
                                "peer_alias": ch.get("alias", "unknown"),
                                "peer_pubkey": ch.get("remote_pubkey"),
                                "capacity_sats": ch.get("capacity"),
                                "local_balance_sats": ch.get("local_balance"),
                                "days_no_routing": self.zombie_days,
                            })
                    except Exception:
                        pass
        return dead

    def _find_stuck_funding(self, channels: list) -> list:
        from datetime import datetime, timedelta
        stuck = []
        for ch in channels:
            if ch.get("is_active") is False and ch.get("local_balance", 0) == 0:
                pending_since = ch.get("open_date")
                if pending_since:
                    try:
                        dt = datetime.fromisoformat(pending_since.replace("Z", "+00:00"))
                        age_days = (datetime.utcnow() - dt.replace(tzinfo=None)).days
                        if age_days > 14:
                            stuck.append({
                                "chan_id": ch.get("chan_id"),
                                "peer_alias": ch.get("alias", "unknown"),
                                "days_pending": age_days,
                            })
                    except Exception:
                        pass
        return stuck

    def _calc_revenue_7d(self, payments: list) -> dict:
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(days=7)
        revenue = {}
        for p in payments:
            if p.get("is_routing") and p.get("fee_sat", 0) > 0:
                try:
                    dt = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
                    if dt.replace(tzinfo=None) >= cutoff:
                        chan_id = p.get("chan_id_out")
                        revenue[chan_id] = revenue.get(chan_id, 0) + p["fee_sat"]
                except Exception:
                    pass
        return revenue

    def _calc_rebalance_cost_7d(self, payments: list) -> dict:
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(days=7)
        costs = {}
        for p in payments:
            if p.get("is_rebalance") and p.get("fee_sat", 0) > 0:
                try:
                    dt = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
                    if dt.replace(tzinfo=None) >= cutoff:
                        chan_id = p.get("chan_id_in")
                        costs[chan_id] = costs.get(chan_id, 0) + p["fee_sat"]
                except Exception:
                    pass
        return costs
