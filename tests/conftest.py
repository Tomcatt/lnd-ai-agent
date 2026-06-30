"""
conftest.py — shared fixtures for all tests.
Field names match real LNDg API responses confirmed on Umbrel.
"""

import pytest
from datetime import datetime, timedelta


@pytest.fixture
def base_config():
    return {
        "agent": {"cycle_interval_minutes": 15, "log_level": "WARNING", "dry_run": True},
        "endpoints": {
            "lnd_grpc": "lightning_lnd_1:10009",
            "lndg_api": "http://lndg_web_1:8889",
            "thunderhub_graphql": "http://thunderhub_web_1:3000/graphql",
            "lnbits_api": "http://lnbits_web_1:5000",
            "albyhub_api": "http://albyhub_server_1:8080",
            "mempool_api": "http://mempool_api_1:8999",
            "lightning_terminal": "https://lightning-terminal_web_1:8443",
        },
        "credentials": {
            "lndg_user": "test_user", "lndg_pass": "test_pass",
            "thunderhub_token": "test_token",
            "lnbits_agent_wallet_key": "test_invoice_key",
            "lnbits_agent_admin_key": "test_admin_key",
            "albyhub_token": "test_alby_token",
            "lit_macaroon_path": "/nonexistent/macaroon",
            "lnd_tls_cert_path": "/nonexistent/tls.cert",
        },
        "rebalancing": {
            "min_local_balance_pct": 20, "max_local_balance_pct": 80,
            "max_fee_rate_sat_vbyte": 20, "cost_revenue_ratio_max": 0.5,
            "loop_fallback_after_failures": 3,
        },
        "fees": {
            "base_fee_msat": 1000, "base_fee_rate_ppm": 1000,
            "normal_min_ppm": 100,
            "congestion_fee_bump_ppm": 200, "congestion_threshold_mb": 50,
        },
        "loop": {"max_fee_rate_sat_vbyte": 15, "trigger_below_local_pct": 10},
        "approval": {
            "channel_action_timeout_hours": 4, "payment_timeout_minutes": 15,
            "payment_approval_threshold_sats": 100000,
        },
        "channel_health": {
            "zombie_routing_days": 30, "close_uptime_threshold_pct": 20,
            "htlc_failure_weekly_threshold": 3,
        },
        "safety": {
            "daily_spending_cap_sats": 50000, "scb_max_age_hours": 24,
            "retry_cooldown_cycles": 4,
        },
    }


@pytest.fixture
def healthy_channel():
    return {
        "chan_id": "111x1x0", "alias": "GoodPeer", "remote_pubkey": "02aaa",
        "capacity": 2_000_000, "local_balance": 1_000_000, "remote_balance": 1_000_000,
        "is_active": True, "is_open": True,
        "total_sent": 500_000, "total_received": 200_000,
        "fees_updated": (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
    }


@pytest.fixture
def depleted_channel():
    return {
        "chan_id": "222x1x0", "alias": "DepletedPeer", "remote_pubkey": "02bbb",
        "capacity": 2_000_000, "local_balance": 200_000, "remote_balance": 1_800_000,
        "is_active": True, "is_open": True,
        "total_sent": 100_000, "total_received": 0,
        "fees_updated": (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
    }


@pytest.fixture
def zombie_channel():
    return {
        "chan_id": "333x1x0", "alias": "ZombiePeer", "remote_pubkey": "02ccc",
        "capacity": 1_000_000, "local_balance": 500_000, "remote_balance": 500_000,
        "is_active": True, "is_open": True,
        "total_sent": 0, "total_received": 0,
        "fees_updated": (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S"),
    }
