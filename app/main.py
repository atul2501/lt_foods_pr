import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.api.routers import health, invoices, metrics
from app.logging_conf import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

app = FastAPI(
    title="LT Foods Invoice Extraction API",
    version="1.0.0",
    description=(
        "Invoice PDFs arriving by email are extracted to structured JSON for SAP "
        "MIRO/FB60 posting; GET /api/v1/invoices/new returns each new result once. "
        "SAP decides routing based on po_number; this API only populates fields."
    ),
)



@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One "http_request" line per request in the app log - including the ones that never
    reach an endpoint (wrong URL 404, wrong method 405, bad parameters 422) - and the full
    traceback for a crash (500), so every error a client sees can be found in the log."""
    started = time.perf_counter()
    client = request.client.host if request.client else None
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "http_request", method=request.method, path=request.url.path, client=client, status_code=500,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return JSONResponse(status_code=500, content={"detail": "internal server error"})
    log = logger.warning if response.status_code >= 400 else logger.info
    log(
        "http_request", method=request.method, path=request.url.path, client=client,
        status_code=response.status_code, duration_ms=round((time.perf_counter() - started) * 1000),
    )
    return response


app.include_router(health.router)
app.include_router(invoices.router)
app.include_router(metrics.router)
