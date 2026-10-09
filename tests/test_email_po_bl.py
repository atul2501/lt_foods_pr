"""Subject_PO / Subject_BL / Body_PO / Body_BL in the `email` block, and email ingestion
extracting only the invoice PDFs of an email."""
from datetime import datetime
from types import SimpleNamespace

import fitz
import pytest

from app.email_ingest import ingest
from app.email_ingest.references import find_bl, find_po
from app.pipeline.doc_classify import is_invoice_pdf


@pytest.mark.parametrize("text, expected", [
    ("Invoice 15915 - PO 6600128207", "6600128207"),
    ("Purchase Order No: 4500-AB12", "4500-AB12"),
    ("purchesh order 4500123", "4500123"),
    ("P.O. # 4500777", "4500777"),
    ("PO#: AB-12345.", "AB-12345"),
    ("ref 6600128207 attached", "6600128207"),
    ("PO 6600128207 and PO 6600128208, PO 6600128207", "6600128207, 6600128208"),
    ("Our address: PO Box 12, Dubai", None),
    ("Please see attached", None),
    ("", None),
    (None, None),
])
def test_find_po(text, expected):
    assert find_po(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("B/L No. MAEU123456789", "MAEU123456789"),
    ("Bill of Lading: ABC12345", "ABC12345"),
    ("BL MSCU7654321 / BOL: 99887766", "MSCU7654321, 99887766"),
    ("bl number - hlcu1234567", "hlcu1234567"),
    ("BL attached, please check", None),
    ("Invoice 15915 - PO 6600128207", None),
    (None, None),
])
def test_find_bl(text, expected):
    assert find_bl(text) == expected


def _pdf(*pages: str) -> bytes:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        # Enough text that triage classifies the page as digital (no OCR in tests).
        page.insert_text((50, 72), text + "\n" + "Lorem ipsum dolor sit amet consectetur. " * 8, fontsize=8)
    data = doc.tobytes()
    doc.close()
    return data


def test_invoice_title_is_invoice():
    assert is_invoice_pdf(_pdf("TAX INVOICE\nInvoice No: 15915"))
    assert is_invoice_pdf(_pdf("Packing list page", "Commercial Invoice (Original)"))
    assert is_invoice_pdf(_pdf("CREDIT NOTE"))


def test_packing_list_mentioning_invoice_is_not_invoice():
    assert not is_invoice_pdf(_pdf("PACKING LIST\nInvoice No: 15915\nInvoice Date: 2026-10-01"))
    assert not is_invoice_pdf(_pdf("BILL OF LADING\nShipper: LT Foods"))


def test_reference_starting_66_counts_as_invoice():
    assert is_invoice_pdf(_pdf("PACKING LIST\nOrder ref 6600128207"))


def test_only_first_pages_are_read(monkeypatch):
    monkeypatch.setattr("app.config.settings.invoice_detect_pages", 4)
    monkeypatch.setattr("app.config.settings.max_pages_to_process", 6)
    assert not is_invoice_pdf(_pdf("Cover", "Cover", "Cover", "Cover", "TAX INVOICE"))


def _message(subject, text, attachments):
    return SimpleNamespace(
        uid="1", subject=subject, from_="ap@vendor.com", text=text, html="",
        headers={"message-id": ["<m1@vendor.com>"]}, date=datetime(2026, 10, 7), attachments=attachments,
    )


def _att(name, payload):
    return SimpleNamespace(filename=name, content_type="application/pdf", payload=payload)


@pytest.fixture
def captured(monkeypatch):
    extracted = []
    monkeypatch.setattr(ingest, "allowed_senders", lambda: [])
    monkeypatch.setattr(ingest.results_store, "exists", lambda key: False)
    monkeypatch.setattr(ingest, "_extract_attachment",
                        lambda key, att, email, log: extracted.append((key, att.filename, email)) or True)
    return extracted


