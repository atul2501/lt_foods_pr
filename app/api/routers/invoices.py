import base64
import json
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.config import settings
from app.logging_conf import get_logger
from app.pipeline.run import run_pipeline
from app.pipeline.to_response import build_failure, build_result
from app.schemas.envelope import API_VERSION, InvoiceResult, JobAccepted
from app.storage import results_store

logger = get_logger(__name__)

# Uploaded PDFs are extracted here, in the background, so POST /invoices answers at once.
_upload_executor = ThreadPoolExecutor(max_workers=settings.upload_max_concurrent, thread_name_prefix="upload")

_CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contract"


def _basic_users() -> dict[str, str]:
    users: dict[str, str] = {}
    for pair in settings.api_basic_users.split(","):
        if ":" in pair:
            user, _, password = pair.strip().partition(":")
            users[user] = password
    return users


def _basic_ok(header: str) -> bool:
    try:
        raw = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8")
    except Exception:  # noqa: BLE001 - malformed header is simply "not authenticated"
        return False
    user, _, password = raw.partition(":")
    expected = _basic_users().get(user)
    return expected is not None and secrets.compare_digest(password.encode(), expected.encode())


def _key_ok(provided: str | None) -> bool:
    return bool(settings.api_key and provided and secrets.compare_digest(provided.encode(), settings.api_key.encode()))


def require_auth(request: Request) -> None:
    """HTTP Basic (SAP ZFTVIA_APICFG AUTH_TYPE = BASIC), X-API-Key (APIKEY) or Bearer token.
    Any one of them is enough; which ones exist is decided by .env."""
    if not settings.api_key and not _basic_users():
        logger.error("auth_not_configured")
        raise HTTPException(status_code=503, detail="API_KEY / API_BASIC_USERS not configured on the server")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("basic ") and _basic_ok(authorization):
        return
    if authorization.lower().startswith("bearer ") and _key_ok(authorization.split(" ", 1)[1].strip()):
        return
    if _key_ok(request.headers.get("x-api-key")):
        return
    logger.warning("auth_rejected", client=request.client.host if request.client else None, path=request.url.path)
    raise HTTPException(
        status_code=401,
        detail="authenticate with HTTP Basic, X-API-Key or Bearer token",
        headers={"WWW-Authenticate": 'Basic realm="invoice-api"'},
    )


router = APIRouter(prefix="/api/v1", tags=["invoices"], dependencies=[Depends(require_auth)])


@router.get("/invoices/new", response_model=list[InvoiceResult])
def get_new_invoices() -> list[dict]:
    """Every invoice extracted from email since the last call, oldest first. Each result is
    returned exactly once: calling again straight away returns []. Results are extracted in
    the background by the email poller (email_ingest_main.py), so this responds instantly."""
    return results_store.claim_pending()


@router.get("/invoices/contract")
def get_contract() -> dict:
    """The SAP ZFTVIA OCR contract this API implements (version, JSON schema, sample)."""
    schema_path = _CONTRACT_DIR / "ZFTVIA_OCR_API_SCHEMA_V1.json"
    sample_path = _CONTRACT_DIR / "SAMPLE_RESPONSE_Brookshaw_15915.json"
    return {
        "api_version": API_VERSION,
        "schema": json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.exists() else None,
        "sample": json.loads(sample_path.read_text(encoding="utf-8")) if sample_path.exists() else None,
    }


@router.get("/invoices/{result_id}/pdf", response_class=FileResponse)
def get_invoice_pdf(result_id: str) -> FileResponse:
    """The original PDF of an invoice - an uploaded job or an email result (its pdf_url),
    shown inline in the browser/Postman. Kept for PDF_RETENTION_DAYS (default 30)."""
    path = results_store.pdf_path(result_id)
    if path is None:
        raise HTTPException(status_code=404, detail="PDF not found")
    return FileResponse(path, media_type="application/pdf", filename=path.name, content_disposition_type="inline")


