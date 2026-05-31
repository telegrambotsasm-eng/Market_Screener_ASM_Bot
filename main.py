"""
IG Markets Spread Betting - Telegram Reporting Bot
Multi-login + multi-account support with banner images.
"""

import os
import sys
import json
import logging
import warnings
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

# Silence the harmless 'munch is not present' warning from trading-ig
warnings.filterwarnings("ignore", message=".*munch.*")

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
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

MAX_TG_CAPTION = 1000  # Telegram caption limit is 1024 — leave headroom
MAX_TG_MSG = 4000      # Plain message limit is 4096

# ---------- Banner paths ----------
# Banners live in the same directory as main.py (the repo root on Railway).
ASSETS_DIR = os.path.dirname(os.path.abspath(__file__))

BANNERS = {
    "main": os.path.join(ASSETS_DIR, "banner_main.png"),
    "positions": os.path.join(ASSETS_DIR, "banner_positions.png"),
    "balance": os.path.join(ASSETS_DIR, "banner_balance.png"),
    "summary": os.path.join(ASSETS_DIR, "banner_summary.png"),
    "accounts": os.path.join(ASSETS_DIR, "banner_accounts.png"),
    "picker_login": os.path.join(ASSETS_DIR, "banner_picker_login.png"),
    "picker_account": os.path.join(ASSETS_DIR, "banner_picker_account.png"),
    "error": os.path.join(ASSETS_DIR, "banner_error.png"),
}

# Cache for uploaded banner file_ids — first send uploads the file,
# every subsequent send just references the cached file_id (much faster).
_banner_file_ids: Dict[str, str] = {}


