import concurrent.futures
import hashlib
import threading
import time
from datetime import datetime, timezone

from app.config import settings
from app.core.exceptions import NoUsableTextError, OcrTimeoutError
from app.logging_conf import get_logger
from app.pipeline.business_rules import run_business_rules
from app.pipeline.grounding import ground_extraction
from app.pipeline.llm_structurer import PROMPT_VERSION, structure_invoice
from app.pipeline.normalize import normalize_document
from app.pipeline.ocr import get_ocr_engine
from app.pipeline.status import assign_status
from app.pipeline.text_extract import ExtractedLine, extract_digital_page_text, render_page_image
from app.pipeline.triage import triage_pdf

logger = get_logger(__name__)

# Shared, bounded thread pool that OCRs scanned pages, sized by ocr_page_workers rather than
# one pool per PDF - this keeps the number of concurrently-loaded PaddleOCR instances equal
# to ocr_page_workers process-wide no matter how many PDFs or pages are in flight at once.
_ocr_executor_lock = threading.Lock()
_ocr_executor: concurrent.futures.ThreadPoolExecutor | None = None


def _get_ocr_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _ocr_executor
    if _ocr_executor is None:
        with _ocr_executor_lock:
            if _ocr_executor is None:
                _ocr_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=settings.ocr_page_workers, thread_name_prefix="ocr-page"
                )
    return _ocr_executor


# One OCR engine per OCR-pool thread rather than a single instance shared across the pool -
# a shared PaddleOCR instance was suspected to be behind a transient crash under concurrent
# OCR calls (see README "Key risks"). Thread-local trades a bit of extra memory (one loaded
# model per pool thread, bounded by ocr_page_workers) for genuine isolation, which is what
# actually lets pages be OCR'd concurrently - both within one job and across jobs - safely.
_thread_local = threading.local()
# Engine construction is serialized: on a fresh install PaddleOCR downloads its model files
# on first init, and several pool threads doing that at once collided on the same .tar
# (WinError 32 on Windows). Only construction is locked - OCR calls still run concurrently.
_ocr_engine_init_lock = threading.Lock()


def _get_ocr_engine():
    engine = getattr(_thread_local, "ocr_engine", None)
    if engine is None:
        with _ocr_engine_init_lock:
            engine = get_ocr_engine("paddle")
        _thread_local.ocr_engine = engine
    return engine


def _ocr_page(pdf_bytes: bytes, page_number: int) -> list[ExtractedLine]:
    """Renders and OCRs one page. Runs entirely on an OCR-pool thread, so rendering
    (PyMuPDF) and recognition (PaddleOCR) for different pages overlap too, not just OCR."""
    image_bytes = render_page_image(pdf_bytes, page_number)
    engine = _get_ocr_engine()
    return engine.ocr_page_image(image_bytes, page_number)


def _submit_ocr_page(pdf_bytes: bytes, page_number: int) -> "concurrent.futures.Future[list[ExtractedLine]]":
    """Submits one page's OCR work to the shared pool without blocking, so every scanned
    page in a document can be in flight at once instead of one at a time."""
    return _get_ocr_executor().submit(_ocr_page, pdf_bytes, page_number)


