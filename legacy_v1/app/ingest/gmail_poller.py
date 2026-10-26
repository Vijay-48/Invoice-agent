"""Polls a Gmail inbox for invoice attachments.

First run opens a browser for OAuth consent and saves token.json.
Run: python -m app.ingest.gmail_poller
"""
import base64
import logging
import os
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import settings
from app.db import SessionLocal
from app.ingest.common import ingest_file, is_supported

log = logging.getLogger("gmail")
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # read + add labels


def get_service():
    creds = None
    if os.path.exists(settings.gmail_token_file):
        creds = Credentials.from_authorized_user_file(settings.gmail_token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(settings.gmail_credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(settings.gmail_token_file, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_or_create_label(svc, name: str) -> str:
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lb in labels:
        if lb["name"] == name:
            return lb["id"]
    return svc.users().labels().create(userId="me", body={"name": name}).execute()["id"]


def iter_attachments(part):
    """Walk nested multipart structures and yield attachment parts."""
    body = part.get("body", {})
    if part.get("filename") and (body.get("attachmentId") or body.get("data")):
        yield part
    for sub in part.get("parts", []) or []:
        yield from iter_attachments(sub)


def download(svc, msg_id: str, part) -> bytes:
    body = part["body"]
    data = body.get("data")
    if not data:
        data = svc.users().messages().attachments().get(
            userId="me", messageId=msg_id, id=body["attachmentId"]).execute()["data"]
    return base64.urlsafe_b64decode(data)


def poll_once(svc, label_id: str) -> int:
    resp = svc.users().messages().list(userId="me", q=settings.gmail_query, maxResults=25).execute()
    count = 0
    for ref in resp.get("messages", []):
        msg = svc.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        sender = headers.get("from", "")
        with SessionLocal() as session:
            for part in iter_attachments(msg["payload"]):
                if not is_supported(part["filename"]):
                    continue
                content = download(svc, ref["id"], part)
                inv, created = ingest_file(session, content, part["filename"], "gmail",
                                           source_ref=f"{ref['id']}|{sender}")
                if created:
                    count += 1
                    log.info("Ingested invoice %s from %s", inv.id, sender)
        # Label only after a successful ingest, so failures get retried next poll
        svc.users().messages().modify(userId="me", id=ref["id"],
                                      body={"addLabelIds": [label_id]}).execute()
    return count


def main():
    logging.basicConfig(level=logging.INFO)
    svc = get_service()
    label_id = get_or_create_label(svc, settings.gmail_processed_label)
    log.info("Polling Gmail every %ss with query: %s", settings.gmail_poll_seconds, settings.gmail_query)
    while True:
        try:
            poll_once(svc, label_id)
        except Exception:
            log.exception("Gmail poll failed")
        time.sleep(settings.gmail_poll_seconds)


if __name__ == "__main__":
    main()
