"""Application factory: REST API under /api/v1 (OpenAPI page at /api/docs) and the server-rendered portal."""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .errors import AppError

log = logging.getLogger("ticketing")


def _wants_html(request: Request) -> bool:
    return (getattr(request.app.state, "portal", False) and not request.url.path.startswith("/api/")
            and "text/html" in request.headers.get("accept", ""))


def create_app() -> FastAPI:
    app = FastAPI(title="Ticketing Management System", version="1.0.0", docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)

    from .api import routes_admin, routes_auth
    app.include_router(routes_auth.router)
    app.include_router(routes_admin.router)
    for name in ("routes_tickets", "routes_config", "routes_sla", "routes_ops"):
        try:
            mod = __import__(f"ticketing.api.{name}", fromlist=["router"])
        except ModuleNotFoundError as e:  # later phases add these modules
            if e.name != f"ticketing.api.{name}":
                raise
            continue
        app.include_router(mod.router)
    try:
        from .web import portal
        portal.mount(app)
        app.state.portal = True
    except ImportError as e:
        if "portal" not in str(e):
            raise

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        if _wants_html(request):
            from .web.portal import render_error
            return render_error(request, exc)
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        fields = {}
        for err in exc.errors():
            loc = [str(x) for x in err.get("loc", []) if x not in ("body", "query", "path")]
            fields[".".join(loc) or "body"] = err.get("msg", "invalid")
        return JSONResponse(AppError("validation_failed", fields=fields).to_dict(), status_code=422)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, exc: IntegrityError):
        log.warning("integrity error: %s", exc.orig)
        return JSONResponse(AppError("conflict", "The change conflicts with existing data.").to_dict(), status_code=409)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {401: "not_authenticated", 403: "forbidden", 404: "not_found", 405: "not_found"}.get(exc.status_code)
        err = AppError(code or "error", str(exc.detail) if code is None else None, status=exc.status_code)
        if _wants_html(request):
            from .web.portal import render_error
            return render_error(request, err)
        return JSONResponse(err.to_dict(), status_code=exc.status_code)

    return app


app = create_app()
