"""
LND AI Agent — Main Entry Point
Runs the core decision loop on a configurable interval.
"""

import time
import logging
import yaml
from pathlib import Path
from datetime import datetime

from agent.core.scheduler import Scheduler
from agent.core.decision_engine import DecisionEngine
from agent.monitors.mempool import MempoolMonitor
from agent.monitors.lndg import LNDgMonitor
from agent.monitors.thunderhub import ThunderHubMonitor
from agent.monitors.faraday import FaradayMonitor
from agent.actions.rebalance import RebalanceAction
from agent.actions.fee_policy import FeePolicyAction
from agent.actions.loop_swap import LoopSwapAction
from agent.approval.alby_gate import AlbyApprovalGate

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def load_config(path: str = "config/config.yml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config not found at {path}. "
            "Copy config/config.example.yml to config/config.yml and fill in your values."
        )
    with open(config_path) as f:
        return yaml.safe_load(f)


def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(f"agent/logs/agent_{datetime.now().strftime('%Y%m%d')}.log"),
        ],
    )


def main():
    config = load_config()
    setup_logging(config["agent"].get("log_level", "INFO"))

    log = logging.getLogger("main")
    dry_run = config["agent"].get("dry_run", True)

    if dry_run:
        log.warning("=" * 60)
        log.warning("DRY RUN MODE — no actions will be executed")
        log.warning("Set dry_run: false in config.yml when ready to go live")
        log.warning("=" * 60)

    log.info("Starting LND AI Agent")
    log.info(f"Cycle interval: {config['agent']['cycle_interval_minutes']} minutes")

    # Initialise monitors
    monitors = {
        "mempool": MempoolMonitor(config),
        "lndg": LNDgMonitor(config),
        "thunderhub": ThunderHubMonitor(config),
        "faraday": FaradayMonitor(config),
    }

    # Initialise actions
    actions = {
        "rebalance": RebalanceAction(config, dry_run=dry_run),
        "fee_policy": FeePolicyAction(config, dry_run=dry_run),
        "loop_swap": LoopSwapAction(config, dry_run=dry_run),
    }

    # Initialise approval gate
    approval_gate = AlbyApprovalGate(config)

    # Initialise decision engine
    engine = DecisionEngine(config, monitors, actions, approval_gate)

    # Initialise scheduler
    scheduler = Scheduler(
        engine=engine,
        interval_minutes=config["agent"]["cycle_interval_minutes"],
    )

    log.info("All components initialised. Starting scheduler.")
    scheduler.run()


if __name__ == "__main__":
    main()
