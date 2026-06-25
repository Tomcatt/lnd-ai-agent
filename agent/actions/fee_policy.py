"""
Fee Policy Action — drives LNDg Auto-Fees (AF) band.

Normal mempool:    AF band 0–500 PPM
Congested mempool: AF band 150–2500 PPM (raises floor and ceiling)
LNDg AF handles per-channel execution within the band.

API: PUT /api/settings/{key}/ with {"value": "..."}
Confirmed keys: AF-MinRate, AF-MaxRate, AF-LowLiqLimit
"""

import logging
import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger("action.fee_policy")


class FeePolicyAction:
    def __init__(self, config: dict, dry_run: bool = True):
        self.base_url = config["endpoints"]["lndg_api"]
        self.auth = HTTPBasicAuth(
            config["credentials"]["lndg_user"],
            config["credentials"]["lndg_pass"],
        )
        self.dry_run = dry_run
        self.base_ppm = config["fees"]["base_fee_rate_ppm"]
        self.bump_ppm = config["fees"]["congestion_fee_bump_ppm"]

    def execute(self, data: dict) -> dict:
        congested = data.get("congested", False)

        if congested:
            af_min = self.base_ppm + self.bump_ppm
            af_max = 2500
            af_low_liq = 10
            mode = "congestion"
        else:
            af_min = 0
            af_max = 500
            af_low_liq = 15
            mode = "normal"

        if self.dry_run:
            log.info(f"[DRY RUN] Would set AF band: "
                     f"min={af_min} max={af_max} "
                     f"low_liq={af_low_liq} ({mode} mode)")
            return {"status": "dry_run", "mode": mode,
                    "af_min": af_min, "af_max": af_max}

        results = {}
        settings = {
            "AF-MinRate": str(af_min),
            "AF-MaxRate": str(af_max),
            "AF-LowLiqLimit": str(af_low_liq),
        }

        for key, value in settings.items():
            try:
                r = requests.put(
                    f"{self.base_url}/api/settings/{key}/",
                    json={"value": value},
                    auth=self.auth, timeout=10,
                )
                r.raise_for_status()
                results[key] = "ok"
                log.info(f"AF setting {key} = {value}")
            except requests.HTTPError as e:
                log.error(f"Failed to set {key}: {e}")
                results[key] = f"failed: {e}"

        success = all(v == "ok" for v in results.values())
        return {
            "status": "success" if success else "partial",
            "mode": mode,
            "af_min": af_min,
            "af_max": af_max,
            "results": results,
        }