# ---------- Login configuration ----------
def load_logins() -> List[Dict[str, Any]]:
    raw = os.environ.get("IG_LOGINS", "").strip()
    if not raw:
        print("=" * 60, file=sys.stderr)
        print("❌ IG_LOGINS env var is empty.", file=sys.stderr)
        print("Set it to a JSON array, e.g.:", file=sys.stderr)
        print('   [{"label":"Demo","username":"X","password":"Y","api_key":"Z","acc_type":"DEMO"}]', file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        import time; time.sleep(3600)
        raise SystemExit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print("=" * 60, file=sys.stderr)
        print(f"❌ IG_LOGINS is not valid JSON: {e}", file=sys.stderr)
        print("First 200 chars of what you set:", file=sys.stderr)
        print(f"   {raw[:200]!r}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        import time; time.sleep(3600)
        raise SystemExit(1)

    if not isinstance(data, list) or not data:
        print("❌ IG_LOGINS must be a non-empty JSON array.", file=sys.stderr)
        import time; time.sleep(3600)
        raise SystemExit(1)

    REQUIRED = ("username", "password", "api_key")
    for i, cfg in enumerate(data):
        if not isinstance(cfg, dict):
            print(f"❌ IG_LOGINS[{i}] must be an object, got {type(cfg).__name__}", file=sys.stderr)
            import time; time.sleep(3600)
            raise SystemExit(1)

        keys_seen = list(cfg.keys())
        missing = [f for f in REQUIRED if not cfg.get(f)]
        if missing:
            print("=" * 60, file=sys.stderr)
            print(f"❌ IG_LOGINS[{i}] is missing required field(s): {missing}", file=sys.stderr)
            print(f"   Fields I found in this entry: {keys_seen}", file=sys.stderr)
            print(f"   Required (exact, case-sensitive): {list(REQUIRED)}", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            import time; time.sleep(3600)
            raise SystemExit(1)

        cfg.setdefault("acc_type", "DEMO")
        cfg.setdefault("label", cfg["username"])

    return data


# ---------- IG session ----------
class IGLogin:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.label: str = cfg["label"]
        self._username: str = cfg["username"]
        self._password: str = cfg["password"]
        self._api_key: str = cfg["api_key"]
        self._acc_type: str = cfg["acc_type"]
        self._svc: Optional[IGService] = None

    def _login(self) -> None:
        logger.info("Logging in to IG '%s' (%s)...", self.label, self._acc_type)
        svc = IGService(self._username, self._password, self._api_key, self._acc_type)
        svc.create_session()
        self._svc = svc
        logger.info("IG login '%s' OK.", self.label)

    def get(self) -> IGService:
        if self._svc is None:
            self._login()
        return self._svc  # type: ignore

    def call(self, fn_name: str, *args, **kwargs):
        try:
            return getattr(self.get(), fn_name)(*args, **kwargs)
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ("unauthor", "token", "session", "401", "403")):
                logger.warning("Session '%s' dead (%s). Re-login.", self.label, e)
                self._svc = None
                return getattr(self.get(), fn_name)(*args, **kwargs)
            raise


class Registry:
    def __init__(self, configs: List[Dict[str, Any]]) -> None:
        self.logins: List[IGLogin] = [IGLogin(c) for c in configs]

    def by_idx(self, idx) -> IGLogin:
        return self.logins[int(idx)]

    def enumerate(self):
        return list(enumerate(self.logins))


registry = Registry(load_logins())


# ---------- Access control ----------
def is_allowed(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == ALLOWED_CHAT_ID)


# ---------- Helpers ----------
def fmt_money(x: float, currency: str = "") -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}{abs(x):,.2f}{(' ' + currency) if currency else ''}"


def direction_emoji(d: str) -> str:
    return "🟢" if d.upper() == "BUY" else "🔴"


def is_preferred(acc_row) -> bool:
    val = acc_row.get("preferred")
    return val is True or val == 1 or str(val).lower() == "true"


def acc_label(acc_row, short: bool = False) -> str:
    name = acc_row.get("accountName") or acc_row.get("accountId", "?")
    acc_type = acc_row.get("accountType", "")
    star = "⭐ " if is_preferred(acc_row) else ""
    return f"{star}{name}" if short else f"{star}{name} ({acc_type})"


def truncate_for_caption(text: str) -> Tuple[str, bool]:
    """Returns (text, fits_in_caption). If too long, caller should send as plain text."""
    if len(text) <= MAX_TG_CAPTION:
        return text, True
    return text, False


# ---------- Keyboards ----------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Positions", callback_data="pick:positions"),
                InlineKeyboardButton("💼 Balance", callback_data="pick:balance"),
            ],
            [
                InlineKeyboardButton("📈 Summary", callback_data="pick:summary"),
                InlineKeyboardButton("🏦 Accounts", callback_data="accounts"),
            ],
            [InlineKeyboardButton("🔄 Refresh", callback_data="menu")],
        ]
    )


def login_picker_keyboard(action: str) -> InlineKeyboardMarkup:
    rows = []
    for idx, login in registry.enumerate():
        rows.append(
            [InlineKeyboardButton(f"🔐 {login.label}", callback_data=f"pickacc:{action}:{idx}")]
        )
    rows.append(
        [InlineKeyboardButton("🌐 All logins (everything)", callback_data=f"run:{action}:ALL:ALL")]
    )
    rows.append([InlineKeyboardButton("🔙 Back to menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def account_picker_keyboard(action: str, login_idx: int, accounts_df) -> InlineKeyboardMarkup:
    rows = []
    if accounts_df is not None and not accounts_df.empty:
        for _, acc in accounts_df.iterrows():
            acc_id = acc.get("accountId")
            label = acc_label(acc, short=True)
            rows.append(
                [InlineKeyboardButton(label, callback_data=f"run:{action}:{login_idx}:{acc_id}")]
            )
    rows.append(
        [InlineKeyboardButton("🌐 All accounts in this login", callback_data=f"run:{action}:{login_idx}:ALL")]
    )
    rows.append([InlineKeyboardButton("🔙 Back to logins", callback_data=f"pick:{action}")])
    return InlineKeyboardMarkup(rows)


def result_keyboard(action: str, login_idx) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Refresh", callback_data="refresh")]]
    if login_idx != "ALL":
        rows.append(
            [
                InlineKeyboardButton("🔀 Change account", callback_data=f"pickacc:{action}:{login_idx}"),
                InlineKeyboardButton("🔀 Change login", callback_data=f"pick:{action}"),
            ]
        )
    else:
        rows.append([InlineKeyboardButton("🔀 Pick a login", callback_data=f"pick:{action}")])
    rows.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def simple_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
                InlineKeyboardButton("🔙 Menu", callback_data="menu"),
            ]
        ]
    )


