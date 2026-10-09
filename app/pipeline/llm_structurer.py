import json

from app.core.exceptions import LLMFormatError
from app.logging_conf import get_logger
from app.schemas.invoice_schema import InvoiceExtraction
from app.schemas.ollama_json_schema import get_invoice_json_schema
from app.services.ollama_client import OllamaClient

logger = get_logger(__name__)

PROMPT_VERSION = "v7-so-number"

# A literal worked example (the canonical sample of the SAP ZFTVIA contract V1, see
# contract/ZFTVIA_OCR_API_CONTRACT_V1.json) rather than just the abstract JSON schema.
# Confirmed necessary: Ollama's `format` JSON-schema constraint is NOT enforced by Ollama
# Cloud's serving backend (several cloud models silently invented their own flat/renamed
# structure). For self-hosted Ollama the `format` param IS enforced and this example is
# redundant but harmless - so it stays in the prompt for both paths.
EXAMPLE_JSON = json.dumps(
    {
        "invoice_header": {
            "invoice_number": "15915",
            "invoice_date": "2026-09-18",
            "due_date": "2026-10-18",
            "payment_terms": "30 days from invoice date",
            "company_code": "LT Foods Europe Ltd",
            "vendor_name": "Brookshaw Stuart Ltd",
            "vendor_address": "Unit 4, Riverside Business Park, Rochdale, OL11 2PX, United Kingdom",
            "vendor_tax_id": "GB123456789",
            "vendor_country": "GB",
            "vendor_bank_name": "Barclays",
            "vendor_account_no": "12345678",
            "vendor_sort_code": "20-00-00",
            "vendor_iban": None,
            "customer_name": "LT Foods Europe Ltd",
            "customer_address": "Harrow, Middlesex, United Kingdom",
            "po_number": "6600128207",
            "SO_number": "4000231178",
            "reference_number": "DN 44821",
            "currency": "GBP",
            "subtotal": 1362.70,
            "tax_amount": 272.54,
            "tax_percent": 20,
            "total_amount": 1635.24,
            "document_type": "INVOICE",
            "document_direction": "VENDOR_TO_CUSTOMER",
            "invoice_period_from": None,
            "invoice_period_to": None,
        },
        "line_items": [
            {
                "line_no": "1",
                "description": "Pallet wrap 500mm x 300m clear",
                "quantity": 40,
                "unit": "ROL",
                "unit_price": 21.50,
                "amount": 860.00,
                "tax_percent": 20,
                "tax_amount": 172.00,
                "reference_code": "PW500",
                "po_number": "6600128207",
                "po_item": "10",
                "gl_account": None,
                "cost_center": None,
                "profit_center": None,
            },
            {
                "line_no": "2",
                "description": "Strapping tape 12mm x 66m",
                "quantity": 120,
                "unit": "ROL",
                "unit_price": 4.189,
                "amount": 502.70,
                "tax_percent": 20,
                "tax_amount": 100.54,
                "reference_code": "ST12",
                "po_number": "6600128207",
                "po_item": "20",
                "gl_account": None,
                "cost_center": None,
                "profit_center": None,
            },
        ],
        "additional_fields": [
            {"field_name": "Delivery Note", "field_value": "DN 44821"},
            {"field_name": "Sales Order No", "field_value": "4000231178"},
            {"field_name": "Customer Account", "field_value": "LTF001"},
            {"field_name": "Vehicle Reg", "field_value": "MX21 ABC"},
        ],
        "tax_details": [{"tax_type": "VAT", "tax_rate": 20, "tax_amount": 272.54, "taxable_amount": 1362.70}],
        "purchase_order_items": [
            {"po_number": "6600128207", "po_item": "10", "invoice_line_no": "1", "quantity": 40, "unit_price": 21.50, "amount": 860.00},
            {"po_number": "6600128207", "po_item": "20", "invoice_line_no": "2", "quantity": 120, "unit_price": 4.189, "amount": 502.70},
        ],
        "document_count_estimate": 1,
    },
    indent=2,
)

