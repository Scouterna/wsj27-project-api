"""FastAPI application for wsj27-project-api."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator, metrics

from .case import db_init_tables
from .case import router as case_router

# from . import oidc, roles
from .config import get_settings
from .db import close_db_connection, connect_to_db, db_configured
from .participants import router as participants_router
from .roles import load_cmt_roles
from .roles import router as roles_router
from .scoutnet import router as scoutnet_router
from .scoutnet import scoutnet_init, scoutnet_shutdown

# --- Create instrumentor, settings and logger objects ---
instrumentator = Instrumentator(
    excluded_handlers=["/metrics"],
    should_instrument_requests_inprogress=True,
    inprogress_name="http_requests_inprogress",
    inprogress_labels=True,
)
instrumentator.add(
    metrics.default(
        latency_lowr_buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 1.0, float("inf")),
    )
)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_db()
    if db_configured():
        await db_init_tables()
    load_cmt_roles()  # CMT Funktion/Roll detail, before any role is minted
    await scoutnet_init()  # Fetch Scoutnet project data

    try:
        logger.info("wsj27-project-api started and is accepting connections")
        yield  # FastAPI runs here!
    finally:
        await scoutnet_shutdown()
        await close_db_connection()


DESCRIPTION = """
TBI.
"""

app = FastAPI(
    title="wsj27-project-api",
    version="0.1.0",
    description=DESCRIPTION,
    lifespan=lifespan,
    root_path=settings.ROOT_PATH,
)


@app.middleware("http")
async def no_cache_headers(request: Request, call_next):
    """Auth responses carry session state; never let a proxy or browser reuse them."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# app.include_router(router)
# /cases is the only router that touches the database, so it is left out
# entirely when none is configured rather than mounted to fail per-request.
if db_configured():
    app.include_router(case_router, prefix="/cases", tags=["Cases"])
else:
    logger.warning("No database configured - the /cases endpoints are not registered")
app.include_router(participants_router, prefix="/participants", tags=["Participants"])
# Same prefix as participants_router on purpose: roles.py owns the role model and
# is kept free of other dependencies, but the URL stays /participants/roles,
# which is what auth-api and the other mirrors call.
app.include_router(roles_router, prefix="/participants", tags=["Roles"])
app.include_router(scoutnet_router, prefix="/scoutnet", tags=["Scoutnet"])


@app.get("/", include_in_schema=False)
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "wsj27-project-api"})
