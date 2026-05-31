# IG Spread Bet — Telegram Reporter Bot (Multi-login)

A Telegram bot that reports on open positions, balance, and P&L across **multiple
separate IG logins**, each of which can contain multiple accounts.

## What this bot does

For any number of IG logins, you can:
- See a **single account's** positions / balance / summary
- See **all accounts within one login**
- See **everything across every login** with per-login subtotals + grand totals

UI is fully button-driven — no slash commands needed.

## Environment variables

| Name              | What                                            |
|-------------------|-------------------------------------------------|
| `TELEGRAM_TOKEN`  | From `@BotFather`                               |
| `ALLOWED_CHAT_ID` | Your Telegram numeric chat id                   |
| `IG_LOGINS`       | JSON array (see below)                          |

### `IG_LOGINS` format

A JSON array. Each object is one IG login:

```json
[
  {
    "label": "Main Demo",
    "username": "user1",
    "password": "pass1",
    "api_key": "key1",
    "acc_type": "DEMO"
  },
  {
    "label": "Test Demo",
    "username": "user2",
    "password": "pass2",
    "api_key": "key2",
    "acc_type": "DEMO"
  },
  {
    "label": "Live",
    "username": "user3",
    "password": "pass3",
    "api_key": "key3",
    "acc_type": "LIVE"
  }
]
```

- `label` (optional) — friendly name shown in buttons. Defaults to `username`.
- `username`, `password`, `api_key` — required.
- `acc_type` — `DEMO` (default) or `LIVE`.

### Setting `IG_LOGINS` in Railway

1. In Railway → your service → **Variables**.
2. Add `IG_LOGINS`. Paste the JSON as the value. Railway accepts multi-line OR single-line.
3. Save. Railway redeploys automatically.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit .env with real values
python main.py
```

## Notes

- Logins are connected **lazily** — only when you first request data from one.
- If a session expires, the bot logs back in automatically.
- For Spread Bet, P&L is computed as `(current − entry) × size` in account currency.
- The "All logins" position view is compact (counts and subtotals only) to fit
  in a Telegram message; drill into a specific account for per-position details.
