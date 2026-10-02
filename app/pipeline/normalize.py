from dataclasses import dataclass
from app.pipeline.text_extract import ExtractedLine


@dataclass
class NormalizedDocument:
    source_text: str
    lines: list[ExtractedLine]
    extraction_source: str  # "digital" | "ocr" | "mixed"
    min_ocr_confidence: float | None = None
    avg_ocr_confidence: float | None = None


def normalize_document(
    all_lines: list[ExtractedLine], page_sources: dict[int, str]
) -> NormalizedDocument:
    """Merges per-page digital/OCR lines into one page-and-position-ordered `source_text` -
    this string is the ground truth every downstream grounding check is verified against."""
    sorted_lines = sorted(all_lines, key=lambda line: (line.page, line.bbox[1], line.bbox[0]))
    source_text = "\n".join(line.text for line in sorted_lines)

    sources_used = set(page_sources.values())
    if sources_used == {"digital"}:
        extraction_source = "digital"
    elif sources_used == {"ocr"}:
        extraction_source = "ocr"
    else:
        extraction_source = "mixed"

    ocr_confidences = [line.confidence for line in sorted_lines if page_sources.get(line.page) == "ocr"]
    min_conf = min(ocr_confidences) if ocr_confidences else None
    avg_conf = (sum(ocr_confidences) / len(ocr_confidences)) if ocr_confidences else None

    return NormalizedDocument(
        source_text=source_text,
        lines=sorted_lines,
        extraction_source=extraction_source,
        min_ocr_confidence=min_conf,
        avg_ocr_confidence=avg_conf,
    )
