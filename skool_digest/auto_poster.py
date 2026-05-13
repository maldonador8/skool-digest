"""Auto-post drafted Skool replies via Playwright.

Risk acknowledgement: Skool's TOS likely prohibits automated posting. Use only
on your own account with eyes open. This script tries to look human (real
browser, persistent profile, char-by-char typing, random pauses, jittered
intervals) but detection is never zero.

First-time login:
    python -m skool_digest.auto_poster --login

After cookies are saved, run a queue:
    python -m skool_digest.auto_poster docs/queue.json --dry-run         # type but don't post
    python -m skool_digest.auto_poster docs/queue.json --max-posts 1     # post 1 then stop
    python -m skool_digest.auto_poster docs/queue.json                   # post all (with jitter)
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

USER_DATA_DIR = Path(__file__).resolve().parent.parent / ".browser-profile"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Patch the most common headless tells before any page script runs.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
window.chrome = window.chrome || {runtime: {}};
"""

REPLY_BOX_SELECTORS = [
    'div.ql-editor[contenteditable="true"][data-placeholder*="comment" i]',
    'div.ql-editor[contenteditable="true"]',
    '[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    'textarea[placeholder*="comment" i]',
    'textarea[placeholder*="reply" i]',
]

POST_BUTTON_SELECTORS = [
    'button:has-text("Comment"):not(:has-text("Comments"))',
    'button:has-text("Post"):not(:has-text("Posts"))',
    'button:has-text("Reply")',
    'button:has-text("Send")',
    'button[aria-label*="post" i]',
    'button[type="submit"]',
]


def _sleep(ms_min: int, ms_max: int) -> None:
    time.sleep(random.uniform(ms_min, ms_max) / 1000.0)


