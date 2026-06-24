"""
Rebalance Action — triggers circular rebalancing via LNDg REST API.
"""

import logging
import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger("action.rebalance")


class RebalanceAction:
    def __init__(self, config: dict, dry_run: bool = True):
        self.base_url = config["endpoints"]["lndg_api"]
        self.auth = HTTPBasicAuth(config["credentials"]["lndg_user"], config["credentials"]["lndg_pass"])
        self.dry_run = dry_run

    def execute(self, data: dict) -> dict:
        peer_alias = data.get("peer_alias", "unknown")
        capacity = data.get("capacity_sats", 0)
        local_sats = data.get("local_balance_sats", 0)
        amount = abs(local_sats - capacity // 2)
        if self.dry_run:
            log.info(f"[DRY RUN] Would rebalance {peer_alias} — move {amount} sats")
            return {"status": "dry_run", "channel": peer_alias, "amount_sats": amount}
        payload = {
            "chan_id": data.get("chan_id"),
            "amt": amount,
            "fee_limit": data.get("estimated_rebalance_cost_sats", 100),
        }
        try:
            r = requests.post(f"{self.base_url}/api/rebalance/", json=payload, auth=self.auth, timeout=30)
            r.raise_for_status()
            log.info(f"Rebalance triggered for {peer_alias}")
            return {"status": "success", "channel": peer_alias}
        except requests.HTTPError as e:
            log.error(f"Rebalance failed for {peer_alias}: {e}")
            return {"status": "failed", "error": str(e)}