def _await_ocr_page(
    future: "concurrent.futures.Future[list[ExtractedLine]]", page_number: int
) -> list[ExtractedLine]:
    """Waits for one page's OCR result with a hard wall-clock ceiling. A pathological page
    (huge/corrupt scan), or a saturated OCR pool under heavy load, would otherwise tie up
    this job with no bound other than the 20-minute stale-job reaper. The OCR call itself
    isn't cancellable mid-flight, so a timeout here abandons waiting on it (the pool thread
    runs it to completion in the background) rather than actually interrupting it - still
    converts a hang into a clean, retryable failure instead of a stuck job."""
    try:
        return future.result(timeout=settings.ocr_timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        raise OcrTimeoutError(
            f"OCR timed out after {settings.ocr_timeout_seconds}s on page {page_number}"
        ) from exc


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def run_pipeline(
    pdf_bytes: bytes, job_id: str, sap_reference: str | None = None, email_context: str | None = None
) -> dict:
    """Runs triage -> extract/OCR -> LLM structuring -> grounding -> business rules ->
    status assignment for a single PDF. Raises ExtractionError subclasses on hard failures
    (no usable text, LLM never produced valid JSON) - the caller is responsible for marking
    the job `failed` in that case. Anything that parses successfully always returns here
    with a status of "success" or "needs_review", never silently drops a flagged field.

    Every stage logs its own start/complete + duration, correlated by job_id, so a slow or
    wrong extraction at 200k/month volume can be traced back to the exact stage that caused it.

    email_context (subject + body of the covering email, email ingestion only) is shown to the
    LLM next to the PDF text and is part of the text grounding checks against, so a value
    taken from the email (e.g. a PO number in the subject) isn't flagged as ungrounded.
    """
    started = time.monotonic()
    log = logger.bind(job_id=job_id)
    log.info("pipeline_started", pdf_bytes=len(pdf_bytes))

    # --- Stage 1: triage ---
    stage_started = time.monotonic()
    pages = triage_pdf(pdf_bytes)
    if not pages:
        log.error("triage_failed", reason="no_pages")
        raise NoUsableTextError("PDF has no pages")
    digital_pages = sum(1 for p in pages if p.is_digital)
    log.info(
        "triage_complete",
        pages=len(pages),
        digital_pages=digital_pages,
        scanned_pages=len(pages) - digital_pages,
        duration_ms=_ms_since(stage_started),
    )

    # --- Stage 2: extract / OCR ---
    stage_started = time.monotonic()
    all_lines: list[ExtractedLine] = []
    page_sources: dict[int, str] = {}

    # Scanned pages are independent of each other, so submit them all to the shared OCR
    # pool up front and let them run concurrently, instead of OCR'ing one page at a time.
    # normalize_document re-sorts every line by (page, position) below, so it doesn't
    # matter what order results come back in here.
    ocr_futures: dict[int, "concurrent.futures.Future[list[ExtractedLine]]"] = {}
    for page in pages:
        if page.is_digital:
            lines = extract_digital_page_text(pdf_bytes, page.page_number)
            page_sources[page.page_number] = "digital"
            all_lines.extend(lines)
            log.debug("page_extracted", page=page.page_number, source="digital", line_count=len(lines))
        else:
            page_sources[page.page_number] = "ocr"
            ocr_futures[page.page_number] = _submit_ocr_page(pdf_bytes, page.page_number)

    for page_number, future in ocr_futures.items():
        lines = _await_ocr_page(future, page_number)
        all_lines.extend(lines)
        log.debug("page_extracted", page=page_number, source="ocr", line_count=len(lines))

    if not all_lines:
        log.error("extraction_failed", reason="no_text_on_any_page")
        raise NoUsableTextError("no text extracted from any page (digital or OCR)")
    log.info("extraction_complete", total_lines=len(all_lines), duration_ms=_ms_since(stage_started))

    # --- Stage 3: normalize into a single source_text ---
    stage_started = time.monotonic()
    normalized = normalize_document(all_lines, page_sources)
    log.info(
        "normalization_complete",
        extraction_source=normalized.extraction_source,
        source_text_chars=len(normalized.source_text),
        avg_ocr_confidence=normalized.avg_ocr_confidence,
        duration_ms=_ms_since(stage_started),
    )

    # --- Stage 4: LLM structuring (Ollama) ---
    stage_started = time.monotonic()
    log.info(
        "llm_structuring_started",
        model=settings.ollama_model,
        prompt_chars=len(normalized.source_text),
        email_context_chars=len(email_context or ""),
    )
    extraction = structure_invoice(normalized.source_text, email_context=email_context, log=log)
    log.info(
        "llm_structuring_complete",
        model=settings.ollama_model,
        duration_ms=_ms_since(stage_started),
    )

    # --- Stage 5: grounding (anti-hallucination check) ---
    stage_started = time.monotonic()
    grounding_text = f"{normalized.source_text}\n{email_context}" if email_context else normalized.source_text
    grounding_results = ground_extraction(extraction, grounding_text)
    grounded_count = sum(1 for r in grounding_results if r.grounded)
    log.info(
        "grounding_complete",
        fields_checked=len(grounding_results),
        grounded=grounded_count,
        ungrounded=len(grounding_results) - grounded_count,
        duration_ms=_ms_since(stage_started),
    )

    # --- Stage 6: business rules ---
    stage_started = time.monotonic()
    rule_violations = run_business_rules(extraction)
    log.info(
        "business_rules_complete",
        violations=len(rule_violations),
        critical=sum(1 for v in rule_violations if v.severity == "critical"),
        warning=sum(1 for v in rule_violations if v.severity == "warning"),
        duration_ms=_ms_since(stage_started),
    )

    # --- Stage 7: status assignment ---
    status_result = assign_status(grounding_results, rule_violations, normalized)
    log.info("status_assigned", status=status_result.status, flag_count=len(status_result.flags))

    source_text_hash = hashlib.sha256(normalized.source_text.encode("utf-8")).hexdigest()
    processing_time_ms = int((time.monotonic() - started) * 1000)

    log.info(
        "pipeline_complete",
        status=status_result.status,
        flag_count=len(status_result.flags),
        processing_time_ms=processing_time_ms,
    )

    return {
        "extraction": extraction,
        "status": status_result.status,
        "flags": status_result.flags,
        "grounding_results": grounding_results,
        "page_count": len(pages),
        "avg_ocr_confidence": normalized.avg_ocr_confidence,
        "model_name": settings.ollama_model,
        "prompt_version": PROMPT_VERSION,
        "extraction_source": normalized.extraction_source,
        "source_text_hash": source_text_hash,
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "sap_reference": sap_reference,
        "processing_time_ms": processing_time_ms,
        "completed_at": datetime.now(timezone.utc),
    }
