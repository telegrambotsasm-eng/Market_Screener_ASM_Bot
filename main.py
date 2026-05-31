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
# ALLOWED_CHAT_ID is optional now — admin_chat_id can come from IG_LOGINS.
# If both are missing, load_config will fail with a clear message.

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


# ---------- Login + member configuration ----------
def load_config() -> Dict[str, Any]:
    """
    Load IG_LOGINS env var. Supports two formats:
    1) Legacy: a JSON array of logins. Admin is ALLOWED_CHAT_ID, no extra members.
    2) New: a JSON object with keys 'admin_chat_id', 'logins', 'members'.
    Returns a dict: {'admin_chat_id', 'logins': [...], 'members': [...]}.
    """
    raw = os.environ.get("IG_LOGINS", "").strip()
    if not raw:
        print("=" * 60, file=sys.stderr)
        print("❌ IG_LOGINS env var is empty.", file=sys.stderr)
        print("Set it to a JSON object (new) or array (legacy).", file=sys.stderr)
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

    # Normalize to the new format
    if isinstance(data, list):
        # Legacy: just an array of logins
        config = {
            "admin_chat_id": int(os.environ["ALLOWED_CHAT_ID"]),
            "logins": data,
            "members": [],
        }
    elif isinstance(data, dict):
        if "logins" not in data:
            print("❌ IG_LOGINS object must have a 'logins' key.", file=sys.stderr)
            import time; time.sleep(3600)
            raise SystemExit(1)
        # admin_chat_id can come from the env var as a fallback
        admin_id = data.get("admin_chat_id")
        if admin_id is None:
            admin_id_str = os.environ.get("ALLOWED_CHAT_ID", "").strip()
            if not admin_id_str:
                print("❌ IG_LOGINS needs 'admin_chat_id' (or set ALLOWED_CHAT_ID env var).", file=sys.stderr)
                import time; time.sleep(3600)
                raise SystemExit(1)
            admin_id = int(admin_id_str)
        config = {
            "admin_chat_id": int(admin_id),
            "logins": data["logins"],
            "members": data.get("members", []),
        }
    else:
        print("❌ IG_LOGINS must be a JSON array or object.", file=sys.stderr)
        import time; time.sleep(3600)
        raise SystemExit(1)

    # Validate logins
    if not isinstance(config["logins"], list) or not config["logins"]:
        print("❌ 'logins' must be a non-empty array.", file=sys.stderr)
        import time; time.sleep(3600)
        raise SystemExit(1)

    REQUIRED = ("username", "password", "api_key")
    for i, cfg in enumerate(config["logins"]):
        if not isinstance(cfg, dict):
            print(f"❌ logins[{i}] must be an object", file=sys.stderr)
            import time; time.sleep(3600)
            raise SystemExit(1)
        missing = [f for f in REQUIRED if not cfg.get(f)]
        if missing:
            print(f"❌ logins[{i}] is missing field(s): {missing}", file=sys.stderr)
            print(f"   Fields found: {list(cfg.keys())}", file=sys.stderr)
            import time; time.sleep(3600)
            raise SystemExit(1)
        cfg.setdefault("acc_type", "DEMO")
        cfg.setdefault("label", cfg["username"])

    # Validate members
    if not isinstance(config["members"], list):
        print("❌ 'members' must be an array.", file=sys.stderr)
        import time; time.sleep(3600)
        raise SystemExit(1)

    n_logins = len(config["logins"])
    for i, m in enumerate(config["members"]):
        if not isinstance(m, dict):
            print(f"❌ members[{i}] must be an object", file=sys.stderr)
            import time; time.sleep(3600)
            raise SystemExit(1)
        if "chat_id" not in m:
            print(f"❌ members[{i}] missing 'chat_id'", file=sys.stderr)
            import time; time.sleep(3600)
            raise SystemExit(1)
        m["chat_id"] = int(m["chat_id"])
        m.setdefault("name", f"User {m['chat_id']}")
        m.setdefault("logins", list(range(n_logins)))  # default: all logins
        # Validate login indices
        bad = [idx for idx in m["logins"] if not (0 <= idx < n_logins)]
        if bad:
            print(f"❌ members[{i}] '{m['name']}' has invalid login indices: {bad}", file=sys.stderr)
            print(f"   Valid range: 0 to {n_logins - 1}", file=sys.stderr)
            import time; time.sleep(3600)
            raise SystemExit(1)

    return config


# Keep load_logins as a backwards-compat shim returning the login list
def load_logins() -> List[Dict[str, Any]]:
    return CONFIG["logins"]


CONFIG = load_config()
ADMIN_CHAT_ID = CONFIG["admin_chat_id"]
MEMBERS = CONFIG["members"]  # list of {chat_id, name, logins}

