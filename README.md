# LT Foods Invoice Extraction API

Reads invoice PDFs (uploaded by SAP, or arriving by email) and turns each one into structured
JSON matching the **SAP ZFTVIA OCR contract V1** (`contract/ZFTVIA_OCR_API_CONTRACT_V1.json`:
`invoice_header` / `line_items` / `additional_fields` / `tax_details` / `purchase_order_items`
/ `confidence`). SAP classifies MIRO / BL-based MIRO / FB60 itself from `po_number`,
`document_type` and the additional fields — this service's only job is to populate every
field correctly, and to say clearly (`confidence`, `needs_review`, `metadata.flags`) when it
isn't sure.

Runs natively on a single server — no Docker, no database, no broker. Two plain Python
processes (managed by systemd in production); results are kept as JSON files on disk.

## Architecture

```
email_ingest_main.py  (polls the IMAP inbox every IMAP_POLL_INTERVAL_SECONDS)
   for every UNREAD email with PDF attachment(s), for each PDF:
     1. triage        - digital text-layer PDF vs scanned/image PDF (per page)
     2. extract/OCR    - PyMuPDF (digital) / PaddleOCR (scanned, pages OCR'd concurrently
                          in a shared OCR_PAGE_WORKERS-sized pool) -> source_text
     3. LLM structure  - Ollama (gemma4:31b by default) turns source_text into the
                          exact JSON schema (JSON-schema-constrained, temperature=0)
     4. grounding      - every field the LLM output is re-verified against source_text
     5. business rules - mandatory fields, subtotal+tax=total, line_items non-empty
     6. status assign  - success / needs_review + flags[]
     7. save           - STORAGE_DIR/pending/<key>.json
   once every PDF of the email has a result file, the email is marked read

     8. (optional)     - push the PDF to SAP by RFC ZFTVIA_PROCESS_DOCUMENT (SAP_PUSH_ENABLED)

FastAPI (uvicorn main:app)
   POST /api/v1/invoices      - SAP uploads a PDF (raw body) -> 202 + id (same PDF twice = same id)
   GET  /api/v1/invoices/{id} - 202 processing | 200 contract JSON
   GET  /api/v1/invoices/new  - returns everything in pending/ and moves it to delivered/
```

So the team just calls `GET /api/v1/invoices/new`: if 10 invoices arrived today they get
all 10; if 11 more arrive tomorrow, the next call returns only those 11. Each result is
returned exactly once.

**Why this design, in one sentence:** LLMs are not reliable enough to trust blindly on
financial data, so the LLM here only *structures* text that deterministic OCR already
extracted, and every value it outputs is checked against that source text afterward —
anything unverifiable is flagged `needs_review`, never silently returned as fact.

### Where results live (`STORAGE_DIR`)

| Folder | Contents |
|---|---|
| `pending/` | extracted, not yet returned by the API |
| `delivered/` | already returned by the API — kept as a backup, deleted after `PDF_RETENTION_DAYS` (default 30) |
| `failed/` | attempt counters for PDFs that keep failing |
| `files/` | the original PDF of each result (served by `/api/v1/invoices/{id}/pdf`), deleted after `PDF_RETENTION_DAYS` (default 30) |
| `jobs/` | PDFs uploaded to `POST /api/v1/invoices`: `<id>.processing` while extracting, then `<id>.json`, deleted after `PDF_RETENTION_DAYS` |
| `email_watermark.json` | newest email UID already dealt with |

A result's file name is `<email received time>_<hash of Message-ID>_<attachment no>.json`,
so it is the same every time the same attachment is seen. That is what replaces the old
database dedup: if the poller crashes after writing a result but before marking the email
read, the next poll sees the file already exists and doesn't extract or return it twice.

The API claims files by atomic rename (`pending/` → `delivered/`), so even with several
Uvicorn workers or simultaneous callers each result goes to exactly one response.

### Failures

- An email is marked read **only when all of its PDFs have a result**. If a PDF fails, the
  email stays unread and that PDF is retried on the next poll.
