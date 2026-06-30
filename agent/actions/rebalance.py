"""
Rebalance Action — drives LNDg AR targets only.

Boundary rule: this agent NEVER fires payments directly.
POST /api/rebalancer/ is LNDg's job. We set the targets;
LNDg AR executes on its own schedule.

Strategy:
- Update ar_out_target/ar_in_target via PUT /api/channels/<id>/
- LNDg AR picks up the targets and handles execution
"""

import logging
import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger("action.rebalance")


class RebalanceAction:
    def __init__(self, config: dict, dry_run: bool = True):
        self.base_url = config["endpoints"]["lndg_api"]
        self.auth = HTTPBasicAuth(
            config["credentials"]["lndg_user"],
            config["credentials"]["lndg_pass"],
        )
        self.dry_run = dry_run

    def execute(self, data: dict) -> dict:
        chan_id = data.get("chan_id")
        peer_alias = data.get("peer_alias", "unknown")
        peer_pubkey = data.get("peer_pubkey", "")
        local_pct = data.get("local_balance_pct", 50)
        capacity = data.get("capacity_sats", 0)
        local_sats = data.get("local_balance_sats", 0)

        if local_pct > 80:
            ar_out_target = 50
            ar_in_target = 90
            direction = "push out"
        else:
            ar_out_target = 75
            ar_in_target = 50
            direction = "pull in"

        amt_target = int(capacity * 0.5)

        if self.dry_run:
            log.info(f"[DRY RUN] Would set AR targets for {peer_alias}: "
                     f"out={ar_out_target}% in={ar_in_target}% "
                     f"amt={amt_target} sats ({direction})")
            return {"status": "dry_run", "channel": peer_alias,
                    "ar_out_target": ar_out_target, "ar_in_target": ar_in_target}

        return self._update_channel_ar(
            chan_id, peer_alias, ar_out_target, ar_in_target, amt_target
        )

    def _update_channel_ar(self, chan_id, peer_alias,
                           ar_out_target, ar_in_target, amt_target) -> dict:
        try:
            r = requests.get(
                f"{self.base_url}/api/channels/{chan_id}/",
                auth=self.auth, timeout=10
            )
            r.raise_for_status()
            channel = r.json()
            channel["auto_rebalance"] = True
            channel["ar_out_target"] = ar_out_target
            channel["ar_in_target"] = ar_in_target
            channel["ar_amt_target"] = amt_target
            r2 = requests.put(
                f"{self.base_url}/api/channels/{chan_id}/",
                json=channel, auth=self.auth, timeout=10,
            )
            r2.raise_for_status()
            log.info(f"AR targets updated for {peer_alias}: "
                     f"out={ar_out_target}% in={ar_in_target}%")
            return {"status": "success", "channel": peer_alias,
                    "ar_out_target": ar_out_target, "ar_in_target": ar_in_target}
        except requests.HTTPError as e:
            log.error(f"AR target update failed for {peer_alias}: {e}")
            return {"status": "failed", "error": str(e)}

