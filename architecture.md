# Architecture Document — LND AI Agent

## Overview

This agent runs as a Python service on Umbrel, communicating exclusively
with containers already present on the node. No external internet calls
are made for core functionality. All decisions are logged with full
reasoning so the human operator can audit what the agent did and why.

---

## 1. Agent Core Loop

The agent runs on a 15-minute cycle by default (configurable).

```
Every cycle:
  1. Collect signals from all monitors
  2. Run decision engine against signals
  3. For each decision:
     a. Autonomous -> execute immediately, log result
     b. Needs approval -> send to Alby Hub, await response
     c. Human instruction -> generate step-by-step guide, notify
  4. Write cycle summary to log
  5. Sleep until next cycle
```

The cycle interval is deliberately conservative. Lightning channels do not
need second-by-second management. 15 minutes catches imbalances quickly
enough while avoiding excessive API chatter.

---

## 2. Monitors

### 2.1 Mempool Monitor
**Container:** `mempool_api_1`
**Endpoints:**
- `GET /api/v1/fees/recommended` — current fee tiers (fastest, halfHour, hour, economy)
- `GET /api/mempool` — mempool size and transaction count

**Signals produced:**
- `fee_rate_sat_vbyte` — current economy fee rate
- `mempool_congested` — boolean, true if mempool > 50MB
- `fee_trend` — rising / falling / stable (compared to last cycle)

**Agent uses:**
- Gate all on-chain actions behind fee threshold
- Adjust routing fee policy when congestion is high
- Pause rebalancing if fees make it uneconomic

---

### 2.2 LNDg Monitor
**Container:** `lndg_web_1`
**Endpoints:**
- `GET /api/channels/` — all channel states, local/remote balance
- `GET /api/payments/` — recent routing events and revenue
- `GET /api/rebalance/` — recent rebalance history and costs
- `GET /api/autopilot/` — current autopilot rule state

**Signals produced:**
- `imbalanced_channels` — list of channels outside 20/80 ratio
- `routing_revenue_7d` — sats earned routing per channel last 7 days
- `rebalance_cost_7d` — sats spent rebalancing per channel last 7 days
- `profitable_channels` — channels where revenue > rebalance cost
- `dead_channels` — channels with zero routing in 30 days

**Agent uses:**
- Primary rebalancing decisions
- Fee policy adjustment targets
- Channel close candidate identification

---

### 2.3 ThunderHub Monitor
**Container:** `thunderhub_web_1`
**Endpoint:** `POST /graphql`

**Queries:**
```graphql
{ getChannels { id partner_public_key local_balance remote_balance
                uptime partner_fee_info { base_fee fee_rate } } }

{ getForwards { created_at fee incoming_channel outgoing_channel } }
```

**Signals produced:**
- `htlc_failures` — channels with recent HTLC failures and reason codes
- `peer_uptime` — uptime percentage per peer last 30 days
- `flow_direction` — net flow direction per channel (inbound vs outbound)
- `scb_age` — age of Static Channel Backup in hours

**Agent uses:**
- Enrich channel close decisions with peer reliability data
- Identify blame for routing failures
- Trigger SCB freshness alert if > 24 hours old

---

### 2.4 Faraday Monitor
**Container:** `lightning-terminal_web_1` (port 8443)

**Signals produced:**
- `channel_revenue_per_sat_deployed` — capital efficiency per channel
- `underperforming_channels` — channels below efficiency threshold
- `recommended_closures` — Faraday's own close suggestions

**Agent uses:**
- Long-term capital efficiency view to complement LNDg's short-term data
- Cross-reference with ThunderHub peer scores before recommending closure

---

## 3. Decision Engine

The decision engine takes all monitor signals and applies rules to produce
a list of decisions each cycle. Rules are evaluated in priority order.

### Rule 1: SCB Freshness
```
IF scb_age > 24 hours:
  ACTION: alert human immediately (Alby notification)
  PRIORITY: critical
```

### Rule 2: Corrupt DB Warning
```
IF lnd_log contains "bbolt" or "corruption" errors:
  ACTION: generate ChanTools compactdb instructions
  ACTION: alert human immediately
  PRIORITY: critical
```

### Rule 3: Stuck Funding Transaction
```
IF channel_state == "pending_open"
AND funding_tx_age > 14 days:
  ACTION: generate ChanTools rescue instructions
  ACTION: alert human
  PRIORITY: high
```

### Rule 4: Zombie Channel Detection
```
IF channel routing_revenue_30d == 0
AND peer_uptime < 10%
AND channel_age > 60 days:
  ACTION: flag as zombie candidate
  ACTION: queue channel close recommendation for human approval
  PRIORITY: medium
```

### Rule 5: Rebalancing
```
IF channel local_balance_pct < 20% OR > 80%:
  IF fee_rate_sat_vbyte < config.max_rebalance_fee_rate:
    IF projected_rebalance_cost < routing_revenue_7d * 0.5:
      ACTION: trigger LNDg circular rebalance (autonomous)
    ELSE:
      ACTION: log "rebalancing uneconomic, skipping"
```

