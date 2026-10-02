from dataclasses import dataclass
import fitz  # PyMuPDF


@dataclass
class ExtractedLine:
    page: int
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float = 1.0  # digital text extraction is character-exact, not a guess


def extract_digital_page_text(pdf_bytes: bytes, page_number: int) -> list[ExtractedLine]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[page_number]
        lines: list[ExtractedLine] = []
        data = page.get_text("dict")
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                if not text:
                    continue
                bbox = tuple(line.get("bbox", (0.0, 0.0, 0.0, 0.0)))
                lines.append(ExtractedLine(page=page_number, text=text, bbox=bbox, confidence=1.0))
        return lines
    finally:
        doc.close()


def render_page_image(pdf_bytes: bytes, page_number: int, dpi: int = 300) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[page_number]
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")
    finally:
        doc.close()
