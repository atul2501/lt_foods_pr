"""The extraction schema = the SAP ZFTVIA OCR API contract, version 1
(see contract/ZFTVIA_OCR_API_CONTRACT_V1.json and contract/ZFTVIA_OCR_API_SCHEMA_V1.json).

Every key the contract lists must be present in the output - a value that is not on the
document is null (mandatory text fields may be "" from the LLM; the business rules flag
them). SAP owns gl_account / cost_center / profit_center, they are always null here.
"""
from typing import Optional
from pydantic import BaseModel, Field

DOCUMENT_TYPES = ("INVOICE", "CREDIT_NOTE", "DEBIT_NOTE", "DELIVERY_NOTE", "TIMESHEET", "PROFORMA", "STATEMENT", "OTHER")
DOCUMENT_DIRECTIONS = ("VENDOR_TO_CUSTOMER", "CUSTOMER_TO_VENDOR")
TAX_TYPES = ("VAT", "GST", "CGST", "SGST", "IGST", "UTGST", "CESS", "SALES_TAX", "WHT", "OTHER")


class InvoiceHeader(BaseModel):
    # Required fields have no default: this makes them "required" in the JSON schema Ollama
    # is constrained to, so the model must always emit the key (value may still be "").
    invoice_number: str
    invoice_date: str                       # ISO YYYY-MM-DD in the API output
    due_date: Optional[str] = None
    payment_terms: Optional[str] = None
    company_code: Optional[str] = None      # "bill to" legal entity as printed - SAP maps it
    vendor_name: str
    vendor_address: Optional[str] = None
    vendor_tax_id: str                      # VAT / GSTIN / NIP - mandatory for SAP vendor matching
    vendor_country: Optional[str] = None    # ISO 3166-1 alpha-2
    vendor_bank_name: Optional[str] = None
    vendor_account_no: Optional[str] = None
    vendor_sort_code: Optional[str] = None
    vendor_iban: Optional[str] = None
    customer_name: Optional[str] = None
    customer_address: Optional[str] = None
    po_number: Optional[str] = None         # null when no PO is printed
    reference_number: Optional[str] = None
    currency: str                           # ISO 4217
    subtotal: float
    tax_amount: float
    tax_percent: Optional[float] = None
    total_amount: float
    document_type: str                      # one of DOCUMENT_TYPES
    document_direction: Optional[str] = None  # one of DOCUMENT_DIRECTIONS
    invoice_period_from: Optional[str] = None
    invoice_period_to: Optional[str] = None


class LineItem(BaseModel):
    line_no: Optional[str] = None
    description: str
    quantity: Optional[float] = None
    unit: Optional[str] = None
    unit_price: Optional[float] = None
    amount: float                           # net line value
    tax_percent: Optional[float] = None
    tax_amount: Optional[float] = None
    reference_code: Optional[str] = None
    po_number: Optional[str] = None
    po_item: Optional[str] = None
    gl_account: Optional[str] = None        # always null - SAP decides
    cost_center: Optional[str] = None       # always null - SAP decides
    profit_center: Optional[str] = None     # always null - SAP decides


class AdditionalField(BaseModel):
    field_name: str
    field_value: Optional[str] = None


class TaxDetail(BaseModel):
    tax_type: str                           # one of TAX_TYPES
    tax_rate: Optional[float] = None
    tax_amount: Optional[float] = None
    taxable_amount: Optional[float] = None


class PurchaseOrderItem(BaseModel):
    po_number: str
    po_item: Optional[str] = None
    invoice_line_no: str
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None


class InvoiceExtraction(BaseModel):
    invoice_header: InvoiceHeader
    line_items: list[LineItem] = Field(default_factory=list)
    additional_fields: list[AdditionalField] = Field(default_factory=list)
    tax_details: list[TaxDetail] = Field(default_factory=list)
    purchase_order_items: list[PurchaseOrderItem] = Field(default_factory=list)
    # How many separate invoices the LLM saw in this one PDF (1 normally).
    document_count_estimate: Optional[int] = 1
