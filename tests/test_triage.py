import fitz

from app.config import settings
from app.pipeline.triage import triage_pdf


def _make_pdf(page_count: int) -> bytes:
    doc = fitz.open()
    for i in range(page_count):
        doc.new_page().insert_text((72, 72), f"Invoice page {i + 1}")
    try:
        return doc.tobytes()
    finally:
        doc.close()


def test_triage_reads_only_first_max_pages():
    pages, total = triage_pdf(_make_pdf(settings.max_pages_to_process + 2))
    assert total == settings.max_pages_to_process + 2
    assert [p.page_number for p in pages] == list(range(settings.max_pages_to_process))


def test_triage_short_pdf_reads_all_pages():
    pages, total = triage_pdf(_make_pdf(3))
    assert total == 3 and len(pages) == 3