MENU_TEXT = (
    f"_{len(registry.logins)} login(s) configured._\n\n"
    "Tap a button below to see your account info."
)


# ---------- Position formatting ----------
def format_positions_df(df) -> Tuple[str, float, int, str]:
    if df is None or df.empty:
        return ("_no open positions_", 0.0, 0, "")

    blocks = []
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
            f"   now: `{cur_price:g}`   {pnl_emoji} `{fmt_money(pnl, currency)}`"
        )
        extras = []
        if stop is not None:
            extras.append(f"SL: `{stop}`")
        if limit is not None:
            extras.append(f"TP: `{limit}`")
        if extras:
            block += "\n   " + "   ".join(extras)
        blocks.append(block)

    return ("\n\n".join(blocks), total_pnl, len(df), currency)


# ============================================================
# POSITIONS
# ============================================================
def build_positions(login_idx, account_id: str) -> str:
    if login_idx == "ALL":
        return build_positions_all_logins()
    login = registry.by_idx(login_idx)
    if account_id == "ALL":
        return build_positions_all_in_login(login)
    return build_positions_one(login, account_id)


def build_positions_one(login: IGLogin, account_id: str) -> str:
    login.call("switch_account", account_id, False)
    df = login.call("fetch_open_positions")
    body, total_pnl, count, currency = format_positions_df(df)

    accounts = login.call("fetch_accounts")
    name = account_id
    if accounts is not None and not accounts.empty:
        m = accounts[accounts["accountId"] == account_id]
        if not m.empty:
            name = acc_label(m.iloc[0], short=True)

    header = f"🔐 _Login:_ {login.label}\n🏦 _Account:_ {name}\n"
    if count == 0:
        return header + "\n📭 _No open positions_"
    emoji = "🟢" if total_pnl >= 0 else "🔴"
    footer = f"\n\n{emoji} *Total P&L:* `{fmt_money(total_pnl, currency)}`"
    return header + f"\n_{count} position(s)_\n\n" + body + footer


def build_positions_all_in_login(login: IGLogin) -> str:
    accounts = login.call("fetch_accounts")
    if accounts is None or accounts.empty:
        return f"No accounts in '{login.label}'."

    sections = []
    grand_count = 0

    for _, acc in accounts.iterrows():
        acc_id = acc.get("accountId")
        label = acc_label(acc, short=True)
        try:
            login.call("switch_account", acc_id, False)
            df = login.call("fetch_open_positions")
        except Exception as e:
            sections.append(f"━━━━━━━━━━\n🏦 *{label}*\n_error: {e}_")
            continue

        body, pnl, count, currency = format_positions_df(df)
        grand_count += count

        section = f"━━━━━━━━━━\n🏦 *{label}* — {count} pos"
        if count > 0:
            section += f"\n\n{body}"
            emoji = "🟢" if pnl >= 0 else "🔴"
            section += f"\n\n{emoji} _Account P&L:_ `{fmt_money(pnl, currency)}`"
        sections.append(section)

    header = f"*{login.label}* — {grand_count} open position(s)\n"
    return header + "\n\n".join(sections)


