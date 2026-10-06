from dataclasses import dataclass
from statistics import median
from app.pipeline.text_extract import ExtractedLine

# Horizontal gap (in points) above which two pieces on the same row are treated as separate columns.
_COLUMN_GAP_PT = 15.0
_FALLBACK_ROW_TOLERANCE_PT = 3.0


@dataclass
class NormalizedDocument:
    source_text: str
    lines: list[ExtractedLine]
    extraction_source: str  # "digital" | "ocr" | "mixed"
    min_ocr_confidence: float | None = None
    avg_ocr_confidence: float | None = None


def _centre_y(line: ExtractedLine) -> float:
    return (line.bbox[1] + line.bbox[3]) / 2


def _group_rows(page_lines: list[ExtractedLine]) -> list[list[ExtractedLine]]:
    """Groups lines that sit on the same visual row, so a label and its value printed side by side
    (e.g. `Invoice No :` and `002/12038`) end up on one line even when their y positions differ slightly."""
    heights = [line.bbox[3] - line.bbox[1] for line in page_lines if line.bbox[3] > line.bbox[1]]
    tolerance = 0.5 * median(heights) if heights else _FALLBACK_ROW_TOLERANCE_PT
    rows: list[list[ExtractedLine]] = []
    row_centre = 0.0
    for line in sorted(page_lines, key=_centre_y):
        centre = _centre_y(line)
        if rows and abs(centre - row_centre) <= tolerance:
            rows[-1].append(line)
            row_centre = sum(_centre_y(item) for item in rows[-1]) / len(rows[-1])
        else:
            rows.append([line])
            row_centre = centre
    return [sorted(row, key=lambda item: item.bbox[0]) for row in rows]


def _row_text(row: list[ExtractedLine]) -> str:
    parts = [row[0].text]
    for previous, current in zip(row, row[1:]):
        gap = current.bbox[0] - previous.bbox[2]
        parts.append("   " if gap > _COLUMN_GAP_PT else " ")
        parts.append(current.text)
    return "".join(parts)


def _build_source_text(sorted_lines: list[ExtractedLine]) -> str:
    rows_text: list[str] = []
    for page in sorted({line.page for line in sorted_lines}):
        page_lines = [line for line in sorted_lines if line.page == page]
        rows_text.extend(_row_text(row) for row in _group_rows(page_lines))
    return "\n".join(rows_text)


def normalize_document(
    all_lines: list[ExtractedLine], page_sources: dict[int, str]
) -> NormalizedDocument:
    """Merges per-page digital/OCR lines into one page-and-position-ordered `source_text` -
    this string is the ground truth every downstream grounding check is verified against."""
    sorted_lines = sorted(all_lines, key=lambda line: (line.page, line.bbox[1], line.bbox[0]))
    source_text = _build_source_text(sorted_lines)

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
