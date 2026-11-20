"""Test harness on PostgreSQL. Each test starts from a freshly seeded database and a fixed clock."""
import os
from datetime import datetime, timezone

os.environ.setdefault("TICKETING_DATABASE_URL", "postgresql+psycopg://postgres@localhost:55432/ticketing_test")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from ticketing import clock, seed  # noqa: E402
from ticketing.api.deps import login_limiter  # noqa: E402
from ticketing.config import get_config  # noqa: E402
from ticketing.db import Base, SessionLocal, get_engine  # noqa: E402
from ticketing.models import (  # noqa: E402
    Calendar, CalendarHours, Organization, Priority, Project, RoleAssignment, Team, TicketType, User,
)
from ticketing.security import hash_password  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(__file__))
PASSWORD = "Correct-Horse-42"
_HASH = None
T0 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)  # a Monday, 10:00 UTC (inside default hours)


def alembic_config() -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.attributes["url"] = get_config().database_url
    return cfg


@pytest.fixture(scope="session", autouse=True)
def schema():
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean_db(schema):
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    with SessionLocal() as db:
        seed.run(db, admin_email="root@example.com", admin_password=PASSWORD)
        root = db.scalar(select(User).where(User.email == "root@example.com"))
        root.must_change_password = False
        db.commit()
    login_limiter.reset()
    clock.set_now(T0)
    yield
    clock.set_now(None)


@pytest.fixture
def client():
    from ticketing.main import app
    with TestClient(app) as c:
        yield c


def pw_hash():
    global _HASH
    if _HASH is None:
        _HASH = hash_password(PASSWORD)
    return _HASH


class Factory:
    """Builds structure directly in the database (fast) and signs people in through the API."""

    def __init__(self, client):
        self.client = client
        self.db = SessionLocal()
        self._tokens = {}

    def close(self):
        self.db.close()

    def commit(self):
        self.db.commit()

    # ---------------------------------------------------------------- structure
    def internal_org(self):
        return self.db.scalar(select(Organization).where(Organization.type == "INTERNAL"))

    def org(self, name, type_="CLIENT"):
        o = Organization(name=name, type=type_)
        self.db.add(o)
        self.db.commit()
        return o

    def project(self, org, key, name=None):
        p = Project(organization_id=org.id, key=key, name=name or key)
        self.db.add(p)
        self.db.commit()
        return p

    def team(self, project, name="Support"):
        t = Team(project_id=project.id, name=name)
        self.db.add(t)
        self.db.commit()
        return t

    def ticket_type(self, project, team, name="Bug", mode="MANUAL"):
        tt = TicketType(project_id=project.id, team_id=team.id, name=name, assignment_mode=mode)
        self.db.add(tt)
        self.db.commit()
        return tt

    def member(self, name, org=None, email=None, platform_admin=False):
        org = org or self.internal_org()
        u = User(user_type="MEMBER", name=name, email=email or f"{name.lower().replace(' ', '.')}@example.com",
                 organization_id=org.id, password_hash=pw_hash(), is_platform_admin=platform_admin)
        self.db.add(u)
        self.db.commit()
        return u

    def product_user(self, project, external_id, name=None, email=None, password=False):
        u = User(user_type="PRODUCT_USER", name=name or external_id, email=email, project_id=project.id,
                 external_id=external_id, password_hash=pw_hash() if password else None)
        self.db.add(u)
        self.db.commit()
        return u

    def grant(self, user, role, team=None, project=None, org=None, calendar=None):
        if team is not None:
            proj = self.db.get(Project, team.project_id)
            ra = RoleAssignment(user_id=user.id, role=role, scope="TEAM", team_id=team.id, project_id=proj.id,
                                organization_id=proj.organization_id, calendar_id=calendar.id if calendar else None)
        elif project is not None:
            ra = RoleAssignment(user_id=user.id, role=role, scope="PROJECT", project_id=project.id,
                                organization_id=project.organization_id)
        else:
            ra = RoleAssignment(user_id=user.id, role=role, scope="ORGANIZATION", organization_id=org.id)
        self.db.add(ra)
        self.db.commit()
        return ra

    def calendar(self, name, tz="UTC", windows=None):
        c = Calendar(name=name, time_zone=tz)
        self.db.add(c)
        self.db.flush()
        for wd, s, e in windows or []:
            self.db.add(CalendarHours(calendar_id=c.id, weekday=wd, start_minute=s, end_minute=e))
        self.db.commit()
        return c

    def priority(self, name):
        return self.db.scalar(select(Priority).where(Priority.name == name))

    # ---------------------------------------------------------------- auth
    def root(self):
        return self.db.scalar(select(User).where(User.email == "root@example.com"))

    def headers(self, user):
        """A bearer token, renewed when the test clock has moved past most of its lifetime."""
        cached = self._tokens.get(user.id)
        if cached is None or (clock.now() - cached[1]).total_seconds() > 45 * 60 or clock.now() < cached[1]:
            r = self.client.post("/api/v1/auth/token", json={"email": user.email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            cached = (r.json()["access_token"], clock.now())
            self._tokens[user.id] = cached
        return {"Authorization": f"Bearer {cached[0]}"}

    def as_(self, user):
        return Caller(self.client, lambda: self.headers(user))

    def as_fixed(self, user):
        """A caller holding one token that is never renewed (for revocation and expiry tests)."""
        return Caller(self.client, self.headers(user))


class Caller:
    def __init__(self, client, headers):
        self.client, self._h = client, headers

    @property
    def h(self):
        return self._h() if callable(self._h) else self._h

    def get(self, url, **kw):
        return self.client.get(url, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def post(self, url, json=None, **kw):
        return self.client.post(url, json=json, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def patch(self, url, json=None, **kw):
        return self.client.patch(url, json=json, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def put(self, url, json=None, **kw):
        return self.client.put(url, json=json, headers={**self.h, **kw.pop("headers", {})}, **kw)


@pytest.fixture
def f(client):
    fac = Factory(client)
    yield fac
    fac.close()


@pytest.fixture
def root(f):
    return f.as_(f.root())