def build_positions_all_logins() -> str:
    sections = []
    grand_count = 0

    for _, login in registry.enumerate():
        try:
            accounts = login.call("fetch_accounts")
        except Exception as e:
            sections.append(f"═══════════════\n🔐 *{login.label}*\n_login error: {e}_")
            continue

        if accounts is None or accounts.empty:
            sections.append(f"═══════════════\n🔐 *{login.label}*\n_no accounts_")
            continue

        acc_lines = []
        login_count = 0

        for _, acc in accounts.iterrows():
            acc_id = acc.get("accountId")
            alabel = acc_label(acc, short=True)
            try:
                login.call("switch_account", acc_id, False)
                df = login.call("fetch_open_positions")
            except Exception as e:
                acc_lines.append(f"   🏦 {alabel}: _error_")
                continue

            _, pnl, count, currency = format_positions_df(df)
            login_count += count
            grand_count += count

            if count > 0:
                emoji = "🟢" if pnl >= 0 else "🔴"
                acc_lines.append(
                    f"   🏦 *{alabel}* — {count} pos  •  {emoji} `{fmt_money(pnl, currency)}`"
                )
            else:
                acc_lines.append(f"   🏦 {alabel} — _no positions_")

        section = (
            f"═══════════════\n🔐 *{login.label}* — {login_count} pos\n"
            + "\n".join(acc_lines)
        )
        sections.append(section)

    header = f"*All Logins* — {grand_count} open position(s) total\n"
    return header + "\n\n".join(sections)


# ============================================================
# BALANCE
# ============================================================
def format_balance_row(acc) -> str:
    name = acc_label(acc, short=False)
    balance = float(acc.get("balance", 0) or 0)
    available = float(acc.get("available", 0) or 0)
    pnl = float(acc.get("profitLoss", 0) or 0)
    deposit = float(acc.get("deposit", 0) or 0)
    currency = acc.get("currency", "")
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    return (
        f"*{name}*\n"
        f"   Balance: `{fmt_money(balance, currency)}`\n"
        f"   Available: `{fmt_money(available, currency)}`\n"
        f"   Margin used: `{fmt_money(deposit, currency)}`\n"
        f"   {pnl_emoji} Open P&L: `{fmt_money(pnl, currency)}`"
    )


def build_balance(login_idx, account_id: str) -> str:
    if login_idx == "ALL":
        return build_balance_all_logins()
    login = registry.by_idx(login_idx)
    if account_id == "ALL":
        return build_balance_all_in_login(login)
    return build_balance_one(login, account_id)


def build_balance_one(login: IGLogin, account_id: str) -> str:
    accounts = login.call("fetch_accounts")
    if accounts is None or accounts.empty:
        return f"No accounts in '{login.label}'."
    m = accounts[accounts["accountId"] == account_id]
    if m.empty:
        return f"Account `{account_id}` not found in '{login.label}'."
    return f"🔐 _Login:_ {login.label}\n\n" + format_balance_row(m.iloc[0])


def build_balance_all_in_login(login: IGLogin) -> str:
    accounts = login.call("fetch_accounts")
    if accounts is None or accounts.empty:
        return f"No accounts in '{login.label}'."
    sections = [format_balance_row(acc) for _, acc in accounts.iterrows()]
    return (
        f"*{login.label}* — _{len(accounts)} account(s)_\n\n"
        + "\n\n".join(sections)
    )


def build_balance_all_logins() -> str:
    sections = []
    total_accounts = 0
    for _, login in registry.enumerate():
        try:
            accounts = login.call("fetch_accounts")
        except Exception as e:
            sections.append(f"═══════════════\n🔐 *{login.label}*\n_error: {e}_")
            continue
        if accounts is None or accounts.empty:
            sections.append(f"═══════════════\n🔐 *{login.label}*\n_no accounts_")
            continue
        total_accounts += len(accounts)
        parts = [f"═══════════════\n🔐 *{login.label}*"]
        for _, acc in accounts.iterrows():
            parts.append("")
            parts.append(format_balance_row(acc))
        sections.append("\n".join(parts))
    header = f"_{len(registry.logins)} login(s), {total_accounts} account(s)_\n"
    return header + "\n\n".join(sections)


# ============================================================
# SUMMARY
# ============================================================
def build_summary(login_idx, account_id: str) -> str:
    if login_idx == "ALL":
        return build_summary_all_logins()
    login = registry.by_idx(login_idx)
    if account_id == "ALL":
        return build_summary_all_in_login(login)
    return build_summary_one(login, account_id)


