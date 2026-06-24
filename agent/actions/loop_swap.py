"""
Loop Swap Action — triggers Loop In submarine swaps via Lightning Terminal.
"""

import logging
import requests

log = logging.getLogger("action.loop_swap")


class LoopSwapAction:
    def __init__(self, config: dict, dry_run: bool = True):
        self.base_url = config["endpoints"]["lightning_terminal"]
        self.macaroon_path = config["credentials"]["lit_macaroon_path"]
        self.dry_run = dry_run
        self._macaroon_hex = self._load_macaroon()

    def execute(self, data: dict) -> dict:
        peer_alias = data.get("peer_alias", "unknown")
        capacity = data.get("capacity_sats", 0)
        local_sats = data.get("local_balance_sats", 0)
        swap_amount = max(0, int(capacity * 0.4) - local_sats)
        if swap_amount < 10000:
            return {"status": "skipped", "reason": "amount_too_small"}
        if self.dry_run:
            log.info(f"[DRY RUN] Would Loop In {swap_amount} sats for {peer_alias}")
            return {"status": "dry_run", "channel": peer_alias, "amount_sats": swap_amount}
        headers = {"Grpc-Metadata-macaroon": self._macaroon_hex}
        payload = {
            "amt": swap_amount,
            "max_swap_routing_fee": int(swap_amount * 0.005),
            "max_miner_fee": int(swap_amount * 0.002),
            "max_swap_fee": int(swap_amount * 0.005),
        }
        try:
            r = requests.post(f"{self.base_url}/v1/loop/in", json=payload, headers=headers, verify=False, timeout=30)
            r.raise_for_status()
            log.info(f"Loop In initiated for {peer_alias}: {swap_amount} sats")
            return {"status": "success", "channel": peer_alias, "swap_amount": swap_amount}
        except requests.HTTPError as e:
            log.error(f"Loop In failed for {peer_alias}: {e}")
            return {"status": "failed", "error": str(e)}

    def _load_macaroon(self) -> str:
        try:
            with open(self.macaroon_path, "rb") as f:
                return f.read().hex()
        except Exception as e:
            log.warning(f"Could not load macaroon: {e}")
            return ""
