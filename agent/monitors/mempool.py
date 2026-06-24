"""
Mempool Monitor — queries local Mempool instance for fee rate and congestion.
"""

import logging
import requests

log = logging.getLogger("monitor.mempool")


class MempoolMonitor:
    def __init__(self, config: dict):
        self.base_url = config["endpoints"]["mempool_api"]
        self.congestion_threshold_mb = config["fees"]["congestion_threshold_mb"]

    def collect(self) -> dict:
        signals = {}
        try:
            fees = self._get_fees()
            signals["fee_rate_sat_vbyte"] = fees.get("economyFee", 1)
            signals["fee_rate_fastest"] = fees.get("fastestFee", 1)
            signals["fee_rate_hour"] = fees.get("hourFee", 1)
        except Exception as e:
            log.warning(f"Failed to fetch fee rates: {e}")
            signals["fee_rate_sat_vbyte"] = 999
        try:
            mempool = self._get_mempool()
            size_mb = mempool.get("vsize", 0) / 1_000_000
            signals["mempool_size_mb"] = round(size_mb, 2)
            signals["mempool_congested"] = size_mb > self.congestion_threshold_mb
            signals["mempool_tx_count"] = mempool.get("count", 0)
        except Exception as e:
            log.warning(f"Failed to fetch mempool state: {e}")
            signals["mempool_congested"] = False
        return signals

    def _get_fees(self) -> dict:
        r = requests.get(f"{self.base_url}/api/v1/fees/recommended", timeout=10)
        r.raise_for_status()
        return r.json()

    def _get_mempool(self) -> dict:
        r = requests.get(f"{self.base_url}/api/mempool", timeout=10)
        r.raise_for_status()
        return r.json()
