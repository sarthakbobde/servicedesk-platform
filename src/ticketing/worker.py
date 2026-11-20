"""Background worker: runs one cycle every `sla.escalation_cycle_seconds` (default 60)."""
import logging
import time

from . import settings
from .db import SessionLocal
from .services import jobs

log = logging.getLogger("ticketing.worker")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("worker started")
    while True:
        started = time.monotonic()
        try:
            log.info("cycle %s", jobs.run_cycle(SessionLocal))
        except Exception:  # keep running; the next cycle retries
            log.exception("cycle failed")
        with SessionLocal() as db:
            interval = settings.get(db, "sla.escalation_cycle_seconds")
        time.sleep(max(1.0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
