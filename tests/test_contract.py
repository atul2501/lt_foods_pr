"""The API result must validate against the SAP ZFTVIA contract schema (contract/)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

from app.pipeline.business_rules import run_business_rules
from app.pipeline.grounding import ground_extraction
from app.pipeline.status import assign_status
from app.pipeline.normalize import NormalizedDocument
from app.pipeline.to_response import build_failure, build_result
from app.schemas.invoice_schema import InvoiceExtraction

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "contract" / "ZFTVIA_OCR_API_SCHEMA_V1.json").read_text(encoding="utf-8"))

SOURCE_TEXT = """Brookshaw Stuart Ltd  Unit 4, Riverside Business Park, Rochdale, OL11 2PX
VAT Reg No GB123456789   INVOICE   Invoice No 15915   Date 18/09/2026   Due 18/10/2026
To: LT Foods Europe Ltd, Harrow   Your PO No 6600128207   Delivery Note DN 44821
1  Pallet wrap 500mm x 300m clear   40 ROL  21.50  860.00
2  Strapping tape 12mm x 66m      120 ROL  4.189  502.70
Sub total 1,362.70   VAT 20% 272.54   Total £1,635.24
"""

LLM_OUTPUT = {
    "invoice_header": {
        "invoice_number": "15915", "invoice_date": "18/09/2026", "due_date": "18/10/2026", "payment_terms": None,
        "company_code": "LT Foods Europe Ltd", "vendor_name": "Brookshaw Stuart Ltd",
        "vendor_address": "Unit 4, Riverside Business Park, Rochdale, OL11 2PX", "vendor_tax_id": "GB123456789",
        "vendor_country": None, "vendor_bank_name": None, "vendor_account_no": None, "vendor_sort_code": None,
        "vendor_iban": None, "customer_name": "LT Foods Europe Ltd", "customer_address": "Harrow",
        "po_number": "6600128207", "reference_number": "DN 44821", "currency": "£",
        "subtotal": 1362.70, "tax_amount": 272.54, "tax_percent": 20, "total_amount": 1635.24,
        "document_type": "Tax Invoice", "document_direction": None, "invoice_period_from": None, "invoice_period_to": None,
    },
    "line_items": [
        {"line_no": "1", "description": "Pallet wrap 500mm x 300m clear", "quantity": 40, "unit": "ROL", "unit_price": 21.50,
         "amount": 860.00, "tax_percent": 20, "tax_amount": None, "reference_code": None, "po_number": None, "po_item": None,
         "gl_account": None, "cost_center": None, "profit_center": None},
        {"line_no": "2", "description": "Strapping tape 12mm x 66m", "quantity": 120, "unit": "ROL", "unit_price": 4.189,
         "amount": 502.70, "tax_percent": 20, "tax_amount": None, "reference_code": "N/A", "po_number": None, "po_item": None,
         "gl_account": None, "cost_center": None, "profit_center": None},
    ],
    "additional_fields": [{"field_name": "Delivery Note", "field_value": "DN 44821"}],
    "tax_details": [{"tax_type": "VAT", "tax_rate": 20, "tax_amount": 272.54, "taxable_amount": 1362.70}],
    "purchase_order_items": [],
    "document_count_estimate": 1,
}


def _pipeline_result(extraction: InvoiceExtraction) -> dict:
    grounding = ground_extraction(extraction, SOURCE_TEXT)
    violations = run_business_rules(extraction)
    normalized = NormalizedDocument(source_text=SOURCE_TEXT, lines=[], extraction_source="digital")
    status = assign_status(grounding, violations, normalized)
    return {
        "extraction": extraction, "status": status.status, "flags": status.flags, "grounding_results": grounding,
        "page_count": 1, "avg_ocr_confidence": None, "model_name": "test", "prompt_version": "test",
        "extraction_source": "digital", "source_text": SOURCE_TEXT, "source_text_hash": "x", "pdf_sha256": "y", "sap_reference": "VIA-2026-000000001",
        "processing_time_ms": 1, "completed_at": datetime.now(timezone.utc),
    }


def test_result_matches_sap_contract():
    extraction = InvoiceExtraction.model_validate(LLM_OUTPUT)
    result = build_result("job-1", _pipeline_result(extraction), "Brookshaw_15915.pdf", None)
    data = result.model_dump(mode="json")
    jsonschema.Draft202012Validator(SCHEMA).validate(data)
    header = data["invoice_header"]
    assert header["invoice_date"] == "2026-09-18" and header["due_date"] == "2026-10-18"
    assert header["currency"] == "GBP" and header["vendor_country"] == "GB"
    assert header["document_type"] == "INVOICE" and header["document_direction"] == "VENDOR_TO_CUSTOMER"
    assert data["line_items"][1]["reference_code"] is None
    assert data["line_items"][0]["gl_account"] is None
    assert data["api_version"] == "1" and data["page_count"] == 1 and data["document_count_estimate"] == 1
    assert data["confidence"]["invoice_number"] == 1.0 and data["confidence"]["total_amount"] == 1.0
    assert data["status"] in ("success", "needs_review")


def test_missing_invoice_date_is_null_not_error():
    llm_output = json.loads(json.dumps(LLM_OUTPUT))
    llm_output["invoice_header"]["invoice_date"] = ""
    extraction = InvoiceExtraction.model_validate(llm_output)
    result = build_result("job-1", _pipeline_result(extraction), "Brookshaw_15915.pdf", None)
    data = result.model_dump(mode="json")
    jsonschema.Draft202012Validator(SCHEMA).validate(data)
    assert data["invoice_header"]["invoice_date"] is None
    assert data["status"] == "needs_review"


def test_failure_matches_sap_contract():
    data = build_failure("job-2", "NoUsableTextError: no text", "scan.pdf", None, page_count=2).model_dump(mode="json")
    jsonschema.Draft202012Validator(SCHEMA).validate(data)
    assert data["status"] == "error" and data["invoice_header"] is None


def test_sample_in_contract_validates():
    sample = json.loads((ROOT / "contract" / "SAMPLE_RESPONSE_Brookshaw_15915.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(SCHEMA).validate(sample)


# --- PO / SO / BL: printed label first, else Reference (66 -> PO, 40 -> SO) ---
import pytest  # noqa: E402

from app.pipeline.to_response import _determine_po_so_bl  # noqa: E402
from app.schemas.invoice_schema import AdditionalField  # noqa: E402


def _build(**header):
    llm_output = json.loads(json.dumps(LLM_OUTPUT))
    additional = header.pop("additional_fields", None)
    llm_output["invoice_header"].update(header)
    if additional is not None:
        llm_output["additional_fields"] = additional
    extraction = InvoiceExtraction.model_validate(llm_output)
    data = build_result("job-1", _pipeline_result(extraction), "x.pdf", None).model_dump(mode="json")
    jsonschema.Draft202012Validator(SCHEMA).validate(data)
    return data


def test_so_number_sits_after_po_number():
    data = _build(reference_number="4000231178")
    keys = list(data["invoice_header"])
    assert keys[keys.index("po_number") + 1] == "SO_number"
    assert data["invoice_header"]["po_number"] == "6600128207"
    assert data["invoice_header"]["SO_number"] == "4000231178"
    assert "SO_number" in data["confidence"]


def test_so_number_null_when_absent():
    assert _build()["invoice_header"]["SO_number"] is None


MAERSK_TEXT = "IMPORT INVOICE\nInvoice Number 7544757640\nVessel/Voyage Direction Reference\nBill of Lading 263377714\n"


@pytest.mark.parametrize("text, llm, ref, fields, expected", [
    # Statement 1: PO / BL printed on the PDF -> the number as printed, no 66 check
    ("PO No: 4500123", {}, None, [], ("4500123", None, None)),
    ("Purchase Order 6600128207", {}, "4000231178", [], ("6600128207", "4000231178", None)),
    ("P.O. Number\n...\n4500999", {"po": "4500999"}, None, [], ("4500999", None, None)),  # table layout
    (MAERSK_TEXT, {}, None, [], (None, None, "263377714")),
    ("B/L No. MAEU1234", {}, None, [], (None, None, "MAEU1234")),
    # per field: BL printed, PO from the Reference
    (MAERSK_TEXT, {}, "6600128207", [], ("6600128207", None, "263377714")),
    # Statements 2-4: no label -> Reference, 66 = PO, 40 = SO, only the number
    ("Invoice 1", {}, "669384043", [], ("669384043", None, None)),
    ("Invoice 1", {}, "{PO}669384043", [], ("669384043", None, None)),
    ("Invoice 1", {}, "4000231178", [], (None, "4000231178", None)),
    ("Invoice 1", {}, None, [("Your Reference", "6612345678")], ("6612345678", None, None)),
    ("Invoice 1", {}, None, [("Delivery Note", "6612345678")], (None, None, None)),  # not a reference
    # Statement 5: other series -> both blank
    ("Invoice 1", {}, "DN 44821", [], (None, None, None)),
    ("Invoice 1", {"po": "4500123"}, "12345", [], (None, None, None)),   # LLM PO without a printed label
    ("P.O. Box 12, London", {}, "6600128207", [], ("6600128207", None, None)),  # P.O. Box is no PO
    (None, {}, None, [], (None, None, None)),
    # a printed sales order (40...) the LLM found stays the SO
    ("Invoice 1", {"so": "SO 4000231178"}, None, [], (None, "4000231178", None)),
])
def test_determine_po_so_bl(text, llm, ref, fields, expected):
    additional = [AdditionalField(field_name=n, field_value=v) for n, v in fields]
    assert _determine_po_so_bl(text or "", llm.get("po"), llm.get("so"), llm.get("bl"), ref, additional) == expected


def test_bl_number_in_result():
    data = _build(BL_number="263377714")   # SOURCE_TEXT has no BL label -> not trusted
    assert data["invoice_header"]["BL_number"] is None
    keys = list(data["invoice_header"])
    assert keys[keys.index("SO_number") + 1] == "BL_number"


def test_ungrounded_so_number_is_reported():
    extraction = InvoiceExtraction.model_validate(
        {**LLM_OUTPUT, "invoice_header": {**LLM_OUTPUT["invoice_header"], "SO_number": "4000231178"}})
    result = next(r for r in ground_extraction(extraction, SOURCE_TEXT) if r.field_path == "invoice_header.SO_number")
    assert not result.grounded
