# Changes for the SAP ZFTVIA contract V1 (2026-10-02)

| Area | Change |
|---|---|
| `contract/` | NEW – the agreed contract (`ZFTVIA_OCR_API_CONTRACT_V1.json`), JSON schema and sample; served by `GET /api/v1/invoices/contract`. |
| `app/schemas/invoice_schema.py` | Header: + `vendor_country`, `document_type` (mandatory), `document_direction`, `invoice_period_from/to`; `vendor_tax_id` now mandatory; `company_code`/`customer_name` optional. Line items: + `unit`, `tax_amount`, `po_number`, `po_item`. NEW `TaxDetail`, `PurchaseOrderItem`; `InvoiceExtraction` + `tax_details`, `purchase_order_items`, `document_count_estimate`. |
| `app/schemas/envelope.py` | Dates ISO `YYYY-MM-DD` (old `DD.MM.YYYY` results still readable); `company_code` and `gl_account/cost_center/profit_center` no longer hidden (contract wants the keys, SAP fields are `null`); status `error` (old `failed` translated); + `api_version`, `page_count`, `document_count_estimate`, `tax_details`, `purchase_order_items`, `confidence`; metadata + `avg_ocr_confidence`, `pdf_sha256`, `sap_reference`. |
| `app/pipeline/to_response.py` | Null convention (`""`/`N/A`/`-` → null), ISO currency (£→GBP…), ISO country from VAT/GSTIN prefix, document type/direction/tax type enumerations, positive amounts, per-field `confidence` from grounding × OCR confidence. |
| `app/pipeline/llm_structurer.py` | Prompt `v4-sap-contract-1`: worked example = contract sample, 14 rules (ISO dates/numbers, enumerations, units, tax breakdown, PO items, additional fields list, multi-invoice files). |
| `app/pipeline/grounding.py` | Grounds the new fields, `tax_details[]` and `purchase_order_items[]`; more date formats; null texts skipped. |
| `app/pipeline/business_rules.py` | Contract checks: unparseable dates, non-ISO currency, unknown document type, PO number format, tax_details vs tax_amount, several invoices in one file; mandatory = contract M fields, should-have = warning. |
| `app/pipeline/status.py` | Inferred fields (currency, country, document type, company code) are informational – they no longer force `needs_review`. |
| `app/pipeline/run.py` | Returns grounding results, page count, OCR confidence, PDF sha256, SAP reference. |
| `app/api/routers/invoices.py` | Auth: HTTP Basic (`API_BASIC_USERS`) / `X-API-Key` / Bearer; raw upload honours `X-Filename` and `X-SAP-Reference`; sha256 dedup returns the existing id; `GET /invoices/contract`. |
| `app/storage/results_store.py` | `hashes/` index (sha256 → result key) for the dedup. |
| `app/sap/push.py` + `app/email_ingest/ingest.py` | Optional RFC push of email PDFs into SAP (`ZFTVIA_PROCESS_DOCUMENT`), `SAP_*` settings. |
| `app/config.py`, `.env.example`, `requirements.txt` | New settings; `pyrfc` optional. |
| `tests/test_contract.py` | Builds a result from a realistic LLM output and validates it against the contract schema. |

Nothing changed in triage / OCR (PaddleOCR) / text extraction / Ollama client / email polling / run.sh / systemd.

# Additions (2026-10-07) – extra keys, allowed by the contract (SAP ignores unknown keys)

| Area | Change |
|---|---|
| `invoice_header` | + `SO_number` right after `po_number`: sales order number(s), start with 40, comma-joined if several, null if none. A 40… value the LLM put in `po_number` is moved to `SO_number`, a 66… value in `SO_number` is moved to `po_number`; an empty field is filled from a "Sales Order No" / "Customer Order No" additional field. Also in `confidence`. Prompt `v7-so-number`. |
| `email` | + `Subject_PO`, `Subject_BL`, `Body_PO`, `Body_BL` (PO / Bill of Lading numbers in the covering email, also read from body tables); `attachments[].is_invoice`. |
| Email ingestion | Only PDFs classified as invoices (first 4 pages) are extracted – `EMAIL_INVOICES_ONLY`, `INVOICE_DETECT_PAGES`; `MAX_PAGES_TO_PROCESS` 6 → 4. Only invoices whose customer starts with LT / L.T. (LT Foods) get a JSON – `EMAIL_CUSTOMER_PREFIX`. |
