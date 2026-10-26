# Roadmap: from working prototype to production

Status (15 October 2026): Phase 0 set up, **Phase 1 done**, **Phase 2 mostly done** (items 1-4, 7, 8);
next decisions are listed at the end. Also built outside the original plan: the AI assistant (chat with
saved conversations) and the scheduled summary report by email.

## Where we are today

Works end to end: Gmail (via Composio) and upload → AI extraction → e-invoice QR → second reading when
checks fail → GST and arithmetic checks → journal/duplicate linking → auto-approve or review → Excel.
180 automated tests. Tested on real invoices and journal vouchers (HFCL, ITI). Golden set: key fields 100%,
0 wrong auto-approvals.

The gaps found at the start (most are now closed; see each phase):

| Area | Gap today | Real effect |
|---|---|---|
| Gmail coverage | No record of emails that were skipped or produced nothing | When "5 emails, 4 invoices" happens, nobody can see why without digging into logs |
| Gmail coverage | Only PDF/JPG/PNG/WEBP attachments are read | Invoices in the email body, as download links, in .xlsx/.docx/.zip, or forwarded as .eml are missed |
| Gmail coverage | Password-protected PDFs fail | Some suppliers protect PDFs with a PAN or customer ID; these end as FAILED |
| Gmail delivery | Local websocket listener + polling every minute | Fine on one PC; a server needs a webhook with signature checks |
| Extraction | One AI pass; a failed check is sent to a person | Most failures (a misread total) could be fixed by a second, guided pass |
| Extraction | Signed e-invoice QR code not used | The QR on an e-invoice holds the GSTINs, number, date, value and IRN, signed by the government portal: ground truth that we currently ignore |
| Extraction | Only the first 5 pages are read | Long invoices or annexures are cut off (flagged for review) |
| Extraction | One confidence number for the whole document | Can't point the reviewer at the uncertain field |
| Accuracy | No test set of real documents with known answers | Prompt and model changes can't be measured; regressions go unnoticed |
| Compliance | GSTIN checked by format/check digit only | A cancelled or wrong-but-valid GSTIN passes |
| Compliance | No GSTR-2B reconciliation | Input tax credit claimed on invoices the supplier never filed |
| Output | Excel is the only record of approved data | Excel edits are not saved back; no audit of changes; a single file doesn't scale to many users |
| Platform | No login or roles; one company; tables created without migrations | Not safe to expose beyond one PC |

## Goals (how "production-ready" is measured)

| Metric | Target | How measured |
|---|---|---|
| Email capture | 100% of emails with attachments accounted for (processed, duplicate, or skipped with a reason) | Daily reconciliation report (Phase 1) |
| Field accuracy | ≥ 98% of key fields correct (supplier GSTIN, invoice no/date, taxable, taxes, total) | Golden test set (Phase 0) |
| Auto-approve precision | 0 wrong invoices auto-approved | Golden set + review of auto-approved samples |
| Touchless rate | ≥ 70% of clean invoices need no human | Dashboard |
| Time to Excel | < 2 minutes from email arrival | Dashboard |

---

## Phase 0: Measure first (≈ 1 week). **Started 2026-10-07**

Done:
- `golden/` with 7 verified documents (2 real invoices incl. a scan, 2 real journal vouchers, a USD
  invoice, a generated clean invoice, a non-invoice), `golden/config.json` for fixed rules.
- `python -m scripts.evaluate` (per-field and per-document scores, decisions, wrong auto-approvals,
  tokens, model comparison, exit code for a gate) and `python -m scripts.golden_add <id>`.
- First baseline (reports/eval-20261007-*): normal setup (gpt-4o-mini for text, gpt-4o for scans):
  key fields 100%, all fields 98.9%, decisions 100%, 0 wrong auto-approvals.
  gpt-4o-mini alone: 96.3% key fields (misread a GSTIN on the scan) and ~13× the input tokens of
  gpt-4o, because images are counted at far more tokens for mini.
  Fixed from the results: currency rule ('$' was read as INR).

Still to do:
- Grow the set to 50–100 documents (needs your documents; see golden/README.md).
- Run the evaluation automatically on every change (CI) once the code is in a repository.

Original plan:

Every later change is judged against this. Without it, "more accurate" is a guess.

1. **Golden test set.** 50–100 real documents (invoices, credit notes, journal vouchers, scans, photos,
   multi-page bills, non-invoices) with the correct values written down by hand (a JSON file per document).
   Include the hard cases: scans, rotated photos, Indian number formats, IGST vs CGST/SGST, round-off.
2. **Evaluation script.** `python -m scripts.evaluate`: runs extraction on the set and reports field-level
   accuracy, classification accuracy and auto-approve decisions per model and prompt version. It also
   reports cost and time per document.
3. **Run it on every change** to the prompt, model or checks (CI gate: accuracy must not drop).

## Phase 1: Never miss an email (≈ 1–2 weeks). **Done 2026-10-15**

