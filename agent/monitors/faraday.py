"""
Faraday Monitor — queries Faraday via Lightning Terminal REST proxy.
"""

import logging
import requests

log = logging.getLogger("monitor.faraday")


class FaradayMonitor:
    def __init__(self, config: dict):
        self.base_url = config["endpoints"]["lightning_terminal"]
        self.macaroon_path = config["credentials"]["lit_macaroon_path"]
        self._macaroon_hex = self._load_macaroon()

    def collect(self) -> dict:
        signals = {}
        try:
            report = self._get_channel_insights()
            signals["channel_revenue_per_sat"] = report.get("revenue_per_sat", {})
            signals["underperforming_channels"] = report.get("underperforming", [])
            signals["faraday_close_recommendations"] = report.get("close_recommendations", [])
        except Exception as e:
            log.warning(f"Faraday query failed: {e}. Skipping this cycle.")
            signals["channel_revenue_per_sat"] = {}
            signals["underperforming_channels"] = []
            signals["faraday_close_recommendations"] = []
        return signals

    def _load_macaroon(self) -> str:
        try:
            with open(self.macaroon_path, "rb") as f:
                return f.read().hex()
        except Exception as e:
            log.warning(f"Could not load macaroon: {e}")
            return ""

    def _get_channel_insights(self) -> dict:
        headers = {"Grpc-Metadata-macaroon": self._macaroon_hex}
        r = requests.get(
            f"{self.base_url}/v1/faraday/insights",
            headers=headers,
            verify=False,
            timeout=20,
        )
        r.raise_for_status()
        raw = r.json()
        revenue_per_sat = {}
        underperforming = []
        close_recs = []
        for ch in raw.get("channel_insights", []):
            chan_id = ch.get("chan_point")
            revenue = ch.get("fees_earned_msat", 0) / 1000
            capacity = ch.get("capacity", 1)
            rps = revenue / capacity if capacity else 0
            revenue_per_sat[chan_id] = round(rps, 6)
            if rps < 0.0001:
                underperforming.append({"chan_id": chan_id, "revenue_per_sat": rps, "capacity": capacity})
            if ch.get("close_recommendation"):
                close_recs.append(chan_id)
        return {"revenue_per_sat": revenue_per_sat, "underperforming": underperforming, "close_recommendations": close_recs}
