"""D32: the worker cycle completes within 30 s at 10,000 open timers.

Builds ~3,400 open tickets with 3 timers each (10,200 timers) under a policy whose steps all carry escalation
rules, then times worker cycles: the first evaluates every rule (and fires many), the second is a steady-state cycle.
Usage: uv run python scripts/load_worker.py   (uses the ticketing_spike database; wiped first)
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TICKETING_DATABASE_URL", "postgresql+psycopg://postgres@localhost:55432/ticketing_spike")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy import insert, select  # noqa: E402

from spikes import build, reset  # noqa: E402
from ticketing import clock  # noqa: E402
from ticketing.db import SessionLocal  # noqa: E402
from ticketing.models import EscalationRule, SlaMetric, SlaPolicy, SlaTimer, Ticket  # noqa: E402
from ticketing.services import jobs  # noqa: E402

N_TICKETS = 3400


def main():
    start = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    clock.set_now(start)
    reset()
    w = build(n_resolvers=50, n_open=N_TICKETS)
    with SessionLocal() as db:
        pol = SlaPolicy(name="Load", priority_id=3, ticket_type_id=None, calendar_id=None)
        db.add(pol)
        db.flush()
        metrics = []
        for pos, (name, minutes, stop) in enumerate([("Response", 60, "IN_PROGRESS"), ("Update", 240, "IN_PROGRESS"),
                                                      ("Resolution", 480, "RESOLVED")], start=1):
            m = SlaMetric(policy_id=pol.id, position=pos, name=name, minutes=minutes, stop_status_code=stop)
            db.add(m)
            db.flush()
            metrics.append(m)
            db.add_all([EscalationRule(metric_id=m.id, name=f"{name} 50%", level=1, trigger="PERCENT_USED",
                                       threshold=50, action="NOTIFY", target_type="RESOLVER"),
                        EscalationRule(metric_id=m.id, name=f"{name} breached", level=2, trigger="BREACHED",
                                       threshold=0, action="NOTIFY", target_type="TEAM_MANAGERS")])
        ids = list(db.scalars(select(Ticket.id)))
        db.query(Ticket).update({"sla_policy_id": pol.id, "sla_started_at": start, "sla_calendar_id": None})
        rows = []
        for tid in ids:
            for m in metrics:
                rows.append(dict(ticket_id=tid, metric_id=m.id, cycle=1, position=m.position, name=m.name,
                                 stop_status_code=m.stop_status_code, minutes_target=m.minutes, extension_minutes=0,
                                 state="RUNNING", seconds_used=0, running_since=start, is_current=True,
                                 due_at=start + timedelta(minutes=m.minutes)))
        db.execute(insert(SlaTimer), rows)
        db.commit()
        print("open timers:", len(rows))
    for label, minutes in (("cycle after 40 min (first rules fire)", 40), ("steady-state cycle", 1),
                           ("cycle after 2 h (breaches + escalations)", 120), ("steady-state cycle", 1)):
        clock.advance(minutes=minutes)
        t = time.perf_counter()
        stats = jobs.run_cycle()
        secs = time.perf_counter() - t
        print(f"{label}: {secs:.1f} s  escalations fired={stats.get('escalation')} breaches={stats.get('sla_breach')}"
              f" warnings={stats.get('sla_warning')}")
        print("   sweep ms:", {k: v for k, v in stats.items() if k.endswith("_ms")})


if __name__ == "__main__":
    main()
