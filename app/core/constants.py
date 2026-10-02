# Mandatory header fields of the SAP ZFTVIA contract (M) that are plain strings.
# A blank value puts the result into needs_review (critical).
CRITICAL_HEADER_STRING_FIELDS = [
    "invoice_number",
    "invoice_date",
    "vendor_name",
    "vendor_tax_id",
    "currency",
    "document_type",
]

# "Should have" (S) fields of the contract - a blank value is a warning, not critical.
WARNING_HEADER_FIELDS = ["customer_name", "vendor_country", "due_date", "company_code"]

# Fields the LLM is allowed to infer from context (currency symbols, VAT-id prefix, document
# title, sign of the total) rather than copy verbatim - exempted from strict grounding and
# reported with a reduced confidence.
INFERRED_FIELDS = {"company_code", "currency", "vendor_country", "document_type", "document_direction"}

# Always resolved inside SAP (account determination rules), never present on the PDF itself.
SAP_MANAGED_LINE_FIELDS = ["gl_account", "cost_center", "profit_center"]

# Header fields the contract wants a 0..1 confidence for.
CONFIDENCE_FIELDS = [
    "invoice_number", "invoice_date", "vendor_name", "vendor_tax_id", "po_number",
    "total_amount", "subtotal", "tax_amount", "currency", "document_type",
]
