"""
Fee Policy Action — updates channel fee rates via LNDg REST API.
"""

import logging
import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger("action.fee_policy")


class FeePolicyAction:
    def __init__(self, config: dict, dry_run: bool = True):
        self.base_url = config["endpoints"]["lndg_api"]
        self.auth = HTTPBasicAuth(config["credentials"]["lndg_user"], config["credentials"]["lndg_pass"])
        self.dry_run = dry_run
        self.base_fee_msat = config["fees"]["base_fee_msat"]

    def execute(self, data: dict) -> dict:
        target_ppm = data.get("target_ppm", 100)
        congested = data.get("congested", False)
        if self.dry_run:
            log.info(f"[DRY RUN] Would set fee rate to {target_ppm} PPM ({'congestion' if congested else 'normal'} mode)")
            return {"status": "dry_run", "target_ppm": target_ppm}
        try:
            r = requests.post(
                f"{self.base_url}/api/fees/update/",
                json={"fee_rate": target_ppm, "base_fee": self.base_fee_msat},
                auth=self.auth,
                timeout=15,
            )
            r.raise_for_status()
            log.info(f"Fee policy updated: {target_ppm} PPM")
            return {"status": "success", "target_ppm": target_ppm}
        except requests.HTTPError as e:
            log.error(f"Fee policy update failed: {e}")
            return {"status": "failed", "error": str(e)}
