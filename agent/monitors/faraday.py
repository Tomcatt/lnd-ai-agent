"""
Faraday Monitor — DISABLED.

litd v0.16.1-alpha in --lnd-mode=remote requires a UI password
session (POST /v1/auth/login) before REST endpoints respond.
Managing session cookies adds fragility with minimal benefit —
LNDg already provides the channel efficiency data we need.

Re-enable when Lightning Terminal adds macaroon-only REST access.
"""

import logging

log = logging.getLogger("monitor.faraday")


class FaradayMonitor:
    def __init__(self, config: dict):
        log.info("Faraday monitor disabled — litd requires UI session auth")

    def collect(self) -> dict:
        return {
            "channel_revenue_per_sat": {},
            "underperforming_channels": [],
            "faraday_close_recommendations": [],
        }
