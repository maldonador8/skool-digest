"""Parse Skool notification emails into structured items.

Skool sends two main email shapes:

1. Single-event emails (admin broadcasts, immediate notifications):
     Subject: "<Author> posted '<title>'" / "<Author> commented on your post" / etc.
     → one SkoolItem per email.

2. Batch digest emails ("X new notifications since Y"):
     One email containing multiple notifications back-to-back, each with its own
     "View post" / "View comment" / "View group" link.
     → split into multiple SkoolItems.

If HTML parsing fails for any reason, we fall back to a single SkoolItem with
the plain-text body so nothing is silently dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Iterable, Optional

from bs4 import BeautifulSoup


SKOOL_THREAD_URL_RE = re.compile(r"https?://(?:www\.)?skool\.com/[^\s\"'<>]+")
SKOOL_REDIRECT_URL_RE = re.compile(r"https?://link\.skool\.com/[^\s\"'<>]+")
FROM_NAME_RE = re.compile(r'^"?([^"<]+?)"?\s*<')
BATCH_SUBJECT_RE = re.compile(r"\b\d+\s+new\s+notifications?\b", re.IGNORECASE)

# Pattern used to split batch digests on each "View post|comment|group" CTA.
# Skool puts these as anchor text at the end of every individual notification block.
VIEW_CTA_RE = re.compile(r"View\s+(?:post|comment|group)", re.IGNORECASE)

# Per-notification opener: "<Author Name> [(following|admin)] <verb>[:] ..."
NOTIF_OPENER_RE = re.compile(
    r"^\s*([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+)*?)"            # author
    r"(?:\s+\((?:following|admin)\))?"                                 # optional tag
    r"\s+(mentioned|liked|posted|commented|replied|tagged|messaged)"   # verb
    r"[:\s]+(.+)$",                                                    # optional colon + rest
    re.DOTALL,
)

VERB_TO_TYPE = {
    "mentioned": "mention",
    "tagged":    "mention",
    "replied":   "reply",
    "commented": "comment",
    "posted":    "post",
    "liked":     "like",       # low signal; filtered out downstream
    "messaged":  "dm",         # private; filtered out
}


@dataclass
class SkoolItem:
    """One parseable unit from a Skool notification email."""
    notification_type: str  # 'post' | 'comment' | 'reply' | 'mention' | 'dm' | 'unknown'
    author: str
    subject: str
    body: str
    thread_url: str
    received_at: str
    message_id: str
    raw_snippet: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _classify(subject: str) -> str:
    s = subject.lower()
    if "messaged you" in s or "sent you a message" in s:
        return "dm"
    if "mentioned you" in s or "tagged you" in s:
        return "mention"
    if "replied to your comment" in s or "replied to you" in s:
        return "reply"
    if "commented on your" in s or "new comment" in s:
        return "comment"
    if "posted" in s or "new post" in s:
        return "post"
    return "unknown"


def _author_from_subject_or_from(subject: str, from_header: str) -> str:
    # Skool subjects often look like: "Jane Doe posted in AI Automation Society"
    # Try to grab the leading name from the subject; fall back to From header.
    m = re.match(r"^([A-Z][^,]*?)\s+(posted|commented|replied|mentioned|messaged|tagged)", subject)
    if m:
        return m.group(1).strip()
    fm = FROM_NAME_RE.match(from_header or "")
    if fm:
        return fm.group(1).strip()
    return (from_header or "Skool").strip()


def _extract_thread_url(html: str, text: str) -> str:
    # Prefer an HTML anchor that looks like a "View" / "Open" CTA button.
    if html:
        soup = BeautifulSoup(html, "html.parser")
        # Heuristic 1: anchor text contains "view", "open", "see", "reply"
        for a in soup.find_all("a", href=True):
            label = (a.get_text() or "").strip().lower()
            href = a["href"]
            if "skool.com" not in href:
                continue
            if any(kw in label for kw in ("view", "open", "see", "reply", "read", "join the conversation")):
                return href
        # Heuristic 2: first skool.com link at all
        for a in soup.find_all("a", href=True):
            if "skool.com" in a["href"]:
                return a["href"]
    # Fallback: regex against plain text
    m = SKOOL_THREAD_URL_RE.search(text or "")
    return m.group(0) if m else "https://www.skool.com/ai-automation-society-plus"


CHROME_PHRASES = (
    "don’t want admin broadcast emails",
    "don't want admin broadcast emails",
    "don’t want daily notifications email",
    "don't want daily notifications email",
    "turn them off",
    "start your own s k o o l",
    "start your own skool",
    "view group",
    "view post",
    "view comment",
)


def _strip_chrome(text: str) -> str:
    """Trim Skool's email footer/chrome out of a body string."""
    out = text
    for phrase in CHROME_PHRASES:
        idx = out.lower().rfind(phrase)
        if idx != -1:
            out = out[:idx]
    return out.strip()


