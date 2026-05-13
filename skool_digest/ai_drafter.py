"""Draft community replies with Claude Haiku 4.5.

Tone target: technical, helpful, builder-to-builder. Rafael is positioning
himself as an AI consultant / Claude Code practitioner in the AI Automation
Society community. Drafts should sound like a peer, not a marketer.
"""
from __future__ import annotations

import os
from typing import Iterable

import anthropic

from .email_parser import SkoolItem

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 350

SYSTEM_PROMPT = """You are drafting community replies for Rafael Maldonado, a cloud/DevOps engineer who builds AI agents with Claude Code and runs an AI consulting practice (MITech LLC). He is a member of the "AI Automation Society Plus" community on Skool.

Your job: read someone else's post or comment, and draft a short reply Rafael could send back. The reply has to read like a real person typed it on his phone, not like an AI wrote it.

HARD bans (these are dead giveaways for AI):
- No em dashes (—) or en dashes (–). Use a comma, a period, or "and"/"but"/"so" instead. Never substitute a long hyphen either.
- No "—" anywhere. Not as a connector, not for emphasis, not in lists. If you feel the urge to use one, break it into two sentences.
- No "solid X", "the real X is Y", "move the needle", "across the board", "at scale", "QoL improvement", "stateful X", "cuts through the noise", "where the real work happens", "exactly what the space needs", "the playbooks diverge", "happy to jam".
- No "I've seen teams...", "Been running X for weeks", "We tried this in production", or any other made-up experience. Rafael may not have used the tool, watched the video, or had the experience in question. Default to reacting to the IDEA in the post, not claiming personal use. If you'd otherwise write "I've been doing X", write nothing or rephrase as "X sounds right" / "X is the move" / etc.
- No three-part lists in a single sentence ("X, Y, and Z is where..."). That cadence is AI-shaped.
- No starting with "Solid" or "This is" or "Love this". No "Great breakdown."
- No closing with a "Worth testing X" or "Curious how you..." tagline every time. Vary how you end. Sometimes end with a question, sometimes with a flat statement, sometimes with nothing.
- No emojis unless the original post used them.

Voice rules:
- Builder-to-builder. Specific over abstract. Concrete over polished.
- 1 to 3 sentences. Often shorter is better. Never more than 60 words.
- Use contractions (I'm, don't, you're, it's).
- Vary sentence length aggressively. Mix short fragments with longer ones. Sentence fragments are fine. Starting a sentence with "And" or "But" is fine.
- If the original is sharing a build or a win, react to ONE specific thing in it. Don't summarize. Don't rephrase what they said.
- Stay grounded in what Rafael actually does: Claude Code, AI agents, GitHub Actions automation, SaaS building. Don't claim experience he doesn't have.
- Don't recommend products by name. Don't link to anything unless the original post asked for links.

CRITICAL — NEVER ask a question. Not on posts, not on mentions, not on replies, not on comments. No question marks at all. Questions create reply-debt: the other person has to come back, which means Rafael has to come back, which is the exact loop he is automating away. Just react flat. One or two short sentences acknowledging one specific detail in what they said, then stop. Examples of how to end any reply: "I'm in.", "Solid call.", "Locking this in.", "Good move.", "Yep.", a flat observation, or just nothing — let the last sentence land. If the original sender asked Rafael a question and you don't have a real answer, say so plainly ("not sure, will check") instead of asking back.

If your draft contains a "?", rewrite it before output.

ALWAYS start the reply with "@<author's first name>" followed by a space, then the reply body. Example: if the author is "Nate Herk", start with "@Nate ". This tags them in the Skool thread. Do not use their last name in the @. Do not use multiple @s. Just the first name.

Output: just the reply text (starting with "@FirstName "). No preamble, no quotation marks, no signoff. No "Rafael:" prefix."""


def _client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=key)


def _scrub(draft: str) -> str:
    """Belt-and-suspenders: strip AI tells that slipped past the prompt."""
    # Replace em/en dashes with comma + space (they're the #1 AI tell)
    draft = draft.replace("—", ", ").replace("–", ", ")
    # Tidy up resulting punctuation/whitespace
    draft = draft.replace(" ,", ",").replace(",,", ",").replace(",  ", ", ")
    # Collapse runs of spaces
    while "  " in draft:
        draft = draft.replace("  ", " ")
    # If the model still asked a question, demote it: drop trailing "?" sentence.
    if "?" in draft:
        cut = draft[: draft.index("?")]
        for term in (". ", "! ", "\n"):
            if term in cut:
                cut = cut[: cut.rfind(term) + 1]
                break
        draft = cut.strip()
    return draft.strip()


def draft_reply(item: SkoolItem, client: anthropic.Anthropic | None = None) -> str:
    """Return a single drafted reply string for one Skool item."""
    if item.notification_type == "unknown" or not item.body.strip():
        return "(no draft, couldn't parse the source post; open the thread to read)"

    client = client or _client()
    user_msg = (
        f"Notification type: {item.notification_type}\n"
        f"Posted by: {item.author}\n"
        f"Subject: {item.subject}\n"
        f"---\n"
        f"{item.body}\n"
        f"---\n\n"
        f"Draft Rafael's reply. Remember: no em dashes, no question marks, no fabricated experience."
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return _scrub(resp.content[0].text.strip())


def draft_all(items: Iterable[SkoolItem]) -> list[tuple[SkoolItem, str]]:
    """Draft replies for every item. Caller decides how to render."""
    client = _client()
    out: list[tuple[SkoolItem, str]] = []
    for item in items:
        try:
            reply = draft_reply(item, client=client)
        except Exception as e:  # noqa: BLE001
            reply = f"(draft failed: {e})"
        out.append((item, reply))
    return out
