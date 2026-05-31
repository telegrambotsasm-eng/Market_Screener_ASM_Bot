"""
IG Markets Spread Betting - Telegram Reporting Bot
Reports on open positions, balance, and account summary from an IG demo account.
"""

import os
import logging
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from trading_ig import IGService
from trading_ig.config import config as ig_config_module  # noqa: F401

# ---------- Setup ----------
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Silence overly chatty libs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("trading_ig").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---------- Environment ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
IG_USERNAME = os.environ["IG_USERNAME"]
IG_PASSWORD = os.environ["IG_PASSWORD"]
IG_API_KEY = os.environ["IG_API_KEY"]
IG_ACC_TYPE = os.environ.get("IG_ACC_TYPE", "DEMO")  # DEMO or LIVE


# ---------- IG session management ----------
class IGSession:
    """Holds a logged-in IGService and re-logs in if the session dies."""

    def __init__(self) -> None:
        self._svc: Optional[IGService] = None

    def get(self) -> IGService:
        if self._svc is None:
            self._login()
        return self._svc  # type: ignore

    def _login(self) -> None:
        logger.info("Logging in to IG (%s)...", IG_ACC_TYPE)
        svc = IGService(IG_USERNAME, IG_PASSWORD, IG_API_KEY, IG_ACC_TYPE)
        svc.create_session()
        self._svc = svc
        logger.info("IG login OK.")

    def call(self, fn_name: str, *args, **kwargs):
        """Call a method on IGService; on auth failure, re-login once and retry."""
        try:
            return getattr(self.get(), fn_name)(*args, **kwargs)
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ("unauthor", "token", "session", "401", "403")):
                logger.warning("Session looks dead (%s). Re-logging in.", e)
                self._svc = None
                return getattr(self.get(), fn_name)(*args, **kwargs)
            raise


ig = IGSession()

# ---------- Access control ----------
def restricted(func):
    """Reject any chat that isn't the configured owner."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or update.effective_chat.id != ALLOWED_CHAT_ID:
            chat_id = update.effective_chat.id if update.effective_chat else "?"
            logger.warning("Blocked access from chat_id=%s", chat_id)
            if update.message:
                await update.message.reply_text("⛔ Access denied.")
            return
        return await func(update, context)

    return wrapper


# ---------- Formatting helpers ----------
def fmt_money(x: float, currency: str = "") -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}{abs(x):,.2f}{(' ' + currency) if currency else ''}"


def direction_emoji(d: str) -> str:
    return "🟢" if d.upper() == "BUY" else "🔴"


# ---------- Command handlers ----------
@restricted
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 *IG Spread Bet Reporter*\n\n"
        "Commands:\n"
        "• /positions — open positions with P&L\n"
        "• /balance — account balance & margin\n"
        "• /summary — totals at a glance\n"
        "• /ping — check the bot is alive"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@restricted
async def cmd_ping(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"pong 🏓  ({datetime.utcnow():%H:%M:%S} UTC)")


@restricted
async def cmd_positions(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action("typing")
    try:
        df = ig.call("fetch_open_positions")
    except Exception as e:
        logger.exception("fetch_open_positions failed")
        await update.message.reply_text(f"❌ Error fetching positions:\n`{e}`",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    if df is None or df.empty:
        await update.message.reply_text("📭 No open positions.")
        return

    lines = [f"📊 *Open positions ({len(df)})*\n"]
    total_pnl = 0.0
    currency = ""

    for _, row in df.iterrows():
        # The IG response merges 'position' and 'market' columns; trading_ig flattens them.
        name = row.get("instrumentName") or row.get("epic", "?")
        direction = str(row.get("direction", "")).upper()
        size = float(row.get("dealSize", 0) or 0)
        open_lvl = float(row.get("openLevel", 0) or 0)
        bid = float(row.get("bid", 0) or 0)
        offer = float(row.get("offer", 0) or 0)
        stop = row.get("stopLevel")
        limit = row.get("limitLevel")
        currency = row.get("currency") or currency

        # Current price depends on direction (close BUY at bid, close SELL at offer)
        cur_price = bid if direction == "BUY" else offer
        # P&L in account currency for spread bet = (cur - open) * size  (BUY)
        if direction == "BUY":
            pnl = (cur_price - open_lvl) * size
        else:
            pnl = (open_lvl - cur_price) * size
        total_pnl += pnl

        pnl_emoji = "🟢" if pnl >= 0 else "🔴"

        block = (
            f"{direction_emoji(direction)} *{name}*\n"
            f"   {direction}  size: `{size:g}` @ `{open_lvl:g}`\n"
            f"   now: `{cur_price:g}`   {pnl_emoji} `{fmt_money(pnl, currency)}`\n"
        )
        if stop is not None:
            block += f"   SL: `{stop}`"
        if limit is not None:
            block += f"   TP: `{limit}`"
        if stop is not None or limit is not None:
            block += "\n"

        lines.append(block)

    total_emoji = "🟢" if total_pnl >= 0 else "🔴"
    lines.append(f"\n{total_emoji} *Total P&L:* `{fmt_money(total_pnl, currency)}`")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@restricted
async def cmd_balance(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action("typing")
    try:
        accounts = ig.call("fetch_accounts")
    except Exception as e:
        logger.exception("fetch_accounts failed")
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    if accounts is None or accounts.empty:
        await update.message.reply_text("No accounts returned.")
        return

    lines = ["💼 *Accounts*\n"]
    for _, acc in accounts.iterrows():
        name = acc.get("accountName") or acc.get("accountId")
        balance = float(acc.get("balance", 0) or 0)
        available = float(acc.get("available", 0) or 0)
        pnl = float(acc.get("profitLoss", 0) or 0)
        deposit = float(acc.get("deposit", 0) or 0)
        currency = acc.get("currency", "")
        preferred = "⭐ " if acc.get("preferred") else ""

        lines.append(
            f"{preferred}*{name}* ({acc.get('accountType', '')})\n"
            f"   Balance: `{fmt_money(balance, currency)}`\n"
            f"   Available: `{fmt_money(available, currency)}`\n"
            f"   Margin used: `{fmt_money(deposit, currency)}`\n"
            f"   Open P&L: `{fmt_money(pnl, currency)}`\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@restricted
async def cmd_summary(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action("typing")
    try:
        positions = ig.call("fetch_open_positions")
        accounts = ig.call("fetch_accounts")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    n_pos = 0 if positions is None or positions.empty else len(positions)
    total_pnl = 0.0
    currency = ""
    if accounts is not None and not accounts.empty:
        # Use the preferred account if marked, otherwise the first
        preferred = accounts[accounts.get("preferred") == True] if "preferred" in accounts.columns else None
        acc = preferred.iloc[0] if preferred is not None and not preferred.empty else accounts.iloc[0]
        total_pnl = float(acc.get("profitLoss", 0) or 0)
        balance = float(acc.get("balance", 0) or 0)
        available = float(acc.get("available", 0) or 0)
        currency = acc.get("currency", "")
    else:
        balance = available = 0.0

    emoji = "🟢" if total_pnl >= 0 else "🔴"
    text = (
        f"📈 *Account summary*\n\n"
        f"Open positions: *{n_pos}*\n"
        f"Balance: `{fmt_money(balance, currency)}`\n"
        f"Available: `{fmt_money(available, currency)}`\n"
        f"{emoji} P&L: `{fmt_money(total_pnl, currency)}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def on_unknown(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.id == ALLOWED_CHAT_ID:
        await update.message.reply_text("Unknown command. Try /start.")


# ---------- Main ----------
def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(MessageHandler(filters.COMMAND, on_unknown))

    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