def test_only_invoice_pdfs_are_extracted(captured):
    msg = _message(
        "Invoice 15915 PO 6600128207 BL MAEU123456789",
        "Hi, please also note purchase order 4500999 for the next lot.",
        [_att("invoice.pdf", _pdf("TAX INVOICE")), _att("packing.pdf", _pdf("PACKING LIST\nInvoice No: 15915")),
         _att("bl.pdf", _pdf("BILL OF LADING"))],
    )
    assert ingest._handle_message(msg) is True
    assert [name for _, name, _ in captured] == ["invoice.pdf"]
    email = captured[0][2]
    assert (email.Subject_PO, email.Subject_BL, email.Body_PO, email.Body_BL) == ("6600128207", "MAEU123456789", "4500999", None)
    assert [(a.filename, a.is_invoice) for a in email.attachments] == [
        ("invoice.pdf", True), ("packing.pdf", False), ("bl.pdf", False)]


def test_email_without_invoice_pdf_is_skipped(captured):
    msg = _message("Docs", "", [_att("packing.pdf", _pdf("PACKING LIST"))])
    assert ingest._handle_message(msg) is None
    assert captured == []


def test_invoices_only_switch(captured, monkeypatch):
    monkeypatch.setattr(ingest.settings, "email_invoices_only", False)
    msg = _message("Docs", "", [_att("packing.pdf", _pdf("PACKING LIST"))])
    assert ingest._handle_message(msg) is True
    assert [name for _, name, _ in captured] == ["packing.pdf"]


# --- the real "PRE_ALERT" email: PO/BL in the subject and in an HTML table in the body ---
PRE_ALERT_SUBJECT = "PRE_ALERT :- PO-9400000909 // INV_9103002612 // BL-46671906"
PRE_ALERT_HTML = """<html><body><div>Hi All,</div><br><div>PFA docs for the shipment scheduled to arrive on 31/07.</div><br>
<table border="1"><tbody>
<tr style="background:yellow"><td><b>PO No.</b></td><td><b>Inv no.</b></td><td><b>Supplier</b></td><td><b>Description</b></td>
<td><b>Qty (MT)</b></td><td><b>No. of FCL</b></td><td><b>Booking/BL<br>number</b></td><td><b>Destination</b></td>
<td><b>Freight Cost (Per FCL)</b></td><td><b>Inco Term</b></td><td><b>CN Code</b></td><td><b>ETA Port</b></td></tr>
<tr><td>9400000909</td><td><a href="#">9103002612</a></td><td>LT Foods Limited-Varpal</td><td>BASANT XL BASMATI RICE 20 KG</td>
<td>72</td><td>3</td><td>46671906</td><td>London Gateway</td><td>$995</td><td>C&amp;I</td><td>1006309892</td><td>31-07-2026</td></tr>
</tbody></table><br>
<p>Thanks &amp; regards,</p><p>Neeraj Sharma | Import &amp; Logistics Coordinator</p>
<table><tr><td><img src="logo.png"></td><td>+91 8109899605<br>Unit 5, Midas, River Way<br>Harlow, CM20 2GJ</td></tr></table>
</body></html>"""


def _pre_alert_body():
    return ingest._email_text(SimpleNamespace(text="", html=PRE_ALERT_HTML))


def test_pre_alert_subject():
    assert find_po(PRE_ALERT_SUBJECT) == "9400000909"
    assert find_bl(PRE_ALERT_SUBJECT) == "46671906"


def test_pre_alert_body_table():
    body = _pre_alert_body()
    assert find_po(body, PRE_ALERT_HTML) == "9400000909"
    assert find_bl(body, PRE_ALERT_HTML) == "46671906"


def test_table_with_several_rows():
    html = ("<table><tr><th>PO Number</th><th>B/L No</th></tr>"
            "<tr><td>4500111</td><td>AB1</td></tr><tr><td>4500222</td><td>MAEU22222</td></tr></table>")
    assert find_po(None, html) == "4500111, 4500222"
    assert find_bl(None, html) == "MAEU22222"   # "AB1" too short to be a reference


def test_table_without_po_bl_columns():
    html = "<table><tr><td>Invoice</td><td>Amount</td></tr><tr><td>9103002612</td><td>995</td></tr></table>"
    assert find_po(None, html) is None and find_bl(None, html) is None