Done: email ledger (`email_messages`, `GET /api/v1/emails`, demo "Email log"), daily reconciliation
(+ `POST /users/{id}/gmail/reconcile`), body invoices, .xlsx/.csv/.docx, .zip/.eml unpacking,
password-protected PDFs (`NEEDS_PASSWORD`), email hints to the AI, Gmail labels, network retries and
sync on reconnect. Verified on the real inbox: 7 emails (incl. 1 in Spam) logged and labelled.
Added 2026-10-15: production webhook (`GMAIL_DELIVERY=webhook`, signed `/webhooks/composio`), Gmail
connection health (expired/revoked access shown as "Reconnect Gmail", skipped by the sync, optional alert
email), invoices behind download links (allow-listed domains, `GMAIL_LINK_DOMAINS`), and sync hardening
(no email skipped above 100 per sync, 10-minute overlap, a permanently failing email no longer holds back
the sync, reconnecting catches up on the outage).
Not done: forwarded Outlook `.msg` files (needs the `extract-msg` package; .eml works).

Original plan:

1. **Email ledger table.** One row per Gmail message seen: message id, sender, subject, received time, folder
   (Inbox/Spam/…), attachments found, and outcome per attachment: `invoice #N`, `same file as #N`,
   `unsupported type`, `no attachments`, `failed: reason`. Shown in the frontend as an "Emails" view.
   This answers "why is the 5th email not an invoice?" instantly.
2. **Daily reconciliation.** A job lists all matching Gmail messages for the last N days and compares them
   with the ledger; anything missing is processed and reported.
3. **More document sources**
   - Invoices in the **email body** (HTML) → rendered to text/PDF and extracted.
   - **Download links** in the body (supplier portals) → fetched when the domain is on an allow-list.
   - **Attachments:** .xlsx/.csv (read the cells directly), .docx (convert), .zip (unpack), forwarded
     .eml/.msg (unpack and process their attachments).
   - **Password-protected PDFs:** try known passwords per supplier (PAN, GSTIN, customer id), stored securely;
     otherwise "needs password" status instead of FAILED.
4. **Email context for the AI.** Pass subject, sender and the first lines of the body as hints
   (e.g. "Credit note against INV-118"). Sender address → known supplier when the domain matches.
5. **Gmail labels.** Label processed emails in Gmail (`AP/Processed`, `AP/Needs review`, `AP/Not invoice`)
   so the team sees the status inside Gmail too.
6. **Production delivery.** Composio **webhook** (`/webhooks/composio`, signature verified with the webhook
   secret) instead of the local websocket; the websocket stays for development.
7. **Connection health.** Detect expired or revoked Gmail connections and show "reconnect Gmail" to that user
   (today an expired connection fails silently).

## Phase 2: More accurate extraction (≈ 2–3 weeks). **Items 1-4, 7, 8 done 2026-10-15**

Done (app/pipeline/reading.py, einvoice.py, words.py, learning.py):
- 1: the e-invoice QR is read when an IRN is shown (OpenCV; zxing-cpp used if installed); its values
  replace the AI's reading. Verified on the real HFCL e-invoice. Signature checked when NIC's key is put in
  `EINVOICE_KEYS_DIR` (not yet: the key file has to be downloaded from the e-invoice portal).
- 2+3: second reading by gpt-4o, told which checks failed, only when a misreading can explain them.
- 4: the demo outlines the fields to check (AI unsure, or a failed check points there).
- 7: reviewers' corrections are remembered per supplier and given to the AI as hints (email hints and
  second reading).
- 8: total in words vs the figure; look-alike invoice numbers (O/0, I/1, S/5, B/8) flagged. Not done: the
  HSN-wise tax summary cross-check.
- Golden set after these changes: key fields 100%, decisions 100%, 0 wrong auto-approvals. The scanned ITI
  invoice sometimes misses place of supply and PO number (it did before too): the next accuracy work.

Open: 5 (documents over 5 pages) and 6 (OCR service for poor scans: a paid service, needs your decision).

Original plan, in order of expected gain per effort:

1. **E-invoice QR code.** Decode the signed QR (present on GST e-invoices) and verify its signature. Its fields
   (seller/buyer GSTIN, invoice number, date, total value, IRN) override the AI's reading; a mismatch is
   flagged. Exact data for every e-invoice.
