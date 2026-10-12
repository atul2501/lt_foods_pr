"""sum(line_items.amount) must match the subtotal (CHEP Inv_6600128209: the LLM listed the
11 per-charge rows of the PRODUCT PRICES pages instead of the 2 SUMMARY rows)."""
from app.pipeline.business_rules import check_line_total
from app.schemas.invoice_schema import InvoiceExtraction

HEADER = {
    "invoice_number": "4402158112", "invoice_date": "2026-08-01", "vendor_name": "CHEP UK Limited",
    "vendor_tax_id": "GB745231643", "currency": "GBP", "subtotal": 15879.71, "tax_amount": 3175.94,
    "total_amount": 19055.65, "document_type": "INVOICE",
}


def _extraction(amounts: list[float]) -> InvoiceExtraction:
    return InvoiceExtraction.model_validate({
        "invoice_header": HEADER,
        "line_items": [{"description": f"line {i}", "amount": a} for i, a in enumerate(amounts, 1)],
    })


def test_summary_lines_match_subtotal():
    assert check_line_total(_extraction([14430.60, 1449.11])) == []


def test_breakdown_lines_with_flipped_credits_are_flagged():
    breakdown = [12285.00, 646.38, 64.37, 1590.21, 1958.06, 1802.70, 2095.60, 173.72, 214.79, 1269.00, 234.00]
    violations = check_line_total(_extraction(breakdown))
    assert len(violations) == 1
    assert violations[0].reason == "line_total_mismatch" and violations[0].severity == "critical"
    assert "22333.83" in violations[0].detail


def test_negative_credit_rows_count_as_sent_to_sap():
    # signed they reconcile, but SAP receives them positive - so this must still be flagged
    signed = [12285.00, 646.38, 64.37, 1590.21, -1958.06, 1802.70, 2095.60, 173.72, 214.79, -1269.00, 234.00]
    assert check_line_total(_extraction(signed))


def test_rounding_within_tolerance_passes():
    assert check_line_total(_extraction([14430.60, 1449.12])) == []
