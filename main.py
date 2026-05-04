import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Header, HTTPException, status, Depends, Request
from fastapi.responses import RedirectResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from routers.vlr_router import router as vlr_router
from routers.v2_router import router as v2_router
from utils.http_client import close_http_client
from utils.constants import API_TITLE, API_DESCRIPTION, API_PORT, API_KEY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def verify_api_key(request: Request):
    """
    Verify the API Key provided in the X-API-Key header.
    Exempts health check endpoints.
    """
    # If no API_KEY is set in environment, skip verification (local development)
    if not API_KEY:
        return

    # Allow health checks without an API Key
    if request.url.path in ["/health", "/v2/health", "/version"]:
        return

    x_api_key = request.headers.get("X-API-Key")
    if x_api_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting vlrggapi")
    yield
    logger.info("Shutting down — closing HTTP client")
    await close_http_client()


app = FastAPI(
    title=API_TITLE,
    description=API_DESCRIPTION,
    docs_url="/",
    redoc_url=None,
    lifespan=lifespan,
    dependencies=[Depends(verify_api_key)]
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(vlr_router)
app.include_router(v2_router)


@app.get("/version", tags=["system"])
async def get_version():
    """Return API version information."""
    return {"version": "2.0.0", "status": "stable", "default_api": "v2"}


@app.get("/health", include_in_schema=False)
async def legacy_health():
    """Redirect legacy health check to V2."""
    return RedirectResponse(url="/v2/health")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(API_PORT), reload=True)
