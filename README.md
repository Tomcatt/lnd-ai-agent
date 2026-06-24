# LND AI Agent

An autonomous Lightning Network node management agent for Umbrel/LND.
Handles routing optimization, rebalancing, fee policy, and liquidity management
with a human-in-the-loop approval gate for high-stakes actions.

## Philosophy

- **Automate the routine** — rebalancing, fee adjustments, monitoring
- **Escalate the irreversible** — channel opens/closes, large payments
- **Never spend more than the cap** — LNbits sandbox enforces hard limits
- **Zero external dependencies** — every API call stays inside your Umbrel

## Stack

| Component | Role | Container |
|---|---|---|
| LND | Core node | `lightning_lnd_1` |
| LNDg | Primary automation target | `lndg_web_1` |
| ThunderHub | Data enrichment + dashboard | `thunderhub_web_1` |
| LNbits | Payment sandbox + spending limits | `lnbits_web_1` |
| Alby Hub | Human approval gate (NWC) | `albyhub_server_1` |
| Loop | Liquidity swaps via API | `lightning-terminal_web_1` |
| Faraday | Channel performance analytics | `lightning-terminal_web_1` |
| Mempool | Fee rate + congestion intelligence | `mempool_api_1` |
| ChanTools | Emergency recovery (manual only) | `chantools_chantools_1` |

## Agent Autonomy Levels

### Fully autonomous (no approval needed)
- Circular rebalancing under cost threshold
- Fee policy adjustments within configured bounds
- Mempool fee monitoring and logging
- Channel health reporting
- Loop swaps under liquidity threshold

### Requires human approval (Alby Hub -> your phone)
- Channel open recommendations
- Channel close recommendations
- Payments above configured threshold (default: 100,000 sats)
- Any action the agent flags as uncertain

### Human only (agent generates instructions, you execute)
- ChanTools recovery operations
- RTL manual actions
- Any action requiring seed phrase

## Architecture Overview

```
+-----------------------------------------------------+
|                   Agent Core Loop                    |
|              (runs every 15 minutes)                 |
+------+----------------------------------+------------+
       |                                  |
       v                                  v
+-------------+                   +--------------+
|   Monitors  |                   |   Decision   |
|             |                   |   Engine     |
| - Mempool   | ---- signals ---> |              |
| - LNDg      |                   | - Rebalance? |
| - Faraday   |                   | - Fee change?|
| - ThunderHub|                   | - Swap?      |
+-------------+                   +------+-------+
                                         |
               +--------------------------+---------------------+
               |                          |                     |
               v                          v                     v
        +------------+          +--------------+      +---------------+
        | Autonomous |          |   Approval   |      |    Instruct   |
        |  Actions   |          |    Gate      |      |     Human     |
        |            |          |              |      |               |
        | LNDg API   |          | Alby Hub NWC |      | ChanTools CLI |
        | Loop API   |          | -> your phone|      | RTL manual    |
        | LNbits API |          |              |      |               |
        +------------+          +--------------+      +---------------+
```

## Quick Start

```bash
# 1. Clone to your Umbrel
git clone https://github.com/YOUR_USERNAME/lnd-ai-agent.git

# 2. Copy and edit config
cp config/config.example.yml config/config.yml

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run connection check
python scripts/check_connections.py

# 5. Start agent
python agent/main.py
```

## Directory Structure

```
lnd-ai-agent/
├── agent/
│   ├── core/           # Main loop, scheduler, logger
│   ├── monitors/       # Mempool, LNDg, Faraday, ThunderHub watchers
│   ├── actions/        # Rebalance, fee policy, Loop swap executors
│   ├── approval/       # Alby Hub NWC approval gate
│   └── interfaces/     # API clients for each service
├── config/
│   ├── config.example.yml
│   └── thresholds.yml
├── docs/
│   └── architecture.md
├── scripts/
│   └── check_connections.py
└── tests/
```

## Safety Guarantees

1. Agent operates within a LNbits wallet with a hard daily spending cap
2. No channel open/close without explicit human approval
3. Rebalancing only proceeds if cost < expected routing revenue
4. All actions logged with full reasoning
5. Agent defaults to "do nothing" on any timeout or uncertainty
