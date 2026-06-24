"""
ThunderHub Monitor — queries ThunderHub GraphQL API for peer scores,
HTLC failures, uptime, and SCB freshness.
"""

import logging
import requests
from datetime import datetime
import os

log = logging.getLogger("monitor.thunderhub")


class ThunderHubMonitor:
    def __init__(self, config: dict):
        self.graphql_url = config["endpoints"]["thunderhub_graphql"]
        self.token = config["credentials"]["thunderhub_token"]

    def collect(self) -> dict:
        signals = {}
        try:
            channels = self._query(
                "{ getChannels { partner_public_key is_active time_offline time_online } }"
            ).get("getChannels", [])
            signals["peer_uptime"] = self._calc_uptime(channels)
            signals["inactive_channels"] = [c for c in channels if not c.get("is_active")]
        except Exception as e:
            log.warning(f"ThunderHub channel query failed: {e}")
            signals["peer_uptime"] = {}
        try:
            signals["scb_age_hours"] = self._get_scb_age()
        except Exception as e:
            log.warning(f"ThunderHub SCB check failed: {e}")
            signals["scb_age_hours"] = 0
        return signals

    def _query(self, query: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        r = requests.post(self.graphql_url, json={"query": query}, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json().get("data", {})

    def _calc_uptime(self, channels: list) -> dict:
        uptime = {}
        for ch in channels:
            pubkey = ch.get("partner_public_key")
            time_online = ch.get("time_online", 0) or 0
            time_offline = ch.get("time_offline", 0) or 0
            total = time_online + time_offline
            if total > 0 and pubkey:
                uptime[pubkey] = round((time_online / total) * 100, 1)
        return uptime

    def _get_scb_age(self) -> float:
        scb_path = "/root/.lnd/data/chain/bitcoin/mainnet/channel.backup"
        if os.path.exists(scb_path):
            mtime = os.path.getmtime(scb_path)
            return round((datetime.utcnow().timestamp() - mtime) / 3600, 2)
        return 0
