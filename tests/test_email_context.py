"""Email subject + body: captured into the result's `email` block and given to the LLM."""
import json
from types import SimpleNamespace

import jsonschema

from app.email_ingest import ingest
from app.pipeline.grounding import ground_extraction
from app.pipeline.llm_structurer import build_prompt
from app.pipeline.to_response import build_result
from app.schemas.envelope import EmailInfo
from app.schemas.invoice_schema import InvoiceExtraction
from tests.test_contract import LLM_OUTPUT, SCHEMA, SOURCE_TEXT, _pipeline_result

PDF_BYTES = b"%PDF-1.4 fake"


def _msg(text="", html="", attachments=()):
    return SimpleNamespace(text=text, html=html, attachments=list(attachments))


def _att(filename, content_type, payload):
    return SimpleNamespace(filename=filename, content_type=content_type, payload=payload)


def test_body_prefers_text_part():
    assert ingest._email_body(_msg(text="  Hi team,\n\n\n\nInvoice   attached  ", html="<p>ignored</p>")) == "Hi team,\n\nInvoice attached"


def test_body_strips_html_when_no_text_part():
    html = "<html><head><style>p{color:red}</style></head><body><p>Invoice 15915</p><p>PO&nbsp;6600128207</p></body></html>"
    body = ingest._email_body(_msg(html=html))
    assert "Invoice 15915" in body and "PO\xa06600128207" in body
    assert "color" not in body and "<" not in body


def test_body_is_capped(monkeypatch):
    monkeypatch.setattr(ingest.settings, "email_body_max_chars", 10)
    assert ingest._email_body(_msg(text="x" * 50)) == "x" * 10


def test_empty_body_is_none():
    assert ingest._email_body(_msg()) is None


def test_attachments_lists_every_file():
    atts = ingest._email_attachments(_msg(attachments=[
        _att("INV-15915.pdf", "application/octet-stream", PDF_BYTES),
        _att("logo.png", "image/png", b"\x89PNG"),
    ]))
    assert [(a.filename, a.is_pdf, a.size_bytes) for a in atts] == [("INV-15915.pdf", True, len(PDF_BYTES)), ("logo.png", False, 4)]


def test_email_context_and_switch(monkeypatch):
    email = EmailInfo(message_id="m1", sender="ap@vendor.com", subject="Invoice 15915 - PO 6600128207", body="Please pay")
    assert ingest._email_context(email) == "Subject: Invoice 15915 - PO 6600128207\nFrom: ap@vendor.com\n\nPlease pay"
    monkeypatch.setattr(ingest.settings, "email_context_in_prompt", False)
    assert ingest._email_context(email) is None


def test_prompt_has_email_block_before_invoice_text():
    prompt = build_prompt("INVOICE BODY", "Subject: PO 123")
    assert prompt.index("--- EMAIL (context only) START ---") < prompt.index("Subject: PO 123") < prompt.index("--- INVOICE TEXT START ---")
    assert "EMAIL (context only)" not in build_prompt("INVOICE BODY")


def test_value_only_in_email_is_grounded():
    output = json.loads(json.dumps(LLM_OUTPUT))
    output["invoice_header"]["po_number"] = "7700999111"
    extraction = InvoiceExtraction.model_validate(output)

    def po_grounded(text):
        return next(r.grounded for r in ground_extraction(extraction, text) if r.field_path == "invoice_header.po_number")

    assert not po_grounded(SOURCE_TEXT)
    assert po_grounded(f"{SOURCE_TEXT}\nSubject: Invoice 15915 - PO 7700999111")


def test_result_with_email_body_matches_contract():
    extraction = InvoiceExtraction.model_validate(LLM_OUTPUT)
    email = EmailInfo(
        message_id="<m1@vendor.com>", sender="ap@vendor.com", subject="Invoice 15915", body="Please find attached.",
        attachments=ingest._email_attachments(_msg(attachments=[_att("INV-15915.pdf", "application/pdf", PDF_BYTES)])),
    )
    data = build_result("key-1", _pipeline_result(extraction), "INV-15915.pdf", email).model_dump(mode="json")
    jsonschema.Draft202012Validator(SCHEMA).validate(data)
    assert data["email"]["body"] == "Please find attached."
    assert data["email"]["attachments"] == [
        {"filename": "INV-15915.pdf", "content_type": "application/pdf", "size_bytes": len(PDF_BYTES), "is_pdf": True}
    ]
