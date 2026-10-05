from dataclasses import dataclass
import fitz  # PyMuPDF
from app.config import settings


@dataclass
class PageTriage:
    page_number: int
    is_digital: bool
    char_count: int


def triage_pdf(pdf_bytes: bytes) -> tuple[list[PageTriage], int]:
    """Classifies each page as digital-text vs scanned/image, independently.

    Mixed PDFs (e.g. an invoice with a scanned delivery note attached) are
    handled per-page rather than assuming the whole document is one or the other.

    Only the first settings.max_pages_to_process pages are triaged (and therefore
    extracted/OCR'd downstream); later pages are ignored. Returns the triaged pages
    and the PDF's total page count, so the caller can tell when pages were skipped.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total_pages = doc.page_count
        pages: list[PageTriage] = []
        for i in range(min(total_pages, settings.max_pages_to_process)):
            text = doc[i].get_text("text") or ""
            printable = sum(1 for c in text if c.isprintable() and not c.isspace())
            is_digital = printable >= settings.digital_text_min_chars_per_page
            pages.append(PageTriage(page_number=i, is_digital=is_digital, char_count=printable))
        return pages, total_pages
    finally:
        doc.close()
