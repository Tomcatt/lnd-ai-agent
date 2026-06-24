"""
Faraday Monitor — queries Faraday via Lightning Terminal REST proxy.
Gracefully skips if Lightning Terminal is unreachable or macaroon missing.
"""
import logging
import requests

log = logging.getLogger("monitor.faraday")

MACAROON_PATHS = [
    "/home/umbrel/umbrel/app-data/lightning-terminal/data/.faraday/mainnet/faraday.macaroon",
    "/home/umbrel/umbrel/app-data/lightning-terminal/data/.lit/mainnet/lit.macaroon",
    "/home/umbrel/umbrel/app-data/lightning/data/lnd/data/chain/bitcoin/mainnet/admin.macaroon",
    "/home/umbrel/umbrel/app-data/lightning/data/lnd/admin.macaroon",
    "/root/.lnd/data/chain/bitcoin/mainnet/admin.macaroon",
]

class FaradayMonitor:
    def __init__(self, config: dict):
        self.base_url = config["endpoints"]["lightning_terminal"]
        configured = config["credentials"].get("lit_macaroon_path", "")
        self._macaroon_hex = self._load_macaroon(configured)

    def collect(self) -> dict:
        signals = {
            "channel_revenue_per_sat": {},
            "underperforming_channels": [],
            "faraday_close_recommendations": [],
        }
        if not self._macaroon_hex:
            log.warning("Faraday: no macaroon loaded — skipping this cycle")
            return signals
        try:
            report = self._get_channel_insights()
            signals["channel_revenue_per_sat"] = report.get("revenue_per_sat", {})
            signals["underperforming_channels"] = report.get("underperforming", [])
            signals["faraday_close_recommendations"] = report.get("close_recommendations", [])
        except Exception as e:
            log.warning(f"Faraday query failed: {e} — skipping this cycle")
        return signals

    def _load_macaroon(self, configured_path: str) -> str:
        paths = [configured_path] + MACAROON_PATHS if configured_path else MACAROON_PATHS
        for path in paths:
            if not path:
                continue
            try:
                with open(path, "rb") as f:
                    log.info(f"Faraday: loaded macaroon from {path}")
                    return f.read().hex()
            except FileNotFoundError:
                continue
            except PermissionError:
                log.warning(f"Faraday: permission denied reading {path}")
                continue
        log.warning("Faraday: could not load macaroon from any known path")
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
