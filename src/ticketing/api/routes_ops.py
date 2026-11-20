"""Phase 4 endpoints: escalation rules (API-098, API-099), G13 notification means (API-100..108) and
G14 operations (API-109, API-110)."""
from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from ..permissions import Principal
from ..services import channels, escalation, operations
from .deps import TxRoute, get_db, person, staff_only
from .routes_tickets import clean

router = APIRouter(prefix="/api/v1/admin", route_class=TxRoute)


def admin(p: Principal = Depends(person)) -> Principal:
    return staff_only(p)


# ---------------------------------------------------------------- escalation rules
G12 = "G12 SLA configuration"


@router.get("/sla-metrics/{mid}/escalation-rules", tags=[G12], summary="API-098 Rules for a step")
def get_rules(mid: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return {"rules": [escalation.rule_view(r) for r in escalation.list_rules(db, p, mid)]}


@router.put("/sla-metrics/{mid}/escalation-rules", tags=[G12], summary="API-099 Replace the rules for a step")
def put_rules(mid: int, body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return {"rules": [escalation.rule_view(r) for r in escalation.put_rules(db, p, mid, body)]}


# ---------------------------------------------------------------- channels and templates
G13 = "G13 Notification means"


def channel_view(db, c) -> dict:
    return clean({"id": c.id, "type": c.type, "name": c.name, "settings": c.settings, "enabled": c.enabled,
                  "last_failure_at": c.last_failure_at, "last_failure_text": c.last_failure_text,
                  "outbox": channels.channel_stats(db, c),
                  "spec": {k: {"type": v[0].__name__, "required": v[1], "help": v[2]}
                           for k, v in channels.REGISTRY[c.type].SPEC.items()}})


def template_view(t) -> dict:
    return {"id": t.id, "notification_type": t.notification_type, "channel_id": t.channel_id, "subject": t.subject,
            "body": t.body, "enabled": t.enabled}


@router.get("/notification-channels", tags=[G13], summary="API-100 List channels")
def list_channels(p: Principal = Depends(admin), db: Session = Depends(get_db)):
    rows = channels.list_channels(db, p)
    return {"items": [channel_view(db, c) for c in rows], "page": 1, "page_size": len(rows), "total": len(rows)}


@router.post("/notification-channels", status_code=201, tags=[G13], summary="API-101 Add a channel")
def create_channel(body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return channel_view(db, channels.create_channel(db, p, body))


@router.get("/notification-channels/{cid}", tags=[G13], summary="API-102 One channel")
def get_channel(cid: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return channel_view(db, channels.get_channel(db, p, cid))


@router.patch("/notification-channels/{cid}", tags=[G13], summary="API-103 Change settings, enable or disable")
def patch_channel(cid: int, body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return channel_view(db, channels.update_channel(db, p, cid, body))


@router.post("/notification-channels/{cid}/test", tags=[G13], summary="API-104 Send a test message")
def test_channel(cid: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return channels.test_channel(db, p, cid)


@router.get("/notification-templates", tags=[G13], summary="API-105 List templates")
def list_templates(channel_id: int | None = None, notification_type: str | None = None,
                   p: Principal = Depends(admin), db: Session = Depends(get_db)):
    rows = channels.list_templates(db, p, channel_id, notification_type)
    return {"items": [template_view(t) for t in rows], "page": 1, "page_size": len(rows), "total": len(rows),
            "placeholders": list(channels.notify.PLACEHOLDERS), "types": list(channels.notify.TYPES)}


@router.post("/notification-templates", status_code=201, tags=[G13], summary="API-106 Add a template")
def create_template(body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return template_view(channels.create_template(db, p, body))


@router.patch("/notification-templates/{tid}", tags=[G13], summary="API-107 Change text, enable or disable")
def patch_template(tid: int, body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return template_view(channels.update_template(db, p, tid, body))


@router.post("/notification-templates/{tid}/preview", tags=[G13], summary="API-108 Render with sample ticket data")
def preview_template(tid: int, body: dict = Body(default={}), p: Principal = Depends(admin),
                     db: Session = Depends(get_db)):
    return channels.preview_template(db, p, tid, body)


# ---------------------------------------------------------------- operations
G14 = "G14 Operations"


@router.get("/dashboard", tags=[G14], summary="API-109 Overdue, at risk, counts, oldest unassigned")
def dashboard(project: int | None = None, team: int | None = None, p: Principal = Depends(admin),
              db: Session = Depends(get_db)):
    return clean(operations.dashboard(db, p, project, team))


@router.get("/system/status", tags=[G14], summary="API-110 Worker, backlog, failures, warnings")
def system_status(p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return clean(operations.system_status(db, p))
