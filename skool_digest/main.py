"""Skool Community Daily Digest — CLI entrypoint.

Usage:
  python -m skool_digest.main                    # full run: fetch → draft → email
  python -m skool_digest.main --dry-run          # print HTML to stdout, no SendGrid
  python -m skool_digest.main --dry-run --no-ai  # parse only, skip Anthropic
  python -m skool_digest.main --from-fixtures tests/fixtures/  # use .eml fixtures
"""
from __future__ import annotations

import argparse
import email
import os
import sys
from email import policy
from pathlib import Path

from .gmail_fetch import fetch_messages, extract_payload, build_query
from .email_parser import parse_all, SkoolItem
from .ai_drafter import draft_all
from .digest_builder import render_html, render_dashboard, send_via_sendgrid


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _payload_from_eml(p: Path) -> dict:
    """Read a .eml file from disk and return the payload dict shape used by parser."""
    msg = email.message_from_bytes(p.read_bytes(), policy=policy.default)
    html_body, text_body = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html" and not html_body:
                html_body = part.get_content()
            elif ct == "text/plain" and not text_body:
                text_body = part.get_content()
    else:
        if msg.get_content_type() == "text/html":
            html_body = msg.get_content()
        else:
            text_body = msg.get_content()
    return {
        "id": p.stem,
        "thread_id": p.stem,
        "subject": msg.get("Subject", ""),
        "from": msg.get("From", ""),
        "date": msg.get("Date", ""),
        "html": html_body or "",
        "text": text_body or "",
        "snippet": (text_body or "")[:200],
    }


def run(dry_run: bool = False, no_ai: bool = False, fixtures_dir: Path | None = None,
        days: int = 1) -> int:
    # Pull payloads — either from Gmail or from local .eml fixtures
    if fixtures_dir is not None:
        eml_files = sorted(fixtures_dir.glob("*.eml"))
        print(f"[digest] loading {len(eml_files)} fixtures from {fixtures_dir}")
        payloads = [_payload_from_eml(p) for p in eml_files]
    else:
        query = build_query(days=days)
        print(f"[digest] fetching from Gmail (last {days}d, query={query!r})")
        msgs = fetch_messages(query=query, max_results=500 if days > 7 else 100)
        payloads = [extract_payload(m) for m in msgs]
        print(f"[digest] fetched {len(payloads)} messages")

    items = parse_all(payloads)
    print(f"[digest] parsed {len(items)} actionable Skool items")

    # Cap items before drafting to avoid burning API tokens on cards that won't render.
    # Priority order matches digest_builder.render_html so direct convo never gets trimmed.
    from .digest_builder import MAX_CARDS
    priority = {"reply": 0, "mention": 1, "comment": 2, "top_post": 3, "post": 4, "unknown": 5}
    items_sorted = sorted(items, key=lambda it: priority.get(it.notification_type, 9))
    items_to_draft = items_sorted[:MAX_CARDS]
    items_overflow = items_sorted[MAX_CARDS:]
    if items_overflow:
        print(f"[digest] capping at {MAX_CARDS} (dropping {len(items_overflow)} older items from draft)")

    # Draft replies (or skip with --no-ai for fast parser-only debugging)
    if no_ai:
        drafts = [(it, "(--no-ai: no draft generated)") for it in items_to_draft]
    else:
        print("[digest] drafting replies with Claude Haiku …")
        drafts = draft_all(items_to_draft)

    # Preserve overflow as "draftless" entries so the +N footer in render_html is accurate
    drafts_full = drafts + [(it, "") for it in items_overflow]
    html_body = render_html(drafts_full)
    dashboard_html = render_dashboard(drafts)  # only drafted items get a dashboard card

    # Always save the dashboard to disk so the cloud workflow can publish it to Pages
    here = Path(__file__).resolve().parent.parent
    docs_dir = here / "docs"
    docs_dir.mkdir(exist_ok=True)
    dashboard_path = docs_dir / "index.html"
    dashboard_path.write_text(dashboard_html)
    print(f"[digest] dashboard written to {dashboard_path}")

    # Also write queue.json for the auto-poster. Apply the same @<FirstName> prefix
    # logic that the dashboard uses so what's posted matches what's previewed.
    import json as _json
    queue = []
    for item, draft in drafts:
        first_name = item.author.split()[0] if item.author else ""
        clean = draft
        if clean.startswith(f"@{first_name} "):
            clean = clean[len(f"@{first_name} "):]
        elif first_name and clean.startswith(f"@{first_name}"):
            clean = clean[len(f"@{first_name}"):].lstrip()
        final_reply = (f"@{first_name} " if first_name else "") + clean
        queue.append({
            "thread_url": item.thread_url,
            "reply": final_reply,
            "author": item.author,
            "subject": item.subject,
            "type": item.notification_type,
        })
    queue_path = docs_dir / "queue.json"
    queue_path.write_text(_json.dumps(queue, indent=2))
    print(f"[digest] queue written to {queue_path} ({len(queue)} items)")

    if dry_run:
        sys.stdout.write(html_body)
        sys.stdout.write("\n")
        print(f"\n[digest] DRY RUN — {len(drafts)} item(s), would send", file=sys.stderr)
        return 0

    ok = send_via_sendgrid(html_body, count=len(drafts))
    return 0 if ok else 1


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Skool Community Daily Digest")
    parser.add_argument("--dry-run", action="store_true", help="Print HTML to stdout; don't send")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI drafting (parser-only mode)")
    parser.add_argument("--from-fixtures", type=Path, default=None,
                        help="Read .eml files from this dir instead of Gmail")
    parser.add_argument("--days", type=int, default=None,
                        help="Lookback window in days (default: 1, or DIGEST_DAYS env var)")
    args = parser.parse_args()

    # Load .env (parent dir, then agent root, then global Projects/.env)
    here = Path(__file__).resolve().parent.parent
    for env_path in (here / ".env", here.parent / ".env", here.parent.parent / ".env"):
        _load_env_file(env_path)

    # Resolve days: CLI flag > env var > default 1
    days = args.days
    if days is None:
        try:
            days = int(os.environ.get("DIGEST_DAYS", "1"))
        except ValueError:
            days = 1

    return run(
        dry_run=args.dry_run,
        no_ai=args.no_ai,
        fixtures_dir=args.from_fixtures,
        days=days,
    )


if __name__ == "__main__":
    sys.exit(_cli())