# In-memory pause state: set of chat_ids that are paused
PAUSED: set = set()


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

    def enumerate_for(self, chat_id: int):
        """Yield only (idx, login) pairs the chat is allowed to see."""
        allowed = allowed_login_indices(chat_id)
        if allowed is None:
            return []
        return [(i, self.logins[i]) for i in allowed]

    def can_access(self, chat_id: int, login_idx) -> bool:
        if login_idx == "ALL":
            return allowed_login_indices(chat_id) is not None
        allowed = allowed_login_indices(chat_id)
        if allowed is None:
            return False
        return int(login_idx) in allowed


registry = Registry(load_logins())


# ---------- Access control ----------
def is_admin(chat_id: int) -> bool:
    return chat_id == ADMIN_CHAT_ID


def find_member(chat_id: int) -> Optional[Dict[str, Any]]:
    """Return the member dict for this chat_id, or None."""
    for m in MEMBERS:
        if m["chat_id"] == chat_id:
            return m
    return None


def allowed_login_indices(chat_id: int) -> Optional[List[int]]:
    """
    Return the list of login indices this chat is allowed to see.
    None = not authorised. Empty list = authorised but no logins assigned.
    Admin always sees all logins.
    """
    if is_admin(chat_id):
        return list(range(len(registry.logins)))
    if chat_id in PAUSED:
        return None  # paused
    m = find_member(chat_id)
    if m is None:
        return None  # not in the member list
    return list(m["logins"])


def is_allowed(update: Update) -> bool:
    """Backward-compat: true if user has any access."""
    chat = update.effective_chat
    if not chat:
        return False
    return allowed_login_indices(chat.id) is not None


