# IG Spread Bet — Telegram Reporter Bot

A small Telegram bot that reports on open positions, balance, and P&L from an
IG Markets Spread Betting account (works for DEMO and LIVE).

## Commands

- `/start` — help
- `/ping` — health check
- `/positions` — list every open position with size, entry, current price, P&L, SL/TP
- `/balance` — balance, available, margin, and open P&L per account
- `/summary` — totals at a glance

Only the chat ID set in `ALLOWED_CHAT_ID` can use the bot.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in real values
python bot.py
```

## Environment variables

| Name              | What                                            |
|-------------------|-------------------------------------------------|
| `TELEGRAM_TOKEN`  | From `@BotFather`                               |
| `ALLOWED_CHAT_ID` | Your Telegram numeric chat id                   |
| `IG_USERNAME`     | IG login username                               |
| `IG_PASSWORD`     | IG login password                               |
| `IG_API_KEY`      | API key created at My IG → Settings → API Keys  |
| `IG_ACC_TYPE`     | `DEMO` or `LIVE` (default `DEMO`)               |

## Deploy on Railway

1. Push this repo to GitHub.
2. On [railway.app](https://railway.app), New Project → *Deploy from GitHub repo*.
3. Pick this repo.
4. In the service’s **Variables** tab, add every variable from `.env.example` with your real values.
5. Done. Railway runs `python bot.py` from the Procfile.

## Notes

- For a Spread Bet account, P&L is computed as `(current − entry) × size` in your
  account currency. The bot pulls live bid/offer from the position response.
- If a session expires, the bot logs in again automatically on the next call.
- Polling mode is used — no public URL or webhook required.