### Rule 6: Fee Policy Adjustment
```
IF mempool_congested == True:
  IF channel_fee_rate < config.congestion_fee_rate:
    ACTION: increase fee rate by config.congestion_fee_bump (autonomous)

IF mempool_congested == False:
  IF channel_fee_rate > config.base_fee_rate:
    ACTION: restore fee rate to base (autonomous)
```

### Rule 7: Liquidity Swap via Loop
```
IF channel local_balance_pct < 10%
AND rebalancing has failed 3 consecutive cycles
AND fee_rate_sat_vbyte < config.max_swap_fee_rate:
  ACTION: trigger Loop In swap (autonomous, within cap)
```

### Rule 8: Channel Open Recommendation
```
IF high_volume_peer detected with insufficient capacity
AND peer_uptime > 90%
AND peer_routing_reliability > 95%:
  ACTION: prepare recommendation with full data
  ACTION: send to human via Alby for approval
```

### Rule 9: Channel Close Recommendation
```
IF channel flagged as zombie (Rule 4)
AND Faraday confirms underperforming
AND ThunderHub peer_uptime < 20%:
  ACTION: prepare close recommendation with full data
  ACTION: send to human via Alby for approval
```

---

## 4. Approval Gate (Alby Hub)

High-stakes decisions are packaged as structured approval requests and
sent via Alby Hub's NWC interface to the operator's phone.

### Approval request format
```json
{
  "type": "channel_close_recommendation",
  "priority": "medium",
  "created_at": "2025-01-15T03:22:00Z",
  "expires_at": "2025-01-15T07:22:00Z",
  "summary": "Close channel with peer XYZ — zombie, 0 routing in 45 days",
  "data": {
    "peer_alias": "SomeNode",
    "peer_pubkey": "02abc...",
    "channel_capacity_sats": 2000000,
    "local_balance_sats": 850000,
    "routing_revenue_30d_sats": 0,
    "peer_uptime_pct": 8,
    "faraday_efficiency_score": 0.02,
    "recommended_action": "cooperative_close"
  },
  "options": ["APPROVE", "REJECT", "SNOOZE_7_DAYS"]
}
```

### Timeout behaviour
- Channel close / open: expires after 4 hours — default REJECT
- Large payment: expires after 15 minutes — default REJECT
- Agent logs timeout and moves on

---

## 5. LNbits Safety Sandbox

The agent operates a dedicated LNbits wallet for all payment actions.

**Wallet setup:**
- Wallet name: `agent-ops`
- Daily spending limit: configured in `config.yml` (default 50,000 sats)
- Separate from personal wallets and routing revenue wallet

**Wallet separation:**
- `agent-ops` — agent operational payments (rebalancing costs, swap fees)
- `routing-revenue` — sweep of routing earnings (read-only for agent)
- Personal wallets — agent has no access

---

## 6. Logging and Audit Trail

Every agent action is logged with:
- Timestamp
- Decision rule that triggered it
- Input signals that informed the decision
- Action taken
- Result (success / failure / pending)
- Cost in sats (if applicable)

Logs are written to `/agent/logs/` and summarised in a daily digest
that appears in the ThunderHub dashboard notes.

---

## 7. Human Instruction Templates

When the agent cannot act autonomously, it generates a structured
instruction card. Example for ChanTools corrupt DB:

```
=== ACTION REQUIRED — Manual step needed ===
Detected: LND database corruption warning in logs
Time: 2025-01-15 03:22 UTC

Steps:
1. SSH into your Umbrel:
   ssh umbrel@umbrel.local

2. Enter the ChanTools container:
   docker exec -it chantools_chantools_1 bash

3. Run the database compaction:
   chantools compactdb \
     --channeldb /data/.lnd/data/graph/mainnet/channel.db

4. If NO ERRORS: restart LND and resume normal operation
   docker restart lightning_lnd_1

5. If ERRORS APPEAR: do NOT restart LND
   Reply ERRORS to this notification and await further instructions

Timeout: This alert does not expire. Do not ignore it.
=========================================
```

---

## 8. Configuration Reference

See `config/config.example.yml` for all parameters with descriptions.

Key parameters:

| Parameter | Default | Description |
|---|---|---|
| `cycle_interval_minutes` | 15 | How often the agent runs |
| `max_rebalance_fee_rate` | 20 | Max sat/vbyte for rebalancing |
| `max_swap_fee_rate` | 15 | Max sat/vbyte to trigger Loop swap |
| `rebalance_cost_threshold` | 0.5 | Max rebalance cost as fraction of 7d revenue |
| `congestion_fee_bump_ppm` | 50 | PPM increase when mempool congested |
| `approval_timeout_channel_hours` | 4 | Hours before channel action expires |
| `approval_timeout_payment_minutes` | 15 | Minutes before payment action expires |
| `daily_spending_cap_sats` | 50000 | LNbits agent wallet daily limit |
| `zombie_days_threshold` | 30 | Days with zero routing before zombie flag |
| `peer_uptime_close_threshold` | 20 | % uptime below which close is considered |
| `scb_max_age_hours` | 24 | Hours before SCB freshness alert fires |