SYSTEM_INSTRUCTIONS = f"""You are an invoice data extraction assistant for an SAP accounts-payable system. You will be given the raw OCR/extracted text of one supplier document (invoice, credit note, debit note, pro-forma or statement). Extract the fields into the exact JSON structure requested.

Your output MUST have EXACTLY these top-level keys: `invoice_header` (object), `line_items` (array), `additional_fields` (array), `tax_details` (array), `purchase_order_items` (array), `document_count_estimate` (integer). Do NOT flatten invoice_header's fields to the top level. Do NOT rename any field (use `tax_amount` not `tax`, `total_amount` not `total`, `payment_terms` not `terms`, `vendor_name`/`vendor_address` as flat strings not a nested "vendor" object, `customer_name`/`customer_address` not a nested "buyer"/"bill_to" object). Do NOT add fields that are not in the structure below - anything else goes into `additional_fields`.

Here is a worked example showing the EXACT structure, field names and nesting to use (the values are an example, not data to copy):
{EXAMPLE_JSON}

STRICT RULES - follow exactly, this data feeds financial accounting:
1. Only use values that literally appear in the provided text. NEVER invent, guess, calculate or hallucinate a value that is not present in the text.
2. Exception - these may be inferred from context: `currency` (from symbols: £ -> GBP, € -> EUR, ₹ or Rs -> INR, $ -> USD), `company_code` (the "bill to" / customer legal entity name as printed), `vendor_country` (from the vendor address or the VAT/GST number prefix, as an ISO 3166-1 alpha-2 code such as GB, PL, IN, DE), `document_type` and `document_direction` (from the document title and who issues it to whom).
3. MANDATORY, never null: `invoice_number`, `invoice_date`, `vendor_name`, `vendor_tax_id` (VAT number / GSTIN / NIP), `currency`, `document_type` (text), `subtotal`, `tax_amount`, `total_amount` (numbers), and `description`, `amount` for every line item. If one is genuinely not in the text use "" for text or 0 for numbers - never omit the key.
3a. `invoice_number` is the value printed next to the "Invoice No" / "Invoice Number" / "Inv No" / "Inv #" (or "Credit Note No") label. NEVER use a value labelled Account No, Customer No, Order No, Reference or Page as the invoice number - those go into `additional_fields` (or `po_number`).
3b. EVERY OTHER field is OPTIONAL and must be `null` when it is not on the document - never "" , "N/A" or 0 for an optional field.
4. `po_number` must always be present as a key: the customer's purchase order number if printed (also look for "Order No", "PO", "Your Order", "Customer Order"), otherwise null. If several PO numbers are printed, put the first in `po_number` and all of them in `additional_fields` as "Customer Order No". `po_number` is ONLY a number starting with 66 - an order number starting with 40 is a sales order (rule 4a), and any other order/reference number goes in `additional_fields`, never in `po_number`.
4a. `SO_number` must always be present as a key: the sales order number if printed (look for "Sales Order", "SO", "S.O. No", "Our Order", "Order Acknowledgement"; it must start with 40), otherwise null. A number not starting with 40 is never the `SO_number`. If several sales order numbers are printed, give all of them separated by ", ". When both a PO and a sales order are printed, fill both `po_number` and `SO_number`.
5. `gl_account`, `cost_center`, `profit_center` on every line item MUST always be null - SAP determines them.
6. Dates: ISO format YYYY-MM-DD exactly as printed on the document (convert 18/09/2026 or 18.09.2026 to 2026-09-18; British documents use day/month/year). If only a month or a period is printed, use `invoice_period_from` / `invoice_period_to` and leave `due_date` null.
7. Numbers: plain JSON numbers with a dot decimal separator and no thousands separators or currency symbols (36 558,51 -> 36558.51; £1,635.24 -> 1635.24). Amounts are positive; a credit note is expressed by `document_type` = "CREDIT_NOTE", not by negative amounts. `amount` of a line item is the NET value of that line.
8. `document_type` must be one of INVOICE, CREDIT_NOTE, DEBIT_NOTE, DELIVERY_NOTE, TIMESHEET, PROFORMA, STATEMENT, OTHER. Use DELIVERY_NOTE for delivery notes, packing lists and despatch notes and TIMESHEET for timesheets (they are not posted in SAP). `document_direction` is VENDOR_TO_CUSTOMER when a supplier bills us, CUSTOMER_TO_VENDOR when the document is issued by us.
9. `line_items`: one entry per physical line in printed order, including freight, packing, pallet or discount lines. `unit` is the unit of measure as printed (EA, PCS, KG, ROL, PAL, HRS ...). Fill `po_number`/`po_item` on a line only when that line prints its own PO number / PO item.
10. `tax_details`: one entry per tax type and rate shown in the totals block (VAT 20, VAT 0, CGST 9 + SGST 9, IGST 18, reverse charge 0 ...). `tax_type` must be one of VAT, GST, CGST, SGST, IGST, UTGST, CESS, SALES_TAX, WHT, OTHER. Use an empty array if no tax breakdown is printed.
11. `purchase_order_items`: only when the document prints a PO number and PO item/line per invoice line (or a PO/line table); `invoice_line_no` refers to the `line_no` of the matching line item. Otherwise an empty array.
12. `additional_fields`: EVERY labelled value on the document that has no field of its own - e.g. Customer Order No, Delivery Note, GRN, BL No / Bill of Lading, Container No, Vessel, Voyage, Incoterms, Vehicle Reg, HSN/SAC, IRN, GSTIN of the customer, PAN, Contract No, Account No, Week Ending, Your Reference, Period. `field_name` = the label as printed, `field_value` = the value as printed. Never invent a new top-level key instead.
13. `document_count_estimate`: how many separate invoices the text contains (normally 1). If the file holds several invoices, extract the FIRST one, set this to the count and add an additional field "Additional invoices in file" with the other invoice numbers separated by ";".
14. Return ONLY the JSON object. No commentary, no markdown fences.
15. If an EMAIL block is given, it is the covering email the document was attached to. The INVOICE TEXT is the authority - always prefer it. Use the email only for a header field the invoice text does not contain (e.g. the PO number or invoice number in the subject). Never take line items, amounts or tax from the email, and ignore other invoices or reply history mentioned in it.
"""


