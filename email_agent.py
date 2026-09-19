"""
Email agent - polls Gmail inbox, runs pipeline on new emails, sends reply.

Run with: python3 email_agent.py
Polls every 30 seconds. On first run, opens a browser for OAuth consent.
"""

from __future__ import annotations

import base64
import os
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from process import PipelineResult, process_email

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
POLL_INTERVAL_SECONDS = 30


def get_gmail_service():
    """Authenticate and return a Gmail API service instance."""
    creds = None

    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # First run: opens browser for OAuth consent.
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_unread_emails(service) -> list[dict]:
    """Return all unread emails in the inbox."""
    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX", "UNREAD"],
    ).execute()
    return result.get("messages", [])


def get_email_details(service, message_id: str) -> dict:
    """Fetch full email details for a given message id."""
    return service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()


def extract_body(message: dict) -> str:
    """Pull plain text body out of a Gmail message."""
    parts = message.get("payload", {}).get("parts", [])

    # Single-part email (no parts array).
    if not parts:
        data = message.get("payload", {}).get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    # Multi-part: find the text/plain part.
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    return ""


def get_sender(message: dict) -> str:
    """Extract the From address from email headers."""
    headers = message.get("payload", {}).get("headers", [])
    for header in headers:
        if header["name"] == "From":
            return header["value"]
    return ""


def get_subject(message: dict) -> str:
    """Extract subject from email headers."""
    headers = message.get("payload", {}).get("headers", [])
    for header in headers:
        if header["name"] == "Subject":
            return header["value"]
    return ""


def build_reply(to: str, subject: str, body: str, zip_path: Path | None) -> dict:
    """Build a Gmail API-compatible raw email message."""
    if zip_path:
        msg = MIMEMultipart()
        html_body = body.replace("\n", "<br>")
        msg.attach(MIMEText(html_body, "html"))
        with open(zip_path, "rb") as f:
            attachment = MIMEApplication(f.read(), _subtype="zip")
            attachment.add_header(
                "Content-Disposition",
                "attachment",
                filename=zip_path.name,
            )
            msg.attach(attachment)
    else:
        msg = MIMEMultipart()
        html_body = body.replace("\n", "<br>")
        msg.attach(MIMEText(html_body, "html"))

    msg["To"] = to
    msg["Subject"] = f"Re: {subject}" if not subject.startswith("Re:") else subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}


def mark_as_read(service, message_id: str) -> None:
    """Remove UNREAD label so we don't reprocess the same email."""
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


def send_reply(service, reply: dict) -> None:
    service.users().messages().send(userId="me", body=reply).execute()


MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 10


MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 10


def process_inbox(service) -> None:
    """Check for unread emails and run the pipeline on each."""
    messages = get_unread_emails(service)
    if not messages:
        return

    print(f"Found {len(messages)} unread email(s)")

    for msg_ref in messages:
        message_id = msg_ref["id"]
        message = get_email_details(service, message_id)

        sender = get_sender(message)
        subject = get_subject(message)
        body = extract_body(message)

        print(f"Processing email from {sender}: {subject!r}")

        result = None
        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = process_email(body)
                break
            except Exception as e:
                last_error = e
                error_str = str(e)
                is_transient = "503" in error_str or "UNAVAILABLE" in error_str or "high demand" in error_str or "429" in error_str
                
                if is_transient and attempt < MAX_RETRIES:
                    # Exponential backoff: double the wait time with each retry (10s -> 20s -> 40s)
                    sleep_time = INITIAL_RETRY_DELAY * (2 ** (attempt - 1))
                    print(f"Attempt {attempt} failed (Transient API error), retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
                else:
                    print(f"Attempt {attempt} failed: {e}")
                    break

        if result is None:
            result = PipelineResult(
                reply_text=(
                    f"Hi,\n\nSomething went wrong processing your request. "
                    f"Please contact customer.support@gmail.com for assistance.\n\nError: {last_error}"
                ),
                zip_path=None,
            )

        reply = build_reply(sender, subject, result.reply_text, result.zip_path)
        send_reply(service, reply)
        mark_as_read(service, message_id)
        print(f"Reply sent to {sender}")

if __name__ == "__main__":
    print("Starting email agent...")
    service = get_gmail_service()
    print(f"Polling inbox every {POLL_INTERVAL_SECONDS}s. Ctrl+C to stop.")

    while True:
        try:
            process_inbox(service)
        except Exception as e:
            print(f"Error in poll cycle: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)