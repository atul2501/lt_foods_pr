from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.metrics import RESULTS_PENDING
from app.storage import results_store

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def metrics() -> Response:
    """Prometheus scrape endpoint. Unauthenticated, same as /healthz and /readyz - restrict
    it at the reverse-proxy/firewall level like those, don't rely on obscurity. The pending
    gauge is refreshed from STORAGE_DIR/pending/ on every scrape."""
    RESULTS_PENDING.set(results_store.pending_count())
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
