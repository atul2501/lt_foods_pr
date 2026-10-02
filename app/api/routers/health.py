import os

import httpx
from fastapi import APIRouter

from app.config import settings
from app.storage import results_store

router = APIRouter(tags=["health"])


def _dependency_checks() -> dict[str, str]:
    checks: dict[str, str] = {}

    for host in settings.ollama_hosts.split(","):
        host = host.strip()
        if not host:
            continue
        try:
            response = httpx.get(f"{host}/api/tags", timeout=3)
            response.raise_for_status()
            checks[f"ollama:{host}"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks[f"ollama:{host}"] = f"error: {exc}"

    try:
        if os.access(results_store.PENDING_DIR, os.W_OK):
            checks["storage"] = "ok"
        else:
            checks["storage"] = f"error: {results_store.PENDING_DIR} is not writable"
    except Exception as exc:  # noqa: BLE001
        checks["storage"] = f"error: {exc}"

    return checks


@router.get("/healthz")
def healthz():
    """Liveness only - is the process up. No dependency calls, safe for a tight
    orchestrator probe interval."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz():
    """Readiness for process managers/load balancers - checks every dependency the API
    needs to actually serve traffic."""
    checks = _dependency_checks()
    healthy = all(value == "ok" for value in checks.values())
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/api/v1/health")
def api_health():
    """Versioned health endpoint for API consumers/monitoring dashboards (as opposed to
    /healthz and /readyz, which are the unprefixed paths orchestrators expect). Same
    dependency checks as /readyz, plus how many extracted results are waiting to be
    fetched from /api/v1/invoices/new."""
    checks = _dependency_checks()
    healthy = all(value == "ok" for value in checks.values())
    return {
        "status": "ok" if healthy else "degraded",
        "checks": checks,
        "pending_results": results_store.pending_count(),
    }
