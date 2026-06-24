"""
LNDg Monitor — queries LNDg REST API for channel state, routing revenue,
rebalance history, and health indicators.

Field mapping confirmed against live LNDg API on Umbrel:
- Channels: chan_id, remote_pubkey, alias, capacity, local_balance,
            remote_balance, is_active, is_open, fees_updated,
            total_sent, total_received
- Payments: id, creation_date, value, fee (float sats), status (2=done),
            chan_out, rebal_chan (null=routing, set=rebalance)
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
            channels = self._get_all_channels()
            open_channels = [c for c in channels if c.get("is_open", False)]
            signals["imbalanced_channels"] = self._find_imbalanced(open_channels)
            signals["dead_channels"] = self._find_dead(open_channels)
            signals["stuck_funding_channels"] = self._find_stuck_funding(channels)
            signals["total_channels"] = len(open_channels)
            log.info(f"LNDg: {len(open_channels)} open channels, "
                     f"{len(signals['imbalanced_channels'])} imbalanced, "
                     f"{len(signals['dead_channels'])} dead candidates")
        except Exception as e:
            log.warning(f"LNDg channel fetch failed: {e}")
            signals["imbalanced_channels"] = []
            signals["dead_channels"] = []
            signals["stuck_funding_channels"] = []
            signals["total_channels"] = 0

        try:
            payments = self._get_all_payments()
            signals["routing_revenue_7d"] = self._calc_revenue_7d(payments)
            signals["rebalance_cost_7d"] = self._calc_rebalance_cost_7d(payments)
            total_rev = sum(signals["routing_revenue_7d"].values())
            total_cost = sum(signals["rebalance_cost_7d"].values())
            log.info(f"LNDg: 7d revenue {total_rev:.0f} sats, "
                     f"rebalance cost {total_cost:.0f} sats")
        except Exception as e:
            log.warning(f"LNDg payment fetch failed: {e}")
            signals["routing_revenue_7d"] = {}
            signals["rebalance_cost_7d"] = {}

        signals["db_corruption_warning"] = False
        return signals

    def _get_all_channels(self) -> list:
        results = []
        url = f"{self.base_url}/api/channels/?limit=100"
        while url:
            r = requests.get(url, auth=self.auth, timeout=15)
            r.raise_for_status()
            data = r.json()
            results.extend(data.get("results", []))
            next_url = data.get("next")
            url = next_url if next_url and next_url != url else None
        return results

    def _get_all_payments(self) -> list:
        results = []
        url = f"{self.base_url}/api/payments/?limit=100"
        pages = 0
        while url and pages < 5:
            r = requests.get(url, auth=self.auth, timeout=15)
            r.raise_for_status()
            data = r.json()
            results.extend(data.get("results", []))
            next_url = data.get("next")
            url = next_url if next_url and next_url != url else None
            pages += 1
        return results

    def _find_imbalanced(self, channels: list) -> list:
        imbalanced = []
        for ch in channels:
            capacity = ch.get("capacity", 1) or 1
            local = ch.get("local_balance", 0) or 0
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
            last_active = ch.get("fees_updated")
            if not last_active:
                continue
            try:
                dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                dt_naive = dt.replace(tzinfo=None)
                total_sent = ch.get("total_sent", 0) or 0
                total_received = ch.get("total_received", 0) or 0
                if total_sent == 0 and total_received == 0 and dt_naive < cutoff:
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
        stuck = []
        for ch in channels:
            if not ch.get("is_open", True) and not ch.get("is_active", True):
                local = ch.get("local_balance", 0) or 0
                if local > 0:
                    stuck.append({
                        "chan_id": ch.get("chan_id"),
                        "peer_alias": ch.get("alias", "unknown"),
                        "days_pending": 0,
                    })
        return stuck

    def _calc_revenue_7d(self, payments: list) -> dict:
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(days=7)
        revenue = {}
        for p in payments:
            if p.get("rebal_chan") is not None:
                continue
            if p.get("status") != 2:
                continue
            fee = p.get("fee", 0) or 0
            if fee <= 0:
                continue
            try:
                dt = datetime.fromisoformat(
                    p["creation_date"].replace("Z", "+00:00")
                )
                if dt.replace(tzinfo=None) >= cutoff:
                    chan_id = str(p.get("chan_out", "unknown"))
                    revenue[chan_id] = revenue.get(chan_id, 0) + fee
            except Exception:
                pass
        return revenue

    def _calc_rebalance_cost_7d(self, payments: list) -> dict:
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(days=7)
        costs = {}
        for p in payments:
            if p.get("rebal_chan") is None:
                continue
            if p.get("status") != 2:
                continue
            fee = p.get("fee", 0) or 0
            if fee <= 0:
                continue
            try:
                dt = datetime.fromisoformat(
                    p["creation_date"].replace("Z", "+00:00")
                )
                if dt.replace(tzinfo=None) >= cutoff:
                    chan_id = str(p.get("rebal_chan", "unknown"))
                    costs[chan_id] = costs.get(chan_id, 0) + fee
            except Exception:
                pass
        return costs