def _human_scroll(page, total_ms: int = 3000) -> None:
    """Scroll the page in small increments to simulate reading."""
    steps = max(1, total_ms // 250)
    for _ in range(steps):
        page.mouse.wheel(0, random.randint(80, 220))
        _sleep(150, 380)


def _find_visible(page, selectors: list[str], timeout_ms: int = 8000):
    """Return the first visible element matching any of the selectors, or None."""
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                pass
        time.sleep(0.4)
    return None


def post_one(page, thread_url: str, reply_text: str, dry_run: bool = False) -> dict:
    """Navigate to a thread, type the reply, and (if not dry_run) click post."""
    print(f"[poster] → {thread_url[:90]}")
    try:
        page.goto(thread_url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        return {"ok": False, "error": f"navigation failed: {e}", "url": thread_url}

    _sleep(2000, 4500)
    _human_scroll(page, total_ms=random.randint(2500, 5500))

    box = _find_visible(page, REPLY_BOX_SELECTORS, timeout_ms=10000)
    if not box:
        return {"ok": False, "error": "reply box not found", "url": thread_url}

    try:
        box.scroll_into_view_if_needed()
    except Exception:
        pass
    _sleep(400, 1100)
    box.click()
    _sleep(300, 900)

    # CLEAR any existing text first — critical: prevents the previous draft from
    # bleeding into this one if Skool persisted the box's contents.
    try:
        page.keyboard.press("ControlOrMeta+a")
        _sleep(80, 180)
        page.keyboard.press("Delete")
        _sleep(150, 350)
        # Belt-and-suspenders: zero it via the DOM if anything remains
        handle = box.element_handle()
        if handle:
            page.evaluate("(el) => { el.innerHTML = ''; el.textContent = ''; }", handle)
        _sleep(150, 300)
    except Exception:
        pass

    # Re-click to make sure focus is still in the box after the clear gymnastics
    box.click()
    _sleep(200, 500)

    # Type char-by-char with per-key jitter
    page.keyboard.type(reply_text, delay=random.randint(55, 160))
    _sleep(900, 2200)

    if dry_run:
        print(f"[poster]    DRY RUN — typed but NOT clicking post")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return {"ok": True, "dry_run": True, "url": thread_url}

    btn = _find_visible(page, POST_BUTTON_SELECTORS, timeout_ms=4000)
    if not btn:
        return {"ok": False, "error": "post button not found (reply typed but not submitted)", "url": thread_url}

    try:
        btn.click()
    except Exception as e:
        return {"ok": False, "error": f"click failed: {e}", "url": thread_url}

    _sleep(2500, 4500)

    # VERIFY the post actually submitted — Skool clears the box after a real post.
    # If it still contains our text, the click hit the wrong button.
    try:
        remaining = (box.inner_text() or "").strip()
        if remaining and reply_text[:40].strip() in remaining:
            return {
                "ok": False,
                "error": "post did not clear box (probably wrong button) — manual cleanup needed",
                "url": thread_url,
                "stuck_text_preview": remaining[:120],
            }
    except Exception:
        pass

    return {"ok": True, "url": thread_url}


def login_flow(ctx) -> int:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    print("[poster] Opening Skool. Log in manually in the visible browser window.")
    print("[poster] Once you see your community feed, return to this terminal.")
    page.goto("https://www.skool.com/login", wait_until="domcontentloaded")
    try:
        # Wait up to 5 minutes for the user to navigate away from /login (i.e. logged in)
        page.wait_for_url(lambda url: "/login" not in url and "/sign-in" not in url, timeout=300000)
        print("[poster] ✓ Logged in. Cookies saved to .browser-profile/.")
        _sleep(2000, 3000)
        return 0
    except Exception:
        print("[poster] Timed out waiting for login. Re-run --login.")
        return 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("queue", nargs="?", help="Path to queue.json (list of {thread_url, reply})")
    p.add_argument("--login", action="store_true", help="One-time browser login, then exit")
    p.add_argument("--dry-run", action="store_true", help="Type the reply but never click post")
    p.add_argument("--max-posts", type=int, default=15, help="Cap on number of posts (default 15)")
    p.add_argument("--start-from", type=int, default=0, help="Skip first N items (resume)")
    p.add_argument("--min-interval", type=int, default=5, help="Min minutes between posts")
    p.add_argument("--max-interval", type=int, default=12, help="Max minutes between posts")
    p.add_argument("--first-interval", type=int, default=0,
                   help="Minutes to wait before the FIRST post (0 = post immediately)")
    args = p.parse_args()

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=False,
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(STEALTH_JS)

        if args.login:
            rc = login_flow(ctx)
            ctx.close()
            return rc

        if not args.queue:
            print("usage: auto_poster.py QUEUE.JSON   (or --login first)")
            ctx.close()
            return 1

        queue_path = Path(args.queue)
        if not queue_path.exists():
            print(f"queue not found: {queue_path}")
            ctx.close()
            return 1

        queue = json.loads(queue_path.read_text())
        # Slice based on start-from and cap
        queue = queue[args.start_from : args.start_from + args.max_posts]
        print(f"[poster] queue: {len(queue)} item(s)")

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Verify logged in
        page.goto("https://www.skool.com/", wait_until="domcontentloaded")
        _sleep(2500, 4000)
        if "login" in page.url or "sign-in" in page.url:
            print("[poster] Not logged in. Run with --login first.")
            ctx.close()
            return 1
        print("[poster] ✓ logged in, starting queue")

        # Optional pre-first-post delay (used for "test 1 tonight, schedule rest for tomorrow")
        if args.first_interval > 0:
            wait_sec = args.first_interval * 60
            print(f"[poster] waiting {args.first_interval}m before first post...")
            time.sleep(wait_sec)

        log_path = queue_path.with_suffix(".log.jsonl")
        successes = 0

        for i, item in enumerate(queue):
            url = item["thread_url"]
            reply = item["reply"]
            print(f"\n[poster] === item {i+1}/{len(queue)} ===")
            print(f"[poster]    author: {item.get('author','?')}")
            print(f"[poster]    reply:  {reply[:80]}…")

            result = post_one(page, url, reply, dry_run=args.dry_run)
            entry = {"i": args.start_from + i, "ts": time.time(), **result}
            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            print(f"[poster]    result: {'✓' if result.get('ok') else '✗'} {result.get('error', '')}")

            if result.get("ok") and not result.get("dry_run"):
                successes += 1

            if i < len(queue) - 1:
                jitter = random.randint(args.min_interval, args.max_interval)
                jitter_sec = jitter * 60 + random.randint(0, 59)
                print(f"[poster]    sleeping {jitter}m {jitter_sec % 60}s before next…")
                time.sleep(jitter_sec)

        print(f"\n[poster] done. {successes}/{len(queue)} posted successfully. log: {log_path}")
        ctx.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
