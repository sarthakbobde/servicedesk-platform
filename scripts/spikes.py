"""Time-boxed spikes (plan Phase 1 task 10, run in Phase 2 — CHANGES P1-19).

Usage: TICKETING_DATABASE_URL=postgresql+psycopg://postgres@localhost:55432/ticketing_spike \
       uv run python scripts/spikes.py

Measures: (1) claim race with N concurrent claimers, (2) database-backed job throughput, (3) auto-assignment
candidate query for a large team with many open tickets. The database is wiped and rebuilt.
"""
import os
import statistics
import sys
import threading
import time
from datetime import datetime, timezone

os.environ.setdefault("TICKETING_DATABASE_URL", "postgresql+psycopg://postgres@localhost:55432/ticketing_spike")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, insert, text  # noqa: E402

from ticketing import clock, seed  # noqa: E402
from ticketing.config import get_config  # noqa: E402
from ticketing.db import SessionLocal  # noqa: E402
from ticketing.errors import AppError  # noqa: E402
from ticketing.models import (  # noqa: E402
    Calendar, CalendarHours, Job, Organization, Project, RoleAssignment, Team, Ticket, TicketType, User,
)
from ticketing.permissions import load_principal  # noqa: E402
from ticketing.services import jobs, routing, tickets  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def reset():
    url = get_config().database_url
    admin = create_engine(url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    name = url.rsplit("/", 1)[1]
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {name}"))
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    with SessionLocal() as db:
        seed.run(db)
        db.commit()


def build(n_resolvers=50, n_open=5000):
    with SessionLocal() as db:
        org = Organization(name="Spike", type="CLIENT")
        db.add(org)
        db.flush()
        proj = Project(organization_id=org.id, name="Spike", key="SPK")
        db.add(proj)
        db.flush()
        team = Team(project_id=proj.id, name="Big team")
        db.add(team)
        db.flush()
        always = Calendar(name="Always", time_zone="UTC")
        never = Calendar(name="Never", time_zone="UTC")
        db.add_all([always, never])
        db.flush()
        for d in range(1, 8):
            db.add(CalendarHours(calendar_id=always.id, weekday=d, start_minute=0, end_minute=1440))
        tt = TicketType(project_id=proj.id, team_id=team.id, name="Bug", assignment_mode="AUTO")
        db.add(tt)
        req = User(user_type="MEMBER", name="Req", email="req@spike.test", organization_id=org.id)
        db.add(req)
        db.flush()
        resolvers = []
        for i in range(n_resolvers):
            u = User(user_type="MEMBER", name=f"R{i}", email=f"r{i}@spike.test", organization_id=org.id)
            db.add(u)
            db.flush()
            resolvers.append(u.id)
            db.add(RoleAssignment(user_id=u.id, role="RESOLVER", scope="TEAM", organization_id=org.id,
                                  project_id=proj.id, team_id=team.id, calendar_id=always.id))
        now = clock.now()
        rows = [dict(project_id=proj.id, number=i + 1, type_id=tt.id, team_id=team.id, priority_id=3,
                     status_code="ASSIGNED", title=f"t{i}", description="x", requester_id=req.id,
                     resolver_id=resolvers[i % n_resolvers], source="API", created_at=now, updated_at=now,
                     assigned_at=now) for i in range(n_open)]
        db.execute(insert(Ticket), rows)
        proj.ticket_seq = n_open
        db.commit()
        return dict(org=org.id, proj=proj.id, team=team.id, tt=tt.id, req=req.id, resolvers=resolvers,
                    never=never.id)


def spike_assignment(w, runs=50):
    times = []
    with SessionLocal() as db:
        for _ in range(runs):
            t = time.perf_counter()
            found = routing.candidates(db, w["team"])
            times.append((time.perf_counter() - t) * 1000)
    return {"resolvers": len(w["resolvers"]), "candidates": len(found),
            "p50_ms": round(statistics.median(times), 1), "p95_ms": round(sorted(times)[int(runs * 0.95) - 1], 1)}


def spike_claim(w, claimers=10, rounds=20):
    """Each round: one waiting ticket, `claimers` threads claim at once. Expect exactly one winner."""
    with SessionLocal() as db:
        db.execute(text("UPDATE role_assignment SET calendar_id = :c"), {"c": w["never"]})
        db.commit()
    winners, latencies = [], []
    for r in range(rounds):
        with SessionLocal() as db:
            p = load_principal(db, db.get(User, w["req"]))
            t, _ = tickets.create_ticket(db, p, {"project_id": w["proj"], "type_id": w["tt"], "title": f"race {r}",
                                                 "description": "x"}, "API")
            db.commit()
            tid = t.id
        barrier = threading.Barrier(claimers)
        ok = []

        def claim(uid):
            with SessionLocal() as db:
                pr = load_principal(db, db.get(User, uid))
                barrier.wait()
                s = time.perf_counter()
                try:
                    tickets.perform(db, pr, tid, "claim")
                    db.commit()
                    ok.append(uid)
                except AppError:
                    db.rollback()
                latencies.append((time.perf_counter() - s) * 1000)

        ths = [threading.Thread(target=claim, args=(u,)) for u in w["resolvers"][:claimers]]
        [th.start() for th in ths]
        [th.join() for th in ths]
        winners.append(len(ok))
    return {"rounds": rounds, "claimers": claimers, "exactly_one_winner_every_round": all(x == 1 for x in winners),
            "p50_ms": round(statistics.median(latencies), 1),
            "p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 1)}


def spike_jobs(n=10000):
    jobs.HANDLERS["noop"] = lambda db, payload: None
    with SessionLocal() as db:
        now = clock.now()
        db.execute(insert(Job), [dict(kind="noop", payload={"i": i}, run_at=now, status="PENDING", attempts=0,
                                      created_at=now) for i in range(n)])
        db.commit()
    t = time.perf_counter()
    done = 0
    threads, counts = [], []

    def worker(i):
        c = 0
        while True:
            k = jobs.run_due_jobs(SessionLocal, limit=500, worker_id=f"w{i}")
            c += k
            if k == 0:
                break
        counts.append(c)

    for i in range(4):
        th = threading.Thread(target=worker, args=(i,))
        threads.append(th)
        th.start()
    [th.join() for th in threads]
    done = sum(counts)
    secs = time.perf_counter() - t
    return {"jobs": n, "processed": done, "workers": 4, "seconds": round(secs, 1), "jobs_per_second": round(done / secs)}


def main():
    clock.set_now(datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc))
    reset()
    w = build()
    print("auto-assignment candidates query:", spike_assignment(w))
    print("claim race:", spike_claim(w))
    print("database job runner:", spike_jobs())


if __name__ == "__main__":
    main()
