"""Maps run_pipeline()'s return value to the SAP contract JSON (contract/ZFTVIA_OCR_API_*.json).

Everything here is deterministic post-processing of what the LLM produced and grounding
verified: ISO dates, ISO currency / country codes, upper-case enumerations, null for
"not on the document", per-field confidence from the grounding results.
"""
import re

from app.core.constants import CONFIDENCE_FIELDS
from app.pipeline.grounding import FieldGroundingResult, parse_date
from app.schemas.envelope import (
    OUTPUT_DATE_FORMAT,
    Confidence,
    EmailInfo,
    ExtractionMetadata,
    InvoiceHeaderOut,
    InvoiceResult,
    LineItemOut,
    ResultStatus,
    ValidationFlag,
)
from app.schemas.invoice_schema import (
    DOCUMENT_DIRECTIONS,
    DOCUMENT_TYPES,
    TAX_TYPES,
    AdditionalField,
    PurchaseOrderItem,
    TaxDetail,
)

_CURRENCY_SYMBOLS = {
    "£": "GBP", "GBP": "GBP", "POUND": "GBP", "POUNDS": "GBP", "STERLING": "GBP",
    "€": "EUR", "EUR": "EUR", "EURO": "EUR", "EUROS": "EUR",
    "$": "USD", "USD": "USD", "US$": "USD",
    "₹": "INR", "INR": "INR", "RS": "INR", "RS.": "INR", "RUPEES": "INR",
    "ZŁ": "PLN", "ZL": "PLN", "PLN": "PLN",
    "CHF": "CHF", "AED": "AED", "AUD": "AUD", "CAD": "CAD", "SGD": "SGD", "JPY": "JPY", "CNY": "CNY",
}

# VAT-id prefix -> ISO country, for vendor_country when the LLM did not fill it.
_VAT_PREFIX_COUNTRIES = {
    "GB": "GB", "XI": "GB", "IE": "IE", "DE": "DE", "FR": "FR", "NL": "NL", "BE": "BE", "PL": "PL", "IT": "IT",
    "ES": "ES", "PT": "PT", "AT": "AT", "DK": "DK", "SE": "SE", "FI": "FI", "CZ": "CZ", "SK": "SK", "HU": "HU",
    "RO": "RO", "BG": "BG", "GR": "GR", "EL": "GR", "LT": "LT", "LV": "LV", "EE": "EE", "LU": "LU", "CY": "CY",
    "MT": "MT", "SI": "SI", "HR": "HR", "CH": "CH", "NO": "NO", "AE": "AE", "SG": "SG", "AU": "AU",
}
_GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

_NULL_TEXTS = {"", "null", "none", "n/a", "na", "-", "--", "nil", "not available", "not applicable"}


def _format_date(value: str | None) -> str | None:
    """ISO YYYY-MM-DD for the API. A value that isn't a recognisable date is passed through
    unchanged rather than dropped or guessed at (SAP's normalizer gets a second chance)."""
    if not value or _is_null_text(value):
        return None
    parsed = parse_date(value)
    return parsed.strftime(OUTPUT_DATE_FORMAT) if parsed else value