def build_prompt(source_text: str, email_context: str | None = None) -> str:
    email_block = f"\n--- EMAIL (context only) START ---\n{email_context}\n--- EMAIL END ---\n" if email_context else ""
    return f"{SYSTEM_INSTRUCTIONS}\n{email_block}\n--- INVOICE TEXT START ---\n{source_text}\n--- INVOICE TEXT END ---\n"


def _build_correction_prompt(original_prompt: str, previous_raw: dict, error: Exception) -> str:
    return (
        f"{original_prompt}\n\n"
        "--- YOUR PREVIOUS RESPONSE WAS INVALID ---\n"
        f"You returned:\n{json.dumps(previous_raw, indent=2)}\n\n"
        f"That failed validation with this error:\n{error}\n\n"
        "Fix it to conform EXACTLY to the three-top-level-key structure and field names shown "
        "in the worked example above (invoice_header / line_items / additional_fields - no "
        "flattening, no renamed fields, no extra top-level keys). Return ONLY the corrected "
        "JSON object, nothing else."
    )


def _build_unparseable_retry_prompt(original_prompt: str, raw_text: str | None) -> str:
    if raw_text and raw_text.strip():
        previous = f"You returned this, which is not valid JSON:\n{raw_text[:2000]}\n\n"
    else:
        previous = "You returned an empty response.\n\n"
    return (
        f"{original_prompt}\n\n"
        "--- YOUR PREVIOUS RESPONSE WAS INVALID ---\n"
        f"{previous}"
        "Respond with ONE complete JSON object only: start with `{` and end with `}`. "
        "No markdown fences, no commentary. Keep `additional_fields` concise - do not repeat "
        "the same label/value pair for every line item."
    )


def structure_invoice(
    source_text: str, *, email_context: str | None = None, client: OllamaClient | None = None, log=None
) -> InvoiceExtraction:
    log = log or logger
    client = client or OllamaClient()
    schema = get_invoice_json_schema()
    prompt = build_prompt(source_text, email_context)

    last_error: Exception | None = None
    last_raw: dict | None = None
    max_attempts = 3  # self-hosted rarely needs more than 1; cloud backends that ignore the
    # schema constraint benefit from the extra error-corrective attempts below
    for attempt in range(1, max_attempts + 1):
        if last_raw is not None:
            attempt_prompt = _build_correction_prompt(prompt, last_raw, last_error)
        elif isinstance(last_error, LLMFormatError) and last_error.raw_text is not None:
            attempt_prompt = _build_unparseable_retry_prompt(prompt, last_error.raw_text)
        else:
            attempt_prompt = prompt
        raw: dict | None = None
        try:
            log.info("llm_call_attempt", attempt=attempt, max_attempts=max_attempts)
            raw = client.generate_structured(attempt_prompt, schema, temperature=0.0)
            extraction = InvoiceExtraction.model_validate(raw)
            log.info("llm_call_succeeded", attempt=attempt)
            return extraction
        except Exception as exc:  # noqa: BLE001 - deliberately broad, retried then re-raised
            last_error = exc
            last_raw = raw
            log.warning("llm_call_attempt_failed", attempt=attempt, error=str(exc))
            continue
    log.error("llm_structuring_failed", attempts=max_attempts, error=str(last_error))
    raise LLMFormatError(f"LLM structuring failed after retries: {last_error}") from last_error
