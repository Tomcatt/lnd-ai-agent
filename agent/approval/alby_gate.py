"""
Alby Hub Approval Gate — sends approval requests via Alby Hub REST API.
"""

import logging
import requests
from datetime import datetime, timedelta

log = logging.getLogger("approval.alby")


class AlbyApprovalGate:
    def __init__(self, config: dict):
        self.base_url = config["endpoints"]["albyhub_api"]
        self.token = config["credentials"]["albyhub_token"]
        self.channel_timeout_hours = config["approval"]["channel_action_timeout_hours"]
        self.payment_timeout_minutes = config["approval"]["payment_timeout_minutes"]
        self._pending: dict = {}

    def send_request(self, decision) -> str:
        timeout_hours = self.channel_timeout_hours if "channel" in decision.rule else self.payment_timeout_minutes / 60
        expires_at = datetime.utcnow() + timedelta(hours=timeout_hours)
        payload = {
            "type": decision.rule,
            "priority": decision.priority,
            "created_at": decision.created_at,
            "expires_at": expires_at.isoformat(),
            "summary": decision.summary,
            "data": decision.data,
        }
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        try:
            r = requests.post(f"{self.base_url}/api/notifications", json=payload, headers=headers, timeout=10)
            r.raise_for_status()
            request_id = r.json().get("id", "unknown")
            self._pending[request_id] = {"decision": decision, "expires_at": expires_at}
            log.info(f"Approval request sent — ID: {request_id}")
            return request_id
        except Exception as e:
            log.error(f"Failed to send Alby approval request: {e}")
            log.warning("Defaulting to REJECT — safe state on failure")
            return None

    def check_pending(self) -> list:
        resolved = []
        now = datetime.utcnow()
        for request_id, pending in list(self._pending.items()):
            if now > pending["expires_at"]:
                log.warning(f"Approval {request_id} expired — defaulting to REJECT")
                resolved.append({"request_id": request_id, "outcome": "REJECT", "reason": "timeout"})
                del self._pending[request_id]
        return resolved