- After `EMAIL_MAX_ATTEMPTS` (default 3) failed polls, a result with `"status": "failed"`
  and an `error` message is returned instead, and the email is marked read — so a broken
  PDF is reported to the team rather than retried forever.
- Unread emails with no PDF are left unread and untouched.
- **Only allowed senders are processed.** Set `IMAP_ALLOWED_SENDERS` in `.env` to the vendor
  addresses or domains that send invoices (e.g. `ap@chep.com,@supplier.co.uk`). Mail from
  anyone else is left unread and logged as `email_skipped_sender_not_allowed`. If it is empty,
  every sender is accepted and a warning is logged at startup.
- **Only emails that arrive after the first start are processed.** On first start the poller
  records the newest email's UID in `STORAGE_DIR/email_watermark.json` and ignores
  everything older, so an existing unread backlog is never downloaded. Delete that file to
  reset it to "from now" again.

## Running it locally

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in OLLAMA_API_KEY and the IMAP_* settings

./run.sh               # starts the API (port 8001) + email poller in the background
./run.sh logs          # follow logs;  ./run.sh stop  to stop
```

Or by hand: `uvicorn main:app --reload` and `python email_ingest_main.py` in two terminals.

## API — SAP ZFTVIA contract V1

The JSON this service returns is the **SAP contract** in [contract/ZFTVIA_OCR_API_CONTRACT_V1.json](contract/ZFTVIA_OCR_API_CONTRACT_V1.json)
(rules, mandatory/should/optional classification, samples) and is validated against
[contract/ZFTVIA_OCR_API_SCHEMA_V1.json](contract/ZFTVIA_OCR_API_SCHEMA_V1.json) by
`tests/test_contract.py`. `GET /api/v1/invoices/contract` serves both. Summary of the shape:

```json
{
  "id": "5b0c…", "status": "success | needs_review | error", "api_version": "1",
  "filename": "Brookshaw_15915.pdf", "pdf_url": "/api/v1/invoices/5b0c…/pdf", "error": null,
  "page_count": 1, "document_count_estimate": 1,
  "invoice_header": { "invoice_number", "invoice_date" (YYYY-MM-DD), "due_date", "payment_terms", "company_code",
                      "vendor_name", "vendor_address", "vendor_tax_id", "vendor_country" (ISO-2), "vendor_bank_name",
                      "vendor_account_no", "vendor_sort_code", "vendor_iban", "customer_name", "customer_address",
                      "po_number", "SO_number" (sales order, 40…), "reference_number", "currency" (ISO-4217), "subtotal", "tax_amount", "tax_percent",
                      "total_amount", "document_type" (INVOICE|CREDIT_NOTE|DEBIT_NOTE|PROFORMA|STATEMENT|OTHER),
                      "document_direction", "invoice_period_from", "invoice_period_to" },
  "line_items": [ { "line_no", "description", "quantity", "unit", "unit_price", "amount", "tax_percent", "tax_amount",
                    "reference_code", "po_number", "po_item", "gl_account": null, "cost_center": null, "profit_center": null } ],
  "additional_fields": [ { "field_name", "field_value" } ],
  "tax_details": [ { "tax_type", "tax_rate", "tax_amount", "taxable_amount" } ],
  "purchase_order_items": [ { "po_number", "po_item", "invoice_line_no", "quantity", "unit_price", "amount" } ],
  "confidence": { "invoice_number": 0.99, "invoice_date": …, "vendor_name": …, "vendor_tax_id": …, "po_number": …,
                  "SO_number": …, "total_amount": …, "subtotal": …, "tax_amount": …, "currency": …, "document_type": … },
  "email": {…}, "metadata": { "flags": [...], "model_name", "prompt_version", "extraction_source", "avg_ocr_confidence",
                              "processing_time_ms", "completed_at", "pdf_sha256", "sap_reference" }
}
```

Conventions (all enforced in `app/pipeline/to_response.py`, after grounding): a value that is
not on the document is `null` (never `""`, `N/A`, `-`); dates are ISO; numbers are JSON
numbers; amounts are positive (a credit note is `document_type = CREDIT_NOTE`); currency and
`vendor_country` are normalised (£ → GBP, VAT prefix GB… → GB, GSTIN → IN); `gl_account`,
`cost_center`, `profit_center` are always `null` (SAP decides); `confidence` is 0–1 per header
field from the grounding check (1.0 = found verbatim, fuzzy/inferred lower, OCR pages scaled by
the OCR confidence). `status = error` replaces the previous `failed` (old stored results are
translated on read). `email` and `metadata` are extra information the contract allows.

### Authentication

Any one of (set in `.env`):

- **HTTP Basic** – `API_BASIC_USERS=user:password[,user2:password2]`. This is what SAP uses
  (table `ZFTVIA_APICFG`, `AUTH_TYPE = BASIC`, one user per SAP system).
- `X-API-Key: <API_KEY>` (Postman, scripts; SAP `AUTH_TYPE = APIKEY`).
- `Authorization: Bearer <API_KEY>` (SAP `AUTH_TYPE = BEARER`).

`401` = none matched, `503` = neither `API_KEY` nor `API_BASIC_USERS` configured.

### Endpoints

- **POST** `/api/v1/invoices` — upload one PDF: raw body with `Content-Type: application/pdf`
  (what SAP's `ZCL_FTVIA_OCR_CLIENT` sends, with headers `X-Filename` and `X-SAP-Reference`
  = the VIA document id), or `multipart/form-data` field `file`. Answers at once with `202`:
  ```json
  {"id": "5b0c...", "status": "processing", "result_url": "/api/v1/invoices/5b0c...", "pdf_url": "/api/v1/invoices/5b0c.../pdf"}
  ```
  A PDF whose exact content was already extracted (same sha256 – e.g. the email poller did it
  and SAP uploads the same file via `ZFTVIA_PROCESS_DOCUMENT`) returns the **existing** id, so
  nothing is OCR'd twice. Extraction runs in the background, at most `UPLOAD_MAX_CONCURRENT`
  (default 2) at a time per API process.
- **GET** `/api/v1/invoices/{id}` — `202` + `"status": "processing"` while extracting (call
  again in a few seconds), `200` + the full contract JSON once done (`success`,
  `needs_review` or `error`). A job still processing after `UPLOAD_JOB_TIMEOUT_SECONDS`
  (default 1800) was cut off by an API restart and is returned as `error` – upload again.
- **GET** `/api/v1/invoices/new` — JSON array of every invoice extracted **from email** since
  the last call, oldest first; each result is returned exactly once (`[]` when nothing new).
  Used when `SAP_PUSH_ENABLED=false`.
- **GET** `/api/v1/invoices/{id}/pdf` — the original PDF, inline.
- **GET** `/api/v1/invoices/contract` — the contract schema + sample this build implements.
- **GET** `/api/v1/health`, `/healthz`, `/readyz`, `/metrics` — as before.

### Handing email invoices to SAP

Two ways, chosen in `.env`:

1. **Pull (default)** – SAP or a scheduler calls `GET /api/v1/invoices/new`.
2. **Push** – `SAP_PUSH_ENABLED=true` + `SAP_ASHOST/SYSNR/CLIENT/USER/PASSWD`: after each
   email PDF is extracted the poller calls RFC `ZFTVIA_PROCESS_DOCUMENT` (`app/sap/push.py`,
   needs the SAP NetWeaver RFC SDK and `pip install pyrfc`) with the PDF bytes and the
   message id as source reference. SAP creates the VIA document and later sends the same PDF
   to `POST /api/v1/invoices`, which answers with the already finished result (sha256
   dedup). A failed push is logged and the result stays in `pending/` for the pull path.

### Changing the fields

The contract is the single source of truth: `app/schemas/invoice_schema.py` (what the LLM
must produce), `app/schemas/envelope.py` (what the API returns), the worked example and rules
in `app/pipeline/llm_structurer.py` (bump `PROMPT_VERSION` after any change) and
`contract/*.json`. Run `pytest tests/` – it validates a built result against the schema.

## Extraction engine: Ollama Cloud (gemma4:31b)

The current model is **`gemma4:31b`** (`OLLAMA_MODEL` in `.env`), run on
[Ollama Cloud](https://ollama.com) by default. This is paid (GPU-time billed) and your invoice
text leaves your infrastructure — a deliberate trade-off, not an oversight. `OLLAMA_MODEL` is
the only place the model name lives, so a model swap is a one-line `.env` change plus a
restart, no code changes.

**No cloud provider guarantees a given model name stays available forever.** Prefer
rolling/family tags with no date suffix (e.g. `gemma4:31b`) over dated snapshots. If a
retirement notice needs an immediate response, self-hosting (`OLLAMA_HOSTS=http://localhost:11434`,
no `OLLAMA_API_KEY`, native `ollama serve`) is the fallback — see the commented block in
`.env.example`.

## Production deployment (systemd)

| Unit | Purpose |
|---|---|
| `invoice-api.service` | `uvicorn main:app --workers 4` |
| `invoice-email-ingest.service` | `email_ingest_main.py` — polls the inbox and extracts PDFs |

Install: copy the app to `/opt/invoice-service`, create a venv there, adjust the `User`/
paths in each unit if needed, then:
```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now invoice-api invoice-email-ingest
```

**Backups:** there is no database any more — back up `STORAGE_DIR` (in particular
`delivered/`, the record of everything handed out) with your normal file backups.

## Logging

- **stdout**: structured JSON (via `structlog`, `app/logging_conf.py`) — every pipeline stage
  logs its own start/complete event with a `duration_ms`, correlated by the result key.
  Under systemd this goes to `journalctl`.
- **`LOG_FILE`** (default `./logs/app.log`): the same JSON lines are also written there.
  `app.log` always holds **today**; when the day changes, the previous day's file is moved to
  `logs/app-2026-09-25.log`, `logs/app-2026-09-26.log`, … Dated files older than
  `LOG_RETENTION_DAYS` (default **30**) are deleted automatically.
  Works the same on Windows and Linux; no `logrotate` needed.
- **`logs/run.log`** (written by `run.sh`: starts, stops, crashes): when it passes 5 MB,
  `run.sh` keeps only its last 5,000 lines the next time it runs.
- `./run.sh logs` follows `run.log` and `app.log`.

## Monitoring

`GET /metrics` exposes `invoice_results_pending` (results waiting to be fetched) and the Ollama
request/token counters for calls made inside the API process. The email poller has no HTTP
server, so use its `email_ingest_cycle_complete` log line (logged every poll) to watch
extracted / failed / retried counts.

## Key risks (read before treating this as fully production-ready)

- **One PDF at a time.** The email poller extracts PDFs sequentially, so throughput is one
  PDF per extraction time (tens of seconds). Fine for tens or hundreds of invoices a day; not
  enough for very high volumes without adding parallelism.
- **Returned once means returned once.** `GET /api/v1/invoices/new` moves results to
  `delivered/` as it returns them. If the caller's request fails after the server responded
  (network drop, crash on their side), those results won't be returned again by the API —
  they are still in `delivered/` and must be recovered from there.
- **`delivered/` is only kept for `PDF_RETENTION_DAYS`.** Back it up if you need a longer
  record of what was handed out. `pending/` (not yet returned) is never deleted.
- **API key only, no TLS, no rate limiting.** The key is sent in plain HTTP, so anyone who
  can watch the network can read it (and the invoices). Put the API behind a reverse proxy
  with TLS before exposing it beyond one trusted machine.
- **Secrets in a plaintext `.env`** (`API_KEY`, `OLLAMA_API_KEY`, `IMAP_PASSWORD`). `.env` is
  git-ignored — never commit it. Move to a real secrets manager for a proper production setup.
- **`company_code`/`currency`** are inference-heavy (nothing to ground against) and are
  always flagged for review by design.
- `OCR_TIMEOUT_SECONDS` gives each scanned page a wall-clock ceiling so a pathological page
  fails cleanly (and is retried next poll) instead of hanging the poller.
