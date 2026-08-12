# Microsoft Mail Watcher 📬

Watches **2 Gmail inboxes**, and every **15 minutes** checks the mail that
arrived recently. Any email **related to Microsoft** gets summarized by a free
open-source AI model (via Groq) and the full summary is pushed to your
**Telegram**. Runs for free on **GitHub Actions** — nothing runs on your Mac.

Great for anxiously waiting on that internship result. 🤞

---

## How it works

```
GitHub Actions (cron every 15 min)
        │
        ▼
   watcher.py  ──IMAP──►  Gmail inbox 1 + inbox 2
        │
        ├─ keep only Microsoft-related mail
        ├─ summarize each with Groq (free Llama model)
        └─ send summary ──► your Telegram chat
        │
        └─ remembers handled mail in state/seen.json (no duplicates)
```

The 15-min cron is the *schedule*. To be safe against GitHub delaying a run,
each run actually looks back **30 minutes** and skips anything already handled
(tracked in `state/seen.json`), so **no email is missed or sent twice**.

---

## Setup (about 15 minutes, one time)

### 1. Get a Gmail App Password (for EACH of the 2 accounts)

App Passwords need 2-Step Verification turned on.

1. Turn on 2-Step Verification: <https://myaccount.google.com/signinoptions/two-step-verification>
2. Create an App Password: <https://myaccount.google.com/apppasswords>
   - Name it e.g. `mail-watcher`, click **Create**.
   - Copy the 16-character password (looks like `abcd efgh ijkl mnop`).
3. Repeat for the second Gmail account.

> Use the App Password — **not** your normal Gmail login password.

### 2. Create a Telegram bot + get your chat ID

1. In Telegram, message **@BotFather** → send `/newbot` → follow prompts.
   Copy the **bot token** (looks like `123456789:AAAA...`).
2. Send any message to your new bot (e.g. "hi") so it can reply to you.
3. Get your chat ID: open this URL in a browser (paste your token):
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
   Find `"chat":{"id":123456789,...}` — that number is your **chat ID**.

### 3. Get a free Groq API key

1. Sign up at <https://console.groq.com> (free).
2. **API Keys** → **Create API Key** → copy it (`gsk_...`).

Groq runs open-source models (Llama 3.3) for free. If Groq ever changes the
free model name, set `GROQ_MODEL` (see below).

### 4. Put the code on GitHub

1. Create a **new repository** (private is fine) on GitHub.
2. Upload these files (or push this folder):
   ```
   git init
   git add .
   git commit -m "Microsoft mail watcher"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

### 5. Add your secrets

In the GitHub repo: **Settings → Secrets and variables → Actions → New repository secret.**
Add each of these:

| Secret name            | Value                                   |
|------------------------|-----------------------------------------|
| `GMAIL_USER_1`         | first gmail address                     |
| `GMAIL_APP_PASSWORD_1` | first app password                      |
| `GMAIL_USER_2`         | second gmail address                    |
| `GMAIL_APP_PASSWORD_2` | second app password                     |
| `GROQ_API_KEY`         | your `gsk_...` key                      |
| `TELEGRAM_BOT_TOKEN`   | your bot token                          |
| `TELEGRAM_CHAT_ID`     | your chat ID                            |

### 6. Turn it on

- Go to the **Actions** tab → enable workflows if prompted.
- Open **Microsoft Mail Watcher** → **Run workflow** to test it immediately.
- After that it runs automatically every 15 minutes.

---

## Test locally first (optional)

```bash
cd microsoft-mail-watcher
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in your real values
python watcher.py
```

`.env` is git-ignored, so your secrets never get committed.

---

## Tuning

Set these as GitHub secrets/variables or in `.env`:

| Variable             | Default                    | Meaning                                        |
|----------------------|----------------------------|------------------------------------------------|
| `MICROSOFT_KEYWORDS` | `microsoft,msft`           | Pre-filter words that let mail reach the AI     |
| `ONLY_INTERNSHIP`    | `false`                    | `true` = only ping about the internship result |
| `LOOKBACK_MINUTES`   | `30`                       | How far back each run scans                    |
| `GROQ_MODEL`         | `llama-3.3-70b-versatile`  | Groq open-source model to use                  |
| `SEEN_RETENTION_DAYS`| `3`                        | How long dedupe memory is kept                 |

**How relevance is decided (two stages):**

1. **Keyword pre-filter** — a cheap check so the AI isn't called on obvious junk.
   Mail passes if the sender contains `microsoft` **or** a keyword appears in the
   subject/body. Add `internship` to `MICROSOFT_KEYWORDS` if recruiters mail you
   from a non-Microsoft domain.
2. **AI verdict (Groq)** — the model then decides whether it's *genuinely* from
   Microsoft (killing marketing emails that merely mention the word) and whether
   it's specifically your **internship conversion / result** (offer, rejection,
   next steps). Result emails get a loud banner like
   `🎉🎉 MICROSOFT — LOOKS LIKE AN OFFER / CONVERSION! 🎉🎉` in Telegram.

Set `ONLY_INTERNSHIP=true` if you want to be notified **only** about the result
email and stay silent on other Microsoft mail.

> Without a `GROQ_API_KEY`, stage 2 is skipped — you'll still get every
> keyword-matched Microsoft email forwarded (raw text), just without the smart
> result-detection.

---

## Notes & limits

- **Free tier:** GitHub Actions gives free minutes for private repos and is
  unlimited for public repos. Each run is quick (well within limits).
- **Read-only:** emails are fetched with `BODY.PEEK`, so they stay **unread**
  in your inbox.
- **Privacy:** email text is sent to Groq for summarizing. If you'd rather not,
  leave `GROQ_API_KEY` empty — it will forward the raw email text instead.
- Scheduled runs on GitHub can occasionally be delayed a few minutes under load;
  the 30-min lookback + dedupe handle that.
