from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ap:ap@localhost:5432/ap"
    storage_dir: str = "./storage"              # original invoice files
    excel_path: str = "./output/invoices.xlsx"  # approved invoices are appended here...
    excel_per_financial_year: bool = True       # ...as "invoices FY2026-27.xlsx", one per financial year
    export_dir: str = "./output/exports"        # files created by the assistant

    # AI extraction. The keys are read from .env here because the OpenAI and
    # Composio SDKs only look at real environment variables.
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_scan_model: str = "gpt-4o"   # for scanned PDFs/photos (no text layer); mini misreads these
    assistant_model: str = "gpt-4o"     # the chat assistant (tool calling over the invoice data)

    # Rules
    company_gstin: str = ""             # your GSTIN(s), comma-separated; empty = don't check.
                                        # Other-state GSTINs with the same PAN also count as yours.
    auto_approve_limit: float = 50000   # correct invoices above this still need approval
    amount_tolerance: float = 1.0       # rupees, for rounding differences
    min_confidence: float = 0.85        # extraction confidence floor
    max_pages: int = 5                  # longer PDFs: first and last page plus the pages with most amounts
    supplier_min_history: int = 3       # approved invoices before "far above the usual amount" applies
    supplier_amount_factor: float = 3.0  # ...a total above this many times the supplier's highest so far
    max_upload_mb: int = 15
    processing_timeout_minutes: int = 15

    ai_second_pass: bool = True         # checks failed -> read once more, told what failed (stronger model)
    ai_second_pass_model: str = "gpt-4o"
    einvoice_qr: bool = True            # read the signed QR on GST e-invoices (exact GSTINs, number, date, total, IRN)
    einvoice_keys_dir: str = ""         # folder with NIC's public key/certificate (*.pem) to verify the QR signature

    log_level: str = "INFO"   # DEBUG for every check and Composio call; TRACE adds library logs

    # Background jobs started with the API (set false to run them as separate processes)
    run_worker: bool = True
    run_gmail_listener: bool = True
    run_excel_export: bool = True    # with RUN_WORKER=false: still write approved invoices to Excel here

    # Many users at once
    worker_concurrency: int = 4      # documents read by the AI at the same time
    gmail_concurrency: int = 4       # inboxes synced / new-email events handled at the same time
    api_threads: int = 100           # requests doing blocking work (files, Excel, Gmail) at the same time
    db_pool_size: int = 10           # database connections kept open
    db_max_overflow: int = 30        # extra connections at peak times
    db_pool_timeout: int = 30        # seconds a request waits for a free connection

    # Gmail through Composio
    composio_api_key: str = ""
    public_base_url: str = "http://localhost:8000"   # where the browser returns after connecting Gmail
    composio_gmail_version: str = "20260915_00"  # pinned Gmail toolkit version
    # How new-email events arrive: "websocket" (no public URL needed; development, one PC) or
    # "webhook" (production: Composio POSTs to PUBLIC_BASE_URL/webhooks/composio, signed)
    gmail_delivery: str = "websocket"
    composio_webhook_secret: str = ""            # from the Composio dashboard (webhook subscription)
    # Which emails are processed: anywhere incl. Spam and filtered/archived mail (Gmail leaves
    # Spam out unless asked), but not sent mail (your own sales invoices), drafts or trash.
    # {...} means OR: emails with an attachment, or mentioning invoice/bill/GSTIN (invoices
    # written in the email body).
    gmail_query: str = 'in:anywhere -in:sent -in:drafts -in:trash {has:attachment invoice bill gstin}'
    gmail_poll_minutes: int = 1                  # how often Composio checks each inbox
    gmail_subject_keywords: str = ""             # e.g. "invoice,bill"; empty = every email
    gmail_read_body_invoices: bool = True        # an invoice written in the email body -> PDF
    # Invoices sent as a download link: only links to these domains (and their subdomains) are
    # fetched, https only, e.g. "zoho.in,invoices.tallyprime.com"; empty = links are ignored
    gmail_link_domains: str = ""
    gmail_apply_labels: bool = True              # label emails in Gmail: AP/Processed, AP/Needs review, ...
    gmail_label_prefix: str = "AP"
    gmail_reconcile_days: int = 7                # daily check of the last N days against the email log
    pdf_passwords: str = ""                      # comma-separated passwords tried on protected PDFs
    gmail_health_alerts: bool = False            # email a user whose Gmail access expired (from another inbox)

    # Summary report by email (sent from a connected Gmail account)
    report_schedule: str = "off"                 # off | daily | weekly | monthly
    report_time: str = "09:00"                   # local time, HH:MM
    report_weekday: str = "mon"                  # for weekly reports
    report_recipients: str = ""                  # comma-separated; empty = every user's email
    report_sender_user_id: int = 0               # whose Gmail sends it; 0 = the first connected user
    report_app_url: str = ""                     # the frontend's address, for links in the email

    # WhatsApp through Twilio: every webhook request must carry a valid X-Twilio-Signature.
    # Empty token = every WhatsApp request is rejected. Twilio signs the public URL, so
    # public_base_url must match the webhook URL set in the Twilio Console exactly.
    twilio_auth_token: str = ""
    # Your Twilio Account SID (AC...): media is downloaded only for this account, with these
    # credentials. Empty = media is never downloaded.
    twilio_account_sid: str = ""
    # Who may send invoices on WhatsApp: "number:user_id", comma-separated, e.g.
    # "+919876543210:1,+14155550100:2". Messages from any other number are ignored.
    whatsapp_senders: str = ""

    # Follow-up WhatsApp messages and calls (app/followup.py, app/voice.py). Off by default; with
    # FOLLOWUP_DRY_RUN=true (the default) nothing is sent: what would be sent is only recorded.
    # People are reached only at the numbers listed in WHATSAPP_SENDERS.
    followup_enabled: bool = False
    followup_dry_run: bool = True
    followup_hours: str = "09:00-19:00"          # local time; nothing is sent or called outside it
    # "channel@minutes": steps of the ladder, counted from when the invoice began waiting
    followup_review_ladder: str = "whatsapp@240,whatsapp@1440,voice@2880"    # NEEDS_REVIEW / NEEDS_PASSWORD
    followup_export_ladder: str = "whatsapp@30"                              # APPROVED but not in Excel
    followup_result_notices: bool = True         # tell a WhatsApp sender what became of their file
    followup_ack_hours: float = 24               # reply 1 ("I'm handling it") pauses reminders this long
    followup_snooze_hours: float = 2             # reply 2 ("remind me later")
    followup_max_per_day: int = 6                # WhatsApp messages (and, separately, emails) per person per day
    followup_max_calls_per_day: int = 2
    followup_escalation_user_id: int = 0         # the voice step calls this user (0 = the invoice owner)
    # "email@minutes" steps go to the invoice owner's users.email, only when it is listed here
    # (comma-separated). Sent from a connected Gmail: this user's, 0 = REPORT_SENDER_USER_ID's / the first
    followup_email_to: str = ""
    followup_email_sender_user_id: int = 0
    followup_batch: int = 50                     # follow-ups handled per check
    twilio_whatsapp_from: str = ""               # "whatsapp:+14155238886" (sandbox) or your approved sender
    # Content template (HX...) for messages outside WhatsApp's 24-hour window; empty = those are skipped.
    # Its body must be one variable: "{{1}}"
    twilio_whatsapp_template_sid: str = ""
    twilio_voice_from: str = ""                  # a Twilio number that can place calls (not the sandbox number)
    # The call is a live AI conversation through Twilio ConversationRelay (needs a public wss:// address
    # and the AI/ML addendum accepted in the Twilio Console)
    voice_model: str = "gpt-4o-mini"
    voice_language: str = "en-US"                # check Twilio's list for your language (e.g. hi-IN)
    voice_tts_provider: str = ""                 # empty = Twilio's default
    voice_name: str = ""
    voice_max_turns: int = 8                     # caller turns before the assistant wraps up
    voice_max_seconds: int = 300                 # Twilio ends the call after this long
    voice_hangup_delay_seconds: float = 6        # after the goodbye, so it is spoken before the call ends


settings = Settings()
