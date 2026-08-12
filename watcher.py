#!/usr/bin/env python3
"""
Microsoft Mail Watcher
----------------------
Every run (scheduled every 15 min by GitHub Actions):
  1. Connects to one or more Gmail inboxes over IMAP.
  2. Looks at mail that arrived in the recent window (default last 30 min,
     wider than the 15-min schedule to absorb cron delays).
  3. Keeps only mail related to Microsoft.
  4. Summarizes each with a free open-source model via the Groq API.
  5. Sends the summary to your Telegram chat.

A small state file (state/seen.json) records Message-IDs already handled so
the same email is never summarized twice, even if windows overlap.
"""

import email
import imaplib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests


# Minimal .env loader for local testing (no extra dependency needed).
def _load_dotenv(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        # Drop inline comments and surrounding quotes.
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv()

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables / GitHub secrets)
# ---------------------------------------------------------------------------

LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "30"))
SEEN_RETENTION_DAYS = int(os.getenv("SEEN_RETENTION_DAYS", "3"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state/seen.json"))

# Cheap keyword pre-filter: an email must clear this before the AI is called,
# so we don't spend API calls on obviously-unrelated mail.
MS_KEYWORDS = [
    k.strip().lower()
    for k in os.getenv("MICROSOFT_KEYWORDS", "microsoft,msft").split(",")
    if k.strip()
]

# If true, ONLY notify about the internship result/conversion email.
# If false (default), notify about any Microsoft mail but loudly flag the result.
ONLY_INTERNSHIP = os.getenv("ONLY_INTERNSHIP", "false").lower() in ("1", "true", "yes")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))


# ---------------------------------------------------------------------------
# Account loading:  GMAIL_USER_1 / GMAIL_APP_PASSWORD_1, _2, ...
# ---------------------------------------------------------------------------

def load_accounts():
    accounts = []
    i = 1
    while True:
        user = os.getenv(f"GMAIL_USER_{i}")
        pw = os.getenv(f"GMAIL_APP_PASSWORD_{i}")
        if not user or not pw:
            break
        # App passwords are often pasted with spaces ("abcd efgh ..."); strip them.
        accounts.append((user.strip(), pw.replace(" ", "").strip()))
        i += 1
    return accounts


# ---------------------------------------------------------------------------
# State (dedupe)
# ---------------------------------------------------------------------------

