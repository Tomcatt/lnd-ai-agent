"""
Scheduler — runs the decision engine on a fixed interval.
Handles graceful shutdown on keyboard interrupt.
"""

import time
import logging
from datetime import datetime

log = logging.getLogger("scheduler")


class Scheduler:
    def __init__(self, engine, interval_minutes: int = 15):
        self.engine = engine
        self.interval_seconds = interval_minutes * 60
        self.cycle_count = 0

    def run(self):
        log.info("Scheduler started. Press Ctrl+C to stop.")
        try:
            while True:
                self.cycle_count += 1
                cycle_start = datetime.utcnow()
                log.info(f"--- Cycle {self.cycle_count} started at {cycle_start.isoformat()} ---")
                try:
                    self.engine.run_cycle()
                except Exception as e:
                    log.error(f"Cycle {self.cycle_count} failed: {e}", exc_info=True)
                    log.warning("Continuing to next cycle — agent defaults to safe state on error")
                elapsed = (datetime.utcnow() - cycle_start).total_seconds()
                sleep_time = max(0, self.interval_seconds - elapsed)
                log.info(f"--- Cycle {self.cycle_count} complete in {elapsed:.1f}s. Next in {sleep_time:.0f}s ---")
                time.sleep(sleep_time)
        except KeyboardInterrupt:
            log.info("Scheduler stopped by operator.")
