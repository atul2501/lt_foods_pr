from dataclasses import dataclass
import fitz  # PyMuPDF
from app.config import settings


@dataclass
class PageTriage:
    page_number: int
    is_digital: bool
    char_count: int


def triage_pdf(pdf_bytes: bytes) -> list[PageTriage]:
    """Classifies each page as digital-text vs scanned/image, independently.

    Mixed PDFs (e.g. an invoice with a scanned delivery note attached) are
    handled per-page rather than assuming the whole document is one or the other.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pages: list[PageTriage] = []
        for i, page in enumerate(doc):
            text = page.get_text("text") or ""
            printable = sum(1 for c in text if c.isprintable() and not c.isspace())
            is_digital = printable >= settings.digital_text_min_chars_per_page
            pages.append(PageTriage(page_number=i, is_digital=is_digital, char_count=printable))
        return pages
    finally:
        doc.close()