def build_summary_one(login: IGLogin, account_id: str) -> str:
    accounts = login.call("fetch_accounts")
    if accounts is None or accounts.empty:
        return f"No accounts in '{login.label}'."
    m = accounts[accounts["accountId"] == account_id]
    if m.empty:
        return f"Account `{account_id}` not found."
    acc = m.iloc[0]

    login.call("switch_account", account_id, False)
    positions = login.call("fetch_open_positions")
    n_pos = 0 if positions is None or positions.empty else len(positions)

    balance = float(acc.get("balance", 0) or 0)
    available = float(acc.get("available", 0) or 0)
    pnl = float(acc.get("profitLoss", 0) or 0)
    currency = acc.get("currency", "")
    name = acc_label(acc, short=False)
    emoji = "🟢" if pnl >= 0 else "🔴"

    return (
        f"🔐 _Login:_ {login.label}\n🏦 _Account:_ {name}\n\n"
        f"Open positions: *{n_pos}*\n"
        f"Balance: `{fmt_money(balance, currency)}`\n"
        f"Available: `{fmt_money(available, currency)}`\n"
        f"{emoji} P&L: `{fmt_money(pnl, currency)}`"
    )


def build_summary_all_in_login(login: IGLogin) -> str:
    accounts = login.call("fetch_accounts")
    if accounts is None or accounts.empty:
        return f"No accounts in '{login.label}'."

    lines = [f"*{login.label}* — _{len(accounts)} account(s)_"]

    for _, acc in accounts.iterrows():
        acc_id = acc.get("accountId")
        label = acc_label(acc, short=True)
        currency = acc.get("currency", "")
        balance = float(acc.get("balance", 0) or 0)
        available = float(acc.get("available", 0) or 0)
        pnl = float(acc.get("profitLoss", 0) or 0)
        try:
            login.call("switch_account", acc_id, False)
            positions = login.call("fetch_open_positions")
            n_pos = 0 if positions is None or positions.empty else len(positions)
        except Exception:
            n_pos = -1
        emoji = "🟢" if pnl >= 0 else "🔴"
        n_str = "?" if n_pos < 0 else str(n_pos)
        lines.append(
            f"\n*{label}*\n"
            f"   Pos: *{n_str}*  •  {emoji} `{fmt_money(pnl, currency)}`\n"
            f"   Bal: `{fmt_money(balance, currency)}`  •  "
            f"Avail: `{fmt_money(available, currency)}`"
        )
    return "\n".join(lines)


def build_summary_all_logins() -> str:
    lines = [f"_{len(registry.logins)} login(s)_"]

    for _, login in registry.enumerate():
        try:
            accounts = login.call("fetch_accounts")
        except Exception as e:
            lines.append(f"\n═══════════════\n🔐 *{login.label}*\n_error: {e}_")
            continue
        if accounts is None or accounts.empty:
            lines.append(f"\n═══════════════\n🔐 *{login.label}*\n_no accounts_")
            continue

        lines.append(f"\n═══════════════\n🔐 *{login.label}*")
        for _, acc in accounts.iterrows():
            acc_id = acc.get("accountId")
            label = acc_label(acc, short=True)
            currency = acc.get("currency", "")
            balance = float(acc.get("balance", 0) or 0)
            available = float(acc.get("available", 0) or 0)
            pnl = float(acc.get("profitLoss", 0) or 0)
            try:
                login.call("switch_account", acc_id, False)
                positions = login.call("fetch_open_positions")
                n_pos = 0 if positions is None or positions.empty else len(positions)
            except Exception:
                n_pos = -1
            emoji = "🟢" if pnl >= 0 else "🔴"
            n_str = "?" if n_pos < 0 else str(n_pos)
            lines.append(
                f"\n*{label}*\n"
                f"   Pos: *{n_str}*  •  {emoji} `{fmt_money(pnl, currency)}`\n"
                f"   Bal: `{fmt_money(balance, currency)}`  •  "
                f"Avail: `{fmt_money(available, currency)}`"
            )
    return "\n".join(lines)


