"""
IG Markets Spread Betting - Telegram Reporting Bot
Multi-login + multi-account support, button-driven UI.
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

MAX_TG_MSG = 4000  # Telegram limit is 4096; leave headroom


# ---------- Login configuration ----------
def load_logins() -> List[Dict[str, Any]]:
    """Load multiple IG logins from the IG_LOGINS env var (JSON array)."""
    raw = os.environ.get("IG_LOGINS", "").strip()
    if not raw:
        print("=" * 60, file=sys.stderr)
        print("❌ IG_LOGINS env var is empty.", file=sys.stderr)
        print("Set it to a JSON array, e.g.:", file=sys.stderr)
        print('   [{"label":"Demo","username":"X","password":"Y","api_key":"Z","acc_type":"DEMO"}]', file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        # Sleep forever instead of crash-looping
        import time; time.sleep(3600)
        raise SystemExit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print("=" * 60, file=sys.stderr)
        print(f"❌ IG_LOGINS is not valid JSON: {e}", file=sys.stderr)
        print("First 200 chars of what you set:", file=sys.stderr)
        print(f"   {raw[:200]!r}", file=sys.stderr)
        print("Tip: make sure quotes are straight \" not curly \u201c \u201d", file=sys.stderr)
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
            print(f"   Required field names (exact, case-sensitive): {list(REQUIRED)}", file=sys.stderr)
            print(f"   Optional: 'label', 'acc_type'", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            import time; time.sleep(3600)
            raise SystemExit(1)

        cfg.setdefault("acc_type", "DEMO")
        cfg.setdefault("label", cfg["username"])

    return data


# ---------- One IG login session ----------
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


# ---------- Registry ----------
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


def truncate(text: str) -> str:
    if len(text) <= MAX_TG_MSG:
        return text
    return text[:MAX_TG_MSG] + "\n\n_... (output truncated)_"


def grand_totals_text(totals: Dict[str, float], heading: str) -> str:
    if not totals:
        return ""
    lines = [f"\n\n━━━━━━━━━━\n*{heading}*"]
    for cur, val in totals.items():
        emoji = "🟢" if val >= 0 else "🔴"
        lines.append(f"{emoji} `{fmt_money(val, cur)}`")
    return "\n".join(lines)


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
            [InlineKeyboardButton("🏓 Ping", callback_data="ping")],
            [InlineKeyboardButton("🔄 Refresh menu", callback_data="menu")],
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
    "🤖 *IG Spread Bet Reporter*\n\n"
    f"_{len(registry.logins)} login(s) configured._\n\n"
    "Tap a button below to see your account info."
)


# ---------- Position formatting ----------
def format_positions_df(df) -> Tuple[str, float, int, str]:
    """Format a positions dataframe. Returns (body, total_pnl, count, currency)."""
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

    header = f"📊 *Open positions*\n🔐 _Login:_ {login.label}\n🏦 _Account:_ {name}\n"
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
    grand_totals: Dict[str, float] = {}
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
        if currency:
            grand_totals[currency] = grand_totals.get(currency, 0.0) + pnl

        section = f"━━━━━━━━━━\n🏦 *{label}* — {count} pos"
        if count > 0:
            section += f"\n\n{body}"
            emoji = "🟢" if pnl >= 0 else "🔴"
            section += f"\n\n{emoji} _Subtotal:_ `{fmt_money(pnl, currency)}`"
        sections.append(section)

    header = f"📊 *{login.label}* — {grand_count} open position(s)\n"
    return header + "\n\n".join(sections) + grand_totals_text(grand_totals, "Total P&L:")


def build_positions_all_logins() -> str:
    """Compact view: per login, per account. Just counts and subtotals (no per-position detail)."""
    sections = []
    grand_totals: Dict[str, float] = {}
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
        login_totals: Dict[str, float] = {}

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
            if currency:
                login_totals[currency] = login_totals.get(currency, 0.0) + pnl
                grand_totals[currency] = grand_totals.get(currency, 0.0) + pnl

            if count > 0:
                emoji = "🟢" if pnl >= 0 else "🔴"
                acc_lines.append(
                    f"   🏦 *{alabel}* — {count} pos  •  {emoji} `{fmt_money(pnl, currency)}`"
                )
            else:
                acc_lines.append(f"   🏦 {alabel} — _no positions_")

        # Login subtotal
        subtotal_str = ""
        if login_totals:
            parts = []
            for c, v in login_totals.items():
                em = "🟢" if v >= 0 else "🔴"
                parts.append(f"{em} `{fmt_money(v, c)}`")
            subtotal_str = "  •  " + " / ".join(parts)

        section = (
            f"═══════════════\n🔐 *{login.label}* — {login_count} pos{subtotal_str}\n"
            + "\n".join(acc_lines)
        )
        sections.append(section)

    header = f"📊 *All Logins* — {grand_count} open position(s) total\n"
    return header + "\n\n".join(sections) + grand_totals_text(grand_totals, "Grand Total P&L:")


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
    return f"💼 *Balance*\n🔐 _Login:_ {login.label}\n\n" + format_balance_row(m.iloc[0])


def build_balance_all_in_login(login: IGLogin) -> str:
    accounts = login.call("fetch_accounts")
    if accounts is None or accounts.empty:
        return f"No accounts in '{login.label}'."

    sections = [format_balance_row(acc) for _, acc in accounts.iterrows()]
    totals: Dict[str, Dict[str, float]] = {}
    for _, acc in accounts.iterrows():
        cur = acc.get("currency", "")
        t = totals.setdefault(cur, {"balance": 0.0, "available": 0.0, "pnl": 0.0})
        t["balance"] += float(acc.get("balance", 0) or 0)
        t["available"] += float(acc.get("available", 0) or 0)
        t["pnl"] += float(acc.get("profitLoss", 0) or 0)

    total_lines = ["━━━━━━━━━━", f"*Totals — {login.label}*"]
    for cur, t in totals.items():
        emoji = "🟢" if t["pnl"] >= 0 else "🔴"
        total_lines.append(
            f"   Balance: `{fmt_money(t['balance'], cur)}`\n"
            f"   Available: `{fmt_money(t['available'], cur)}`\n"
            f"   {emoji} P&L: `{fmt_money(t['pnl'], cur)}`"
        )

    return f"💼 *{login.label}*\n\n" + "\n\n".join(sections) + "\n\n" + "\n".join(total_lines)


def build_balance_all_logins() -> str:
    sections = []
    grand: Dict[str, Dict[str, float]] = {}

    for _, login in registry.enumerate():
        try:
            accounts = login.call("fetch_accounts")
        except Exception as e:
            sections.append(f"═══════════════\n🔐 *{login.label}*\n_error: {e}_")
            continue
        if accounts is None or accounts.empty:
            sections.append(f"═══════════════\n🔐 *{login.label}*\n_no accounts_")
            continue

        # Per-login totals (compact)
        per_login: Dict[str, Dict[str, float]] = {}
        for _, acc in accounts.iterrows():
            cur = acc.get("currency", "")
            t = per_login.setdefault(cur, {"balance": 0.0, "available": 0.0, "pnl": 0.0})
            g = grand.setdefault(cur, {"balance": 0.0, "available": 0.0, "pnl": 0.0})
            for k, key in (
                ("balance", "balance"),
                ("available", "available"),
                ("pnl", "profitLoss"),
            ):
                val = float(acc.get(key, 0) or 0)
                t[k] += val
                g[k] += val

        parts = [f"═══════════════\n🔐 *{login.label}* ({len(accounts)} acc)"]
        for cur, t in per_login.items():
            emoji = "🟢" if t["pnl"] >= 0 else "🔴"
            parts.append(
                f"   Balance: `{fmt_money(t['balance'], cur)}`\n"
                f"   Available: `{fmt_money(t['available'], cur)}`\n"
                f"   {emoji} P&L: `{fmt_money(t['pnl'], cur)}`"
            )
        sections.append("\n".join(parts))

    grand_lines = ["━━━━━━━━━━", "*GRAND TOTALS (all logins)*"]
    for cur, t in grand.items():
        emoji = "🟢" if t["pnl"] >= 0 else "🔴"
        grand_lines.append(
            f"   Balance: `{fmt_money(t['balance'], cur)}`\n"
            f"   Available: `{fmt_money(t['available'], cur)}`\n"
            f"   {emoji} P&L: `{fmt_money(t['pnl'], cur)}`"
        )

    return "💼 *All Logins — Balance*\n\n" + "\n\n".join(sections) + "\n\n" + "\n".join(grand_lines)


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
        f"📈 *Summary*\n🔐 _Login:_ {login.label}\n🏦 _Account:_ {name}\n\n"
        f"Open positions: *{n_pos}*\n"
        f"Balance: `{fmt_money(balance, currency)}`\n"
        f"Available: `{fmt_money(available, currency)}`\n"
        f"{emoji} P&L: `{fmt_money(pnl, currency)}`"
    )


def build_summary_all_in_login(login: IGLogin) -> str:
    accounts = login.call("fetch_accounts")
    if accounts is None or accounts.empty:
        return f"No accounts in '{login.label}'."

    lines = [f"📈 *Summary — {login.label}*"]
    totals: Dict[str, Dict[str, float]] = {}
    total_pos = 0

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
        total_pos += max(n_pos, 0)

        emoji = "🟢" if pnl >= 0 else "🔴"
        n_str = "?" if n_pos < 0 else str(n_pos)
        lines.append(
            f"\n*{label}*\n"
            f"   Pos: *{n_str}*  •  {emoji} `{fmt_money(pnl, currency)}`\n"
            f"   Bal: `{fmt_money(balance, currency)}`  •  "
            f"Avail: `{fmt_money(available, currency)}`"
        )

        t = totals.setdefault(currency, {"balance": 0.0, "available": 0.0, "pnl": 0.0})
        t["balance"] += balance
        t["available"] += available
        t["pnl"] += pnl

    lines.append(f"\n━━━━━━━━━━\n*Totals* — {total_pos} pos")
    for cur, t in totals.items():
        emoji = "🟢" if t["pnl"] >= 0 else "🔴"
        lines.append(
            f"   Bal: `{fmt_money(t['balance'], cur)}`  •  "
            f"Avail: `{fmt_money(t['available'], cur)}`  •  "
            f"{emoji} `{fmt_money(t['pnl'], cur)}`"
        )
    return "\n".join(lines)


def build_summary_all_logins() -> str:
    lines = ["📈 *Summary — All Logins*"]
    grand: Dict[str, Dict[str, float]] = {}
    grand_pos = 0

    for _, login in registry.enumerate():
        try:
            accounts = login.call("fetch_accounts")
        except Exception as e:
            lines.append(f"\n═══════════════\n🔐 *{login.label}*\n_error: {e}_")
            continue
        if accounts is None or accounts.empty:
            lines.append(f"\n═══════════════\n🔐 *{login.label}*\n_no accounts_")
            continue

        per: Dict[str, Dict[str, float]] = {}
        login_pos = 0
        for _, acc in accounts.iterrows():
            acc_id = acc.get("accountId")
            currency = acc.get("currency", "")
            balance = float(acc.get("balance", 0) or 0)
            available = float(acc.get("available", 0) or 0)
            pnl = float(acc.get("profitLoss", 0) or 0)
            try:
                login.call("switch_account", acc_id, False)
                positions = login.call("fetch_open_positions")
                n_pos = 0 if positions is None or positions.empty else len(positions)
            except Exception:
                n_pos = 0
            login_pos += n_pos
            grand_pos += n_pos

            for d in (per, grand):
                t = d.setdefault(currency, {"balance": 0.0, "available": 0.0, "pnl": 0.0})
                t["balance"] += balance
                t["available"] += available
                t["pnl"] += pnl

        lines.append(f"\n═══════════════\n🔐 *{login.label}* ({len(accounts)} acc, {login_pos} pos)")
        for cur, t in per.items():
            emoji = "🟢" if t["pnl"] >= 0 else "🔴"
            lines.append(
                f"   Bal: `{fmt_money(t['balance'], cur)}`  •  "
                f"Avail: `{fmt_money(t['available'], cur)}`  •  "
                f"{emoji} `{fmt_money(t['pnl'], cur)}`"
            )

    lines.append(f"\n━━━━━━━━━━\n*GRAND TOTALS* — {grand_pos} pos")
    for cur, t in grand.items():
        emoji = "🟢" if t["pnl"] >= 0 else "🔴"
        lines.append(
            f"   Bal: `{fmt_money(t['balance'], cur)}`  •  "
            f"Avail: `{fmt_money(t['available'], cur)}`  •  "
            f"{emoji} `{fmt_money(t['pnl'], cur)}`"
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

    header = f"🏦 *All accounts*\n{len(registry.logins)} login(s), {total} account(s)\n"
    return header + "\n\n".join(sections)


def build_ping() -> str:
    return f"🏓 *pong*\nServer time: `{datetime.utcnow():%Y-%m-%d %H:%M:%S} UTC`"


# ============================================================
# HANDLERS
# ============================================================
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        if update.message:
            await update.message.reply_text("⛔ Access denied.")
        return
    if update.callback_query:
        await update.callback_query.edit_message_text(
            MENU_TEXT, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard()
        )
    elif update.message:
        await update.message.reply_text(
            MENU_TEXT, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard()
        )


async def safe_edit(query, text: str, keyboard: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(
            truncate(text), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
    except Exception as e:
        if "not modified" in str(e).lower():
            await query.answer("Already up to date ✓")
        else:
            raise


async def show_login_picker(query, action: str) -> None:
    text = f"📂 *Choose a login for {action.title()}:*"
    await safe_edit(query, text, login_picker_keyboard(action))


async def show_account_picker(query, action: str, login_idx) -> None:
    login = registry.by_idx(login_idx)
    try:
        accounts = login.call("fetch_accounts")
    except Exception as e:
        await safe_edit(
            query, f"❌ Error fetching accounts from '{login.label}':\n`{e}`",
            main_menu_keyboard(),
        )
        return
    text = f"📂 *Choose an account*\n🔐 _Login:_ {login.label}\n_Action:_ {action.title()}"
    await safe_edit(query, text, account_picker_keyboard(action, int(login_idx), accounts))


async def run_action(query, action: str, login_idx, account_id: str) -> None:
    # Slow operations: show a loading state first
    is_slow = login_idx == "ALL"
    if is_slow:
        try:
            await query.edit_message_text(
                "⏳ *Fetching across all logins...*",
                parse_mode=ParseMode.MARKDOWN,
            )
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
        text = f"❌ *Error*\n`{e}`"

    await safe_edit(query, text, result_keyboard(action, login_idx))


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

        if last_action == "ping":
            await safe_edit(query, build_ping(), simple_result_keyboard())
        elif last_action == "accounts":
            try:
                text = build_accounts_list()
            except Exception as e:
                text = f"❌ *Error*\n`{e}`"
            await safe_edit(query, text, simple_result_keyboard())
        elif last_action in ("positions", "balance", "summary") and last_login is not None:
            await run_action(query, last_action, last_login, last_acc)
        else:
            await show_menu(update, context)
        return

    if data.startswith("pick:"):
        action = data.split(":", 1)[1]
        await show_login_picker(query, action)
        return

    if data.startswith("pickacc:"):
        _, action, login_idx = data.split(":", 2)
        await show_account_picker(query, action, login_idx)
        return

    if data.startswith("run:"):
        # run:action:login_idx:account_id    (login_idx can be int or "ALL", account_id can be id or "ALL")
        _, action, login_idx, account_id = data.split(":", 3)
        # Normalize: if int, keep as string for consistency, but we need int for indexing
        if login_idx != "ALL":
            login_idx = int(login_idx)
        context.user_data["last_action"] = action
        context.user_data["last_login_idx"] = login_idx
        context.user_data["last_account_id"] = account_id
        await run_action(query, action, login_idx, account_id)
        return

    if data == "ping":
        context.user_data["last_action"] = "ping"
        context.user_data["last_login_idx"] = None
        context.user_data["last_account_id"] = None
        await safe_edit(query, build_ping(), simple_result_keyboard())
        return

    if data == "accounts":
        context.user_data["last_action"] = "accounts"
        context.user_data["last_login_idx"] = None
        context.user_data["last_account_id"] = None
        try:
            await query.edit_message_text(
                "⏳ *Loading accounts...*", parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass
        try:
            text = build_accounts_list()
        except Exception as e:
            logger.exception("accounts list failed")
            text = f"❌ *Error*\n`{e}`"
        await safe_edit(query, text, simple_result_keyboard())
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

    logger.info("Bot starting with %d login(s) configured.", len(registry.logins))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
