"""
Agent Dashboard Server — Flask app that serves the dashboard UI
and proxies real-time data from LNDg, Mempool, ThunderHub and the agent log.

Run: python agent/dashboard/server.py
Access: http://umbrel.local:5001
"""

import os
import json
import logging
import yaml
import requests
from pathlib import Path
from datetime import datetime
from requests.auth import HTTPBasicAuth
from flask import Flask, jsonify, send_from_directory, request

log = logging.getLogger("dashboard")
app = Flask(__name__, static_folder="static")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))


def load_config():
    path = Path(_REPO_ROOT) / "config" / "config.yml"
    if not path.exists():
        raise FileNotFoundError("config/config.yml not found")
    with open(path) as f:
        return yaml.safe_load(f)


@app.route("/")
def index():
    return send_from_directory(_HERE, "index.html")


@app.route("/api/mempool")
def api_mempool():
    config = load_config()
    base = config["endpoints"]["mempool_api"]
    try:
        fees = requests.get(f"{base}/api/v1/fees/recommended", timeout=5).json()
        mempool = requests.get(f"{base}/api/mempool", timeout=5).json()
        size_mb = round(mempool.get("vsize", 0) / 1_000_000, 2)
        congested = size_mb > config["fees"]["congestion_threshold_mb"]
        return jsonify({
            "economy_fee": fees.get("economyFee", 0),
            "fastest_fee": fees.get("fastestFee", 0),
            "hour_fee": fees.get("hourFee", 0),
            "size_mb": size_mb,
            "tx_count": mempool.get("count", 0),
            "congested": congested,
            "fee_mode": "Congestion" if congested else "Normal",
            "fee_ppm": config["fees"]["base_fee_rate_ppm"] + (
                config["fees"]["congestion_fee_bump_ppm"] if congested else 0
            ),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/channels")
def api_channels():
    config = load_config()
    base = config["endpoints"]["lndg_api"]
    auth = HTTPBasicAuth(config["credentials"]["lndg_user"], config["credentials"]["lndg_pass"])
    try:
        r = requests.get(f"{base}/api/channels/", auth=auth, timeout=10)
        r.raise_for_status()
        channels = r.json().get("results", [])
        result = []
        for ch in channels:
            capacity = ch.get("capacity", 1)
            local = ch.get("local_balance", 0)
            local_pct = round((local / capacity) * 100, 1) if capacity else 50
            if local_pct < 10:
                bal_status = "critical"
            elif local_pct < 20 or local_pct > 80:
                bal_status = "low"
            else:
                bal_status = "ok"
            result.append({
                "chan_id": ch.get("chan_id"),
                "alias": ch.get("alias", "unknown"),
                "pubkey": (ch.get("remote_pubkey", "") or "")[:16] + "...",
                "capacity": capacity,
                "local_balance": local,
                "remote_balance": ch.get("remote_balance", 0),
                "local_pct": local_pct,
                "bal_status": bal_status,
                "is_active": ch.get("is_active", False),
                "fees_earned": ch.get("fees_earned", 0),
            })
        imbalanced = [c for c in result if c["local_pct"] < 20 or c["local_pct"] > 80]
        return jsonify({
            "channels": result,
            "total": len(result),
            "active": sum(1 for c in result if c["is_active"]),
            "imbalanced": len(imbalanced),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/stats")
def api_stats():
    config = load_config()
    base = config["endpoints"]["lndg_api"]
    auth = HTTPBasicAuth(config["credentials"]["lndg_user"], config["credentials"]["lndg_pass"])
    try:
        payments = requests.get(f"{base}/api/payments/", auth=auth, timeout=10).json()
        results = payments.get("results", [])
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=7)
        revenue_7d = 0
        rebalance_cost_7d = 0
        for p in results:
            try:
                dt = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
                if dt.replace(tzinfo=None) >= cutoff:
                    if p.get("is_routing") and p.get("fee_sat", 0) > 0:
                        revenue_7d += p["fee_sat"]
                    if p.get("is_rebalance") and p.get("fee_sat", 0) > 0:
                        rebalance_cost_7d += p["fee_sat"]
            except Exception:
                pass
        cost_ratio = round((rebalance_cost_7d / revenue_7d) * 100, 1) if revenue_7d else 0
        return jsonify({
            "revenue_7d_sats": revenue_7d,
            "rebalance_cost_7d_sats": rebalance_cost_7d,
            "cost_ratio_pct": cost_ratio,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/log")
def api_log():
    n = int(request.args.get("lines", 50))
    log_dir = Path(_REPO_ROOT) / "agent" / "logs"
    try:
        today = datetime.utcnow().strftime("%Y%m%d")
        log_file = log_dir / f"agent_{today}.log"
        if not log_file.exists():
            logs = sorted(log_dir.glob("agent_*.log"), reverse=True)
            if not logs:
                return jsonify({"lines": [], "file": None})
            log_file = logs[0]
        with open(log_file) as f:
            lines = f.readlines()
        parsed = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                parts = line.split(" ", 3)
                level = parts[2].strip("[]")
                message = parts[3] if len(parts) > 3 else line
                display_level = level
                if any(x in message for x in ["Rebalanc", "Loop In", "fee policy"]):
                    display_level = "AUTO"
                elif any(x in message for x in ["Approval", "Alby", "GATE"]):
                    display_level = "GATE"
                parsed.append({
                    "time": f"{parts[0]} {parts[1]}"[11:19],
                    "level": display_level,
                    "message": message,
                })
            except Exception:
                parsed.append({"time": "--:--:--", "level": "INFO", "message": line})
        return jsonify({"lines": parsed, "file": str(log_file)})
    except Exception as e:
        return jsonify({"error": str(e), "lines": []}), 503


@app.route("/api/agent/status")
def api_agent_status():
    try:
        config = load_config()
        dry_run = config["agent"].get("dry_run", True)
        import subprocess
        result = subprocess.run(["pgrep", "-f", "agent/main.py"], capture_output=True, text=True)
        running = result.returncode == 0
        return jsonify({
            "running": running,
            "dry_run": dry_run,
            "cycle_interval_minutes": config["agent"]["cycle_interval_minutes"],
            "mode": "dry-run" if dry_run else "live",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/approvals")
def api_approvals():
    approvals_file = Path(_REPO_ROOT) / "agent" / "logs" / "pending_approvals.json"
    try:
        if approvals_file.exists():
            with open(approvals_file) as f:
                data = json.load(f)
            return jsonify(data)
        return jsonify({"approvals": []})
    except Exception as e:
        return jsonify({"error": str(e), "approvals": []}), 503


@app.route("/api/approvals/<request_id>", methods=["POST"])
def api_approval_response(request_id):
    outcome = request.json.get("outcome")
    if outcome not in ("APPROVE", "REJECT", "SNOOZE_7_DAYS"):
        return jsonify({"error": "Invalid outcome"}), 400
    responses_file = Path(_REPO_ROOT) / "agent" / "logs" / "approval_responses.json"
    try:
        existing = {}
        if responses_file.exists():
            with open(responses_file) as f:
                existing = json.load(f)
        existing[request_id] = {
            "outcome": outcome,
            "responded_at": datetime.utcnow().isoformat(),
        }
        with open(responses_file, "w") as f:
            json.dump(existing, f, indent=2)
        return jsonify({"status": "recorded", "outcome": outcome})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("\nLND Agent Dashboard")
    print("=" * 40)
    print("Access at: http://umbrel.local:5001")
    print("=" * 40)
    app.run(host="0.0.0.0", port=5001, debug=False)
