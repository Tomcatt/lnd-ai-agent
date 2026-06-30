"""
ntfy push notification sender.

Sends alerts to the user's ntfy topic. Includes per-key deduplication
so a repeating condition (e.g. Loop In candidate) only fires once every
cooldown_hours, not every agent cycle.
"""

import logging
import time
from datetime import datetime

import requests

log = logging.getLogger("notifications.ntfy")

PRIORITY_MAP = {
    "critical": "urgent",
    "high": "high",
    "medium": "default",
    "low": "low",
}


class NtfyNotifier:
    def __init__(self, config: dict):
        ntfy_cfg = config.get("notifications", {})
        server = ntfy_cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        topic = ntfy_cfg.get("ntfy_topic", "")
        self.url = f"{server}/{topic}" if topic else ""
        self._sent: dict[str, float] = {}  # dedup: key → last sent timestamp

    def enabled(self) -> bool:
        return bool(self.url)

    def send(
        self,
        title: str,
        message: str,
        priority: str = "default",
        tags: str = "zap",
        dedup_key: str = "",
        cooldown_hours: float = 24.0,
    ) -> bool:
        if not self.enabled():
            log.debug("ntfy not configured — skipping notification")
            return False

        if dedup_key:
            last = self._sent.get(dedup_key, 0)
            if time.time() - last < cooldown_hours * 3600:
                log.debug(f"ntfy dedup: suppressing '{dedup_key}' (cooldown {cooldown_hours}h)")
                return False

        try:
            requests.post(
                self.url,
                data=message.encode("utf-8"),
                headers={
                    "Title": title,
                    "Priority": priority,
                    "Tags": tags,
                },
                timeout=5,
            )
            if dedup_key:
                self._sent[dedup_key] = time.time()
            log.info(f"ntfy sent: {title}")
            return True
        except Exception as e:
            log.warning(f"ntfy send failed: {e}")
            return False
