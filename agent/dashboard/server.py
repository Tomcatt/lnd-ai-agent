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
import signal
import yaml
import requests
import concurrent.futures
from pathlib import Path
from datetime import datetime, timezone, timedelta
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


def _lnd_rest_session(config):
    """Return (base_url, headers, verify) for LND REST calls."""
    creds = config["credentials"]
    ep = config["endpoints"]
    mac_path = creds.get("lit_macaroon_path", "")
    tls_cert = creds.get("lnd_tls_cert_path", "") or False
    try:
        mac_hex = Path(mac_path).read_bytes().hex()
    except Exception:
        mac_hex = ""
    lnd_ip = ep["lnd_grpc"].split(":")[0]
    base = f"https://{lnd_ip}:8080"
    headers = {"Grpc-Metadata-macaroon": mac_hex} if mac_hex else {}
    return base, headers, tls_cert


@app.route("/api/node/info")
def api_node_info():
    """LND node summary: sync status, block height, wallet balances, pending HTLCs."""
    config = load_config()
    base, headers, verify = _lnd_rest_session(config)
    if not headers:
        return jsonify({"error": "macaroon not available"}), 503
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            f_info    = ex.submit(requests.get, f"{base}/v1/getinfo",
                                  headers=headers, verify=verify, timeout=8)
            f_onchain = ex.submit(requests.get, f"{base}/v1/balance/blockchain",
                                  headers=headers, verify=verify, timeout=8)
            f_ln      = ex.submit(requests.get, f"{base}/v1/balance/channels",
                                  headers=headers, verify=verify, timeout=8)
            f_pending = ex.submit(requests.get, f"{base}/v1/channels",
                                  headers=headers, verify=verify, timeout=8)

        info    = f_info.result().json()
        onchain = f_onchain.result().json()
        ln      = f_ln.result().json()
        chans   = f_pending.result().json().get("channels", [])

        pending_htlcs = sum(len(c.get("pending_htlcs", [])) for c in chans)
        htlc_sats     = sum(
            int(h.get("amount", 0)) for c in chans for h in c.get("pending_htlcs", [])
        )

        return jsonify({
            "alias":            info.get("alias", ""),
            "pubkey":           info.get("identity_pubkey", ""),
            "block_height":     info.get("block_height", 0),
            "synced_to_chain":  info.get("synced_to_chain", False),
            "synced_to_graph":  info.get("synced_to_graph", False),
            "onchain_confirmed_sats": int(onchain.get("confirmed_balance", 0)),
            "onchain_unconfirmed_sats": int(onchain.get("unconfirmed_balance", 0)),
            "ln_local_sats":    int(ln.get("local_balance", {}).get("sat", 0)),
            "ln_remote_sats":   int(ln.get("remote_balance", {}).get("sat", 0)),
            "pending_htlcs":    pending_htlcs,
            "htlc_sats":        htlc_sats,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 503


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


@app.route("/api/chart")
def api_chart():
    from datetime import timedelta
    config = load_config()
    base = config["endpoints"]["lndg_api"]
    auth = HTTPBasicAuth(config["credentials"]["lndg_user"], config["credentials"]["lndg_pass"])
    mempool_base = config["endpoints"]["mempool_api"]
    try:
        now = datetime.utcnow()
        days = [(now - timedelta(days=i)).date() for i in range(6, -1, -1)]
        labels = [d.strftime("%a %-d") for d in days]
        revenue_by_day = {d: 0.0 for d in days}
        rebalance_by_day = {d: 0.0 for d in days}

        fwd_r = requests.get(f"{base}/api/forwards/?limit=1000", auth=auth, timeout=10).json()
        for f in fwd_r.get("results", []):
            try:
                dt = datetime.fromisoformat(f["forward_date"].replace("Z", "+00:00")).date()
                if dt in revenue_by_day:
                    revenue_by_day[dt] += f.get("fee", 0) or 0
            except Exception:
                pass

        pay_r = requests.get(f"{base}/api/payments/?limit=1000", auth=auth, timeout=10).json()
        for p in pay_r.get("results", []):
            if p.get("rebal_chan") is None or p.get("status") != 2:
                continue
            fee = p.get("fee", 0) or 0
            if fee <= 0:
                continue
            try:
                dt = datetime.fromisoformat(p["creation_date"].replace("Z", "+00:00")).date()
                if dt in rebalance_by_day:
                    rebalance_by_day[dt] += fee
            except Exception:
                pass

        ch_r = requests.get(f"{base}/api/channels/?limit=100", auth=auth, timeout=10).json()
        channel_count = sum(1 for c in ch_r.get("results", []) if c.get("is_open"))

        fee_rate = 0
        try:
            fees = requests.get(f"{mempool_base}/api/v1/fees/recommended", timeout=5).json()
            fee_rate = fees.get("hourFee", 0)
        except Exception:
            pass

        return jsonify({
            "labels": labels,
            "revenue": [round(revenue_by_day[d], 1) for d in days],
            "rebalance": [round(rebalance_by_day[d], 1) for d in days],
            "channel_count": channel_count,
            "fee_rate": fee_rate,
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


@app.route("/api/earnings")
def api_earnings():
    """Per-channel 7-day routing revenue, rebalance cost, and net earnings."""
    config = load_config()
    base = config["endpoints"]["lndg_api"]
    auth = HTTPBasicAuth(config["credentials"]["lndg_user"], config["credentials"]["lndg_pass"])
    try:
        cutoff = datetime.utcnow() - timedelta(days=7)
        channels = {
            c["chan_id"]: {"alias": c.get("alias", c.get("chan_id", "")),
                           "pubkey": c.get("remote_pubkey", ""),
                           "revenue": 0.0, "rebalance": 0.0}
            for c in requests.get(f"{base}/api/channels/?limit=200", auth=auth, timeout=10)
                                  .json().get("results", [])
            if c.get("is_open")
        }

        fwds = requests.get(f"{base}/api/forwards/?limit=2000", auth=auth, timeout=10).json()
        for f in fwds.get("results", []):
            try:
                dt = datetime.fromisoformat(f["forward_date"].replace("Z", "+00:00"))
                if dt.replace(tzinfo=None) < cutoff:
                    continue
                for key in ("chan_id_in", "chan_id_out"):
                    cid = f.get(key)
                    if cid and cid in channels:
                        channels[cid]["revenue"] += (f.get("fee", 0) or 0) / 2
            except Exception:
                pass

        pays = requests.get(f"{base}/api/payments/?limit=1000", auth=auth, timeout=10).json()
        for p in pays.get("results", []):
            if p.get("rebal_chan") is None or p.get("status") != 2:
                continue
            fee = p.get("fee", 0) or 0
            if fee <= 0:
                continue
            try:
                dt = datetime.fromisoformat(p["creation_date"].replace("Z", "+00:00"))
                if dt.replace(tzinfo=None) < cutoff:
                    continue
                cid = str(p["rebal_chan"])
                if cid in channels:
                    channels[cid]["rebalance"] += fee
            except Exception:
                pass

        rows = []
        for cid, d in channels.items():
            rev = round(d["revenue"], 1)
            reb = round(d["rebalance"], 1)
            rows.append({
                "chan_id": cid,
                "alias": d["alias"],
                "pubkey": d["pubkey"],
                "revenue_7d": rev,
                "rebalance_7d": reb,
                "net_7d": round(rev - reb, 1),
            })
        rows.sort(key=lambda r: r["revenue_7d"], reverse=True)
        return jsonify({"channels": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/alerts/history")
def api_alerts_history():
    """Recent Telegram alerts fired by the heartbeat monitor and agent."""
    log_path = Path(_REPO_ROOT) / "agent" / "logs" / "alert_history.json"
    if not log_path.exists():
        return jsonify({"alerts": []})
    try:
        with open(log_path) as f:
            return jsonify(json.load(f))
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
        result = subprocess.run(["pgrep", "-f", "agent\\.main"], capture_output=True, text=True)
        running = result.returncode == 0
        return jsonify({
            "running": running,
            "dry_run": dry_run,
            "cycle_interval_minutes": config["agent"]["cycle_interval_minutes"],
            "mode": "dry-run" if dry_run else "live",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/agent/start", methods=["POST"])
def api_agent_start():
    import subprocess as _sp
    try:
        chk = _sp.run(["pgrep", "-f", "agent\\.main"], capture_output=True, text=True)
        if chk.returncode == 0:
            return jsonify({"ok": True, "message": "Already running"})
        proc = _sp.Popen(
            ["python3", "-m", "agent.main"],
            cwd=_REPO_ROOT,
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
            start_new_session=True,
        )
        return jsonify({"ok": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/agent/stop", methods=["POST"])
def api_agent_stop():
    import subprocess as _sp, signal
    try:
        chk = _sp.run(["pgrep", "-f", "agent\\.main"], capture_output=True, text=True)
        if chk.returncode != 0:
            return jsonify({"ok": True, "message": "Not running"})
        for pid in chk.stdout.strip().split("\n"):
            try:
                os.kill(int(pid.strip()), signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        # Remove immediately from pending so the UI updates without waiting for agent cycle
        approvals_file = Path(_REPO_ROOT) / "agent" / "logs" / "pending_approvals.json"
        if approvals_file.exists():
            with open(approvals_file) as f:
                pending = json.load(f)
            pending["approvals"] = [a for a in pending.get("approvals", []) if a.get("id") != request_id]
            with open(approvals_file, "w") as f:
                json.dump(pending, f, indent=2)
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

    # LND REST needs macaroon header
    try:
        mac_path = config["credentials"]["lit_macaroon_path"]
        tls_cert = config["credentials"].get("lnd_tls_cert_path")
        with open(mac_path, "rb") as fh:
            mac_hex = fh.read().hex()
        lnd_ip = ep["lnd_grpc"].split(":")[0]
        lnd_url = f"https://{lnd_ip}:8080"
    except Exception:
        mac_hex, tls_cert, lnd_url = "", None, ""

    def probe_lnd():
        if not lnd_url:
            return {"ok": False, "ms": None, "error": "macaroon not loaded"}
        try:
            t0 = _time.time()
            r = requests.get(f"{lnd_url}/v1/getinfo",
                             headers={"Grpc-Metadata-macaroon": mac_hex},
                             verify=tls_cert or False, timeout=5)
            ms = round((_time.time() - t0) * 1000)
            alias = r.json().get("alias", "") if r.ok else ""
            return {"ok": r.ok, "ms": ms, "detail": alias or lnd_url}
        except Exception as e:
            return {"ok": False, "ms": None, "error": str(e)[:80]}

    def probe_thunderhub():
        try:
            t0 = _time.time()
            r = requests.post(
                ep["thunderhub_graphql"],
                json={"query": "{ getNodeInfo { alias } }"},
                timeout=5,
            )
            ms = round((_time.time() - t0) * 1000)
            ok = r.ok and "data" in r.json()
            alias = (r.json().get("data") or {}).get("getNodeInfo", {}).get("alias", "") if ok else ""
            return {"ok": ok, "ms": ms, "detail": alias or ep["thunderhub_graphql"]}
        except Exception as e:
            return {"ok": False, "ms": None, "error": str(e)[:80]}

    results = {
        "lnd":        {**probe_lnd(),                                                "label": "LND REST"},
        "lndg":       {**probe(f"{ep['lndg_api']}/api/channels/", auth=auth),       "label": "LNDg",       "detail": ep["lndg_api"]},
        "mempool":    {**probe(f"{ep['mempool_api']}/api/v1/fees/recommended"),      "label": "Mempool",    "detail": ep["mempool_api"]},
        "thunderhub": {**probe_thunderhub(),                                         "label": "ThunderHub"},
        "albyhub":    {**probe(f"{ep['albyhub_api']}/api/version"),                 "label": "Alby Hub",   "detail": ep["albyhub_api"]},
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


_BOOKMARKS_FILE = Path(_REPO_ROOT) / "agent" / "logs" / "bookmarks.json"

def _load_bookmarks() -> dict:
    if _BOOKMARKS_FILE.exists():
        with open(_BOOKMARKS_FILE) as f:
            return json.load(f)
    return {}

def _save_bookmarks(data: dict):
    with open(_BOOKMARKS_FILE, "w") as f:
        json.dump(data, f, indent=2)

@app.route("/api/bookmarks", methods=["GET"])
def api_bookmarks_get():
    try:
        return jsonify({"bookmarks": list(_load_bookmarks().values())})
    except Exception as e:
        return jsonify({"bookmarks": [], "error": str(e)})

@app.route("/api/bookmarks", methods=["POST"])
def api_bookmarks_add():
    peer = request.json
    if not peer or not peer.get("pubkey"):
        return jsonify({"error": "pubkey required"}), 400
    try:
        data = _load_bookmarks()
        peer["bookmarked_at"] = datetime.utcnow().isoformat()
        data[peer["pubkey"]] = peer
        _save_bookmarks(data)
        return jsonify({"ok": True, "count": len(data)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/bookmarks/<pubkey>", methods=["DELETE"])
def api_bookmarks_remove(pubkey):
    try:
        data = _load_bookmarks()
        data.pop(pubkey, None)
        _save_bookmarks(data)
        return jsonify({"ok": True, "count": len(data)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_peer_suggestions_cache: dict = {"peers": [], "ts": 0.0}
_PEER_SUGGESTIONS_TTL = 900  # 15 minutes — peer landscape doesn't change quickly

@app.route("/api/peers/suggestions")
def api_peer_suggestions():
    import time as _t
    from datetime import datetime
    now = _t.time()

    if now - _peer_suggestions_cache["ts"] < _PEER_SUGGESTIONS_TTL:
        return jsonify({"peers": _peer_suggestions_cache["peers"], "cached": True})

    try:
        config = load_config()
        mempool_base = config["endpoints"]["mempool_api"]
        lndg_base    = config["endpoints"]["lndg_api"]
        lnd_ip       = config["endpoints"]["lnd_grpc"].split(":")[0]
        mac_path     = config["credentials"]["lit_macaroon_path"]
        tls_cert     = config["credentials"].get("lnd_tls_cert_path")
        auth         = HTTPBasicAuth(config["credentials"]["lndg_user"],
                                     config["credentials"]["lndg_pass"])

        # Our own pubkey from LND
        with open(mac_path, "rb") as fh:
            mac_hex = fh.read().hex()
        info = requests.get(
            f"https://{lnd_ip}:8080/v1/getinfo",
            headers={"Grpc-Metadata-macaroon": mac_hex},
            verify=tls_cert or False, timeout=8,
        ).json()
        our_pubkey = info.get("identity_pubkey", "")

        # Existing open-channel peers
        chs_r = requests.get(f"{lndg_base}/api/channels/", auth=auth, timeout=10).json()
        our_peers = {c["remote_pubkey"] for c in chs_r.get("results", []) if c.get("is_open")}

        # Top nodes from local mempool (two ranking lists)
        liq  = requests.get(f"{mempool_base}/api/v1/lightning/nodes/rankings/liquidity",    timeout=10).json()
        conn = requests.get(f"{mempool_base}/api/v1/lightning/nodes/rankings/connectivity", timeout=10).json()

        liq_ranks  = {n["publicKey"]: i + 1 for i, n in enumerate(liq)}
        conn_ranks = {n["publicKey"]: i + 1 for i, n in enumerate(conn)}

        all_nodes = {n["publicKey"]: n for n in liq}
        for n in conn:
            if n["publicKey"] not in all_nodes:
                all_nodes[n["publicKey"]] = n

        stale_cutoff = now - 30 * 86400

        results = []
        for pk, n in all_nodes.items():
            if pk in (our_pubkey,) or pk in our_peers:
                continue

            channels     = n.get("channels", 0)
            cap_sats     = n.get("capacity", 0)
            cap_btc      = round(cap_sats / 1e8, 2)
            last_update  = n.get("updatedAt") or 0
            days_ago     = max(0, round((now - last_update) / 86400))
            is_stale     = last_update < stale_cutoff
            liq_rank     = liq_ranks.get(pk)
            conn_rank    = conn_ranks.get(pk)
            in_both      = bool(liq_rank and conn_rank)

            score   = 0
            reasons = []

            # Channel count
            if channels >= 200:
                score += 3; reasons.append(f"{channels:,} channels — highly connected")
            elif channels >= 50:
                score += 2; reasons.append(f"{channels:,} channels — well connected")
            elif channels >= 10:
                score += 1; reasons.append(f"{channels:,} channels")
            else:
                reasons.append(f"Only {channels} channels — lightly connected")

            # Capacity
            if cap_sats >= 5_000_000_000:
                score += 3; reasons.append(f"{cap_btc} BTC total capacity — major hub")
            elif cap_sats >= 500_000_000:
                score += 2; reasons.append(f"{cap_btc} BTC total capacity")
            else:
                score += 1; reasons.append(f"{cap_btc} BTC total capacity — smaller node")

            # Dual ranking bonus
            if in_both:
                score += 2; reasons.append("Top-ranked for both liquidity & connectivity")
            elif liq_rank:
                reasons.append(f"Ranked #{liq_rank} by liquidity")
            else:
                reasons.append(f"Ranked #{conn_rank} by connectivity")

            # Staleness penalty
            if is_stale:
                score -= 3
                reasons.append(f"Not seen in {days_ago} days — may be offline")

            # Location
            country = n.get("country") or {}
            city    = n.get("city")    or {}
            country_en = country.get("en", "") if isinstance(country, dict) else ""
            city_en    = city.get("en", "")    if isinstance(city,    dict) else ""
            location   = ", ".join(filter(None, [city_en, country_en]))

            rec = "recommended" if score >= 6 else "consider" if score >= 3 else "skip"

            results.append({
                "pubkey":       pk,
                "alias":        n.get("alias") or "Unknown",
                "channels":     channels,
                "capacity_btc": cap_btc,
                "capacity_sats": cap_sats,
                "location":     location,
                "iso_code":     n.get("iso_code", ""),
                "liq_rank":     liq_rank,
                "conn_rank":    conn_rank,
                "last_seen_days": days_ago,
                "recommendation": rec,
                "reasons":      reasons,
                "score":        score,
            })

        # Our on-chain balance (for fundability filter)
        our_balance_sats = 0
        try:
            bal = requests.get(
                f"https://{lnd_ip}:8080/v1/balance/blockchain",
                headers={"Grpc-Metadata-macaroon": mac_hex},
                verify=tls_cert or False, timeout=6,
            ).json()
            total = int(bal.get("confirmed_balance", 0))
            reserved = int(bal.get("reserved_balance_anchor_chan", 0))
            our_balance_sats = max(0, total - reserved)
        except Exception:
            pass

        # Add avg channel size (proxy for min channel requirement)
        for r in results:
            r["avg_channel_sats"] = int(r["capacity_sats"] / max(1, r["channels"]))

        _ORDER = {"recommended": 0, "consider": 1, "skip": 2}
        results.sort(key=lambda x: (_ORDER[x["recommendation"]], -x["score"]))
        results = results[:50]

        _peer_suggestions_cache.update({"peers": results, "ts": now})
        return jsonify({"peers": results, "our_balance_sats": our_balance_sats, "cached": False})

    except Exception as e:
        if _peer_suggestions_cache["ts"] > 0:
            return jsonify({"peers": _peer_suggestions_cache["peers"], "cached": True, "stale": True})
        return jsonify({"error": str(e), "peers": []}), 503


# ── LN+ Pool Browser ─────────────────────────────────────────────────────────
OUR_MAX_CHANNEL_SAT = 2_000_000

_pool_cache: dict = {"nodes": [], "stats": {}, "ts": 0.0}
_POOL_TTL = 21600  # 6 hours


def _lnplus_get_node(pubkey: str) -> dict:
    try:
        r = requests.get(
            f"https://lightningnetwork.plus/api/2/get_node/pubkey={pubkey}",
            timeout=8,
        )
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _score_pool_node(node: dict, our_pubkey: str, our_peers: set):
    pubkey = node.get("pubkey", "")
    if pubkey == our_pubkey:
        return None, 0, [], "Unknown"

    a1 = (node.get("address_1") or "").lower()
    a2 = (node.get("address_2") or "").lower()
    has_clearnet = bool(a1 and ".onion" not in a1 and "@" in a1)
    has_tor = ".onion" in a1 or ".onion" in a2
    conn = "Both" if (has_clearnet and has_tor) else ("Clearnet" if has_clearnet else ("Tor" if has_tor else "Unknown"))

    if pubkey in our_peers:
        return "red", -50, ["Already your peer"], conn

    if node.get("banned"):
        return "red", -100, ["Banned by admin"], conn
    if node.get("inactive"):
        return "red", -100, ["Marked inactive"], conn

    cap = node.get("capacity_sats") or node.get("capacity") or 0
    channels = node.get("channels_count") or node.get("open_channels") or 0
    min_ch = node.get("min_channel_size") or 0

    if cap == 0:
        return "red", -50, ["Zero capacity"], conn
    if channels == 0:
        return "red", -50, ["No public channels"], conn
    if min_ch > OUR_MAX_CHANNEL_SAT:
        return "red", -50, [f"Min channel {min_ch:,} sat — exceeds our 2M max"], conn

    pos = node.get("positive_ratings_count") or node.get("lnp_positive_ratings_received") or 0
    neg = node.get("negative_ratings_count") or node.get("lnp_negative_ratings_received") or 0
    total = pos + neg
    happy = round(pos / total * 100) if total > 0 else None
    if happy is not None and happy < 50:
        return "red", -50, [f"Only {happy}% happy ({total} ratings)"], conn

    last_seen_str = node.get("last_seen_at") or node.get("lnp_updated_at") or ""
    hours_ago = None
    if last_seen_str:
        try:
            dt = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
            hours_ago = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            pass
    if hours_ago is not None and hours_ago > 168:
        return "red", -50, [f"Last seen {int(hours_ago/24)}d ago"], conn

    score = 0
    reasons = []

    if hours_ago is not None:
        if hours_ago < 24:
            score += 3; reasons.append(f"Online recently ({int(hours_ago)}h ago)")
        elif hours_ago < 168:
            score += 1; reasons.append(f"Last seen {int(hours_ago/24)}d ago")

    if happy is not None and total >= 2:
        if happy >= 80 and total >= 5:
            score += 3; reasons.append(f"{happy}% happy · {total} ratings")
        elif happy >= 60:
            score += 1; reasons.append(f"{happy}% happy · {total} ratings")
        else:
            reasons.append(f"{happy}% happy · {total} ratings")
    else:
        reasons.append("New node — no rating history")

    if channels >= 10:
        score += 2; reasons.append(f"{channels:,} public channels")
    elif channels >= 3:
        score += 1; reasons.append(f"{channels} public channels")
    else:
        reasons.append(f"Only {channels} channels")

    if has_clearnet and has_tor:
        score += 2; reasons.append("Dual-stack clearnet + Tor")
    elif has_clearnet:
        score += 1; reasons.append("Clearnet only")
    elif has_tor:
        reasons.append("Tor only")

    rank = node.get("lnplus_rank_number") or node.get("lnp_rank") or 0
    rank_name = node.get("lnplus_rank_name") or node.get("lnp_rank_name") or ""
    if rank >= 7:
        score += 2; reasons.append(f"LN+ rank {rank} / {rank_name} — elite")
    elif rank >= 5:
        score += 1; reasons.append(f"LN+ rank {rank} / {rank_name}")
    elif rank:
        reasons.append(f"LN+ rank {rank} / {rank_name}")

    if min_ch and min_ch <= 500_000:
        score += 1; reasons.append(f"Min channel {min_ch:,} sat — fits our range")
    elif min_ch:
        reasons.append(f"Min channel {min_ch:,} sat")

    rec = "green" if score >= 8 else "amber" if score >= 3 else "red"
    return rec, score, reasons, conn


@app.route("/api/pool")
def api_pool():
    now = _time.time()
    if now - _pool_cache["ts"] < _POOL_TTL:
        return jsonify({"nodes": _pool_cache["nodes"], "stats": _pool_cache["stats"], "cached": True})

    try:
        config = load_config()
        lnd_ip = config["endpoints"]["lnd_grpc"].split(":")[0]
        mac_path = config["credentials"]["lit_macaroon_path"]
        tls_cert = config["credentials"].get("lnd_tls_cert_path")
        lndg_base = config["endpoints"]["lndg_api"]
        lndg_auth = HTTPBasicAuth(config["credentials"]["lndg_user"],
                                  config["credentials"]["lndg_pass"])

        with open(mac_path, "rb") as fh:
            mac_hex = fh.read().hex()

        info = requests.get(
            f"https://{lnd_ip}:8080/v1/getinfo",
            headers={"Grpc-Metadata-macaroon": mac_hex},
            verify=tls_cert or False, timeout=8,
        ).json()
        our_pubkey = info.get("identity_pubkey", "")

        chs = requests.get(f"{lndg_base}/api/channels/", auth=lndg_auth, timeout=10).json()
        our_peers = {c["remote_pubkey"] for c in chs.get("results", []) if c.get("is_open")}

        swaps = requests.get("https://lightningnetwork.plus/api/2/get_swaps", timeout=15).json()

        # Collect unique participants from all swaps
        seen: dict[str, dict] = {}
        for swap in (swaps if isinstance(swaps, list) else []):
            for p in swap.get("participants", []):
                pk = p.get("pubkey")
                if pk and pk not in seen:
                    seen[pk] = p

        # Enrich with get_node in parallel (for min_channel_size + full ratings)
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
            enriched = dict(ex.map(lambda pk: (pk, _lnplus_get_node(pk)), seen.keys()))

        results = []
        for pk, base in seen.items():
            node = {**base, **(enriched.get(pk) or {})}
            rec, score, reasons, conn = _score_pool_node(node, our_pubkey, our_peers)
            if rec is None:
                continue

            pos = node.get("positive_ratings_count") or node.get("lnp_positive_ratings_received") or 0
            neg = node.get("negative_ratings_count") or node.get("lnp_negative_ratings_received") or 0
            total_r = pos + neg
            happy = round(pos / total_r * 100) if total_r > 0 else None
            cap_sats = node.get("capacity_sats") or node.get("capacity") or 0
            channels = node.get("channels_count") or node.get("open_channels") or 0
            rank = node.get("lnplus_rank_number") or node.get("lnp_rank") or 0
            rank_name = node.get("lnplus_rank_name") or node.get("lnp_rank_name") or ""
            min_ch = node.get("min_channel_size") or 0
            last_seen = node.get("last_seen_at") or node.get("lnp_updated_at") or ""

            results.append({
                "pubkey": pk,
                "alias": node.get("alias") or "Unknown",
                "avatar": node.get("avatar") or "",
                "capacity_btc": round(cap_sats / 1e8, 3),
                "capacity_sats": cap_sats,
                "channels": channels,
                "happy_pct": happy,
                "ratings_total": total_r,
                "min_channel_sat": min_ch,
                "rank": rank,
                "rank_name": rank_name,
                "conn_type": conn,
                "last_seen_at": last_seen,
                "recommendation": rec,
                "score": score,
                "reasons": reasons,
                "lnplus_url": f"https://lightningnetwork.plus/nodes/{pk}",
                "amboss_url": f"https://amboss.space/node/{pk}",
            })

        results.sort(key=lambda x: ({"green": 0, "amber": 1, "red": 2}[x["recommendation"]], -x["score"]))
        stats = {k: sum(1 for r in results if r["recommendation"] == k) for k in ("green", "amber", "red")}
        stats["total"] = len(results)

        _pool_cache.update({"nodes": results, "stats": stats, "ts": now})
        return jsonify({"nodes": results, "stats": stats, "cached": False})

    except Exception as e:
        if _pool_cache["ts"] > 0:
            return jsonify({"nodes": _pool_cache["nodes"], "stats": _pool_cache["stats"], "cached": True, "stale": True})
        return jsonify({"error": str(e), "nodes": [], "stats": {}}), 503


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