2. **Self-correction pass.** When a check fails (total doesn't add up, qty × rate wrong), send the document
   back to the AI once with the specific problem ("lines add up to 4,77,755 but taxable total says
   5,63,751"). Only if it still fails does a person see it.
3. **Model escalation.** Read with the cheaper model first; re-read with the stronger model when checks
   fail or confidence is low. Measure on the golden set which split gives the best accuracy per rupee.
4. **Field-level confidence.** The AI marks which fields it is unsure of; the frontend highlights exactly those
   fields for the reviewer.
5. **Better page handling.** Choose which pages to send (skip T&C pages, annexures), and handle documents
   over 5 pages in chunks.
6. **OCR fallback for poor scans** (e.g. Azure Document Intelligence or Google Document AI), used only when
   there is no text layer and the AI's confidence is low. Compare on the golden set before adopting.
7. **Learn from corrections.** Every reviewer correction is stored as (document, wrong value, right value).
   Use it to build per-supplier examples for the prompt, and add the document to the golden set.
8. **Cross-checks on the document itself.** Amount in words vs grand total; sum of HSN-wise tax summary vs
   line items; invoice number OCR variants (O/0, I/1, S/5) treated as possible duplicates.

## Matching engine (after Gmail; waiting on the PO/GRN source)

Supplier master, purchase orders (PO) and goods receipts (GRN): match each invoice to its PO and GRN
(quantities, rates, totals within tolerance) and split the outcome into **Approval** (matched) and
**Exception** (mismatch, no PO, over-delivery). Needs to know where PO and GRN data come from (an ERP export,
Excel sheets, or entered in the app) before it can be designed.

## Phase 3: Compliance checks (≈ 2 weeks; needs a GST Suvidha Provider (GSP) account)

1. **GSTIN verification.** Check that the supplier GSTIN is active and the legal name matches (via a GSP
   API), cached per GSTIN.
2. **IRN verification.** Confirm the e-invoice exists on the Invoice Registration Portal.
3. **GSTR-2B reconciliation.** Monthly: match approved invoices against the supplier-filed data in GSTR-2B;
   report invoices missing from 2B (input tax credit at risk) and 2B entries with no invoice received.
4. **TDS.** Detect TDS sections/rates applicable per supplier and record them; journal vouchers already show
   TDS ledgers.
5. **Supplier master.** Built automatically from approved invoices (GSTIN → name, state, usual HSN codes,
   typical amounts). Flag unusual invoices (new bank details, amount far above the supplier's usual).

## Phase 4: Production platform (≈ 3–4 weeks)

1. **Login and roles** (accountant, owner, admin), replacing the `X-User-Id` header. Approvals above a
   limit need an owner.
2. **Multiple companies** (`company_id` on every record, as in the PRD), each with its own GSTINs,
   Gmail inboxes and Excel/register.
3. **Database migrations** (Alembic) instead of create_all; daily backups; retention policy for documents.
4. **Deployment** on a server with HTTPS, the API and workers as separate services, secrets outside `.env`
   files, file storage in object storage (S3-compatible) instead of a local folder.
5. **Reliability.** Rate limiting and backoff for OpenAI and Composio; dead-letter list for documents that keep
   failing; cost tracking per invoice.
6. **Monitoring and alerts.** Dashboard (received, auto-approved, in review, failed, average time, AI cost);
   alerts when the Gmail trigger stops, failures pile up, or the review queue grows.
7. **Security.** Virus scan on attachments; documents treated as untrusted input (text inside a PDF can't
   instruct the AI to approve anything, and decisions stay with the rules engine); access logs; data
   protection under India's DPDP Act (who can see which supplier data, deletion on request).

## Phase 5: Output and reporting (≈ 1–2 weeks)

1. **Database as the purchase register.** The database becomes the record of approved invoices; Excel is
   generated from it on demand (any period, any company), so concurrent users and "file open in Excel"
   stop being problems.
2. **Edits saved back.** Corrections made in the frontend's Excel view are saved to the database with an
   audit trail (who, when, old → new), instead of living only in the browser.
3. **Formats.** Monthly sheets, a purchase register in the layout your accountants use, and a Tally-importable
   export when you want to connect Tally later.
4. **Reports.** Payables by supplier, ageing, credit notes pending adjustment, GST input summary per month.

---

## Suggested order and why

| Order | Phase | Reason |
|---|---|---|
| 1 | Phase 0: golden set and evaluation | Every later step is measured against it |
| 2 | Phase 1: items 1, 2, 4, 7 (ledger, reconciliation, email hints, connection health) | Directly fixes "emails missing" and makes problems visible |
| 3 | Phase 2: items 1–3 (QR, self-correction, escalation) | Biggest accuracy gain for the effort |
| 4 | Phase 1: items 3, 5, 6 (more sources, labels, webhook) | Wider coverage and production delivery |
| 5 | Phase 4: login, roles, migrations, deployment | Required before other people use it |
| 6 | Phase 3 and Phase 5 | Compliance and reporting on a stable base |

## Decisions needed from you

0. **PO/GRN data source** for the matching engine (ERP export, Excel, entered in the app?).

1. **Golden set:** can you collect 50–100 real documents (all types) for testing? They stay local.
2. **Spend:** use the stronger model (gpt-4o) more often for accuracy, or optimise for cost? An OCR service
   for poor scans would be an extra monthly cost.
3. **GSP account** for GSTIN/IRN verification and GSTR-2B (Phase 3): do you have a provider, or should we
   compare options?
4. **Where it will run** in production (your own server, a cloud VM, …), and whether Docker is acceptable
   there (it isn't needed locally).
5. **System of record:** keep Excel as the main output, or move to the database register with Excel exports
   (recommended, Phase 5)?
6. **Multi-company:** will one installation serve several companies (as in the PRD), or one company?
