"""Small helpers shared by services: paging, lookups, text checks."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import AppError, not_found


def paginate(db: Session, query, page: int = 1, page_size: int = 25, sort: str | None = None,
             sorts: dict | None = None, default_sort=None):
    try:
        page = max(1, int(page or 1))
        page_size = int(page_size or 25)
    except (TypeError, ValueError):
        raise AppError("validation_failed", fields={"page": "must be a number"})
    if not 1 <= page_size <= 100:
        raise AppError("validation_failed", fields={"page_size": "1 to 100"})
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    order = []
    if sort:
        desc = sort.startswith("-")
        name = sort.lstrip("-")
        if not sorts or name not in sorts:
            raise AppError("validation_failed", fields={"sort": f"unknown sort field {name}"})
        col = sorts[name]
        order.append(col.desc().nulls_last() if desc else col.asc().nulls_last())
    if default_sort is not None:
        order.extend(default_sort if isinstance(default_sort, (list, tuple)) else [default_sort])
    if order:
        query = query.order_by(*order)
    rows = db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return rows, {"page": page, "page_size": page_size, "total": total}


def get_or_404(db: Session, model, id_, lock: bool = False):
    try:
        id_ = int(id_)
    except (TypeError, ValueError):
        raise not_found()
    if lock:
        obj = db.scalar(select(model).where(model.id == id_).with_for_update())
    else:
        obj = db.get(model, id_)
    if obj is None:
        raise not_found()
    return obj


def text_field(fields: dict, data: dict, name: str, max_len: int = 200, required: bool = True):
    v = data.get(name)
    if v is None or not str(v).strip():
        if required:
            fields[name] = "required"
        return None
    v = str(v).strip()
    if len(v) > max_len:
        fields[name] = f"at most {max_len} characters"
        return None
    return v


def bool_field(fields: dict, data: dict, name: str):
    v = data.get(name)
    if v is None:
        return None
    if not isinstance(v, bool):
        fields[name] = "must be true or false"
        return None
    return v


def int_field(fields: dict, data: dict, name: str, lo: int | None = None, hi: int | None = None,
              required: bool = False):
    v = data.get(name)
    if v is None or v == "":
        if required:
            fields[name] = "required"
        return None
    try:
        if isinstance(v, bool):
            raise ValueError
        v = int(v)
    except (TypeError, ValueError):
        fields[name] = "must be a whole number"
        return None
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        fields[name] = f"{lo} to {hi}"
        return None
    return v


def raise_if(fields: dict):
    if fields:
        raise AppError("validation_failed", fields=fields)
