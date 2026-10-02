from dataclasses import dataclass

import re

from app.config import settings
from app.core.constants import CRITICAL_HEADER_STRING_FIELDS, WARNING_HEADER_FIELDS
from app.pipeline.grounding import parse_date
from app.pipeline.to_response import normalize_country, normalize_document_type
from app.schemas.invoice_schema import InvoiceExtraction

_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_CURRENCY_SYMBOL_OK = {"£", "€", "$", "₹"}  # normalized to ISO in to_response.py


@dataclass
class RuleViolation:
    field: str
    reason: str
    severity: str  # "warning" | "critical"
    detail: str = ""


def check_mandatory_fields(extraction: InvoiceExtraction) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    header = extraction.invoice_header.model_dump()

    for field_name in CRITICAL_HEADER_STRING_FIELDS:
        if not header.get(field_name):
            violations.append(RuleViolation(f"invoice_header.{field_name}", "missing_mandatory", "critical"))

    for field_name in WARNING_HEADER_FIELDS:
        if field_name == "vendor_country" and normalize_country(header.get("vendor_country"), header.get("vendor_tax_id")):
            continue  # derived from the VAT/GST number in to_response.py
        if not header.get(field_name):
            violations.append(RuleViolation(f"invoice_header.{field_name}", "missing_should_have", "warning"))

    return violations


def check_contract_values(extraction: InvoiceExtraction) -> list[RuleViolation]:
    """Format rules of the SAP contract: ISO date, ISO currency, document_type enumeration,
    numeric po_number, tax_details consistency, document_count_estimate."""
    violations: list[RuleViolation] = []
    header = extraction.invoice_header

    if header.invoice_date and parse_date(header.invoice_date) is None:
        violations.append(RuleViolation("invoice_header.invoice_date", "unparseable_date", "critical", header.invoice_date))
    for name in ("due_date", "invoice_period_from", "invoice_period_to"):
        value = getattr(header, name)
        if value and parse_date(value) is None:
            violations.append(RuleViolation(f"invoice_header.{name}", "unparseable_date", "warning", value))

    cur = (header.currency or "").strip().upper()
    if cur and not _ISO_CURRENCY_RE.match(cur) and cur not in _CURRENCY_SYMBOL_OK:
        violations.append(RuleViolation("invoice_header.currency", "not_iso_currency", "warning", cur))

    if normalize_document_type(header.document_type, header.total_amount) == "OTHER":
        violations.append(RuleViolation("invoice_header.document_type", "unknown_document_type", "warning", header.document_type))

    if header.po_number and not re.search(r"\d{4,}", header.po_number):
        violations.append(RuleViolation("invoice_header.po_number", "po_number_format", "warning", header.po_number))

    if extraction.tax_details:
        tax_sum = sum(t.tax_amount or 0.0 for t in extraction.tax_details)
        tolerance = max(settings.arithmetic_tolerance_abs, abs(header.tax_amount) * settings.arithmetic_tolerance_rel)
        if abs(tax_sum - header.tax_amount) > tolerance:
            violations.append(
                RuleViolation("tax_details", "tax_details_mismatch", "warning",
                              f"sum(tax_details.tax_amount)={tax_sum:.2f} != tax_amount({header.tax_amount})")
            )

    if (extraction.document_count_estimate or 1) > 1:
        violations.append(
            RuleViolation("_document", "multiple_invoices_in_file", "warning",
                          f"document_count_estimate={extraction.document_count_estimate}")
        )
    return violations


def check_arithmetic(extraction: InvoiceExtraction) -> list[RuleViolation]:
    header = extraction.invoice_header
    expected_total = header.subtotal + header.tax_amount
    tolerance = max(
        settings.arithmetic_tolerance_abs,
        abs(header.total_amount) * settings.arithmetic_tolerance_rel,
    )
    if abs(expected_total - header.total_amount) > tolerance:
        return [
            RuleViolation(
                "invoice_header.total_amount",
                "arithmetic_mismatch",
                "warning",
                detail=(
                    f"subtotal({header.subtotal}) + tax_amount({header.tax_amount}) "
                    f"!= total_amount({header.total_amount})"
                ),
            )
        ]
    return []


def check_line_items(extraction: InvoiceExtraction) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    if not extraction.line_items:
        return [RuleViolation("line_items", "empty_line_items", "critical")]

    for idx, item in enumerate(extraction.line_items):
        if not item.description:
            violations.append(RuleViolation(f"line_items[{idx}].description", "missing_mandatory", "critical"))
        if item.amount is None:
            violations.append(RuleViolation(f"line_items[{idx}].amount", "missing_mandatory", "critical"))
    return violations


def run_business_rules(extraction: InvoiceExtraction) -> list[RuleViolation]:
    return [
        *check_mandatory_fields(extraction),
        *check_contract_values(extraction),
        *check_arithmetic(extraction),
        *check_line_items(extraction),
    ]