@router.get(
    "/invoices/{result_id}",
    response_model=InvoiceResult,
    responses={202: {"model": JobAccepted, "description": "Still extracting - call again shortly"}},
)
def get_invoice(result_id: str):
    """The JSON for one id (SAP contract V1): the job id returned by POST /invoices, or the
    id of an email result. 202 with status "processing" while an upload is still being
    extracted, 200 with the full result once it is done (status success / needs_review /
    error)."""
    logger.info("get_invoice_request", result_id=result_id)
    result = results_store.get_result(result_id)
    if result is not None:
        if result.get("status") == "failed":        # stored by the previous version
            result["status"] = "error"
        result.setdefault("api_version", API_VERSION)
        logger.info("get_invoice_response", result_id=result_id, status_code=200, status=result.get("status"))
        return result

    started_at = results_store.job_started_at(result_id)
    if started_at is None:
        logger.warning("get_invoice_response", result_id=result_id, status_code=404)
        raise HTTPException(status_code=404, detail="no invoice or job with this id")
    if time.time() - started_at > settings.upload_job_timeout_seconds:
        # The API restarted mid-extraction, so this job will never finish.
        failure = build_failure(result_id, "extraction was interrupted - upload the PDF again", None, None)
        failure.pdf_url = results_store.pdf_url(result_id)
        return failure
    content = _job_accepted(result_id).model_dump(mode="json")
    logger.info("get_invoice_response", result_id=result_id, status_code=202)
    return JSONResponse(status_code=202, content=content)


@router.post("/invoices", response_model=JobAccepted, status_code=202)
async def extract_invoice(request: Request) -> JobAccepted:
    """Uploads one PDF and returns its job id at once; the PDF is extracted in the
    background. Poll GET /invoices/{id} (the result_url) for the JSON and open
    GET /invoices/{id}/pdf (the pdf_url) for the PDF.

    SAP (ZCL_FTVIA_OCR_CLIENT) sends the raw PDF as the request body with
    Content-Type: application/pdf plus optional headers X-Filename (original file name) and
    X-SAP-Reference (the VIA document id). A multipart/form-data upload with a "file" field
    (browsers, Swagger UI) is accepted too.

    A PDF whose content was already extracted (same sha256 - e.g. the email poller did it and
    SAP now uploads the same file) answers with the existing id; its result is available
    immediately, no second OCR/LLM run."""
    content_type = request.headers.get("content-type", "")
    filename: str | None = request.headers.get("x-filename")
    sap_reference: str | None = request.headers.get("x-sap-reference")

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            logger.warning("upload_rejected", reason="missing_file_field")
            raise HTTPException(status_code=400, detail="multipart upload must include a 'file' field")
        content = await upload.read()
        filename = getattr(upload, "filename", None) or filename
    else:
        content = await request.body()

    logger.info("upload_received", filename=filename, sap_reference=sap_reference,
                content_type=content_type, size_bytes=len(content))

    if not content:
        logger.warning("upload_rejected", filename=filename, reason="empty_file")
        raise HTTPException(status_code=400, detail="empty file")
    if b"%PDF-" not in content[:1024]:
        logger.warning("upload_rejected", filename=filename, reason="not_a_pdf")
        raise HTTPException(status_code=400, detail="file is not a valid PDF")
    content = content[content.find(b"%PDF-"):]

    existing = results_store.find_by_hash(content)
    if existing is not None:
        logger.info("upload_deduplicated", existing_id=existing, filename=filename, sap_reference=sap_reference)
        return _job_accepted(existing)

    job_id = str(uuid.uuid4())
    results_store.save_pdf(job_id, content)
    results_store.start_job(job_id)
    _upload_executor.submit(_run_upload_job, job_id, content, filename, sap_reference)
    accepted = _job_accepted(job_id)
    logger.info("upload_job_created", job_id=job_id, filename=filename, sap_reference=sap_reference)
    return accepted


def _job_accepted(job_id: str) -> JobAccepted:
    return JobAccepted(
        id=job_id,
        status="processing",
        result_url=f"/api/v1/invoices/{job_id}",
        pdf_url=results_store.pdf_url(job_id),
    )


def _run_upload_job(job_id: str, content: bytes, filename: str | None, sap_reference: str | None) -> None:
    try:
        response = build_result(job_id, run_pipeline(content, job_id, sap_reference), filename, email=None)
    except Exception as exc:  # noqa: BLE001 - the job must always end with a result
        logger.exception("extraction_failed", result_id=job_id)
        response = build_failure(job_id, f"{type(exc).__name__}: {exc}", filename, email=None)
    response.pdf_url = results_store.pdf_url(job_id)
    data = response.model_dump(mode="json")
    results_store.finish_job(job_id, data)
    if response.status.value != "error":
        results_store.register_hash(content, job_id)
    logger.info("upload_result", result_id=job_id, status=data["status"])
