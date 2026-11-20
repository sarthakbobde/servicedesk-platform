"""Database-backed jobs (D13): rows in `job`, claimed with FOR UPDATE SKIP LOCKED, at-least-once, idempotent
handlers, retries with back-off. Plus the periodic sweeps the worker runs every cycle."""
from __future__ import annotations

import logging
import time
import traceback
from datetime import timedelta
from typing import Callable

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .. import clock
from ..config import get_config
from ..models import IdempotencyKey, Job, Setting

log = logging.getLogger("ticketing.jobs")

HANDLERS: dict[str, Callable[[Session, dict], None]] = {}
SWEEPS: list[tuple[str, Callable[[Session], object]]] = []
MAX_ATTEMPTS = 5


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def sweep(name: str):
    def deco(fn):
        if name not in [n for n, _ in SWEEPS]:
            SWEEPS.append((name, fn))
        return fn
    return deco


def enqueue(db: Session, kind: str, payload: dict, run_at=None, dedupe_key: str | None = None) -> None:
    """Add a job. With a dedupe key, a PENDING job with the same key is kept (no duplicate)."""
    values = dict(kind=kind, payload=payload, run_at=run_at or clock.now(), dedupe_key=dedupe_key,
                  status="PENDING", attempts=0, created_at=clock.now())
    stmt = insert(Job).values(**values)
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"],
                                           index_where=(Job.__table__.c.status == "PENDING")
                                           & Job.__table__.c.dedupe_key.isnot(None))
    db.execute(stmt)


def run_due_jobs(session_factory, limit: int = 100, worker_id: str | None = None) -> int:
    worker_id = worker_id or get_config().worker_id
    done = 0
    for _ in range(limit):
        with session_factory() as db:
            job = db.scalar(select(Job).where(Job.status == "PENDING", Job.run_at <= clock.now())
                            .order_by(Job.run_at, Job.id).limit(1).with_for_update(skip_locked=True))
            if job is None:
                return done
            job.locked_by, job.locked_at = worker_id, clock.now()
            job.attempts += 1
            fn = HANDLERS.get(job.kind)
            try:
                if fn is None:
                    raise RuntimeError(f"no handler for job kind {job.kind}")
                with db.begin_nested():
                    fn(db, job.payload or {})
                job.status, job.finished_at, job.last_error = "DONE", clock.now(), None
            except Exception as exc:  # retried with back-off; never blocks other jobs
                job.last_error = "".join(traceback.format_exception_only(type(exc), exc)).strip()[:2000]
                if job.attempts >= MAX_ATTEMPTS:
                    job.status, job.finished_at = "FAILED", clock.now()
                else:
                    job.run_at = clock.now() + timedelta(seconds=30 * 2 ** job.attempts)
                log.warning("job %s %s failed: %s", job.id, job.kind, job.last_error)
            db.commit()
            done += 1
    return done


def _load_modules():
    # Importing registers handlers and sweeps.
    from . import tickets, routing  # noqa: F401
    for name in ("sla", "escalation", "channels"):
        try:
            __import__(f"ticketing.services.{name}")
        except ModuleNotFoundError as e:  # modules of later phases
            if e.name != f"ticketing.services.{name}":
                raise


def run_cycle(session_factory=None) -> dict:
    """One worker cycle: due jobs, then every sweep, each in its own transaction. Returns timings and counts."""
    from ..db import SessionLocal
    session_factory = session_factory or SessionLocal
    _load_modules()
    stats = {}
    t0 = time.perf_counter()
    stats["jobs"] = run_due_jobs(session_factory)
    for name, fn in SWEEPS:
        t = time.perf_counter()
        with session_factory() as db:
            try:
                stats[name] = fn(db)
                db.commit()
            except Exception:
                db.rollback()
                log.exception("sweep %s failed", name)
                stats[name] = "error"
        stats[f"{name}_ms"] = round((time.perf_counter() - t) * 1000, 1)
    with session_factory() as db:
        mark_worker_run(db)
        db.commit()
    stats["cycle_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return stats


def mark_worker_run(db: Session):
    now = clock.now().isoformat()
    row = db.scalar(select(Setting).where(Setting.key == "system.worker_last_run", Setting.project_id.is_(None)))
    if row is None:
        db.add(Setting(key="system.worker_last_run", value=now, updated_at=clock.now()))
    else:
        row.value, row.updated_at = now, clock.now()


def worker_last_run(db: Session):
    row = db.scalar(select(Setting).where(Setting.key == "system.worker_last_run", Setting.project_id.is_(None)))
    return row.value if row else None


@sweep("idempotency_cleanup")
def cleanup_idempotency(db: Session) -> int:
    """Keys are kept 24 hours per caller (A11.1)."""
    res = db.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < clock.now() - timedelta(hours=24)))
    return res.rowcount or 0
