"""
Connection Checker — verifies all Umbrel endpoints are reachable.
Run this before starting the agent.

Usage: python scripts/check_connections.py
"""

import sys
import yaml
import requests
from requests.auth import HTTPBasicAuth
from pathlib import Path


def load_config():
    path = Path("config/config.yml")
    if not path.exists():
        print("ERROR: config/config.yml not found.")
        print("Copy config/config.example.yml to config/config.yml and fill in your values.")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def check(name: str, fn) -> bool:
    try:
        fn()
        print(f"  [OK]   {name}")
        return True
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        return False


def main():
    config = load_config()
    e = config["endpoints"]
    c = config["credentials"]
    print("\nLND AI Agent — Connection Check")
    print("=" * 40)
    results = [
        check("Mempool API", lambda: requests.get(f"{e['mempool_api']}/api/v1/fees/recommended", timeout=5).raise_for_status()),
        check("LNDg API", lambda: requests.get(f"{e['lndg_api']}/api/channels/", auth=HTTPBasicAuth(c["lndg_user"], c["lndg_pass"]), timeout=5).raise_for_status()),
        check("ThunderHub GraphQL", lambda: requests.post(e["thunderhub_graphql"], json={"query": "{ getNode { alias } }"}, headers={"Authorization": f"Bearer {c['thunderhub_token']}"}, timeout=5).raise_for_status()),
        check("LNbits API", lambda: requests.get(f"{e['lnbits_api']}/api/v1/wallet", headers={"X-Api-Key": c["lnbits_agent_wallet_key"]}, timeout=5).raise_for_status()),
        check("Alby Hub API", lambda: requests.get(f"{e['albyhub_api']}/api/info", headers={"Authorization": f"Bearer {c['albyhub_token']}"}, timeout=5).raise_for_status()),
    ]
    print("=" * 40)
    passed = sum(results)
    total = len(results)
    print(f"\nResult: {passed}/{total} connections OK")
    if passed == total:
        print("\nAll good. Set dry_run: false in config/config.yml when ready to go live.")
    else:
        print(f"\n{total - passed} connection(s) failed. Fix config/config.yml before starting.")
        sys.exit(1)


if __name__ == "__main__":
    main()
