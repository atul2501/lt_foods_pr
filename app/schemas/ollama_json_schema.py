from app.schemas.invoice_schema import InvoiceExtraction


def get_invoice_json_schema() -> dict:
    """Single source of truth for the Ollama `format` constraint.

    Generated from the same pydantic model the API responds with, so the LLM's
    forced-JSON output shape can never drift from the pydantic validation it is
    parsed into afterwards.
    """
    return InvoiceExtraction.model_json_schema()
