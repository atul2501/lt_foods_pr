import io
import time
import numpy as np
from PIL import Image
from abc import ABC, abstractmethod

from app.logging_conf import get_logger
from app.pipeline.text_extract import ExtractedLine
from paddleocr import PaddleOCR  # heavy import (loads paddle at module import time)

logger = get_logger(__name__)


class OcrEngine(ABC):
    @abstractmethod
    def ocr_page_image(self, image_bytes: bytes, page_number: int) -> list[ExtractedLine]: ...


class PaddleOcrEngine(OcrEngine):
    """Primary OCR engine: better multilingual handling (Indian GST/Italian fiscal text)
    and layout/table awareness than Tesseract, at the cost of a heavier install."""

    def __init__(self, lang: str = "en"):

        logger.info("ocr_engine_initializing", engine="paddleocr", lang=lang)
        started = time.monotonic()
        self._ocr = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)
        logger.info(
            "ocr_engine_initialized",
            engine="paddleocr",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def ocr_page_image(self, image_bytes: bytes, page_number: int) -> list[ExtractedLine]:

        started = time.monotonic()
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        result = self._ocr.ocr(np.array(image), cls=True)

        lines: list[ExtractedLine] = []
        for page_result in result or []:
            # PaddleOCR returns [None] for a page with no detectable text (blank page)
            if not page_result:
                continue
            for box, (text, confidence) in page_result:
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                bbox = (min(xs), min(ys), max(xs), max(ys))
                lines.append(
                    ExtractedLine(page=page_number, text=text, bbox=bbox, confidence=float(confidence))
                )

        avg_confidence = (sum(line.confidence for line in lines) / len(lines)) if lines else None
        logger.info(
            "ocr_page_complete",
            engine="paddleocr",
            page=page_number,
            line_count=len(lines),
            avg_confidence=avg_confidence,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return lines


def get_ocr_engine(name: str = "paddle") -> OcrEngine:
    if name == "paddle":
        return PaddleOcrEngine()
    raise ValueError(f"unknown OCR engine: {name}")
