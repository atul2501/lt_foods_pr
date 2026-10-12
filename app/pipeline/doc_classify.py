"""Is this PDF an invoice? Email ingestion uses it to extract only the invoice PDFs of an email
(which often also carries packing lists, BLs, certificates...). Reads only the first
settings.invoice_detect_pages pages."""
import re

from app.config import settings
from app.email_ingest.references import PO_66_RE
from app.logging_conf import get_logger
from app.pipeline.text_extract import extract_digital_page_text
from app.pipeline.triage import triage_pdf

logger = get_logger(__name__)

# A line that IS the document title - "TAX INVOICE", "Commercial Invoice (Original)",
# "COMM.INVOICE", "CREDIT NOTE" - not "Invoice No: 123" / "Invoice Date" as printed on a
# packing list or BL. Carrier invoices are titled "IMPORT INVOICE" / "DEMURRAGE INVOICE" / "DETENTION INVOICE" (Maersk) / "FREIGHT INVOICE". Prefixes may be abbreviated ("COMM.", "COML.") and joined by a dot or
# nothing; "inv[o0][i1l]ce" tolerates OCR misreads (INVOlCE, INV0ICE).
_TITLE_RE = re.compile(
    r"^(?:(?:tax|commercial|comm|coml|com'l|sales|export|gst|vat|final|original|proforma|pro\s*-?\s*forma"
    r"|customs|retail|service|import|freight|shipping|demurrage|detention)[\s.]*)*"
    r"(?:inv[o0][i1l]ce|credit\s*note|debit\s*note)"
    r"(?:\s*[-/(]?\s*(?:original|duplicate|triplicate|copy|for\s+[a-z ]+)?\s*\)?)?$",
    re.IGNORECASE,
)
_TITLE_MAX_CHARS = 40
# A credit / debit note prints its number on the title line: "CREDIT NOTE NO.: 2409341997".
# Not done for "Invoice No:", which packing lists and BLs print too.
_NOTE_NO_RE = re.compile(r"^(?:credit|debit)\s*note\s*(?:no\b|number\b|#)", re.IGNORECASE)
# Letter-spaced titles: "C R E D I T   N O T E", "I N V O I C E" -> "CREDIT   NOTE", "INVOICE".
_SPACED_WORD_RE = re.compile(r"(?<![A-Za-z])(?:[A-Za-z] ){2,}[A-Za-z](?![A-Za-z])")
# Non-invoice documents that print "Commercial invoice" as a field value (CHED: "Type:
# Commercial invoice"). An invoice title on a page carrying one of these is ignored.
_NOT_INVOICE_PAGE_RE = re.compile(
    r"common\s+health\s+entry\s+document|certificate\s+of\s+insurance|\bMRN\s*:", re.IGNORECASE)


def _is_invoice_line(text: str) -> bool:
    return _has_po_66(text) or _is_title_line(text)


def _has_po_66(text: str) -> bool:
    return bool(PO_66_RE.search(text))


def _is_title_line(text: str) -> bool:
    text = _SPACED_WORD_RE.sub(lambda m: m.group().replace(" ", ""), text)
    line = re.sub(r"\s+", " ", text).strip(" \t:-*_=|.")
    if _NOTE_NO_RE.match(line):
        return True
    return len(line) <= _TITLE_MAX_CHARS and bool(_TITLE_RE.match(line))


def _is_invoice_page(lines: list[str]) -> bool:
    """A 66... PO anywhere, or an invoice title line on a page that isn't a known non-invoice
    document (customs declaration, CHED, insurance certificate)."""
    if any(_has_po_66(l) for l in lines):
        return True
    return any(_is_title_line(l) for l in lines) and not _NOT_INVOICE_PAGE_RE.search("\n".join(lines))


def is_invoice_pdf(pdf_bytes: bytes) -> bool:
    """True if one of the first invoice_detect_pages pages has an invoice title line or a
    reference starting with 66 (SAP PO) - see _is_invoice_page. If the PDF can't be read it returns True, so the
    normal pipeline (and its failure handling) deals with it instead of it being dropped."""
    # Imported here: app.pipeline.run imports the whole pipeline (and PaddleOCR lazily).
    from app.pipeline.run import _await_ocr_page, _submit_ocr_page

    try:
        pages, _ = triage_pdf(pdf_bytes)
        pages = pages[: settings.invoice_detect_pages]
        # Digital pages are cheap - check them first; OCR the scanned ones only if needed.
        for page in pages:
            if page.is_digital and _is_invoice_page([l.text for l in extract_digital_page_text(pdf_bytes, page.page_number)]):
                return True
        scanned = [p.page_number for p in pages if not p.is_digital]
        futures = {n: _submit_ocr_page(pdf_bytes, n) for n in scanned}
        try:
            for page_number, future in futures.items():
                if _is_invoice_page([l.text for l in _await_ocr_page(future, page_number)]):
                    return True
        finally:
            for future in futures.values():
                future.cancel()  # pages not started yet aren't needed any more
        return False
    except Exception:  # noqa: BLE001 - unreadable PDF: let the pipeline report it
        logger.exception("invoice_classification_failed")
        return True
