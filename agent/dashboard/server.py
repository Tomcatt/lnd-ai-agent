"""
Agent Dashboard Server — Flask app that serves the dashboard UI
and proxies real-time data from LNDg, Mempool, ThunderHub and the agent log.

Run: python agent/dashboard/server.py
Access: http://umbrel.local:5001
"""

import os
import re
import json
import time as _time
import logging
import yaml
import requests
from pathlib import Path
from datetime import datetime
from requests.auth import HTTPBasicAuth
from flask import Flask, jsonify, send_from_directory, request

_SCB_PATH = Path("/home/umbrel/umbrel/app-data/lightning/data/lnd/data/chain/bitcoin/mainnet/channel.backup")


def _read_agent_logs(n_files=5):
    """Concatenate lines from the most recent agent log files, oldest first."""
    log_dir = Path(_REPO_ROOT) / "agent" / "logs"
    files = sorted(log_dir.glob("agent_*.log"))[-n_files:]
    lines = []
    for f in files:
        try:
            lines.extend(open(f).readlines())
        except Exception:
            pass
    return lines

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
        mempool = requests.get(f"{base}/api/v1/mempool", timeout=5).json()
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
        for ch in [c for c in channels if c.get("is_open")]:
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
                "full_pubkey": ch.get("remote_pubkey", ""),
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
    from datetime import timedelta
    config = load_config()
    base = config["endpoints"]["lndg_api"]
    auth = HTTPBasicAuth(config["credentials"]["lndg_user"], config["credentials"]["lndg_pass"])
    try:
        cutoff = datetime.utcnow() - timedelta(days=7)

        # Routing revenue: forwarded payments earned by this node
        fwd_r = requests.get(f"{base}/api/forwards/?limit=500", auth=auth, timeout=10).json()
        revenue_7d = 0
        for f in fwd_r.get("results", []):
            try:
                dt = datetime.fromisoformat(f["forward_date"].replace("Z", "+00:00"))
                if dt.replace(tzinfo=None) >= cutoff:
                    revenue_7d += f.get("fee", 0) or 0
            except Exception:
                pass

        # Rebalance cost: circular payments tagged with a destination channel
        pay_r = requests.get(f"{base}/api/payments/?limit=500", auth=auth, timeout=10).json()
        rebalance_cost_7d = 0
        for p in pay_r.get("results", []):
            if p.get("rebal_chan") is None:
                continue
            if p.get("status") != 2:
                continue
            fee = p.get("fee", 0) or 0
            if fee <= 0:
                continue
            try:
                dt = datetime.fromisoformat(p["creation_date"].replace("Z", "+00:00"))
                if dt.replace(tzinfo=None) >= cutoff:
                    rebalance_cost_7d += fee
            except Exception:
                pass

        cost_ratio = round((rebalance_cost_7d / revenue_7d) * 100, 1) if revenue_7d else 0
        return jsonify({
            "revenue_7d_sats": round(revenue_7d, 3),
            "rebalance_cost_7d_sats": round(rebalance_cost_7d, 3),
            "cost_ratio_pct": cost_ratio,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 503


_LOG_SKIP = re.compile(
    r'action\.fee_policy: AF setting |'
    r'main: Cycle interval:|'
    r'decision_engine: Rebalance check '
)

def _humanize(msg):
    """Translate a raw agent log message into plain English. Returns None to drop the line."""
    if _LOG_SKIP.match(msg):
        return None

    # Autonomous action result dicts → plain summary
    if 'Autonomous action' in msg and 'result:' in msg:
        if '[rebalancing]' in msg:
            m = re.search(r"'channel': '(.+?)'", msg)
            alias = m.group(1) if m else '?'
            if "'status': 'success'" in msg:
                shot = "'oneshot': {'status': 'success'" in msg
                return f"✓ Rebalanced {alias}" + (" — one-shot fired" if shot else "")
            return f"✗ Rebalance failed for {alias}"
        if '[fee_policy]' in msg:
            m_mode = re.search(r"'mode': '(\w+)'", msg)
            m_min  = re.search(r"'af_min': (\d+)", msg)
            m_max  = re.search(r"'af_max': (\d+)", msg)
            mode = m_mode.group(1).capitalize() if m_mode else '?'
            mn   = m_min.group(1) if m_min else '?'
            mx   = m_max.group(1) if m_max else '?'
            return f"✓ Fee band set: {mode} mode, {mn}–{mx} PPM"
        return None  # unknown result dict — drop it

    # Cycle boundary markers
    m = re.match(r'scheduler: --- Cycle (\d+) started', msg)
    if m:
        return f"── Cycle {m.group(1)} started ──"
    m = re.match(r'scheduler: --- Cycle (\d+) complete in ([\d.]+)s\. Next in (\d+)s', msg)
    if m:
        mins = round(int(m.group(3)) / 60)
        return f"── Cycle {m.group(1)} done in {m.group(2)}s · next in ~{mins} min ──"

    # Rebalancing actions
    m = re.match(r'action\.rebalance: AR targets updated for (.+?): out=(\d+)% in=(\d+)%', msg)
    if m:
        return f"AR targets → {m.group(1)}: out {m.group(2)}%, in {m.group(3)}%"
    m = re.match(r'action\.rebalance: One-shot rebalance fired for (.+?): (\d+) sats', msg)
    if m:
        return f"One-shot rebalance — {m.group(1)}, {int(m.group(2)):,} sats"
    m = re.match(r'action\.rebalance: Critical imbalance on (.+?) \(([\d.]+)%\)', msg)
    if m:
        return f"{m.group(1)}: critically imbalanced at {m.group(2)}% local — rebalancer firing"

    # Decision engine
    m = re.match(r'decision_engine: Rebalancing (.+?) — (.+)', msg)
    if m:
        return f"Rebalancing {m.group(1)} — {m.group(2)}"
    m = re.match(r'decision_engine: (\d+) decisions produced', msg)
    if m:
        n = int(m.group(1))
        return f"{n} decision{'s' if n != 1 else ''} this cycle"
    m = re.match(r'decision_engine: \[(\w+)\] rebalancing: (.+)', msg)
    if m:
        return f"[{m.group(1)}] {m.group(2)}"
    m = re.match(r'decision_engine: \[(\w+)\] fee_policy: (.+)', msg)
    if m:
        txt = m.group(2)
        mv = re.search(r'(\d+) PPM \((.+?) mode\)', txt)
        return f"Fee rate: {mv.group(1)} PPM ({mv.group(2)} mode)" if mv else f"Fee policy: {txt}"

    # Strip module prefix and return the rest, truncated if needed
    m = re.match(r'[\w.]+: (.+)', msg)
    rest = m.group(1) if m else msg
    return rest if len(rest) <= 110 else rest[:107] + '...'


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
            raw_lines = f.readlines()
        parsed = []
        for line in raw_lines[-(n * 3):]:  # read extra to account for skipped lines
            line = line.strip()
            if not line:
                continue
            try:
                parts = line.split(" ", 3)
                level = parts[2].strip("[]")
                message = parts[3] if len(parts) > 3 else line

                human = _humanize(message)
                if human is None:
                    continue

                # Assign display level from original message content
                display_level = level
                if any(x in message for x in ["rebalanc", "Rebalanc", "fee_policy", "fee policy", "Loop In"]):
                    display_level = "AUTO"
                elif any(x in message for x in ["Approval", "Alby", "alby_gate", "GATE"]):
                    display_level = "GATE"

                # Override based on humanized prefix
                if human.startswith("──"):
                    display_level = "CYCLE"
                elif human.startswith("✓"):
                    display_level = "AUTO"
                elif human.startswith("✗"):
                    display_level = "ERROR"

                parsed.append({
                    "time": f"{parts[0]} {parts[1]}"[11:19],
                    "level": display_level,
                    "message": human,
                })
            except Exception:
                parsed.append({"time": "--:--:--", "level": "INFO", "message": line[:110]})
        return jsonify({"lines": parsed[-n:], "file": str(log_file)})
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


@app.route("/api/channels/detail")
def api_channels_detail():
    config = load_config()
    base = config["endpoints"]["lndg_api"]
    auth = HTTPBasicAuth(config["credentials"]["lndg_user"], config["credentials"]["lndg_pass"])
    try:
        r = requests.get(f"{base}/api/channels/", auth=auth, timeout=10)
        r.raise_for_status()
        channels = [c for c in r.json().get("results", []) if c.get("is_open")]
        result = []
        for ch in channels:
            cap = ch.get("capacity", 1)
            local = ch.get("local_balance", 0)
            local_pct = round((local / cap) * 100, 1) if cap else 50
            result.append({
                "alias": ch.get("alias", "unknown"),
                "chan_id": ch.get("chan_id"),
                "full_pubkey": ch.get("remote_pubkey", ""),
                "capacity": cap,
                "local_balance": local,
                "remote_balance": ch.get("remote_balance", 0),
                "local_pct": local_pct,
                "is_active": ch.get("is_active", False),
                "auto_rebalance": ch.get("auto_rebalance", False),
                "auto_fees": ch.get("auto_fees", False),
                "ar_out_target": ch.get("ar_out_target", 75),
                "ar_in_target": ch.get("ar_in_target", 90),
                "ar_max_cost": ch.get("ar_max_cost", 65),
                "local_fee_rate": ch.get("local_fee_rate", 0),
            })
        return jsonify({"channels": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/connections")
def api_connections():
    config = load_config()
    ep = config["endpoints"]
    auth = HTTPBasicAuth(config["credentials"]["lndg_user"], config["credentials"]["lndg_pass"])

    def probe(url, **kw):
        try:
            t0 = _time.time()
            r = requests.get(url, timeout=5, **kw)
            return {"ok": r.ok, "ms": round((_time.time() - t0) * 1000)}
        except Exception as e:
            return {"ok": False, "ms": None, "error": str(e)[:80]}

    results = {
        "lndg":    {**probe(f"{ep['lndg_api']}/api/channels/", auth=auth),  "label": "LNDg",     "detail": ep["lndg_api"]},
        "mempool": {**probe(f"{ep['mempool_api']}/api/v1/fees/recommended"), "label": "Mempool",  "detail": ep["mempool_api"]},
        "albyhub": {**probe(f"{ep['albyhub_api']}/api/version"),             "label": "Alby Hub", "detail": ep["albyhub_api"]},
    }
    if _SCB_PATH.exists():
        age_h = round((_time.time() - _SCB_PATH.stat().st_mtime) / 3600, 1)
        results["scb"] = {"ok": True, "ms": None, "label": "SCB Backup", "detail": f"{age_h}h old"}
    else:
        results["scb"] = {"ok": False, "ms": None, "label": "SCB Backup", "detail": "File not found"}
    return jsonify(results)


@app.route("/api/safety")
def api_safety():
    config = load_config()
    result = {}
    max_age = config.get("safety", {}).get("scb_max_age_hours", 24)
    if _SCB_PATH.exists():
        age_h = round((_time.time() - _SCB_PATH.stat().st_mtime) / 3600, 1)
        result.update({"scb_age_hours": age_h, "scb_ok": age_h < max_age, "scb_max_age": max_age})
    else:
        result.update({"scb_age_hours": None, "scb_ok": False, "scb_max_age": max_age})
    try:
        base = config["endpoints"]["lndg_api"]
        auth = HTTPBasicAuth(config["credentials"]["lndg_user"], config["credentials"]["lndg_pass"])
        chs = [c for c in requests.get(f"{base}/api/channels/", auth=auth, timeout=10).json().get("results", []) if c.get("is_open")]
        result.update({
            "open_channels": len(chs),
            "total_capacity_sats": sum(c.get("capacity", 0) for c in chs),
            "total_local_sats": sum(c.get("local_balance", 0) for c in chs),
        })
    except Exception:
        result.update({"open_channels": None, "total_capacity_sats": None, "total_local_sats": None})
    return jsonify(result)


@app.route("/api/cyclelog")
def api_cyclelog():
    lines = _read_agent_logs()
    cycles, current = [], None
    for raw in lines:
        line = raw.strip()
        m = re.search(r'scheduler: --- Cycle (\d+) started at (.+) ---', line)
        if m:
            current = {"num": int(m.group(1)), "started": m.group(2)[:19].replace("T", " "), "actions": [], "duration": None}
            continue
        m = re.search(r'scheduler: --- Cycle \d+ complete in ([\d.]+)s', line)
        if m and current:
            current["duration"] = float(m.group(1))
            cycles.append(current)
            current = None
            continue
        if current and any(x in line for x in ["action.rebalance:", "action.fee_policy:", "action.loop:", "alby_gate:"]):
            msg = line.split("] ", 1)[-1]
            current["actions"].append(msg[:120])
    return jsonify({"cycles": list(reversed(cycles[-50:]))})


@app.route("/api/rebalancing/history")
def api_rebalancing_history():
    lines = _read_agent_logs()
    events = []
    for raw in lines:
        line = raw.strip()
        if "action.rebalance:" not in line:
            continue
        try:
            ts = line[:19]
            msg = line.split("action.rebalance:", 1)[1].strip()
            event = {"ts": ts, "msg": msg}
            m = re.search(r'AR targets updated for (.+?): out=(\d+)% in=(\d+)%', msg)
            if m:
                event.update({"type": "ar_update", "alias": m.group(1), "out": int(m.group(2)), "inn": int(m.group(3))})
            m = re.search(r'One-shot rebalance fired for (.+?): (\d+) sats', msg)
            if m:
                event.update({"type": "oneshot", "alias": m.group(1), "sats": int(m.group(2))})
            events.append(event)
        except Exception:
            pass
    return jsonify({"events": list(reversed(events[-100:]))})


@app.route("/api/feepolicy/history")
def api_feepolicy_history():
    lines = _read_agent_logs()
    cycles = []
    for raw in lines:
        line = raw.strip()
        if "fee_policy" not in line or "result:" not in line or "mode" not in line:
            continue
        try:
            ts = line[:19]
            m_mode = re.search(r"'mode': '(\w+)'", line)
            m_min  = re.search(r"'af_min': (\d+)", line)
            m_max  = re.search(r"'af_max': (\d+)", line)
            cycles.append({
                "ts":      ts,
                "mode":    m_mode.group(1) if m_mode else "unknown",
                "min_ppm": int(m_min.group(1)) if m_min else None,
                "max_ppm": int(m_max.group(1)) if m_max else None,
            })
        except Exception:
            pass
    return jsonify({"cycles": list(reversed(cycles[-50:]))})


_lnplus_cache: dict = {"count": 0, "notifications": [], "ts": 0.0}
_LNPLUS_TTL = 240  # seconds (challenge expires after 5 min, we refresh at 4)

@app.route("/api/lndplus/messages")
def api_lndplus_messages():
    """
    Auth flow (per lightningnetwork.plus API v2.3):
      1. GET  /api/2/get_message       → challenge string (valid 5 min)
      2. POST LND REST /v1/signmessage  → zbase32 signature proving node ownership
      3. POST /api/2/get_notifications  → notification_count + list
    No stored token needed — we sign fresh each cache cycle.
    """
    import time, base64
    now = time.time()

    # Serve cached result if still fresh
    if now - _lnplus_cache["ts"] < _LNPLUS_TTL:
        return jsonify({
            "count": _lnplus_cache["count"],
            "notifications": _lnplus_cache["notifications"],
            "configured": True,
        })

    try:
        config = load_config()
        macaroon_path = config["credentials"]["lit_macaroon_path"]
        lnd_ip = config["endpoints"]["lnd_grpc"].split(":")[0]
        lnd_rest = f"https://{lnd_ip}:8080"
        tls_cert = config["credentials"].get("lnd_tls_cert_path")

        with open(macaroon_path, "rb") as fh:
            macaroon_hex = fh.read().hex()

        # Step 1 — get challenge
        msg_r = requests.get(
            "https://lightningnetwork.plus/api/2/get_message", timeout=10
        ).json()
        message = msg_r["message"]

        # Step 2 — sign with LND REST (msg must be base64-encoded)
        msg_b64 = base64.b64encode(message.encode()).decode()
        sign_r = requests.post(
            f"{lnd_rest}/v1/signmessage",
            headers={"Grpc-Metadata-macaroon": macaroon_hex},
            json={"msg": msg_b64},
            verify=tls_cert or False,
            timeout=10,
        ).json()
        signature = sign_r["signature"]

        # Step 3 — fetch notifications
        notif_r = requests.post(
            "https://lightningnetwork.plus/api/2/get_notifications",
            json={"message": message, "signature": signature},
            timeout=10,
        ).json()

        count = notif_r.get("notification_count", 0)
        notifications = notif_r.get("notifications", [])[:20]  # cap list
        _lnplus_cache.update({"count": count, "notifications": notifications, "ts": now})

        return jsonify({"count": count, "notifications": notifications, "configured": True})

    except Exception as e:
        # Return stale cache rather than an error badge
        if _lnplus_cache["ts"] > 0:
            return jsonify({
                "count": _lnplus_cache["count"],
                "notifications": _lnplus_cache["notifications"],
                "configured": True, "stale": True,
            })
        return jsonify({"count": 0, "notifications": [], "error": str(e), "configured": True})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("\nLND Agent Dashboard")
    print("=" * 40)
    print("Access at: http://umbrel.local:5001")
    print("=" * 40)
    app.run(host="0.0.0.0", port=5001, debug=False)