def access_denied_reason(chat_id: int) -> str:
    """Human-readable reason why a non-admin can't use the bot right now."""
    if chat_id in PAUSED:
        return "⛔ Your access has been temporarily paused by the admin."
    return "⛔ You don't have access to this bot. Please ask the admin."


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
def main_menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📊 Positions", callback_data="pick:positions"),
            InlineKeyboardButton("💼 Balance", callback_data="pick:balance"),
        ],
        [
            InlineKeyboardButton("📈 Summary", callback_data="pick:summary"),
            InlineKeyboardButton("🏦 Accounts", callback_data="accounts"),
        ],
    ]
    if is_admin(chat_id):
        rows.append([InlineKeyboardButton("👥 Manage members", callback_data="members")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def login_picker_keyboard(action: str, chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    for idx, login in registry.enumerate_for(chat_id):
        rows.append(
            [InlineKeyboardButton(f"🔐 {login.label}", callback_data=f"pickacc:{action}:{idx}")]
        )
    # Only show "All logins" if the user has access to more than one
    if len(registry.enumerate_for(chat_id)) > 1:
        rows.append(
            [InlineKeyboardButton("🌐 All logins (everything)", callback_data=f"run:{action}:ALL:ALL")]
        )
    rows.append([InlineKeyboardButton("🔙 Back to menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def members_menu_keyboard() -> InlineKeyboardMarkup:
    """Admin-only: list members with toggle buttons."""
    rows = []
    for m in MEMBERS:
        paused = m["chat_id"] in PAUSED
        status = "⛔ Paused" if paused else "✅ Active"
        action = "resume" if paused else "pause"
        rows.append(
            [InlineKeyboardButton(
                f"{m['name']} — {status}",
                callback_data=f"member:{action}:{m['chat_id']}",
            )]
        )
    if not MEMBERS:
        rows.append([InlineKeyboardButton("(no members configured)", callback_data="menu")])
    rows.append([InlineKeyboardButton("🔙 Back to menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)
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


def menu_text_for(chat_id: int) -> str:
    n_allowed = len(registry.enumerate_for(chat_id))
    if is_admin(chat_id):
        role = "_Admin._"
    else:
        m = find_member(chat_id)
        name = m["name"] if m else "User"
        role = f"_Member: {name}_"
    return (
        f"{role}\n"
        f"_{n_allowed} login(s) available._\n\n"
        "Tap a button below to see your account info."
    )


def members_text() -> str:
    if not MEMBERS:
        return "👥 *Members*\n\n_No members configured._"
    lines = [f"👥 *Members ({len(MEMBERS)})*\n"]
    lines.append("_Tap a member to pause or resume their access._")
    return "\n".join(lines)


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
def build_positions(login_idx, account_id: str, chat_id: int) -> str:
    if login_idx == "ALL":
        return build_positions_all_logins(chat_id)
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


def build_positions_all_logins(chat_id: int) -> str:
    sections = []
    grand_count = 0

    for _, login in registry.enumerate_for(chat_id):
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


def build_balance(login_idx, account_id: str, chat_id: int) -> str:
    if login_idx == "ALL":
        return build_balance_all_logins(chat_id)
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


def build_balance_all_logins(chat_id: int) -> str:
    sections = []
    total_accounts = 0
    allowed_logins = registry.enumerate_for(chat_id)
    for _, login in allowed_logins:
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
    header = f"_{len(allowed_logins)} login(s), {total_accounts} account(s)_\n"
    return header + "\n\n".join(sections)


# ============================================================
# SUMMARY
# ============================================================
def build_summary(login_idx, account_id: str, chat_id: int) -> str:
    if login_idx == "ALL":
        return build_summary_all_logins(chat_id)
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


def build_summary_all_logins(chat_id: int) -> str:
    allowed_logins = registry.enumerate_for(chat_id)
    lines = [f"_{len(allowed_logins)} login(s)_"]

    for _, login in allowed_logins:
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
def build_accounts_list(chat_id: int) -> str:
    sections = []
    total = 0
    allowed_logins = registry.enumerate_for(chat_id)
    for _, login in allowed_logins:
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

    header = f"_{len(allowed_logins)} login(s), {total} account(s)_\n"
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
    chat = update.effective_chat
    if not chat:
        return
    if not is_allowed(update):
        msg_text = access_denied_reason(chat.id)
        if update.message:
            await update.message.reply_text(msg_text)
        elif update.callback_query:
            try:
                await update.callback_query.answer(msg_text, show_alert=True)
            except Exception:
                pass
        return
    await send_or_edit_banner(
        update, context, "main",
        menu_text_for(chat.id),
        main_menu_keyboard(chat.id),
    )


async def show_login_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    chat = update.effective_chat
    if not chat:
        return
    text = f"_Pick a login for {action.title()}_"
    await send_or_edit_banner(
        update, context, "picker_login", text, login_picker_keyboard(action, chat.id)
    )


async def show_account_picker(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, login_idx
) -> None:
    chat = update.effective_chat
    if not chat or not registry.can_access(chat.id, login_idx):
        if update.callback_query:
            await update.callback_query.answer("⛔ Not allowed.", show_alert=True)
        return
    login = registry.by_idx(login_idx)
    try:
        accounts = login.call("fetch_accounts")
    except Exception as e:
        await send_or_edit_banner(
            update, context, "error",
            f"❌ Error fetching accounts from *{login.label}*:\n`{e}`",
            main_menu_keyboard(chat.id),
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
    chat = update.effective_chat
    if not chat or not registry.can_access(chat.id, login_idx):
        if update.callback_query:
            await update.callback_query.answer("⛔ Not allowed.", show_alert=True)
        return

    query = update.callback_query
    is_slow = login_idx == "ALL"
    if is_slow and query:
        try:
            await query.answer("⏳ Fetching across all logins...", show_alert=False)
        except Exception:
            pass

    try:
        if action == "positions":
            text = build_positions(login_idx, account_id, chat.id)
        elif action == "balance":
            text = build_balance(login_idx, account_id, chat.id)
        elif action == "summary":
            text = build_summary(login_idx, account_id, chat.id)
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
    chat = update.effective_chat
    if not chat:
        return
    try:
        text = build_accounts_list(chat.id)
    except Exception as e:
        logger.exception("accounts list failed")
        await send_or_edit_banner(
            update, context, "error",
            f"*Error*\n`{e}`",
            simple_result_keyboard(),
        )
        return
    await send_or_edit_banner(update, context, "accounts", text, simple_result_keyboard())


async def show_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only screen to pause/resume members."""
    chat = update.effective_chat
    if not chat or not is_admin(chat.id):
        if update.callback_query:
            await update.callback_query.answer("⛔ Admin only.", show_alert=True)
        return
    await send_or_edit_banner(
        update, context, "accounts",  # reuse the accounts banner visually
        members_text(),
        members_menu_keyboard(),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if not chat or not is_allowed(update):
        if query:
            await query.answer(access_denied_reason(chat.id if chat else 0), show_alert=True)
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
        elif last_action == "members":
            await show_members(update, context)
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

    if data == "members":
        if not is_admin(chat.id):
            await query.answer("⛔ Admin only.", show_alert=True)
            return
        context.user_data["last_action"] = "members"
        await show_members(update, context)
        return

    if data.startswith("member:"):
        if not is_admin(chat.id):
            await query.answer("⛔ Admin only.", show_alert=True)
            return
        _, sub_action, member_chat_id_str = data.split(":", 2)
        member_chat_id = int(member_chat_id_str)
        member = find_member(member_chat_id)
        name = member["name"] if member else str(member_chat_id)
        if sub_action == "pause":
            PAUSED.add(member_chat_id)
            await query.answer(f"⛔ Paused: {name}")
            logger.info("Admin paused member chat_id=%s (%s)", member_chat_id, name)
        elif sub_action == "resume":
            PAUSED.discard(member_chat_id)
            await query.answer(f"✅ Resumed: {name}")
            logger.info("Admin resumed member chat_id=%s (%s)", member_chat_id, name)
        # Refresh the members screen so the button label updates
        await show_members(update, context)
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

    logger.info(
        "Bot starting: %d login(s), %d member(s), admin=%s",
        len(registry.logins), len(MEMBERS), ADMIN_CHAT_ID,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
