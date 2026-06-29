"""
Heartbeat monitor — checks key services every 60 s.
Sends Telegram alert on state changes (down → up, up → down).
Auto-restarts Umbrel UI when port 80 is dead.
"""

import subprocess
import sys
import time
import logging
from pathlib import Path
from datetime import datetime

import requests
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("heartbeat")

_REPO_ROOT = Path(__file__).resolve().parents[2]
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
CHECK_INTERVAL = 60   # seconds
UMBREL_RESTART_COOLDOWN = 300  # don't restart more than once per 5 min


def load_config():
    path = _REPO_ROOT / "config" / "config.yml"
    with open(path) as f:
        return yaml.safe_load(f)


def send_telegram(token, chat_id, text):
    try:
        requests.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def probe(url, timeout=5, **kwargs):
    try:
        r = requests.get(url, timeout=timeout, **kwargs)
        return r.status_code < 500
    except Exception:
        return False


def build_checks(config):
    ep = config["endpoints"]
    creds = config["credentials"]
    from requests.auth import HTTPBasicAuth
    lndg_auth = HTTPBasicAuth(creds["lndg_user"], creds["lndg_pass"])

    mac_path = creds.get("lnd_macaroon_path") or creds.get("lit_macaroon_path", "")
    tls_cert = creds.get("lnd_tls_cert_path", "")
    try:
        mac_hex = Path(mac_path).read_bytes().hex() if mac_path else ""
    except Exception:
        mac_hex = ""

    checks = [
        {
            "name": "Umbrel UI",
            "url": "http://localhost:80",
            "is_umbrel": True,
        },
        {
            "name": "Dashboard",
            "url": "http://localhost:5001",
        },
        {
            "name": "LNDg",
            "url": f"{ep['lndg_api']}/api/channels/?limit=1",
            "kwargs": {"auth": lndg_auth},
        },
        {
            "name": "LND REST",
            "url": f"http://{ep['lnd_grpc'].replace(':10009', '')}:8080/v1/getinfo"
                   if ":" in ep.get("lnd_grpc", "") else None,
            "kwargs": {
                "headers": {"Grpc-Metadata-macaroon": mac_hex},
                "verify": tls_cert or False,
            } if mac_hex else {},
            "skip": not mac_hex,
        },
        {
            "name": "Mempool",
            "url": f"{ep['mempool_api']}/api/v1/fees/recommended",
        },
        {
            "name": "ThunderHub",
            "url": ep.get("thunderhub_graphql", "").replace("/graphql", ""),
        },
    ]
    return [c for c in checks if not c.get("skip") and c.get("url")]


def restart_umbrel():
    log.info("Auto-restarting umbrel service...")
    result = subprocess.run(
        ["sudo", "-n", "/bin/systemctl", "restart", "umbrel"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        log.info("umbrel restarted OK")
        return True
    else:
        log.warning(f"umbrel restart failed: {result.stderr.strip()}")
        return False


def run():
    config = load_config()
    tg_token = config["credentials"].get("telegram_bot_token", "")
    tg_chat = config["credentials"].get("telegram_chat_id", "")

    def alert(text):
        log.info(f"ALERT: {text}")
        if tg_token and tg_chat:
            send_telegram(tg_token, tg_chat, text)

    checks = build_checks(config)
    state = {c["name"]: True for c in checks}   # assume up at start
    last_umbrel_restart = 0

    log.info(f"Heartbeat monitor started — watching {len(checks)} services")
    alert("🟢 <b>Seoul Node Runner</b> — heartbeat monitor started")

    while True:
        config = load_config()   # reload each cycle so credential changes apply
        checks = build_checks(config)

        for check in checks:
            name = check["name"]
            ok = probe(check["url"], **check.get("kwargs", {}))

            was_up = state.get(name, True)

            if not ok and was_up:
                icon = "🔴"
                state[name] = False
                msg = f"{icon} <b>{name} is DOWN</b>\n<i>{datetime.utcnow().strftime('%H:%M UTC')}</i>"
                alert(msg)

                if check.get("is_umbrel"):
                    now = time.time()
                    if now - last_umbrel_restart > UMBREL_RESTART_COOLDOWN:
                        last_umbrel_restart = now
                        restarted = restart_umbrel()
                        status = "Restart triggered ✓" if restarted else "Auto-restart failed — check manually"
                        alert(f"🔄 <b>Umbrel UI</b> — {status}")
                    else:
                        alert("⏳ <b>Umbrel UI</b> — restart cooldown active, skipping")

            elif ok and not was_up:
                state[name] = True
                msg = f"🟢 <b>{name} is back UP</b>\n<i>{datetime.utcnow().strftime('%H:%M UTC')}</i>"
                alert(msg)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Heartbeat monitor stopped")
        sys.exit(0)