def test_pre_alert_email_end_to_end(captured):
    msg = _message(PRE_ALERT_SUBJECT, "", [_att("INV_9103002612.pdf", _pdf("TAX INVOICE"))])
    msg.html = PRE_ALERT_HTML
    assert ingest._handle_message(msg) is True
    email = captured[0][2]
    assert (email.Subject_PO, email.Subject_BL, email.Body_PO, email.Body_BL) == (
        "9400000909", "46671906", "9400000909", "46671906")


@pytest.mark.parametrize("line, expected", [
    ("COMM.INVOICE", True),            # OCR of the LT Foods PRE_ALERT commercial invoice
    ("Comm. Invoice", True),
    ("COML INVOICE", True),
    ("TAXINVOICE", True),
    ("TAX INVOlCE", True),             # OCR l for I
    ("IMPORT INVOICE", True),          # Maersk carrier invoice
    ("FREIGHT INVOICE", True),
    ("Involce No. & Date", False),
    ("INVOICE NO.", False),
    ("INVOICE VALUE", False),
    ("CERTIFICATE OF INSURANCE", False),
])
def test_invoice_title_variants(line, expected):
    from app.pipeline.doc_classify import _is_invoice_line
    assert _is_invoice_line(line) is expected


# --- only invoices billed to LT Foods (customer starting with LT / L.T.) are kept ---
from app.schemas.envelope import EmailInfo  # noqa: E402


def _header(customer=None, company=None):
    return SimpleNamespace(customer_name=customer, company_code=company)


@pytest.mark.parametrize("customer, company, expected", [
    ("LT FOODS UK LIMITED", None, True),
    ("L.T. Foods Europe Ltd", None, True),
    ("L. T. FOODS LTD", None, True),
    ("lt foods", None, True),
    ("LTFOODS", None, True),
    (None, "LT Foods Europe Ltd", True),
    ("Tesco Stores Ltd", None, False),
    ("LTD Logistics", None, False),
    ("ACME for LT Foods", None, False),
    (None, None, None),
])
def test_customer_matches(customer, company, expected):
    assert ingest._customer_matches(_header(customer, company)) is expected


def test_customer_check_off(monkeypatch):
    monkeypatch.setattr(ingest.settings, "email_customer_prefix", "")
    assert ingest._customer_matches(_header("Tesco")) is None


@pytest.fixture
def extract_env(monkeypatch):
    written, pushed = [], []
    monkeypatch.setattr(ingest.results_store, "save_pdf", lambda key, data: None)
    monkeypatch.setattr(ingest.results_store, "pdf_url", lambda key: f"/pdf/{key}")
    monkeypatch.setattr(ingest.results_store, "write_pending", lambda key, data: written.append(data))
    monkeypatch.setattr(ingest.results_store, "register_hash", lambda data, key: None)
    monkeypatch.setattr(ingest.results_store, "clear_fail", lambda key: None)
    monkeypatch.setattr(ingest, "_push_to_sap", lambda *a: pushed.append(a))
    monkeypatch.setattr(ingest, "run_pipeline", lambda *a, **k: {"status": "success"})

    def run(customer):
        monkeypatch.setattr(ingest, "build_result", lambda key, result, filename, email: SimpleNamespace(
            invoice_header=_header(customer), pdf_url=None, model_dump=lambda mode: {"customer": customer}))
        log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, exception=lambda *a, **k: None)
        done = ingest._extract_attachment("k1", _att("inv.pdf", b"%PDF-1.4"), EmailInfo(message_id="m1"), log)
        return done, list(written), list(pushed)
    return run


def test_lt_foods_customer_is_written(extract_env):
    done, written, pushed = extract_env("LT FOODS UK LIMITED")
    assert done and written == [{"customer": "LT FOODS UK LIMITED"}] and len(pushed) == 1


def test_other_customer_is_skipped(extract_env):
    done, written, pushed = extract_env("ACME Ltd")
    assert done and written == [] and pushed == []


def test_unknown_customer_is_kept(extract_env):
    done, written, _ = extract_env(None)
    assert done and written == [{"customer": None}]


def test_other_customer_kept_when_check_off(extract_env, monkeypatch):
    monkeypatch.setattr(ingest.settings, "email_customer_prefix", "")
    _, written, _ = extract_env("ACME Ltd")
    assert written == [{"customer": "ACME Ltd"}]
