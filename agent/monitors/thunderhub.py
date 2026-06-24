"""
ThunderHub Monitor — reads SCB freshness directly from filesystem.
ThunderHub on Umbrel uses Umbrel SSO cookie — no API key exists.
"""
import logging
import os
from datetime import datetime

log = logging.getLogger("monitor.thunderhub")

SCB_PATHS = [
    "/home/umbrel/umbrel/app-data/lightning/data/lnd/data/chain/bitcoin/mainnet/channel.backup",
    "/root/.lnd/data/chain/bitcoin/mainnet/channel.backup",
]

class ThunderHubMonitor:
    def __init__(self, config: dict):
        pass

    def collect(self) -> dict:
        signals = {
            "peer_uptime": {},
            "inactive_channels": [],
            "scb_age_hours": 0,
        }
        try:
            signals["scb_age_hours"] = self._get_scb_age()
            if signals["scb_age_hours"] > 0:
                log.debug(f"ThunderHub: SCB age {signals['scb_age_hours']:.1f}h")
            else:
                log.warning("ThunderHub: SCB file not found at any known path")
        except Exception as e:
            log.warning(f"ThunderHub: SCB check failed: {e}")
        return signals

    def _get_scb_age(self) -> float:
        for path in SCB_PATHS:
            try:
                if os.path.exists(path):
                    mtime = os.path.getmtime(path)
                    return round((datetime.utcnow().timestamp() - mtime) / 3600, 2)
            except PermissionError:
                log.debug(f"ThunderHub: permission denied reading {path}")
                continue
        return 0
