class ExtractionError(Exception):
    """Base class for pipeline extraction errors. Jobs raising this are routed to failed status."""


class OcrTimeoutError(ExtractionError):
    pass


class LLMFormatError(ExtractionError):
    """Raised when the LLM output cannot be parsed/validated as schema-conformant JSON, even after retry."""

    def __init__(self, message: str, *, raw_text: str | None = None):
        super().__init__(message)
        self.raw_text = raw_text


class NoUsableTextError(ExtractionError):
    """Raised when neither digital text extraction nor OCR produced any usable content."""