# ============================================================
# ACCOUNTS LIST
# ============================================================
def build_accounts_list() -> str:
    sections = []
    total = 0
    for _, login in registry.enumerate():
        try:
            accounts = login.call("fetch_accounts")
        except Exception as e:
            sections.append(f"═══════════════\n🔐 *{login.label}*\n_error: {e}_")
            continue
        if accounts is None or accounts.empty:
            sections.append(f"═══════════════\n🔐 *{login.label}*\n_no accounts_")
            continue
        total += len(accounts)
        acc_lines = []
        for _, acc in accounts.iterrows():
            star = "⭐" if is_preferred(acc) else "  "
            acc_lines.append(
                f"{star} *{acc.get('accountName', '?')}*  `{acc.get('accountId', '?')}`\n"
                f"      {acc.get('accountType', '')} • "
                f"{acc.get('currency', '')} • {acc.get('status', '')}"
            )
        sections.append(f"═══════════════\n🔐 *{login.label}*\n" + "\n".join(acc_lines))

    header = f"_{len(registry.logins)} login(s), {total} account(s)_\n"
    return header + "\n\n".join(sections)


# ============================================================
# BANNER MESSAGE HELPERS
# ============================================================
async def send_or_edit_banner(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    banner_key: str,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """
    Display content with the right banner. Tries to edit the current message
    in place; falls back to deleting + sending a new one when needed.

    If the text doesn't fit in a 1024-char caption, sends as plain text (no banner).
    """
    chat = update.effective_chat
    if not chat:
        return

    banner_path = BANNERS.get(banner_key)
    fits_caption = len(text) <= MAX_TG_CAPTION
    is_callback = update.callback_query is not None

    # If text is too long for a caption, send/edit as plain text
    if not fits_caption:
        text_to_send = text[:MAX_TG_MSG] if len(text) > MAX_TG_MSG else text
        if is_callback:
            try:
                await update.callback_query.edit_message_text(
                    text_to_send,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard,
                )
                return
            except Exception as e:
                msg = str(e).lower()
                if "not modified" in msg:
                    await update.callback_query.answer("Already up to date ✓")
                    return
                # Message was a photo — can't edit caption to plain text. Delete + resend.
                try:
                    await update.callback_query.message.delete()
                except Exception:
                    pass
        await chat.send_message(text_to_send, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    # Text fits — try to use a banner
    if banner_path and os.path.exists(banner_path):
        cached_id = _banner_file_ids.get(banner_key)
        media_source = cached_id if cached_id else open(banner_path, "rb")

        try:
            if is_callback:
                # Try to edit current message's media to this banner with new caption
                try:
                    sent = await update.callback_query.edit_message_media(
                        media=InputMediaPhoto(
                            media=media_source,
                            caption=text,
                            parse_mode=ParseMode.MARKDOWN,
                        ),
                        reply_markup=keyboard,
                    )
                    # Cache file_id after first upload
                    if not cached_id and sent and sent.photo:
                        _banner_file_ids[banner_key] = sent.photo[-1].file_id
                    return
                except Exception as e:
                    msg = str(e).lower()
                    if "not modified" in msg:
                        await update.callback_query.answer("Already up to date ✓")
                        return
                    # Previous message wasn't a photo (e.g. very first /start sent plain),
                    # or some other edit failure — delete and resend.
                    try:
                        await update.callback_query.message.delete()
                    except Exception:
                        pass
                    # Reopen file if needed (it may have been consumed)
                    if not isinstance(media_source, str):
                        media_source = open(banner_path, "rb")

            # Fresh send (either /start or fallback from a failed edit)
            sent = await chat.send_photo(
                photo=media_source,
                caption=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
            if not cached_id and sent.photo:
                _banner_file_ids[banner_key] = sent.photo[-1].file_id
            return
        finally:
            # Close file handle if we opened one
            if not isinstance(media_source, str):
                try:
                    media_source.close()
                except Exception:
                    pass

    # No banner available — fall back to plain text
    if is_callback:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
            )
            return
        except Exception:
            try:
                await update.callback_query.message.delete()
            except Exception:
                pass
    await chat.send_message(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


# ============================================================
# HANDLERS
# ============================================================
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        if update.message:
            await update.message.reply_text("⛔ Access denied.")
        return
    await send_or_edit_banner(update, context, "main", MENU_TEXT, main_menu_keyboard())


async def show_login_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    text = f"_Pick a login for {action.title()}_"
    await send_or_edit_banner(
        update, context, "picker_login", text, login_picker_keyboard(action)
    )


async def show_account_picker(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, login_idx
) -> None:
    login = registry.by_idx(login_idx)
    try:
        accounts = login.call("fetch_accounts")
    except Exception as e:
        await send_or_edit_banner(
            update,
            context,
            "error",
            f"❌ Error fetching accounts from *{login.label}*:\n`{e}`",
            main_menu_keyboard(),
        )
        return
    text = f"🔐 _Login:_ *{login.label}*\n_Action:_ {action.title()}\n\n_Pick an account_"
    await send_or_edit_banner(
        update, context, "picker_account", text,
        account_picker_keyboard(action, int(login_idx), accounts),
    )


async def run_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    action: str, login_idx, account_id: str,
) -> None:
    query = update.callback_query
    # Show a "loading" state for slow operations
    is_slow = login_idx == "ALL"
    if is_slow and query:
        try:
            await query.answer("⏳ Fetching across all logins...", show_alert=False)
        except Exception:
            pass

    try:
        if action == "positions":
            text = build_positions(login_idx, account_id)
        elif action == "balance":
            text = build_balance(login_idx, account_id)
        elif action == "summary":
            text = build_summary(login_idx, account_id)
        else:
            text = "Unknown action."
    except Exception as e:
        logger.exception("Action %s failed", action)
        await send_or_edit_banner(
            update, context, "error",
            f"*Error*\n`{e}`",
            result_keyboard(action, login_idx),
        )
        return

    await send_or_edit_banner(update, context, action, text, result_keyboard(action, login_idx))


async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        text = build_accounts_list()
    except Exception as e:
        logger.exception("accounts list failed")
        await send_or_edit_banner(
            update, context, "error",
            f"*Error*\n`{e}`",
            simple_result_keyboard(),
        )
        return
    await send_or_edit_banner(update, context, "accounts", text, simple_result_keyboard())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_allowed(update):
        if query:
            await query.answer("Access denied.", show_alert=True)
        return
    await query.answer()
    data = query.data or ""

    if data == "menu":
        await show_menu(update, context)
        return

    if data == "refresh":
        last_action = context.user_data.get("last_action")
        last_login = context.user_data.get("last_login_idx")
        last_acc = context.user_data.get("last_account_id")

        if last_action == "accounts":
            await show_accounts(update, context)
        elif last_action in ("positions", "balance", "summary") and last_login is not None:
            await run_action(update, context, last_action, last_login, last_acc)
        else:
            await show_menu(update, context)
        return

    if data.startswith("pick:"):
        action = data.split(":", 1)[1]
        await show_login_picker(update, context, action)
        return

    if data.startswith("pickacc:"):
        _, action, login_idx = data.split(":", 2)
        await show_account_picker(update, context, action, login_idx)
        return

    if data.startswith("run:"):
        _, action, login_idx, account_id = data.split(":", 3)
        if login_idx != "ALL":
            login_idx = int(login_idx)
        context.user_data["last_action"] = action
        context.user_data["last_login_idx"] = login_idx
        context.user_data["last_account_id"] = account_id
        await run_action(update, context, action, login_idx, account_id)
        return

    if data == "accounts":
        context.user_data["last_action"] = "accounts"
        context.user_data["last_login_idx"] = None
        context.user_data["last_account_id"] = None
        await show_accounts(update, context)
        return

    await show_menu(update, context)


async def on_any_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_menu(update, context)


# ---------- Main ----------
def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", show_menu))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("help", show_menu))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_any_text))
    app.add_handler(MessageHandler(filters.COMMAND, on_any_text))

    logger.info("Bot starting with %d login(s).", len(registry.logins))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
