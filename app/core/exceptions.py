class ExtractionError(Exception):
    """Base class for pipeline extraction errors. Jobs raising this are routed to failed status."""


class OcrTimeoutError(ExtractionError):
    pass


class LLMFormatError(ExtractionError):
    """Raised when the LLM output cannot be parsed/validated as schema-conformant JSON, even after retry."""


class NoUsableTextError(ExtractionError):
    """Raised when neither digital text extraction nor OCR produced any usable content."""