def _is_null_text(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _NULL_TEXTS


def _clean_text(value: object) -> str | None:
    """Null convention of the contract: never "", "N/A", "-" - those become null."""
    if value is None or _is_null_text(value):
        return None
    return str(value).strip() or None


def normalize_currency(value: str | None) -> str | None:
    if not value:
        return None
    key = value.strip().upper()
    if key in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[key]
    letters = re.sub(r"[^A-Z]", "", key)
    if len(letters) == 3:
        return letters
    return _CURRENCY_SYMBOLS.get(key[:1], key[:3] or None)


def normalize_country(country: str | None, tax_id: str | None) -> str | None:
    if country:
        c = country.strip().upper()
        if len(c) == 2:
            return c
        names = {"UNITED KINGDOM": "GB", "UK": "GB", "GREAT BRITAIN": "GB", "ENGLAND": "GB", "POLAND": "PL",
                 "INDIA": "IN", "GERMANY": "DE", "NETHERLANDS": "NL", "FRANCE": "FR", "IRELAND": "IE",
                 "ITALY": "IT", "SPAIN": "ES", "BELGIUM": "BE", "USA": "US", "UNITED STATES": "US"}
        if c in names:
            return names[c]
    if tax_id:
        t = re.sub(r"[\s\-.]", "", tax_id.upper())
        if _GSTIN_RE.match(t):
            return "IN"
        if t[:2] in _VAT_PREFIX_COUNTRIES and t[2:3].isdigit():
            return _VAT_PREFIX_COUNTRIES[t[:2]]
    return None


def normalize_document_type(value: str | None, total_amount: float | None) -> str:
    v = (value or "").strip().upper().replace(" ", "_").replace("-", "_")
    aliases = {"TAX_INVOICE": "INVOICE", "VAT_INVOICE": "INVOICE", "COMMERCIAL_INVOICE": "INVOICE",
               "CREDIT": "CREDIT_NOTE", "CREDITNOTE": "CREDIT_NOTE", "CREDIT_MEMO": "CREDIT_NOTE",
               "DEBIT": "DEBIT_NOTE", "DEBITNOTE": "DEBIT_NOTE", "PRO_FORMA": "PROFORMA", "PROFORMA_INVOICE": "PROFORMA",
               "STATEMENT_OF_ACCOUNT": "STATEMENT", "DELIVERY": "DELIVERY_NOTE", "DELIVERYNOTE": "DELIVERY_NOTE",
               "PACKING_LIST": "DELIVERY_NOTE", "DESPATCH_NOTE": "DELIVERY_NOTE", "TIME_SHEET": "TIMESHEET", "": "INVOICE"}
    v = aliases.get(v, v)
    if v in DOCUMENT_TYPES:
        return v
    if total_amount is not None and total_amount < 0:
        return "CREDIT_NOTE"
    return "OTHER" if v else "INVOICE"


def normalize_direction(value: str | None) -> str | None:
    v = (value or "").strip().upper().replace(" ", "_").replace("-", "_")
    return v if v in DOCUMENT_DIRECTIONS else None


def normalize_tax_type(value: str | None) -> str:
    v = (value or "").strip().upper().replace(" ", "_")
    aliases = {"V.A.T.": "VAT", "VAT_20%": "VAT", "MWST": "VAT", "TVA": "VAT", "IVA": "VAT", "BTW": "VAT",
               "GOODS_AND_SERVICES_TAX": "GST", "SALES": "SALES_TAX", "WITHHOLDING": "WHT", "TDS": "WHT"}
    v = aliases.get(v, v)
    return v if v in TAX_TYPES else "OTHER"


_SO_PREFIX = "40"   # LT Foods sales orders
_PO_PREFIX = "66"   # SAP purchase orders
# A reference token with at least one digit ("4000231178", "PO-4500/22") - not "SO" / "No".
_REFERENCE_RE = re.compile(r"(?=[A-Za-z0-9\-/.]*\d)[A-Za-z0-9][A-Za-z0-9\-/.]*")
_SO_LABEL_RE = re.compile(r"sales\s*order|\bs\.?\s?o\b|our\s+order|order\s+ack", re.IGNORECASE)
_PO_LABEL_RE = re.compile(r"\bp\.?\s?o\b|purchase\s+order|customer\s+order|your\s+order|order\s+no", re.IGNORECASE)


def _references(value: str | None) -> list[str]:
    return _REFERENCE_RE.findall(value or "")


def _join_unique(values: list[str]) -> str | None:
    return ", ".join(dict.fromkeys(v.strip(".-/") for v in values if v.strip(".-/"))) or None


def _split_po_so(po_number: str | None, so_number: str | None,
                 additional_fields: list[AdditionalField]) -> tuple[str | None, str | None]:
    """Puts each reference in its field by prefix: 40... is a sales order (SO_number), 66... is
    a purchase order (po_number) - whichever field the LLM put it in. An empty field is filled
    from a matching labelled additional field ("Sales Order No", "Customer Order No" ...)."""
    po_values = [v for v in _references(po_number) if not v.startswith(_SO_PREFIX)]
    so_values = [v for v in _references(po_number) if v.startswith(_SO_PREFIX)]
    for value in _references(so_number):
        (po_values if value.startswith(_PO_PREFIX) else so_values).append(value)
    if not so_values:
        so_values = [v for f in additional_fields if _SO_LABEL_RE.search(f.field_name)
                     for v in _references(f.field_value) if v.startswith(_SO_PREFIX)]
    if not po_values:
        po_values = [v for f in additional_fields if _PO_LABEL_RE.search(f.field_name)
                     for v in _references(f.field_value) if v.startswith(_PO_PREFIX)][:1]
    return (po_number if po_values == _references(po_number) else _join_unique(po_values)), _join_unique(so_values)


def _abs_or_none(value: float | None) -> float | None:
    """Amounts are never negative in the contract (document_type carries the sign)."""
    if value is None:
        return None
    return abs(float(value))


def _confidence_from_grounding(
    grounding: list[FieldGroundingResult], avg_ocr_confidence: float | None, flags: list[dict]
) -> Confidence:
    """Grounding (did the value appear in the document text?) is the main signal; OCR'd pages
    scale it by the average OCR confidence; a critical flag on the field caps it at 0.5."""
    by_path = {r.field_path: r for r in grounding}
    flagged = {f["field"] for f in flags if f.get("severity") == "critical"}
    scale = avg_ocr_confidence if avg_ocr_confidence is not None else 1.0
    values: dict[str, float | None] = {}
    for name in CONFIDENCE_FIELDS:
        path = f"invoice_header.{name}"
        result = by_path.get(path)
        if result is None:
            values[name] = None
            continue
        if result.match_type in ("exact", "numeric", "date"):
            score = 1.0
        elif result.match_type == "fuzzy":
            score = max(0.5, min(1.0, result.score / 100.0))
        elif result.match_type == "inferred":
            score = 0.7
        elif result.match_type == "skipped":
            score = None
        else:
            score = 0.2
        if score is not None:
            score = round(score * scale, 2)
            if path in flagged:
                score = min(score, 0.5)
        values[name] = score
    return Confidence(**values)


def build_result(result_id: str, pipeline_result: dict, filename: str | None, email: EmailInfo | None) -> InvoiceResult:
    """Maps run_pipeline()'s return value to the JSON shape the API hands out."""
    extraction = pipeline_result["extraction"]
    h = extraction.invoice_header
    raw = h.model_dump()
    tax_id = _clean_text(raw.get("vendor_tax_id"))
    total = _abs_or_none(raw.get("total_amount"))
    header_data = {
        **{k: (_clean_text(v) if isinstance(v, str) else v) for k, v in raw.items()},
        # Reformatted here, after grounding, so grounding still checks the LLM's raw value.
        "invoice_date": _format_date(raw.get("invoice_date")),
        "due_date": _format_date(raw.get("due_date")),
        "invoice_period_from": _format_date(raw.get("invoice_period_from")),
        "invoice_period_to": _format_date(raw.get("invoice_period_to")),
        "vendor_tax_id": tax_id,
        "vendor_country": normalize_country(raw.get("vendor_country"), tax_id),
        "currency": normalize_currency(raw.get("currency")) or raw.get("currency") or None,
        "subtotal": _abs_or_none(raw.get("subtotal")),
        "tax_amount": _abs_or_none(raw.get("tax_amount")),
        "total_amount": total,
        "document_type": normalize_document_type(raw.get("document_type"), raw.get("total_amount")),
        "document_direction": normalize_direction(raw.get("document_direction")) or "VENDOR_TO_CUSTOMER",
    }
    additional_fields = [
        AdditionalField(field_name=f.field_name.strip(), field_value=_clean_text(f.field_value))
        for f in extraction.additional_fields
        if f.field_name and f.field_name.strip()
    ]
    header_data["po_number"], header_data["SO_number"] = _split_po_so(
        header_data.get("po_number"), header_data.get("SO_number"), additional_fields)
    # mandatory string fields keep "" rather than null only when the LLM produced ""
    for name in ("invoice_number", "vendor_name", "currency"):
        if header_data.get(name) is None:
            header_data[name] = ""
    header = InvoiceHeaderOut.model_validate(header_data)

    line_items = []
    for item in extraction.line_items:
        d = item.model_dump()
        d.update(
            {k: _clean_text(d.get(k)) for k in ("line_no", "description", "unit", "reference_code", "po_number", "po_item")},
            amount=_abs_or_none(d.get("amount")),
            gl_account=None, cost_center=None, profit_center=None,
        )
        if d["description"] is None:
            d["description"] = ""
        line_items.append(LineItemOut.model_validate(d))

    tax_details = [
        TaxDetail(tax_type=normalize_tax_type(t.tax_type), tax_rate=t.tax_rate,
                  tax_amount=_abs_or_none(t.tax_amount), taxable_amount=_abs_or_none(t.taxable_amount))
        for t in extraction.tax_details
    ]
    po_items = [
        PurchaseOrderItem(po_number=p.po_number.strip(), po_item=_clean_text(p.po_item), invoice_line_no=str(p.invoice_line_no),
                          quantity=p.quantity, unit_price=p.unit_price, amount=_abs_or_none(p.amount))
        for p in extraction.purchase_order_items
        if p.po_number and p.po_number.strip() and p.invoice_line_no
    ]

    flags = pipeline_result["flags"]
    return InvoiceResult(
        id=result_id,
        status=ResultStatus(pipeline_result["status"]),
        filename=filename,
        email=email,
        page_count=pipeline_result.get("page_count"),
        document_count_estimate=extraction.document_count_estimate or 1,
        invoice_header=header,
        line_items=line_items,
        additional_fields=additional_fields,
        tax_details=tax_details,
        purchase_order_items=po_items,
        confidence=_confidence_from_grounding(
            pipeline_result.get("grounding_results", []), pipeline_result.get("avg_ocr_confidence"), flags
        ),
        metadata=ExtractionMetadata(
            flags=[ValidationFlag(**flag) for flag in flags],
            model_name=pipeline_result["model_name"],
            prompt_version=pipeline_result["prompt_version"],
            extraction_source=pipeline_result["extraction_source"],
            avg_ocr_confidence=pipeline_result.get("avg_ocr_confidence"),
            processing_time_ms=pipeline_result["processing_time_ms"],
            completed_at=pipeline_result["completed_at"],
            source_text_hash=pipeline_result.get("source_text_hash"),
            pdf_sha256=pipeline_result.get("pdf_sha256"),
            sap_reference=pipeline_result.get("sap_reference"),
        ),
    )


def build_failure(result_id: str, error: str, filename: str | None, email: EmailInfo | None,
                  page_count: int | None = None) -> InvoiceResult:
    return InvoiceResult(
        id=result_id,
        status=ResultStatus.ERROR,
        filename=filename,
        email=email,
        error=error,
        page_count=page_count,
        document_count_estimate=0,
    )
