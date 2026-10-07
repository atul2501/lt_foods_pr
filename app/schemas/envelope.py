"""API response shape = SAP ZFTVIA OCR contract V1 (contract/ZFTVIA_OCR_API_SCHEMA_V1.json).

GET /api/v1/invoices/{id} and GET /api/v1/invoices/new return InvoiceResult. Everything the
contract lists is always present (null when unknown); `email` and `metadata` are extra
information the contract allows (additionalProperties) and SAP ignores.
"""
from datetime import datetime
from enum import Enum
from typing import Annotated, Optional
from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer
from app.schemas.invoice_schema import (
    AdditionalField,
    InvoiceHeader,
    LineItem,
    PurchaseOrderItem,
    TaxDetail,
)

API_VERSION = "1"
OUTPUT_DATE_FORMAT = "%Y-%m-%d"          # ISO 8601, as the SAP contract requires
_LEGACY_DATE_FORMAT = "%d.%m.%Y"         # results stored by the previous version


def _parse_output_date(value: object) -> object:
    # Stored results are read back through these models (GET /invoices/...), so a date string
    # written by the serializer below (or by the previous DD.MM.YYYY version) must validate.
    if isinstance(value, str):
        for fmt in (OUTPUT_DATE_FORMAT, _LEGACY_DATE_FORMAT):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return value


# A timestamp the API hands out as a date only, YYYY-MM-DD.
OutputDate = Annotated[
    datetime,
    BeforeValidator(_parse_output_date),
    PlainSerializer(lambda value: value.strftime(OUTPUT_DATE_FORMAT), return_type=str, when_used="json"),
]


# Every contract key is returned. company_code is sent as printed (SAP maps it to BUKRS);
# gl_account / cost_center / profit_center are always null (SAP determines them) but the keys
# stay in the JSON because the contract requires them.
class InvoiceHeaderOut(InvoiceHeader):
    invoice_date: Optional[str] = None      # null when not printed / unreadable (contract allows it)
    vendor_tax_id: Optional[str] = None     # may be missing on old stored results
    document_type: str = "INVOICE"


class LineItemOut(LineItem):
    gl_account: Optional[str] = None
    cost_center: Optional[str] = None
    profit_center: Optional[str] = None


class ResultStatus(str, Enum):
    PROCESSING = "processing"
    SUCCESS = "success"
    NEEDS_REVIEW = "needs_review"
    ERROR = "error"
    FAILED = "failed"   # written by the previous version; new results use ERROR


class ValidationFlag(BaseModel):
    field: str
    reason: str
    severity: str  # "warning" | "critical"
    detail: Optional[str] = None


class ExtractionMetadata(BaseModel):
    # model_name isn't one of pydantic's own model_* methods, just our field name - silence
    # pydantic's protected-namespace warning rather than renaming a field the API returns.
    model_config = ConfigDict(protected_namespaces=())

    flags: list[ValidationFlag] = []
    model_name: Optional[str] = None
    prompt_version: Optional[str] = None
    extraction_source: Optional[str] = None
    avg_ocr_confidence: Optional[float] = None
    processing_time_ms: Optional[int] = None
    completed_at: Optional[OutputDate] = None
    source_text_hash: Optional[str] = None
    pdf_sha256: Optional[str] = None
    sap_reference: Optional[str] = None     # X-SAP-Reference header of the upload (VIA id)


class EmailAttachment(BaseModel):
    filename: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: int = 0
    is_pdf: bool = False
    is_invoice: bool = False    # PDF classified as an invoice (only those are extracted)


class EmailInfo(BaseModel):
    message_id: str
    sender: Optional[str] = None
    subject: Optional[str] = None
    received_at: Optional[OutputDate] = None
    body: Optional[str] = None                  # plain text, cut to email_body_max_chars
    attachments: list[EmailAttachment] = []     # every attachment of the email, not just PDFs
    # PO / BL (Bill of Lading) numbers found in the subject / full body, comma-joined; null when none.
    Subject_PO: Optional[str] = None
    Subject_BL: Optional[str] = None
    Body_PO: Optional[str] = None
    Body_BL: Optional[str] = None


class Confidence(BaseModel):
    """0.0 - 1.0 per header field, as the SAP contract defines it. 1.0 = the value was found
    verbatim in the document text, lower = fuzzy match / inferred / OCR-uncertain, 0.0 = not
    grounded, null = field not populated."""
    invoice_number: Optional[float] = None
    invoice_date: Optional[float] = None
    vendor_name: Optional[float] = None
    vendor_tax_id: Optional[float] = None
    po_number: Optional[float] = None
    SO_number: Optional[float] = None
    total_amount: Optional[float] = None
    subtotal: Optional[float] = None
    tax_amount: Optional[float] = None
    currency: Optional[float] = None
    document_type: Optional[float] = None


class InvoiceResult(BaseModel):
    """One extracted PDF, in the SAP contract shape. Written to STORAGE_DIR/pending/ or
    jobs/ and returned by GET /api/v1/invoices/{id} and /new. status "error" means the PDF
    could not be extracted - invoice_header is null and error says why."""

    id: str
    status: ResultStatus
    api_version: str = API_VERSION
    filename: Optional[str] = None
    # Path of the original PDF on this API (GET, same credentials).
    pdf_url: Optional[str] = None
    error: Optional[str] = None
    page_count: Optional[int] = None
    document_count_estimate: Optional[int] = None
    invoice_header: Optional[InvoiceHeaderOut] = None
    line_items: list[LineItemOut] = []
    additional_fields: list[AdditionalField] = []
    tax_details: list[TaxDetail] = []
    purchase_order_items: list[PurchaseOrderItem] = []
    confidence: Optional[Confidence] = None
    email: Optional[EmailInfo] = None
    metadata: ExtractionMetadata = ExtractionMetadata()


class JobAccepted(BaseModel):
    """Answer to POST /api/v1/invoices (and to GET /api/v1/invoices/{id} while the upload
    is still being extracted): fetch result_url for the JSON, pdf_url for the PDF."""

    id: str
    status: str  # "processing"
    result_url: str
    pdf_url: str
