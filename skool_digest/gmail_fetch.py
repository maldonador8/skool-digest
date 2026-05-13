"""Gmail fetcher for Skool notification emails.

Auth model:
  - First run locally: `python -m skool_digest.gmail_fetch --auth`
      → opens browser, mints token.json from credentials.json
  - Then base64 the token: `base64 -i token.json | pbcopy`
      → paste into GitHub repo secret GMAIL_TOKEN_B64
  - In CI: token is loaded from GMAIL_TOKEN_B64 env var

Scopes: gmail.readonly only. We never modify, send, or delete mail.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Gmail search query — pulls anything in the "Skool" label from the last 24h.
# User configures the Gmail filter to apply the "Skool" label automatically.
DEFAULT_QUERY = "label:Skool newer_than:1d"


def build_query(days: int = 1) -> str:
    """Build a Gmail query for the Skool label over the last N days."""
    return f"label:Skool newer_than:{int(days)}d"


def _credentials_from_env() -> Credentials | None:
    """Load OAuth credentials from GMAIL_TOKEN_B64 env (CI path)."""
    b64 = os.environ.get("GMAIL_TOKEN_B64")
    if not b64:
        return None
    raw = base64.b64decode(b64).decode("utf-8")
    info = json.loads(raw)
    return Credentials.from_authorized_user_info(info, SCOPES)


def _credentials_from_disk(token_path: Path) -> Credentials | None:
    if not token_path.exists():
        return None
    return Credentials.from_authorized_user_file(str(token_path), SCOPES)


def get_credentials(token_path: Path | None = None) -> Credentials:
    """Resolve credentials in this order: env var, local token.json."""
    creds = _credentials_from_env()
    if creds is None and token_path is not None:
        creds = _credentials_from_disk(token_path)
    if creds is None:
        raise RuntimeError(
            "No Gmail credentials found. Set GMAIL_TOKEN_B64 env var, "
            "or run `python -m skool_digest.gmail_fetch --auth` to mint token.json."
        )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def auth_flow(credentials_path: Path, token_path: Path) -> None:
    """One-time local browser flow. Writes token.json next to credentials.json."""
    if not credentials_path.exists():
        raise FileNotFoundError(
            f"{credentials_path} not found. Download OAuth client JSON from "
            "Google Cloud Console (https://console.cloud.google.com/) and save it here."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json())
    print(f"Wrote {token_path}")
    print("Next: base64 -i token.json | pbcopy  →  paste into GitHub secret GMAIL_TOKEN_B64")


def fetch_messages(query: str = DEFAULT_QUERY, max_results: int = 100) -> list[dict]:
    """Return a list of raw Gmail message dicts (headers + body) matching query."""
    creds = get_credentials(token_path=Path(__file__).parent.parent / "token.json")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    list_resp = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    ids = [m["id"] for m in list_resp.get("messages", [])]

    out = []
    for mid in ids:
        msg = service.users().messages().get(
            userId="me", id=mid, format="full"
        ).execute()
        out.append(msg)
    return out


def extract_payload(msg: dict) -> dict:
    """Pull subject, from, date, html body, plain body from a Gmail message dict."""
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    html_body, text_body = "", ""

    def _walk(part: dict) -> None:
        nonlocal html_body, text_body
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
            if mime == "text/html" and not html_body:
                html_body = decoded
            elif mime == "text/plain" and not text_body:
                text_body = decoded
        for child in part.get("parts", []) or []:
            _walk(child)

    _walk(msg.get("payload", {}))
    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "date": headers.get("date", ""),
        "html": html_body,
        "text": text_body,
        "snippet": msg.get("snippet", ""),
    }


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Skool Gmail fetcher")
    parser.add_argument("--auth", action="store_true", help="Mint token.json from credentials.json")
    parser.add_argument("--list", action="store_true", help="List matching messages and exit")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    args = parser.parse_args()

    here = Path(__file__).parent.parent
    if args.auth:
        auth_flow(here / "credentials.json", here / "token.json")
        return 0

    msgs = fetch_messages(args.query)
    print(f"Found {len(msgs)} messages for query: {args.query!r}")
    if args.list:
        for m in msgs:
            p = extract_payload(m)
            print(f"  [{p['date']}] {p['subject']}  ←  {p['from']}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
