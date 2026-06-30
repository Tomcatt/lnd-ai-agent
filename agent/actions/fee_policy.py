"""
Fee Policy Action — drives LNDg Auto-Fees (AF) band.

Normal:     AF band min=normal_min_ppm  max=base_fee_rate_ppm
Congested:  AF band min=base+bump       max=base_fee_rate_ppm*2
LNDg AF handles per-channel execution within the band.

Boundary rule: never set individual channel fee rates directly —
that belongs to LNDg AF. Only set the global band here.

API: PUT /api/settings/{key}/ with {"value": "..."}
Confirmed keys: AF-MinRate, AF-MaxRate
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
        self.normal_min = config["fees"]["normal_min_ppm"]
        self.normal_max = config["fees"]["base_fee_rate_ppm"]
        self.bump_ppm = config["fees"]["congestion_fee_bump_ppm"]

    def execute(self, data: dict) -> dict:
        congested = data.get("congested", False)

        if congested:
            af_min = self.normal_min + self.bump_ppm
            af_max = self.normal_max * 2
            mode = "congestion"
        else:
            af_min = self.normal_min
            af_max = self.normal_max
            mode = "normal"

        if self.dry_run:
            log.info(f"[DRY RUN] Would set AF band: "
                     f"min={af_min} max={af_max} ({mode} mode)")
            return {"status": "dry_run", "mode": mode,
                    "af_min": af_min, "af_max": af_max}

        results = {}
        settings = {
            "AF-MinRate": str(af_min),
            "AF-MaxRate": str(af_max),
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