def _extract_body(html: str, text: str) -> str:
    """Pull the human-written content out of the email, stripping Skool chrome."""
    if html:
        soup = BeautifulSoup(html, "html.parser")
        # Remove obvious chrome: script/style/footer-ish, unsubscribe links
        for tag in soup(["script", "style", "head", "title", "meta"]):
            tag.decompose()
        # Skool wraps the actual post body in a quoted blockquote-ish container.
        # Try several common container patterns; fall back to all visible text.
        candidates = []
        for selector in ["blockquote", "[class*='quote']", "[class*='content']", "[class*='body']"]:
            for el in soup.select(selector):
                txt = el.get_text(" ", strip=True)
                if 30 < len(txt) < 4000:
                    candidates.append(txt)
        if candidates:
            # Longest candidate is usually the post body
            return _strip_chrome(max(candidates, key=len))
        # Fallback: full visible text, collapsed
        return _strip_chrome(_collapse(soup.get_text(" ", strip=True)))
    return _strip_chrome(_collapse(text or ""))


def _view_links_from_html(html: str) -> list[str]:
    """Return the ordered list of 'View post/comment/group' anchor hrefs."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        label = (a.get_text() or "").strip().lower()
        if label.startswith("view ") and any(k in label for k in ("post", "comment", "group")):
            out.append(a["href"])
    return out


def _split_batch_body(text: str) -> list[str]:
    """Split a batch-digest body into one chunk per notification.

    Uses the 'View post|comment|group' CTAs that Skool appends to each
    notification block as separators. Returns the textual chunks BEFORE
    each CTA, in order.
    """
    if not text:
        return []
    # Insert a delimiter before each "View post|comment|group" match, then split.
    delimited = VIEW_CTA_RE.sub("§VIEW§", text)
    raw_chunks = [c.strip() for c in delimited.split("§VIEW§") if c.strip()]

    # Strip the leading community-name preamble from the first chunk
    # (e.g. "AI Automation Society Plus 4 new notifications").
    if raw_chunks:
        raw_chunks[0] = re.sub(
            r"^.+?\d+\s+new\s+notifications?\s*",
            "",
            raw_chunks[0],
            count=1,
            flags=re.IGNORECASE,
        ).strip()

    return [c for c in raw_chunks if c]


def _collapse(s: str, limit: int = 2000) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def _parse_batch(payload: dict) -> list[SkoolItem]:
    """Split a 'X new notifications' email into one SkoolItem per inner item."""
    html_body = payload.get("html", "") or ""
    text_body = payload.get("text", "") or ""

    # Use the cleaned text from BeautifulSoup so HTML tags don't leak into chunks
    if html_body:
        soup = BeautifulSoup(html_body, "html.parser")
        for tag in soup(["script", "style", "head", "title", "meta"]):
            tag.decompose()
        full_text = soup.get_text(" ", strip=True)
    else:
        full_text = text_body

    chunks = _split_batch_body(full_text)
    view_links = _view_links_from_html(html_body)

    items: list[SkoolItem] = []
    for i, chunk in enumerate(chunks):
        # Try to match "<Author> <verb> <rest>"
        m = NOTIF_OPENER_RE.match(_collapse(chunk, limit=4000))
        if not m:
            continue
        author, verb, rest = m.group(1).strip(), m.group(2).lower(), m.group(3).strip()
        notif_type = VERB_TO_TYPE.get(verb, "unknown")
        # Skip low-signal notification types in the output
        if notif_type in ("like", "dm"):
            continue

        body = _strip_chrome(_collapse(rest, limit=1200))
        thread_url = view_links[i] if i < len(view_links) else _extract_thread_url(html_body, text_body)

        items.append(SkoolItem(
            notification_type=notif_type,
            author=author,
            subject=f"{author} {verb} (from batch digest)",
            body=body,
            thread_url=thread_url,
            received_at=payload.get("date", ""),
            message_id=f"{payload.get('id','')}#{i}",
            raw_snippet=payload.get("snippet", "") or "",
        ))
    return items


def parse(payload: dict) -> list[SkoolItem]:
    """Convert a Gmail payload dict into one or more SkoolItems.

    Returns an empty list if the email shouldn't surface in the digest
    (DMs, non-Skool senders, unparseable content).
    """
    subject = payload.get("subject", "") or ""
    from_header = payload.get("from", "") or ""

    # Guard: only process emails that came from Skool
    if "skool.com" not in from_header.lower() and "skool" not in subject.lower():
        return []

    # Batch digest: split into multiple items
    if BATCH_SUBJECT_RE.search(subject):
        return _parse_batch(payload)

    # Single-event email
    notif_type = _classify(subject)
    if notif_type == "dm":
        return []

    author = _author_from_subject_or_from(subject, from_header)
    body = _extract_body(payload.get("html", ""), payload.get("text", ""))
    thread_url = _extract_thread_url(payload.get("html", ""), payload.get("text", ""))

    return [SkoolItem(
        notification_type=notif_type,
        author=author,
        subject=subject,
        body=body,
        thread_url=thread_url,
        received_at=payload.get("date", ""),
        message_id=payload.get("id", ""),
        raw_snippet=payload.get("snippet", "") or "",
    )]


def parse_all(payloads: Iterable[dict]) -> list[SkoolItem]:
    items: list[SkoolItem] = []
    for p in payloads:
        try:
            items.extend(parse(p))
        except Exception as e:  # noqa: BLE001 — defensive: never drop on parse error
            items.append(SkoolItem(
                notification_type="unknown",
                author="(parse error)",
                subject=p.get("subject", ""),
                body=f"[parse error: {e}] " + (p.get("snippet") or "")[:300],
                thread_url="https://www.skool.com/ai-automation-society-plus",
                received_at=p.get("date", ""),
                message_id=p.get("id", ""),
                raw_snippet=p.get("snippet", "") or "",
            ))
    return items
