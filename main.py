"""
IG Markets Spread Betting - Telegram Reporting Bot
Button-driven UI. No need to type slash commands.
"""

import os
import logging
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from trading_ig import IGService

# ---------- Setup ----------
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("trading_ig").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---------- Environment ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
IG_USERNAME = os.environ["IG_USERNAME"]
IG_PASSWORD = os.environ["IG_PASSWORD"]
IG_API_KEY = os.environ["IG_API_KEY"]
IG_ACC_TYPE = os.environ.get("IG_ACC_TYPE", "DEMO")


# ---------- IG session ----------
class IGSession:
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
def is_allowed(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == ALLOWED_CHAT_ID)


# ---------- Keyboards ----------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Positions", callback_data="positions"),
                InlineKeyboardButton("💼 Balance", callback_data="balance"),
            ],
            [
                InlineKeyboardButton("📈 Summary", callback_data="summary"),
                InlineKeyboardButton("🏓 Ping", callback_data="ping"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh menu", callback_data="menu"),
            ],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
                InlineKeyboardButton("🔙 Back to menu", callback_data="menu"),
            ]
        ]
    )


MENU_TEXT = (
    "🤖 *IG Spread Bet Reporter*\n\n"
    "Tap a button below to see your account info."
)


# ---------- Formatting helpers ----------
def fmt_money(x: float, currency: str = "") -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}{abs(x):,.2f}{(' ' + currency) if currency else ''}"


def direction_emoji(d: str) -> str:
    return "🟢" if d.upper() == "BUY" else "🔴"


# ---------- Data formatting ----------
def build_positions_text() -> str:
    df = ig.call("fetch_open_positions")
    if df is None or df.empty:
        return "📭 *No open positions.*"

    lines = [f"📊 *Open positions ({len(df)})*\n"]
    total_pnl = 0.0
    currency = ""

    for _, row in df.iterrows():
        name = row.get("instrumentName") or row.get("epic", "?")
        direction = str(row.get("direction", "")).upper()
        size = float(row.get("dealSize", 0) or 0)
        open_lvl = float(row.get("openLevel", 0) or 0)
        bid = float(row.get("bid", 0) or 0)
        offer = float(row.get("offer", 0) or 0)
        stop = row.get("stopLevel")
        limit = row.get("limitLevel")
        currency = row.get("currency") or currency

        cur_price = bid if direction == "BUY" else offer
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
    return "\n".join(lines)


def build_balance_text() -> str:
    accounts = ig.call("fetch_accounts")
    if accounts is None or accounts.empty:
        return "No accounts returned."

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
    return "\n".join(lines)


def build_summary_text() -> str:
    positions = ig.call("fetch_open_positions")
    accounts = ig.call("fetch_accounts")

    n_pos = 0 if positions is None or positions.empty else len(positions)
    total_pnl = balance = available = 0.0
    currency = ""

    if accounts is not None and not accounts.empty:
        if "preferred" in accounts.columns:
            preferred_rows = accounts[accounts["preferred"] == True]
            acc = preferred_rows.iloc[0] if not preferred_rows.empty else accounts.iloc[0]
        else:
            acc = accounts.iloc[0]
        total_pnl = float(acc.get("profitLoss", 0) or 0)
        balance = float(acc.get("balance", 0) or 0)
        available = float(acc.get("available", 0) or 0)
        currency = acc.get("currency", "")

    emoji = "🟢" if total_pnl >= 0 else "🔴"
    return (
        f"📈 *Account summary*\n\n"
        f"Open positions: *{n_pos}*\n"
        f"Balance: `{fmt_money(balance, currency)}`\n"
        f"Available: `{fmt_money(available, currency)}`\n"
        f"{emoji} P&L: `{fmt_money(total_pnl, currency)}`"
    )


def build_ping_text() -> str:
    return f"🏓 *pong*\nServer time: `{datetime.utcnow():%Y-%m-%d %H:%M:%S} UTC`"


# Map callback_data → builder function
ACTIONS = {
    "positions": build_positions_text,
    "balance": build_balance_text,
    "summary": build_summary_text,
    "ping": build_ping_text,
}


# ---------- Handlers ----------
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the main menu (on /start, /menu, or a 'menu' button press)."""
    if not is_allowed(update):
        if update.message:
            await update.message.reply_text("⛔ Access denied.")
        return

    if update.callback_query:
        await update.callback_query.edit_message_text(
            MENU_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(),
        )
    elif update.message:
        await update.message.reply_text(
            MENU_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(),
        )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle every inline-keyboard button tap."""
    query = update.callback_query
    if not is_allowed(update):
        if query:
            await query.answer("Access denied.", show_alert=True)
        return

    await query.answer()  # dismiss the spinner on the button

    action = query.data

    # Menu / back
    if action == "menu":
        await show_menu(update, context)
        return

    # Refresh — re-run whatever the user last looked at
    if action == "refresh":
        last = context.user_data.get("last_action")
        if not last:
            await show_menu(update, context)
            return
        action = last

    builder = ACTIONS.get(action)
    if not builder:
        await show_menu(update, context)
        return

    context.user_data["last_action"] = action
    try:
        text = builder()
    except Exception as e:
        logger.exception("Action %s failed", action)
        text = f"❌ *Error*\n`{e}`"

    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_keyboard(),
        )
    except Exception as e:
        # If new text is identical to old (user mashed Refresh), Telegram errors. Just ignore.
        if "not modified" in str(e).lower():
            await query.answer("Already up to date ✓")
        else:
            raise


async def on_any_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any text the user types just brings the menu back."""
    await show_menu(update, context)


# ---------- Main ----------
def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # /start, /menu, /help all show the menu
    app.add_handler(CommandHandler("start", show_menu))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("help", show_menu))

    # All button presses
    app.add_handler(CallbackQueryHandler(on_button))

    # Any text or unknown command -> show menu
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_any_text))
    app.add_handler(MessageHandler(filters.COMMAND, on_any_text))

    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
