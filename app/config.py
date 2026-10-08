from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ollama_hosts: str = "http://localhost:11434"
    ollama_model: str = "gemma4:31b"
    ollama_num_ctx: int = 16384
    ollama_timeout_seconds: int = 120
    # Set to use Ollama Cloud (https://ollama.com) instead of / alongside a self-hosted
    # instance - sent as "Authorization: Bearer <key>" on every request. Leave unset for a
    # purely self-hosted setup. Applies to every host in ollama_hosts, so don't mix a local
    # and a cloud host in the same list if only one of them needs the key.
    ollama_api_key: str | None = None

    storage_dir: str = "/var/lib/invoice-service/pdfs"
    # Original PDFs (STORAGE_DIR/files/), upload job results (jobs/) and email results already
    # handed out (delivered/) older than this are deleted. pending/ is never deleted.
    # 0 = keep forever.
    pdf_retention_days: int = 30
    # PDFs uploaded to POST /api/v1/invoices are extracted in the background, at most this
    # many at once per API worker process (the rest wait their turn).
    upload_max_concurrent: int = 2
    # An upload job still "processing" after this long was cut off (API restarted mid-
    # extraction) and is reported as failed - upload the PDF again.
    upload_job_timeout_seconds: int = 1800

    # Authentication for /api/v1/invoices* - any ONE of these satisfies a request:
    #   * HTTP Basic (Authorization: Basic base64(user:password)) - what SAP (ZFTVIA_APICFG,
    #     AUTH_TYPE = BASIC) uses. Several users may be given as "user1:pw1,user2:pw2".
    #   * X-API-Key header = api_key (Postman, scripts, SAP AUTH_TYPE = APIKEY)
    #   * Authorization: Bearer <api_key> (SAP AUTH_TYPE = BEARER)
    # With neither api_key nor api_basic_users set the endpoints refuse every request (503),
    # so the API can't accidentally run open.
    api_key: str | None = None
    api_basic_users: str = ""

    # Optional push of every extracted email result into SAP (RFC ZFTVIA_PROCESS_DOCUMENT of
    # the ZFTVIA framework, which then fetches the JSON back from GET /api/v1/invoices/{id}).
    # Needs the SAP NetWeaver RFC SDK + pyrfc installed; off by default. With it off, SAP
    # (or anyone) collects email results with GET /api/v1/invoices/new instead.
    sap_push_enabled: bool = False
    sap_ashost: str = ""
    sap_sysnr: str = "00"
    sap_client: str = "100"
    sap_user: str = ""
    sap_passwd: str = ""
    sap_lang: str = "EN"
    sap_rfc_function: str = "ZFTVIA_PROCESS_DOCUMENT"
    sap_source_type: str = "EMAIL"
    sap_bukrs: str = ""
    # Optional load balancing instead of ashost/sysnr: message server host, system id, group.
    sap_mshost: str = ""
    sap_sysid: str = ""
    sap_group: str = "PUBLIC"

    grounding_fuzzy_threshold: float = 90.0
    arithmetic_tolerance_abs: float = 0.02
    arithmetic_tolerance_rel: float = 0.005

    ocr_min_confidence: float = 0.80
    digital_text_min_chars_per_page: int = 200
    # A pathological scanned page (huge/corrupt image) has no other bound on how long OCR
    # can run - this is what turns that into a clean OcrTimeoutError (retried next poll)
    # instead of the email poller hanging on one page forever.
    ocr_timeout_seconds: int = 90
    # Bounds how many PaddleOCR engine instances are loaded process-wide: all PDFs' scanned
    # pages are OCR'd through one shared pool of this size, not a new pool per PDF.
    # CPU-only PaddleOCR (see requirements.txt) - tune to the box's core count.
    ocr_page_workers: int = 4
    # Invoices are expected to be at most 4 pages; only the first N pages of any PDF are
    # read (triage, text extraction, OCR). Later pages are ignored.
    max_pages_to_process: int = 4
    # Email ingestion: only PDFs classified as invoices are extracted (an email often carries
    # packing lists, BLs, certificates too). A PDF is classified from its first
    # invoice_detect_pages pages. email_invoices_only=false extracts every PDF.
    email_invoices_only: bool = True
    # An extracted email invoice is kept only when its customer (customer_name / company_code)
    # starts with this - "LT" matches "LT FOODS UK LIMITED", "L.T. Foods Ltd", not "LTD ..." or
    # "Tesco". Other invoices get no JSON. Empty = keep every invoice. Not applied to API uploads.
    email_customer_prefix: str = "LT"
    invoice_detect_pages: int = 4

    # Email ingestion (IMAP) - app/email_ingest/, run as its own service (email_ingest_main.py).
    # Every PDF attached to an unread message is extracted to STORAGE_DIR/pending/ and
    # handed out by GET /api/v1/invoices/new; imap_username/password is a mailbox login (an
    # app password for providers that require one, e.g. Gmail/Yahoo with 2FA).
    imap_host: str = ""
    imap_port: int = 993
    imap_use_ssl: bool = True
    imap_username: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    # Comma-separated senders whose PDFs are extracted - full addresses (ap@chep.com) or
    # whole domains (@chep.com), case-insensitive. Mail from anyone else is left unread and
    # ignored. Empty = accept every sender (logged as a warning at startup).
    imap_allowed_senders: str = ""
    # How many polls a PDF that fails extraction is retried on (the email stays unread
    # meanwhile) before a "failed" result is handed out and the email is marked read.
    email_max_attempts: int = 3
    # If set, a fully-ingested message is moved here instead of just being flagged Seen -
    # gives an audit trail of what the ingester actually consumed. Leave unset to just mark
    # Seen and leave the message where it is.
    imap_processed_folder: str | None = None
    imap_poll_interval_seconds: float = 60.0
    imap_timeout_seconds: float = 30.0
    # The email's subject + body are sent to the LLM alongside the PDF text, so a value only
    # in the covering email (e.g. "PO 6600128207" in the subject) can fill a field the PDF
    # lacks - the PDF still wins on a conflict. false = the email is only recorded in the
    # result's `email` block, not used for extraction.
    email_context_in_prompt: bool = True
    # Email body is cut to this many characters (in the result JSON and in the prompt) -
    # signatures and quoted reply chains can be huge.
    email_body_max_chars: int = 4000

    log_level: str = "INFO"
    # JSON logs are also written to this file (in addition to stdout). It always holds today;
    # earlier days are moved to logs/app-2026-09-25.log etc., and those older than
    # log_retention_days are deleted automatically. A bare folder such as "logs" means
    # logs/app.log; relative paths are from the project root.
    log_file: str | None = "logs/app.log"
    log_retention_days: int = 30


settings = Settings()
