"""Optional push of extracted email invoices into SAP (ZFTVIA framework).

SAP's side is the RFC-enabled function module ZFTVIA_PROCESS_DOCUMENT (function group
ZFTVIA): it receives the PDF bytes + a source reference, creates the VIA document, stores
the PDF (Z table / ArchiveLink / DMS as configured there) and later calls this API back
(POST /api/v1/invoices with the same PDF) - which answers with the already extracted result
thanks to the sha256 index in results_store, so nothing is OCR'd twice.

Needs the SAP NetWeaver RFC SDK and `pyrfc` (pip install pyrfc). Both are optional: with
SAP_PUSH_ENABLED=false (default) this module is never imported at runtime and the SAP side
collects results with GET /api/v1/invoices/new instead.
"""
from app.config import settings
from app.logging_conf import get_logger

logger = get_logger(__name__)


def _connection():
    from pyrfc import Connection  # imported lazily: optional dependency

    if settings.sap_mshost:
        params = dict(mshost=settings.sap_mshost, sysid=settings.sap_sysid, group=settings.sap_group)
    else:
        params = dict(ashost=settings.sap_ashost, sysnr=settings.sap_sysnr)
    return Connection(client=settings.sap_client, user=settings.sap_user, passwd=settings.sap_passwd,
                      lang=settings.sap_lang, **params)


def push_document(key: str, pdf_bytes: bytes, filename: str | None, source_ref: str, bukrs: str | None = None) -> dict | None:
    """Calls ZFTVIA_PROCESS_DOCUMENT. Returns SAP's exporting parameters (EV_VIA_ID,
    EV_STATUS, EV_MESSAGE, ET_RETURN) or None when the push is disabled. Raises on RFC
    errors - the caller decides whether that is fatal (it is not: the result stays in
    pending/ and can still be collected with GET /api/v1/invoices/new)."""
    if not settings.sap_push_enabled:
        return None
    log = logger.bind(key=key, function=settings.sap_rfc_function)
    log.info("sap_push_started", filename=filename, source_ref=source_ref)
    conn = _connection()
    try:
        result = conn.call(
            settings.sap_rfc_function,
            IV_FILE_NAME=filename or f"{key}.pdf",
            IV_MIME_TYPE="application/pdf",
            IV_CONTENT=pdf_bytes,
            IV_SOURCE_REF=source_ref,
            IV_SOURCE_TYPE=settings.sap_source_type,
            IV_BUKRS=bukrs or settings.sap_bukrs,
            IV_RUN_NOW="",
        )
    finally:
        conn.close()
    log.info("sap_push_complete", via_id=result.get("EV_VIA_ID"), status=result.get("EV_STATUS"),
             message=result.get("EV_MESSAGE"))
    return result