def load_seen():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_seen(seen):
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)
    trimmed = {}
    for mid, ts in seen.items():
        try:
            if datetime.fromisoformat(ts) >= cutoff:
                trimmed[mid] = ts
        except ValueError:
            trimmed[mid] = ts  # keep unparseable rather than lose it
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(trimmed, indent=2))


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def decode_str(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def is_microsoft(from_hdr, subject, body):
    """Match on sender containing 'microsoft', or a keyword in subject/body."""
    from_l = (from_hdr or "").lower()
    if "microsoft" in from_l:
        return True
    haystack = f"{subject or ''}\n{body or ''}".lower()
    return any(kw in haystack for kw in MS_KEYWORDS)


def extract_body(msg):
    """Return best-effort plain text of an email message."""
    plain, html = None, None
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain" and plain is None:
                plain = text
            elif ctype == "text/html" and html is None:
                html = text
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        if payload is not None:
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html = text
            else:
                plain = text

    body = plain if plain else strip_html(html or "")
    return re.sub(r"\n{3,}", "\n\n", (body or "").strip())


def strip_html(html):
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n\n", html)
    text = re.sub(r"(?s)<[^>]+>", "", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    return text


# ---------------------------------------------------------------------------
# AI analysis (Groq — free, serves open-source Llama models)
# The AI does the judgment: is this really from Microsoft, is it the internship
# result/conversion, what's the decision — plus a detailed summary.
# ---------------------------------------------------------------------------

def _raw_fallback(body):
    """Used when there's no AI key or the API call fails."""
    text = (body[:1500] + " …") if len(body) > 1500 else body
    return {
        "is_microsoft": True,          # trust the keyword pre-filter
        "is_internship_result": False, # can't tell without AI
        "decision": "unknown",
        "summary": text,
    }


def analyze(subject, from_hdr, date_str, body):
    if not GROQ_API_KEY:
        return _raw_fallback(body)

    system = (
        "You classify and summarize emails for a person anxiously waiting on the "
        "result of their Microsoft internship — specifically whether it converts "
        "to a full-time offer / PPO (pre-placement offer) / return offer. "
        "Respond with STRICT JSON only, no prose outside the JSON."
    )
    user_prompt = (
        "Analyze the email below and return a JSON object with exactly these keys:\n"
        '- "is_microsoft" (boolean): true ONLY if the email genuinely comes from '
        "Microsoft or is really about this person's Microsoft internship / job "
        "application. A marketing newsletter or promo that merely mentions the word "
        "Microsoft must be false.\n"
        '- "is_internship_result" (boolean): true ONLY if the email is specifically '
        "about the OUTCOME of the internship — conversion to full-time, a PPO / "
        "return offer, an offer letter, a rejection, or concrete next steps in that "
        "hiring decision.\n"
        '- "decision" (string): one of "offer", "rejection", "next_steps", '
        '"pending", "not_result".\n'
        '- "summary" (string): a thorough bullet-point summary capturing EVERY '
        "important detail — the decision, dates, deadlines, actions required from "
        "the reader, names, links, and reference numbers.\n\n"
        f"From: {from_hdr}\nSubject: {subject}\nDate: {date_str}\n\n"
        f"Email body:\n{body[:12000]}"
    )
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
        return {
            "is_microsoft": bool(data.get("is_microsoft", True)),
            "is_internship_result": bool(data.get("is_internship_result", False)),
            "decision": str(data.get("decision", "not_result")),
            "summary": (data.get("summary") or "").strip() or _raw_fallback(body)["summary"],
        }
    except Exception as e:
        print(f"  ! Groq analysis failed ({e}); falling back to raw body.")
        return _raw_fallback(body)


def build_message(user, from_hdr, subject, date_str, result):
    if result.get("is_internship_result"):
        decision = (result.get("decision") or "").lower()
        banner = {
            "offer": "🎉🎉 MICROSOFT — LOOKS LIKE AN OFFER / CONVERSION! 🎉🎉",
            "rejection": "💙 Microsoft internship result (looks like a no this time)",
            "next_steps": "📌 Microsoft internship — ACTION NEEDED / next steps",
            "pending": "⏳ Microsoft internship — status update",
        }.get(decision, "⭐ Microsoft internship — result update")
        header = f"{banner}\n\n"
    else:
        header = "📬 Microsoft-related email\n\n"
    return (
        f"{header}"
        f"📥 Inbox: {user}\n"
        f"👤 From: {from_hdr}\n"
        f"📝 Subject: {subject}\n"
        f"🕒 Date: {date_str}\n\n"
        f"🧠 Summary:\n{result.get('summary', '').strip()}"
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("  ! Telegram not configured; printing instead:\n" + text)
        return
    # Telegram hard-limits messages to 4096 chars; chunk safely.
    for chunk in chunk_text(text, 3900):
        try:
            r = requests.post(
                TELEGRAM_URL,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            r.raise_for_status()
        except Exception as e:
            print(f"  ! Telegram send failed: {e}")
        time.sleep(0.3)


def chunk_text(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


# ---------------------------------------------------------------------------
# Core per-account processing
# ---------------------------------------------------------------------------

def process_account(user, password, cutoff, seen):
    print(f"Checking {user} …")
    handled = 0
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        M.login(user, password)
    except imaplib.IMAP4.error as e:
        print(f"  ! Login failed for {user}: {e}")
        return handled

    try:
        M.select("INBOX", readonly=True)
        # Search a bit wider than the window so nothing is missed near midnight.
        since = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")
        status, data = M.search(None, f'(SINCE "{since}")')
        if status != "OK":
            return handled
        uids = data[0].split()

        for uid in reversed(uids):  # newest first
            status, hdr = M.fetch(uid, "(INTERNALDATE BODY.PEEK[HEADER])")
            if status != "OK" or not hdr or not isinstance(hdr[0], tuple):
                continue

            internal = imaplib.Internaldate2tuple(hdr[0][0])
            if internal:
                arrived = datetime.fromtimestamp(time.mktime(internal), tz=timezone.utc)
                if arrived < cutoff:
                    break  # older than window; the rest are older too

            head = email.message_from_bytes(hdr[0][1])
            from_hdr = decode_str(head.get("From"))
            subject = decode_str(head.get("Subject"))
            date_str = decode_str(head.get("Date"))
            msg_id = (head.get("Message-ID") or f"{user}:{uid.decode()}").strip()

            if msg_id in seen:
                continue

            # Fetch full message to inspect body + confirm Microsoft relevance.
            status, full = M.fetch(uid, "(BODY.PEEK[])")
            if status != "OK" or not full or not isinstance(full[0], tuple):
                continue
            msg = email.message_from_bytes(full[0][1])
            body = extract_body(msg)

            # Stage 1 — cheap keyword pre-filter (saves AI calls on junk mail).
            if not is_microsoft(from_hdr, subject, body):
                seen[msg_id] = datetime.now(timezone.utc).isoformat()
                continue

            # Stage 2 — AI verdict: really Microsoft? the internship result? summary.
            result = analyze(subject, from_hdr, date_str, body)

            # AI's second opinion overrides the keyword match: drop false positives
            # (e.g. a marketing email that merely mentions "Microsoft").
            if not result.get("is_microsoft"):
                print(f"  · AI ruled not-Microsoft: {subject!r}")
                seen[msg_id] = datetime.now(timezone.utc).isoformat()
                continue

            # If configured to only care about the internship result, skip the rest.
            if ONLY_INTERNSHIP and not result.get("is_internship_result"):
                print(f"  · Microsoft mail but not the result (skipped): {subject!r}")
                seen[msg_id] = datetime.now(timezone.utc).isoformat()
                continue

            tag = "INTERNSHIP RESULT" if result.get("is_internship_result") else "Microsoft"
            print(f"  → [{tag}] {subject!r} from {from_hdr}")
            send_telegram(build_message(user, from_hdr, subject, date_str, result))
            seen[msg_id] = datetime.now(timezone.utc).isoformat()
            handled += 1
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return handled


def main():
    accounts = load_accounts()
    if not accounts:
        print("No Gmail accounts configured. Set GMAIL_USER_1 / GMAIL_APP_PASSWORD_1 …")
        sys.exit(1)

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    seen = load_seen()
    total = 0
    for user, pw in accounts:
        total += process_account(user, pw, cutoff, seen)
    save_seen(seen)
    print(f"Done. {total} Microsoft email(s) sent to Telegram this run.")


if __name__ == "__main__":
    main()
