"""
Alby Hub Approval Gate — writes pending approvals to disk
so the dashboard APPROVE/REJECT/SNOOZE buttons actually work.

Flow:
1. Agent calls send_request(decision)
2. Written to agent/logs/pending_approvals.json
3. Dashboard reads file, shows buttons
4. User clicks → Flask POSTs to approval_responses.json
5. Agent reads responses next cycle and acts
"""

import json
import logging
import requests
import uuid
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("approval.alby")

APPROVALS_FILE = Path("agent/logs/pending_approvals.json")
RESPONSES_FILE = Path("agent/logs/approval_responses.json")


class AlbyApprovalGate:
    def __init__(self, config: dict):
        self.base_url = config["endpoints"]["albyhub_api"]
        self.token = config["credentials"].get("albyhub_token", "")
        self.channel_timeout_hours = config["approval"]["channel_action_timeout_hours"]
        self.payment_timeout_minutes = config["approval"]["payment_timeout_minutes"]

    def send_request(self, decision) -> str:
        timeout_hours = (
            self.channel_timeout_hours
            if "channel" in decision.rule or "zombie" in decision.rule
            else self.payment_timeout_minutes / 60
        )
        expires_at = datetime.utcnow() + timedelta(hours=timeout_hours)
        request_id = str(uuid.uuid4())[:8]

        approval = {
            "id": request_id,
            "rule": decision.rule,
            "priority": decision.priority,
            "summary": decision.summary,
            "meta": self._format_meta(decision.data),
            "created_at": decision.created_at,
            "expires_at": expires_at.strftime("%Y-%m-%d %H:%M UTC"),
            "data": decision.data,
        }

        self._write_approval(approval)
        self._notify_alby(approval)
        log.info(f"Approval request {request_id} written: {decision.summary}")
        return request_id

    def check_pending(self) -> list:
        resolved = []
        now = datetime.utcnow()
        pending = self._read_approvals()
        responses = self._read_responses()
        remaining = []

        for approval in pending:
            request_id = approval["id"]
            if request_id in responses:
                outcome = responses[request_id]["outcome"]
                log.info(f"Approval {request_id} resolved: {outcome}")
                resolved.append({
                    "request_id": request_id,
                    "outcome": outcome,
                    "decision_rule": approval["rule"],
                    "data": approval["data"],
                })
                continue
            try:
                expires = datetime.strptime(approval["expires_at"], "%Y-%m-%d %H:%M UTC")
                if now > expires:
                    log.warning(f"Approval {request_id} expired — defaulting to REJECT")
                    resolved.append({"request_id": request_id, "outcome": "REJECT",
                                     "reason": "timeout", "data": approval["data"]})
                    continue
            except Exception:
                pass
            remaining.append(approval)

        self._write_approvals_list(remaining)
        return resolved

    def _format_meta(self, data: dict) -> str:
        parts = []
        if "peer_alias" in data:
            parts.append(f"Peer: {data['peer_alias']}")
        if "capacity_sats" in data:
            parts.append(f"Capacity: {data['capacity_sats']:,} sats")
        if "local_balance_sats" in data:
            parts.append(f"Local: {data['local_balance_sats']:,} sats")
        if "peer_uptime_pct" in data:
            parts.append(f"Uptime: {data['peer_uptime_pct']:.0f}%")
        if "days_no_routing" in data:
            parts.append(f"{data['days_no_routing']}d zero routing")
        return " · ".join(parts)

    def _write_approval(self, approval: dict):
        APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = self._read_approvals()
        existing = [a for a in existing if a.get("id") != approval["id"]]
        existing.append(approval)
        with open(APPROVALS_FILE, "w") as f:
            json.dump({"approvals": existing}, f, indent=2)

    def _write_approvals_list(self, approvals: list):
        APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(APPROVALS_FILE, "w") as f:
            json.dump({"approvals": approvals}, f, indent=2)

    def _read_approvals(self) -> list:
        try:
            if APPROVALS_FILE.exists():
                with open(APPROVALS_FILE) as f:
                    return json.load(f).get("approvals", [])
        except Exception:
            pass
        return []

    def _read_responses(self) -> dict:
        try:
            if RESPONSES_FILE.exists():
                with open(RESPONSES_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _notify_alby(self, approval: dict):
        if not self.token:
            return
        try:
            requests.post(
                f"{self.base_url}/api/notifications",
                json={"title": f"[{approval['priority'].upper()}] Agent approval needed",
                      "body": approval["summary"]},
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=5,
            )
        except Exception as e:
            log.debug(f"Alby Hub notification failed (non-fatal): {e}")
