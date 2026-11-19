#!/usr/bin/env python3
"""
BAGENT claim Telegram bot — works like @MR_COMPANY_2ND_BOT.

What it does
------------
1. /start -> buttons: Connect Wallet / Claim BAGENT / New QR code /
   Check status / Disconnect / Official Flap page.

   Every Telegram user has their own separate wallet session: connecting
   your wallet never touches another user's wallet, and your claim goes
   to your own wallet only.
2. Connect Wallet -> runs `baw auth signin`, shows the pairing code + QR
   (urlForWeb) with buttons underneath — Open Binance App / Check now /
   New QR / Cancel — just like @MR_COMPANY_2ND_BOT. The user scans it with
   the Binance App and taps Confirm in the app; the bot auto-verifies in
   the background (`baw auth verify --qrCodeId <id>`, which blocks until
   the user confirms) and announces "Wallet connected" by itself. No need
   to type `confirm` (it still works as a manual fallback). Check now runs
   one manual verify; Cancel drops the pending sign-in.
3. Claim BAGENT -> previews `claimAirdrop(0)` on the vault contract with
   value 0, asks for confirmation, executes, waits for the receipt and
   reports the tx hash + BAGENT received.
4. Check status -> wallet, Agent ID, BNB + BAGENT balances, last claim tx.

What it NEVER does
------------------
No buys, no swaps, no token approvals, no transfers, no sells. The claim
is a single value-0 transaction; gas is the only cost. It never asks for
a seed phrase or private key.

IMPORTANT — the claim target
----------------------------
Calling claimAirdrop on the Flap page contract
0xf713d440d84bd149e8940c2ae3932db67524e43b REVERTS (verified on-chain
2026-11-19). The working call is claimAirdrop(0) on the vault proxy
0x80CABb78d05205Bb0f7C395ec6326Bf1890E7a00 — proven by the successful
on-chain claim tx 0x97f4e7c244ccb930ff963d274d84e5c82a646c4169893c5f6f5480fde3ec9143
(2026-11-19 19:30 +06, 6,614,574.88 BAGENT to 0x3c74...).

SECURITY — read this before running it
---------------------------------------
Connecting a Binance Agentic Wallet to ANY bot hands that bot FULL control
of the wallet (Binance shows the same warning in the App). Each Telegram
user gets their OWN baw session (own HOME dir on the server), so your
wallet is separate from every other user's — but anyone who taps Connect
Wallet on YOUR Telegram account connects their wallet to your session, so
never share your Telegram account. Use a wallet that holds only what the
claim needs + a little BNB for gas, and Disconnect after claiming.

Setup: see README.md
"""

import asyncio
import io
import json
import logging
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("bagent-bot")

# ---------------------------------------------------------------- config ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_IDS = {
    int(x) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()
}

CHAIN_ID = 56  # BSC
VAULT_CONTRACT = "0x80CABb78d05205Bb0f7C395ec6326Bf1890E7a00"  # claim target (works)
FLAP_PAGE_CONTRACT = "0xf713d440d84bd149e8940c2ae3932db67524e43b"  # page contract (reverts)
FLAP_PAGE_URL = (
    "https://flap.sh/apps/bagent/bnb/0xf713d440d84bd149e8940c2ae3932db67524e43b"
)
BAGENT_TOKEN = "0xc5dc1F2aa1D962789F1A06fDb199A25c038D7777"
BAGENT_DECIMALS = 18
CLAIM_CALLDATA = "0xe30d4440" + "0" * 64  # claimAirdrop(0)
AIRDROP_OPENS_UTC = "2026-11-19 13:24:44 UTC"
BSC_RPCS = [
    "https://bsc-dataseed1.binance.org",
    "https://bsc-rpc.publicnode.com",
    "https://bsc-dataseed1.ninicoin.io",
]
MIN_BNB_FOR_GAS = 0.0001

STATE_PATH = Path(__file__).with_name("state.json")

SESSIONS_DIR = Path(__file__).with_name("baw-sessions")
# Each Telegram user gets their own baw HOME dir. baw stores its session in
# ~/.baw/session.json, so a separate HOME per user = a separate wallet
# session per user. Verified 2026-11-20: `HOME=<dir> baw auth signin`
# writes <dir>/.baw/session.json and a second HOME gets a different
# pairing code, leaving the first session untouched.


# ------------------------------------------------------------------ state ---
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def user_state(chat_id: int) -> dict:
    return load_state().get(str(chat_id), {})


def set_user_state(chat_id: int, patch: dict) -> None:
    state = load_state()
    cur = state.get(str(chat_id), {})
    cur.update(patch)
    state[str(chat_id)] = cur
    save_state(state)


# -------------------------------------------------------------- baw calls ---
def _run(cmd: list[str], uid: int, timeout: int = 120) -> dict:
    """Run a baw command with --json and return the parsed JSON (or raw text).

    `uid` is the Telegram user id. baw stores its session in ~/.baw, so each
    user runs with their own HOME dir and their wallet session is isolated
    from every other user's.
    """
    home = SESSIONS_DIR / str(uid)
    home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HOME": str(home)}
    try:
        proc = subprocess.run(
            ["baw", *cmd, "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        out = (proc.stdout or "").strip()
        try:
            return json.loads(out)
        except Exception:
            return {"success": False, "raw": out or proc.stderr, "_rc": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": {"name": "TIMEOUT", "message": "baw timed out"}}
    except FileNotFoundError:
        return {
            "success": False,
            "error": {
                "name": "BAW_MISSING",
                "message": "baw is not installed on this server",
            },
        }


async def baw(uid: int, *cmd: str, timeout: int = 120) -> dict:
    return await asyncio.to_thread(_run, list(cmd), uid, timeout)


def _find(obj, *keys):
    """Recursively find the first of `keys` in a nested dict/list."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            r = _find(v, *keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, *keys)
            if r is not None:
                return r
    return None


def err_text(res: dict) -> str:
    err = res.get("error") or {}
    if isinstance(err, dict):
        return err.get("message") or err.get("name") or str(err)
    return str(err) or (res.get("raw") or "unknown error")


async def wallet_status(uid: int) -> tuple[bool, str]:
    res = await baw(uid, "wallet", "status", timeout=30)
    status = _find(res, "status")
    connected = isinstance(status, str) and status.upper() == "CONNECTED"
    return connected, (status or "UNKNOWN")


async def wallet_address(uid: int) -> str | None:
    """The wallet's BSC (chain 56) EVM address — never the Solana one.

    `baw wallet address` returns an `addresses` list with Solana (CT_501)
    FIRST. The old code took the first `address` key it found, which was
    Solana's — so the status showed the Solana address AND the claim
    preview sent `--from <solana-address>` to a BSC contract, which the
    backend rejected as "Invalid contract-call parameters". That was the
    real cause of the claim failure (the vault checksum was also wrong,
    but this was the blocker).
    """
    res = await baw(uid, "wallet", "address", timeout=30)
    data = res.get("data") or {}
    addrs = data.get("addresses") or []
    if isinstance(addrs, list):
        # Prefer the BSC entry; any EVM entry shares the same 0x address
        # (one key), so fall back to the first 0x address in the list.
        for entry in addrs:
            if isinstance(entry, dict) and str(entry.get("binanceChainId")) == "56":
                addr = entry.get("address")
                if isinstance(addr, str) and re.match(r"^0x[0-9a-fA-F]{40}$", addr):
                    return addr
        for entry in addrs:
            if isinstance(entry, dict):
                addr = entry.get("address")
                if isinstance(addr, str) and re.match(r"^0x[0-9a-fA-F]{40}$", addr):
                    return addr
    return None


def _agent_id(agent: dict) -> str:
    """Agent ID as the user knows it: handle + '.ai'.

    baw returns the handle without the suffix ("salauddin7"); the Agent
    ID is "salauddin7.ai". Add the suffix when it is missing.
    """
    handle = (agent or {}).get("handle") or ""
    if not handle:
        return "—"
    return handle if handle.endswith(".ai") else handle + ".ai"


async def wallet_agent(uid: int) -> dict:
    res = await baw(uid, "wallet", "agent-id", timeout=30)
    data = res.get("data") or {}
    return {
        "claimed": data.get("claimed"),
        "handle": data.get("handle"),
        "agentId": data.get("agentId"),
    }


async def dev_mode_enabled(uid: int) -> bool | None:
    res = await baw(uid, "wallet", "settings", timeout=30)
    dm = _find(res, "devMode")
    if isinstance(dm, dict):
        return bool(dm.get("enabled"))
    return None


async def bnb_balance(uid: int) -> float | None:
    res = await baw(uid, "wallet", "balance", "--symbol", "BNB", "--binanceChainId", "56", timeout=30)
    data = res.get("data") or []
    items = data if isinstance(data, list) else [data]
    for it in items:
        bal = it.get("balance") or it.get("amount")
        if bal is not None:
            try:
                return float(bal)
            except ValueError:
                pass
    return None


# -------------------------------------------------------------- on-chain ----
def _rpc(method: str, params, rpc_url: str, timeout: int = 20):
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _eth_call(to: str, data: str) -> str | None:
    for rpc_url in BSC_RPCS:
        try:
            res = _rpc("eth_call", [{"to": to, "data": data}, "latest"], rpc_url, timeout=8)
            if isinstance(res.get("result"), str):
                return res["result"]
        except Exception as e:
            log.warning("eth_call failed on %s: %s", rpc_url, e)
    return None


def bagent_balance(address: str) -> float | None:
    data = "0x70a08231" + "000000000000000000000000" + address[2:].lower()
    raw = _eth_call(BAGENT_TOKEN, data)
    if raw is None:
        return None
    return int(raw, 16) / (10**BAGENT_DECIMALS)


def _receipt(tx_hash: str) -> dict | None:
    for rpc_url in BSC_RPCS:
        try:
            res = _rpc("eth_getTransactionReceipt", [tx_hash], rpc_url, timeout=10)
            if isinstance(res.get("result"), dict):
                return res["result"]
        except Exception as e:
            log.warning("receipt failed on %s: %s", rpc_url, e)
    return None


async def wait_receipt(tx_hash: str, timeout: int = 240) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        receipt = await asyncio.to_thread(_receipt, tx_hash)
        if receipt:
            return receipt
        await asyncio.sleep(5)
    return None


# ------------------------------------------------------------------ ui ------
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔗 Connect Wallet", callback_data="connect"),
                InlineKeyboardButton("🎁 Claim BAGENT", callback_data="claim"),
            ],
            [
                InlineKeyboardButton("📊 Check status", callback_data="status"),
            ],
            [
                InlineKeyboardButton("🔌 Disconnect", callback_data="disconnect"),
            ],
            [InlineKeyboardButton("🌐 Official Flap page", url=FLAP_PAGE_URL)],
        ]
    )


WELCOME = (
    "**👋 Welcome to Ultimate Crypto**\n\n"
    "I connect your **Binance Agentic Wallet** and claim the **free BAGENT "
    "airdrop** on Flap (BSC).\n\n"
    "**🔒 Claim-only:** I sign just one transaction — `claimAirdrop(0)` → "
    "the official vault.\n"
    "**💰 Value: 0 BNB** — no buys, no swaps, no token approvals, no "
    "transfers. Gas is the only cost.\n\n"
    "**▶️ Tap Connect Wallet to begin** — scan the QR, confirm in the App, "
    "and I'll tell you here as soon as you're connected. No need to type "
    "anything.\n\n"
    "**⚠️** Connecting gives this bot full control of your Agentic Wallet. "
    "Use a wallet that holds only what the claim needs + a little BNB for "
    "gas, and Disconnect after claiming. This bot never asks for your seed "
    "phrase or private key."
)

def owner_ok(update: Update) -> bool:
    """Public bot: anyone may use it when ALLOWED_IDS is empty.

    Each Telegram user has their own baw session (own HOME dir), so one
    user's wallet never touches another user's claim. Set ALLOWED_IDS to
    restrict the bot to specific Telegram user IDs.
    """
    if ALLOWED_IDS:
        return update.effective_user.id in ALLOWED_IDS
    return True


async def guard(update: Update) -> bool:
    if owner_ok(update):
        return True
    if update.callback_query:
        await update.callback_query.answer("This bot is private.", show_alert=True)
    elif update.message:
        await update.message.reply_text("Sorry, this bot is private.")
    return False


def _format_expiry(expire_at_ms) -> str:
    try:
        return time.strftime(
            "%Y-%m-%d %H:%M:%S UTC", time.gmtime(float(expire_at_ms) / 1000)
        )
    except (TypeError, ValueError):
        return "in about 5 minutes"


async def safe_edit(query, text: str, reply_markup=None, parse_mode=None):
    """Edit the message a button came from, whatever kind it is.

    Buttons under the QR photo belong to a photo message, where
    edit_message_text fails — so fall back to editing the caption, and
    finally to sending a new message. Returns the id of the message that
    ended up showing the text (or None), so the caller can track it as
    the dashboard message.
    """
    try:
        await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode
        )
        return query.message.message_id
    except Exception:
        pass
    try:
        await query.edit_message_caption(
            caption=text, reply_markup=reply_markup, parse_mode=parse_mode
        )
        return query.message.message_id
    except Exception:
        pass
    try:
        msg = await query.message.reply_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode
        )
        return msg.message_id
    except Exception:
        return None


DASHBOARD_TTL_SECONDS = 300  # dashboard deletes itself 5 min after its last update


async def _expire_dashboard(bot, chat_id: int, msg_id: int, gen: int) -> None:
    """Delete the dashboard after 5 idle minutes.

    Only the dashboard goes away — the user's own /start messages and
    the claim notifications stay. If a newer dashboard update replaced
    this one (a higher generation), this task does nothing: the timer
    restarts on every dashboard update, so an active user never loses
    the dashboard mid-flow.
    """
    await asyncio.sleep(DASHBOARD_TTL_SECONDS)
    st = user_state(chat_id)
    if st.get("dashboard_msg_id") != msg_id or st.get("dashboard_gen") != gen:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass
    set_user_state(chat_id, {"dashboard_msg_id": None, "dashboard_msg_ids": []})


async def _set_dashboard(bot, chat_id: int, msg_id: int) -> None:
    """Make `msg_id` the chat's only dashboard message.

    Deletes every previously tracked dashboard message, so the dashboard
    never piles up: connect again and again and only the latest dashboard
    stays. Failures are ignored — a message the user already deleted
    needs no cleanup. Also (re)starts the 5-minute auto-delete timer.
    """
    st = user_state(chat_id)
    for mid in list(st.get("dashboard_msg_ids") or []):
        if mid != msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass
    gen = (st.get("dashboard_gen") or 0) + 1
    set_user_state(
        chat_id,
        {"dashboard_msg_id": msg_id, "dashboard_msg_ids": [msg_id],
         "dashboard_gen": gen},
    )
    asyncio.create_task(_expire_dashboard(bot, chat_id, msg_id, gen))


async def _show_dashboard(bot, chat_id: int, text: str, reply_markup=None,
                          parse_mode=None, from_msg_id=None):
    """Show dashboard content in exactly one message.

    Prefers editing the message the user just tapped when it is already
    the dashboard; otherwise edits the stored dashboard message; if there
    is none (or it is gone), sends a new one. Old dashboard copies are
    deleted, so repeated connects never stack dashboard messages.
    """
    st = user_state(chat_id)
    dash_id = st.get("dashboard_msg_id")
    target = from_msg_id if from_msg_id == dash_id else dash_id
    if target:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=target, text=text,
                reply_markup=reply_markup, parse_mode=parse_mode,
            )
            await _set_dashboard(bot, chat_id, target)
            return target
        except Exception as e:
            if "not modified" in str(e).lower():
                await _set_dashboard(bot, chat_id, target)
                return target
    msg = await bot.send_message(
        chat_id=chat_id, text=text, reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    await _set_dashboard(bot, chat_id, msg.message_id)
    return msg.message_id


async def _delete_qr_messages(bot, chat_id: int) -> None:
    """Delete every QR photo message we have sent in this chat.

    Every QR we send is tracked in state (`qrMsgIds` list + `qrMsgId` for
    the latest one). Deleting all of them — not just the latest — is what
    keeps an old QR from staying in the chat after a connect, a cancel,
    or a fresh code. Failures are logged, never silent: a delete that
    fails is how a stale QR survives. Ids that fail to delete stay in
    state so the next cleanup retries them.
    """
    st = user_state(chat_id)
    ids = list(st.get("qrMsgIds") or [])
    if st.get("qrMsgId"):
        ids.append(st["qrMsgId"])
    failed = []
    for mid in dict.fromkeys(ids):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception as e:
            log.warning("could not delete QR message %s: %s", mid, e)
            failed.append(mid)
    set_user_state(
        chat_id,
        {"qrMsgId": failed[-1] if failed else None, "qrMsgIds": failed},
    )


async def _connected_info(uid: int) -> tuple:
    """Address + agent + BNB balance, fetched in parallel.

    Each `baw` call spawns a Node process, so three sequential calls
    cost seconds — that was the "click korle late" feeling. One gather
    costs about as much as the slowest single call.
    """
    addr, agent, bnb = await asyncio.gather(
        wallet_address(uid),
        wallet_agent(uid),
        bnb_balance(uid),
    )
    return addr, agent, bnb


def _connected_lines(addr, agent, bnb) -> list[str]:
    """The connected dashboard: status, wallet address, agent ID, BNB.

    That is all it shows. Everything bold; the address and the agent ID
    stay in backticks so they are one-tap copyable — and the `.ai`
    handle does not turn into a link (Telegram linkifies it otherwise).
    """
    return [
        "**Status:** connected",
        f"**Wallet:** `{addr}`" if addr else "**Wallet:** —",
        f"**Agent:** `{_agent_id(agent)}`",
        f"**BNB:** {bnb if bnb is not None else '—'}",
    ]


async def _announce_connected(bot, chat_id: int, uid: int, qr_id) -> bool:
    """Announce a fresh connection once, and clean up the QR.

    Returns True if this call did the announcing. If another path already
    announced the same wallet, or a different pairing code is now pending,
    it stays silent so the user never gets "Wallet connected" twice — but
    the QR is deleted whenever the wallet is connected, even then, so a
    connected wallet never leaves a stale QR behind.
    """
    connected, _ = await wallet_status(uid)
    if not connected:
        return False
    # One parallel fetch for everything the dashboard needs — no second
    # round of baw calls after the already-announced check.
    addr, agent, bnb = await _connected_info(uid)
    st = user_state(chat_id)
    await _delete_qr_messages(bot, chat_id)
    if addr and st.get("wallet") == addr:
        return False  # already announced
    if qr_id is not None and st.get("qrCodeId") not in (None, qr_id):
        return False  # a different code is pending; its task will announce
    set_user_state(chat_id, {"qrCodeId": None, "wallet": addr})
    # The dashboard shows the connection: one message, updated in place,
    # so repeated connects never stack dashboard copies.
    lines = _connected_lines(addr, agent, bnb)
    await _show_dashboard(
        bot,
        chat_id,
        text=(
            "**✅ Wallet connected**\n"
            + "\n".join(lines)
            + "\n\n**Tap Claim BAGENT when you are ready.**"
        ),
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )
    return True


async def _auto_verify(bot, chat_id: int, uid: int, qr_id: str, expire_at_ms) -> None:
    """Wait for the user to confirm in the Binance App, then announce it.

    `baw auth verify` blocks until the user confirms (verified on the VM
    2026-11-20: a verify with no confirmation sat silent for the full
    25 s test window), so one call normally returns exactly when the user
    confirms. If a verify ever returns early with "not confirmed" instead
    of blocking, the loop retries until the code expires, so auto-detect
    works either way. If the user asks for a fresh code or cancels, the
    state check inside _announce_connected makes this task exit silently.
    """
    try:
        deadline = float(expire_at_ms) / 1000
    except (TypeError, ValueError):
        deadline = time.time() + 300
    while time.time() < deadline - 5:
        remaining = deadline - time.time()
        res = await baw(
            uid, "auth", "verify", "--qrCodeId", qr_id,
            timeout=max(10, min(60, int(remaining))),
        )
        if res.get("success"):
            if await _announce_connected(bot, chat_id, uid, qr_id):
                return
            # Verify said yes but the wallet still reads unconnected
            # (transient read) — keep watching until the code expires.
            await asyncio.sleep(5)
            continue
        # Not confirmed yet (or a transient error) — wait and try again.
        await asyncio.sleep(10)
    # Expired — the expiry watch tells the user.


# -------------------------------------------------------------- handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    # /start: the old dashboard above is deleted and a fresh welcome
    # appears below, where the user just typed — instead of editing the
    # old message in place up the chat. (Other dashboard updates —
    # connect, Check status — still edit the one dashboard in place,
    # because they are tapped on that message itself.)
    bot = update.get_bot()
    chat_id = update.message.chat_id
    msg = await update.message.reply_text(
        WELCOME, reply_markup=main_keyboard(), parse_mode="Markdown"
    )
    # _set_dashboard deletes the old tracked dashboard and (re)starts
    # the 5-minute auto-delete timer for the new one.
    await _set_dashboard(bot, chat_id, msg.message_id)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data
    if action in ("check_now", "status"):
        await query.answer("Checking…")
    else:
        await query.answer()
    if not await guard(update):
        return

    if action == "connect":
        await do_connect(query)
        return

    if action == "new_qr":
        # Fresh pairing code + QR. Replaces any pending (possibly expired)
        # code: do_connect stores the new qrCodeId in state, and the old
        # code's expiry watch sees the mismatch and stays silent.
        await do_connect(query)
        return

    if action == "check_now":
        await do_check_now(query)
        return

    if action == "cancel_signin":
        await do_cancel_signin(query)
        return

    if action == "disconnect":
        await baw(query.from_user.id, "auth", "signout")
        set_user_state(
            query.message.chat_id,
            {"qrCodeId": None, "watching_claim": False},
        )
        await _delete_qr_messages(query.get_bot(), query.message.chat_id)
        await query.edit_message_text(
            "Disconnected from this bot.\nYour wallet and on-chain claim history "
            "are unchanged.",
            reply_markup=main_keyboard(),
        )
        return

    if action == "copy_address":
        await do_copy_address(query)
        return

    if action == "status":
        await do_status(query)
        return

    if action == "claim_status":
        await do_claim_status(query)
        return

    if action == "claim":
        await do_claim_start(query)
        return

    if action == "claim_yes":
        await do_claim_execute(query)
        return

    if action == "claim_no":
        await query.edit_message_text(
            "Claim cancelled. Nothing was sent.", reply_markup=main_keyboard()
        )
        return

    # Unknown/stale button (e.g. a Stop watching button from before the
    # update, when there was a stop option): just tell the user where to
    # look. The tap was already answered at the top of this handler.
    try:
        await query.answer(
            "That button is from an older version — tap Check status."
        )
    except Exception:
        pass


async def _expiry_watch(bot, chat_id: int, uid: int, qr_id: str, expire_at_ms) -> None:
    """Tell the user when their pairing code expires.

    baw pairing codes last about 5 minutes. If the user still has not
    connected by then, send them a message instead of leaving them in
    silence. Does nothing if they already connected or asked for a fresh
    code (a new qrCodeId replaces the old one in state).
    """
    now = time.time()
    try:
        delay = float(expire_at_ms) / 1000 - now + 5
    except (TypeError, ValueError):
        delay = 300
    if delay <= 0:
        delay = 5
    await asyncio.sleep(delay)
    st = user_state(chat_id)
    if st.get("qrCodeId") != qr_id:
        return  # verified, cancelled, or replaced by a fresh code
    connected, _ = await wallet_status(uid)
    if connected:
        return
    set_user_state(chat_id, {"qrCodeId": None})
    # Delete the expired QR photo so nobody scans it by mistake.
    await _delete_qr_messages(bot, chat_id)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⏰ Pairing code expired.\n\n"
                "The code only lasts about 5 minutes. Tap 🔄 New QR code "
                "for a fresh one."
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 New QR code", callback_data="new_qr")]]
            ),
        )
    except Exception:
        pass


async def do_connect(query):
    uid = query.from_user.id
    chat_id = query.message.chat_id
    # Instant feedback first: the wallet check is a baw call and takes
    # seconds, and the user should not watch a frozen button meanwhile.
    try:
        await query.edit_message_text(
            "Checking…", reply_markup=main_keyboard()
        )
    except Exception:
        pass
    connected, status = await wallet_status(uid)
    if connected:
        # Connected dashboard: status, wallet, agent, BNB — that is all.
        addr, agent, bnb = await _connected_info(uid)
        lines = _connected_lines(addr, agent, bnb)
        await _show_dashboard(
            query.get_bot(),
            chat_id,
            "**Already connected**\n" + "\n".join(lines),
            reply_markup=main_keyboard(),
            parse_mode="Markdown",
            from_msg_id=query.message.message_id,
        )
        return

    res = await baw(uid, "auth", "signin")
    if not res.get("success"):
        await safe_edit(
            query,
            f"Sign-in failed: {err_text(res)}",
            reply_markup=main_keyboard(),
        )
        return
    data = res.get("data") or {}
    if data.get("status") == "ALREADY_CONNECTED":
        await safe_edit(query, "Already connected.", reply_markup=main_keyboard())
        return

    qr_id = data.get("qrCodeId")
    code = data.get("pairingCode")
    url = data.get("urlForWeb")
    if not qr_id or not code:
        await safe_edit(
            query,
            "Sign-in failed: no pairing code returned.",
            reply_markup=main_keyboard(),
        )
        return

    # Delete every previous QR message so only the latest QR can be scanned.
    # Old QR photos stay in the chat otherwise, and scanning an expired one
    # is the most common reason a connect "is not detected".
    await _delete_qr_messages(query.get_bot(), chat_id)
    expire_at = data.get("expireAt")
    set_user_state(
        chat_id,
        {"qrCodeId": qr_id, "qrIssuedAt": time.time()},
    )
    bot = query.get_bot()
    asyncio.create_task(_expiry_watch(bot, chat_id, uid, qr_id, expire_at))
    # Auto-verify: waits in the background for the App confirmation and
    # announces the connection by itself. No need to type `confirm`.
    asyncio.create_task(_auto_verify(bot, chat_id, uid, qr_id, expire_at))
    img = qrcode.make(url or code)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    caption_lines = [
        "📱 Connect your Agentic Wallet",
        "",
        "1. Scan this QR with the Binance App (or tap Open Binance App).",
        f"2. Confirm the pairing code matches: `{code}`",
        "3. Approve the sign-in in the App. Auto-verify is active — I'll tell "
        "you here as soon as you're connected. No need to type anything.",
        "",
    ]
    if url:
        caption_lines.append(f"Link: [Binance Wallet]({url})")
    caption_lines.append(f"Expires {_format_expiry(expire_at)}.")
    qr_keyboard = InlineKeyboardMarkup(
        ([[InlineKeyboardButton("Open Binance App", url=url)]] if url else [])
        + [
            [
                InlineKeyboardButton("Check now", callback_data="check_now"),
                InlineKeyboardButton("New QR", callback_data="new_qr"),
            ],
            [InlineKeyboardButton("Cancel", callback_data="cancel_signin")],
        ]
    )
    qr_msg = await query.message.reply_photo(
        photo=bio,
        caption="\n".join(caption_lines),
        parse_mode="Markdown",
        reply_markup=qr_keyboard,
    )
    st_now = user_state(chat_id)
    ids_now = list(st_now.get("qrMsgIds") or [])
    ids_now.append(qr_msg.message_id)
    set_user_state(
        chat_id, {"qrMsgId": qr_msg.message_id, "qrMsgIds": ids_now}
    )
    if not query.message.photo:
        # Invoked from the main keyboard (a text message). When invoked
        # from the QR photo itself (New QR), the photo already says it all.
        await safe_edit(
            query,
            "QR code sent below — scan it, confirm in the Binance App, and "
            "I'll tell you here as soon as you're connected.",
            reply_markup=main_keyboard(),
        )


async def do_check_now(query):
    """Manual verify check from the button under the QR photo."""
    uid = query.from_user.id
    chat_id = query.message.chat_id
    if await _announce_connected(query.get_bot(), chat_id, uid, None):
        return
    connected, _ = await wallet_status(uid)
    if connected:
        # Already announced before (e.g. auto-verify got there first).
        addr, agent, bnb = await _connected_info(uid)
        lines = _connected_lines(addr, agent, bnb)
        await _show_dashboard(
            query.get_bot(),
            chat_id,
            "**✅ Wallet connected**\n" + "\n".join(lines),
            reply_markup=main_keyboard(),
            parse_mode="Markdown",
            from_msg_id=query.message.message_id,
        )
        return
    st = user_state(chat_id)
    qr_id = st.get("qrCodeId")
    if not qr_id:
        await query.message.reply_text(
            "No sign-in in progress. Tap Connect Wallet first.",
            reply_markup=main_keyboard(),
        )
        return
    issued = st.get("qrIssuedAt") or 0
    if time.time() - issued > 300:
        set_user_state(chat_id, {"qrCodeId": None})
        await query.message.reply_text(
            "⏰ That pairing code expired (codes last about 5 minutes).\n\n"
            "Tap Connect Wallet for a fresh one.",
            reply_markup=main_keyboard(),
        )
        return
    res = await baw(uid, "auth", "verify", "--qrCodeId", qr_id, timeout=30)
    if res.get("success"):
        if await _announce_connected(query.get_bot(), chat_id, uid, qr_id):
            return
        await query.message.reply_text(
            "✅ Wallet connected.", reply_markup=main_keyboard()
        )
        return
    await query.message.reply_text(
        "Not confirmed yet — confirm in the Binance App.\n\n"
        "Auto-verify is still watching: I'll tell you here as soon as it's "
        "done. Or tap New QR for a fresh code.",
        reply_markup=main_keyboard(),
    )


async def do_cancel_signin(query):
    """Drop a pending sign-in from the Cancel button under the QR photo."""
    chat_id = query.message.chat_id
    set_user_state(chat_id, {"qrCodeId": None})
    await _delete_qr_messages(query.get_bot(), chat_id)
    await query.message.reply_text(
        "Sign-in cancelled. Nothing was connected.",
        reply_markup=main_keyboard(),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text = (update.message.text or "").strip().lower()
    if text != "confirm":
        await update.message.reply_text(
            "Tap a button below. After you approve the sign-in in the Binance "
            "App, the bot tells you here by itself — no need to type anything.",
            reply_markup=main_keyboard(),
        )
        return

    # Manual fallback: auto-verify normally handles the connect, but typing
    # `confirm` still works — e.g. if the bot restarted after the QR was
    # issued and its background watch is gone.
    uid = update.message.from_user.id
    chat_id = update.message.chat_id
    if await _announce_connected(update.get_bot(), chat_id, uid, None):
        return
    connected, _ = await wallet_status(uid)
    if connected:
        # Already announced before (e.g. auto-verify got there first).
        addr, agent, bnb = await _connected_info(uid)
        lines = _connected_lines(addr, agent, bnb)
        await _show_dashboard(
            update.get_bot(),
            chat_id,
            "**✅ Wallet connected**\n"
            + "\n".join(lines)
            + "\n\n**Tap Claim BAGENT when you are ready.**",
            reply_markup=main_keyboard(),
            parse_mode="Markdown",
        )
        return

    st = user_state(chat_id)
    qr_id = st.get("qrCodeId")
    if not qr_id:
        await update.message.reply_text(
            "No sign-in in progress. Tap Connect Wallet first.",
            reply_markup=main_keyboard(),
        )
        return

    issued = st.get("qrIssuedAt") or 0
    if time.time() - issued > 300:
        set_user_state(chat_id, {"qrCodeId": None})
        await update.message.reply_text(
            "⏰ That pairing code expired (codes last about 5 minutes).\n\n"
            "Tap Connect Wallet for a fresh one.",
            reply_markup=main_keyboard(),
        )
        return

    await update.message.reply_text("Verifying your sign-in, please wait…")
    res = await baw(uid, "auth", "verify", "--qrCodeId", qr_id, timeout=60)
    if not res.get("success"):
        await update.message.reply_text(
            "Still not confirmed — confirm in the Binance App.\n\n"
            "Auto-verify is still watching and will tell you here as soon as "
            "it's done. If the code expired, tap Connect Wallet for a fresh one.",
            reply_markup=main_keyboard(),
        )
        return

    if await _announce_connected(update.get_bot(), chat_id, uid, qr_id):
        return
    await update.message.reply_text(
        "✅ Wallet connected.", reply_markup=main_keyboard()
    )


def _claim_state(st: dict) -> tuple[str, str]:
    """(short label, detail line) for the BAGENT claim state.

    While the watch waits for the round to open the line says
    "claiming" — and once the claim lands it says "success". There is
    no stop option: the watch just runs until the round opens, the
    wallet disconnects, or 24 hours pass.
    """
    if st.get("last_amount") is not None and st.get("last_tx"):
        return "✅ claimed", f"✅ success — {st['last_amount']} BAGENT"
    if st.get("last_tx"):
        return "⏳ claiming", "⏳ claiming… (tx sent, waiting for confirmation)"
    if st.get("pendingRequestId"):
        return "📝 preview ready", "📝 preview ready — tap Yes to send"
    if st.get("watching_claim"):
        return "⏳ claiming", "⏳ claiming… (waiting for the round to open — auto-claims when it opens)"
    return "not claimed", "— not claimed yet"


def status_keyboard(chat_id: int):
    """Only Check status — nothing else under the claiming text.

    The claim state is already in the status text itself (BAGENT +
    Claim line, tx + BscScan once it lands), so the extra rows under
    it were just clutter: Connect, Claim, Disconnect, Flap, the BAGENT
    row and Copy address all stay on the main keyboard / the connected
    dashboard instead.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📊 Check status", callback_data="status")]]
    )


async def do_claim_status(query):
    """Show the BAGENT claim state when the token is tapped in Check status."""
    uid = query.from_user.id
    chat_id = query.message.chat_id
    # Instant feedback first — the wallet + balance checks take seconds.
    try:
        await query.edit_message_text(
            "Checking…", reply_markup=main_keyboard()
        )
    except Exception:
        pass
    st = user_state(chat_id)
    short, detail = _claim_state(st)
    lines = ["**🎁 BAGENT claim**", f"**Status:** {detail}"]
    if st.get("last_tx"):
        lines.append(f"**Tx:** `{st['last_tx']}`")
        lines.append(f"**BscScan:** https://bscscan.com/tx/{st['last_tx']}")
    if st.get("last_amount") is not None:
        lines.append(f"**BAGENT received:** {st['last_amount']}")
    # Status + address in parallel: the address call fails fast when
    # the wallet is not connected, so no round trip is wasted.
    (connected, _), addr = await asyncio.gather(
        wallet_status(uid), wallet_address(uid)
    )
    if connected:
        if addr:
            bal = await asyncio.to_thread(bagent_balance, addr)
            lines.append(f"**BAGENT balance now:** {bal if bal is not None else '—'}")
    if st.get("watching_claim"):
        lines.append(
            "\n**Claiming is on — I'll claim automatically when the round opens.**"
        )
    else:
        lines.append("\n**Tap Claim BAGENT to start a claim.**")
    kb = main_keyboard()
    await _show_dashboard(
        query.get_bot(),
        query.message.chat_id,
        "\n".join(lines),
        reply_markup=kb,
        parse_mode="Markdown",
        from_msg_id=query.message.message_id,
    )


async def _status_lines(uid: int, chat_id: int) -> list[str]:
    """Check status: BAGENT balance + claim state only.

    While the watch runs the claim line says "claiming"; once the claim
    lands it says "success" and the tx is shown big (backticked +
    BscScan link). Everything bold; the tx stays in backticks so it is
    one-tap copyable.
    """
    async def _safe(coro, default=None):
        try:
            return await coro
        except Exception:
            return default

    # Status + address in parallel: the address call fails fast when
    # the wallet is not connected, so both round trips overlap instead
    # of stacking — that was the "click korle late" on Check status.
    (connected, status), addr = await asyncio.gather(
        _safe(wallet_status(uid), (False, "UNKNOWN")),
        _safe(wallet_address(uid)),
    )
    bagent = None
    if connected and addr:
        bagent = await asyncio.to_thread(bagent_balance, addr)
    st = user_state(chat_id)
    lines = [
        f"**BAGENT:** {bagent if bagent is not None else '—'}",
    ]
    short, detail = _claim_state(st)
    lines.append(f"**Claim (BAGENT):** {detail}")
    if st.get("last_tx"):
        # Tx shown big: backticked (one tap copies it) + BscScan link.
        lines.append(f"**Last tx:** `{st['last_tx']}`")
        lines.append(f"**BscScan:** https://bscscan.com/tx/{st['last_tx']}")
    if st.get("last_amount") is not None:
        lines.append(f"**BAGENT received:** {st['last_amount']}")
    return lines


async def do_status(query):
    uid = query.from_user.id
    # Answer instantly so the button never looks dead, then fill in the
    # numbers. The old version ran every check first, so a slow RPC made
    # the button appear to do nothing.
    try:
        await query.edit_message_text(
            "Checking your status…", reply_markup=main_keyboard()
        )
    except Exception:
        pass

    lines = await _status_lines(uid, query.message.chat_id)
    # One dashboard message: updated in place, old copies deleted.
    await _show_dashboard(
        query.get_bot(),
        query.message.chat_id,
        "\n".join(lines),
        reply_markup=status_keyboard(query.message.chat_id),
        parse_mode="Markdown",
        from_msg_id=query.message.message_id,
    )


async def do_copy_address(query):
    """Send the wallet address alone in a code block — one tap copies it."""
    uid = query.from_user.id
    addr = await wallet_address(uid)
    if not addr:
        await query.message.reply_text(
            "Wallet is not connected. Tap Connect Wallet first.",
            reply_markup=main_keyboard(),
        )
        return
    await query.message.reply_text(
        f"`{addr}`\n\nTap the address above to copy it.",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


WATCH_INTERVAL_SECONDS = 300  # retry the preview every 5 minutes
WATCH_MAX_SECONDS = 24 * 3600  # stop watching after 24 hours


async def _preview_claim(uid: int, addr: str) -> dict:
    """Run the claimAirdrop(0) preview for one wallet. Returns baw JSON."""
    return await baw(uid,
        "contract-call",
        "preview",
        "--binanceChainId",
        str(CHAIN_ID),
        "--from",
        addr,
        "--to",
        VAULT_CONTRACT,
        "--value",
        "0",
        "--inputData",
        CLAIM_CALLDATA,
    )


def _preview_clean(res: dict) -> tuple[bool, str]:
    """True + requestId when the preview is clean and safe to execute.

    Otherwise False + the reason (error text, "preview returned no
    request ID", or "preview changes an approval or authority").
    """
    if not res.get("success"):
        return False, err_text(res)
    data = res.get("data") or {}
    request_id = data.get("requestId")
    if not request_id:
        return False, "preview returned no request ID"
    sim = data.get("simulationResult") or {}
    if sim.get("allowanceChanges") or sim.get("authorityChanges"):
        return False, "preview changes an approval or authority"
    return True, request_id


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _received_from_receipt(receipt: dict, wallet_addr: str) -> str | None:
    """BAGENT received in this claim, read from the receipt's Transfer log.

    Exact (the log's uint256), unlike the wallet balance, which mixes in
    any BAGENT the wallet already held. Returns the amount as a decimal
    string, or None when the log is not found.
    """
    if not receipt or not wallet_addr:
        return None
    padded_to = "0x000000000000000000000000" + wallet_addr[2:].lower()
    for log in receipt.get("logs") or []:
        if not isinstance(log, dict):
            continue
        if (log.get("address") or "").lower() != BAGENT_TOKEN.lower():
            continue
        topics = log.get("topics") or []
        if len(topics) < 3 or (topics[0] or "").lower() != TRANSFER_TOPIC:
            continue
        if (topics[2] or "").lower() != padded_to:
            continue
        try:
            value = int(log.get("data") or "0x0", 16)
        except ValueError:
            continue
        whole, frac = divmod(value, 10 ** BAGENT_DECIMALS)
        return f"{whole}.{frac:018d}"
    return None


async def _report_claim_result(bot, chat_id: int, uid: int, tx_hash: str) -> None:
    """Wait for the claim receipt, then report success/failure + balance.

    Success looks like MR_COMPANY_2ND_BOT's confirmation: "BAGENT claim
    confirmed", Received amount, Wallet, Tx — all big — plus a View on
    BscScan button and a Check status button.
    """
    receipt = await wait_receipt(tx_hash)
    addr = await wallet_address(uid)
    amount = await asyncio.to_thread(bagent_balance, addr) if addr else None
    if receipt and receipt.get("status") == "0x1":
        received = _received_from_receipt(receipt, addr)
        if received is not None:
            # Store as a string: state is JSON, and Decimal is not
            # JSON-serializable (saving a Decimal crashes save_state).
            set_user_state(chat_id, {"last_amount": received})
        elif amount is not None:
            set_user_state(chat_id, {"last_amount": str(amount)})
        shown = received if received is not None else (
            str(amount) if amount is not None else "—"
        )
        wallet_line = f"Wallet: `{addr}`" if addr else "Wallet: —"
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ BAGENT claim confirmed\n\n"
                f"Received:\n{shown}\nBAGENT\n"
                f"{wallet_line}\n"
                f"Tx: `{tx_hash}`\n\n"
                "No swaps, approvals, or transfers were made."
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "View on BscScan",
                            url=f"https://bscscan.com/tx/{tx_hash}",
                        )
                    ],
                    [InlineKeyboardButton("Check status", callback_data="status")],
                ]
            ),
        )
    elif receipt:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ Transaction confirmed but FAILED on-chain (status 0).\n"
                f"Tx: `{tx_hash}`\n"
                f"BscScan: https://bscscan.com/tx/{tx_hash}"
            ),
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "Transaction sent but no receipt seen yet.\n"
                f"Tx: `{tx_hash}`\nCheck it on BscScan."
            ),
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )


async def _claim_watch(bot, chat_id: int, uid: int) -> None:
    """Auto-claim as soon as the round opens.

    Runs in the background after a claim preview fails because the round
    is not open yet. Every 5 minutes it re-runs the preview; the moment a
    preview comes back clean (value 0, no approval/authority changes) it
    executes the claim by itself and reports the tx + balance. There is
    no stop option: it stops only when the wallet disconnects, after
    24 hours, or when the claim is done. Never executes an unsafe preview.
    """
    started = time.time()
    while user_state(chat_id).get("watching_claim"):
        if time.time() - started > WATCH_MAX_SECONDS:
            break
        connected, _ = await wallet_status(uid)
        if not connected:
            set_user_state(chat_id, {"watching_claim": False})
            await bot.send_message(
                chat_id=chat_id,
                text="👀 Watch stopped — wallet disconnected. Nothing was sent.",
                reply_markup=main_keyboard(),
            )
            return
        addr = await wallet_address(uid)
        if not addr:
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)
            continue
        res = await _preview_claim(uid, addr)
        ok, info = _preview_clean(res)
        if ok:
            request_id = info
            set_user_state(chat_id, {"watching_claim": False})
            await bot.send_message(
                chat_id=chat_id,
                text="🎉 Round opened — claiming now (auto-claim).",
                reply_markup=main_keyboard(),
            )
            ex = await baw(uid, "contract-call", "execute", "--requestId", request_id)
            if not ex.get("success"):
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"Auto-claim failed to send: {err_text(ex)}",
                    reply_markup=main_keyboard(),
                )
                return
            tx_hash = _find(ex, "transactionHash", "hash", "txHash")
            if not tx_hash:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"Sent, but no tx hash was returned: {json.dumps(ex)[:500]}",
                    reply_markup=main_keyboard(),
                )
                return
            set_user_state(chat_id, {"pendingRequestId": None, "last_tx": tx_hash})
            await _report_claim_result(bot, chat_id, uid, tx_hash)
            return
        if "approval" in info or "authority" in info:
            set_user_state(chat_id, {"watching_claim": False})
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "👀 Watch stopped — the preview now changes an approval "
                    "or authority, so I will not auto-claim it. Nothing was sent."
                ),
                reply_markup=main_keyboard(),
            )
            return
        await asyncio.sleep(WATCH_INTERVAL_SECONDS)
    set_user_state(chat_id, {"watching_claim": False})


async def do_claim_start(query):
    uid = query.from_user.id
    # Instant feedback first: the wallet checks below are baw calls and
    # take seconds, and the user should not watch a frozen button meanwhile.
    try:
        await query.edit_message_text(
            "Checking your wallet…", reply_markup=main_keyboard()
        )
    except Exception:
        pass
    connected, status = await wallet_status(uid)
    if not connected:
        await query.edit_message_text(
            "Wallet is not connected. Tap Connect Wallet first.",
            reply_markup=main_keyboard(),
        )
        return
    # Address + Developer Mode + BNB in parallel: three baw calls that
    # used to run one after another — that was the "click korle late"
    # on Claim. The address call is the only one that can fail hard;
    # the other two fail soft (None / not False) and pass the checks.
    addr, dev, bnb = await asyncio.gather(
        wallet_address(uid),
        dev_mode_enabled(uid),
        bnb_balance(uid),
    )
    if not addr:
        await query.edit_message_text(
            "Could not read the wallet address. Try Connect Wallet again.",
            reply_markup=main_keyboard(),
        )
        return

    if dev is False:
        await query.edit_message_text(
            "Developer Mode is OFF in the Binance App.\n\n"
            "Turn it on first: Binance App → Agentic Wallet → Settings → "
            "Developer Mode, then tap Claim BAGENT again.",
            reply_markup=main_keyboard(),
        )
        return

    if bnb is not None and bnb < MIN_BNB_FOR_GAS:
        await query.edit_message_text(
            f"Not enough BNB for gas (balance {bnb} BNB). Top up a little BNB "
            "and try again.",
            reply_markup=main_keyboard(),
        )
        return

    await query.edit_message_text("Simulating the claim, please wait…")
    res = await _preview_claim(uid, addr)
    if not res.get("success"):
        err = err_text(res)
        err_l = err.lower()
        if "invalid" in err_l or "parameter" in err_l:
            # A parameter error is a bot-side problem, not a closed round:
            # do not start the watch on it, or it would loop forever.
            await query.edit_message_text(
                "Claim preview failed — the preview could not be built "
                "(parameter problem).\n"
                f"Error: {err}\n\n"
                "This looks like a bot-side problem, not a closed round. "
                "Nothing was sent and no gas was spent.",
                reply_markup=main_keyboard(),
            )
            return
        if "already claimed" in err_l or "already airdropped" in err_l:
            await query.edit_message_text(
                "This wallet already claimed the BAGENT airdrop.\n\n"
                "Nothing was sent.",
                reply_markup=main_keyboard(),
            )
            return
        # Round not open yet (or not eligible yet) — the watch starts and
        # runs silently in the background. No separate "Watching now"
        # message and no stop option at all: the dashboard stays, and its
        # Claim (BAGENT) line shows "claiming" until the claim lands.
        set_user_state(
            query.message.chat_id,
            {"watching_claim": True, "watch_started_at": time.time()},
        )
        asyncio.create_task(
            _claim_watch(query.get_bot(), query.message.chat_id, uid)
        )
        lines = await _status_lines(uid, query.message.chat_id)
        await _show_dashboard(
            query.get_bot(),
            query.message.chat_id,
            "\n".join(lines),
            reply_markup=status_keyboard(query.message.chat_id),
            parse_mode="Markdown",
            from_msg_id=query.message.message_id,
        )
        return

    # Manual claim takes over from the watch, if one was running.
    set_user_state(query.message.chat_id, {"watching_claim": False})
    data = res.get("data") or {}
    request_id = data.get("requestId")
    if not request_id:
        await query.edit_message_text(
            "Preview returned no request ID — not executing.",
            reply_markup=main_keyboard(),
        )
        return

    sim = data.get("simulationResult") or {}
    bal_changes = sim.get("balanceChanges") or []
    allow_changes = sim.get("allowanceChanges") or []
    auth_changes = sim.get("authorityChanges") or []
    set_user_state(query.message.chat_id, {"pendingRequestId": request_id})

    lines = [
        "Claim preview (simulation):",
        f"Call: claimAirdrop(0) → {VAULT_CONTRACT}",
        "Value: 0 BNB",
        f"Balance changes: {bal_changes if bal_changes else 'BAGENT to your wallet (see receipt after)'}",
        f"Allowance changes: {allow_changes or 'none'}",
        f"Authority changes: {auth_changes or 'none'}",
    ]
    if allow_changes or auth_changes:
        lines.append(
            "\n⚠️ This preview changes an approval or authority — NOT executing."
        )
        await query.edit_message_text(
            "\n".join(lines), reply_markup=main_keyboard()
        )
        return
    lines.append("\nSend this claim transaction?")
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Yes, claim", callback_data="claim_yes"),
                    InlineKeyboardButton("❌ Cancel", callback_data="claim_no"),
                ]
            ]
        ),
    )


async def do_claim_execute(query):
    uid = query.from_user.id
    st = user_state(query.message.chat_id)
    request_id = st.get("pendingRequestId")
    if not request_id:
        await query.edit_message_text(
            "No pending preview. Tap Claim BAGENT to start over.",
            reply_markup=main_keyboard(),
        )
        return

    # Manual execute takes over from the watch, if one was running.
    set_user_state(query.message.chat_id, {"watching_claim": False})
    await query.edit_message_text("Claiming… sending the transaction.")
    res = await baw(uid, "contract-call", "execute", "--requestId", request_id)
    if not res.get("success"):
        await query.edit_message_text(
            f"Claim failed to send: {err_text(res)}",
            reply_markup=main_keyboard(),
        )
        return

    tx_hash = _find(res, "transactionHash", "hash", "txHash")
    if not tx_hash:
        await query.edit_message_text(
            f"Sent, but no tx hash was returned: {json.dumps(res)[:500]}",
            reply_markup=main_keyboard(),
        )
        return

    set_user_state(
        query.message.chat_id, {"pendingRequestId": None, "last_tx": tx_hash}
    )
    await query.edit_message_text(
        f"Transaction sent: `{tx_hash}`\nWaiting for confirmation…",
        parse_mode="Markdown",
    )
    await _report_claim_result(query.get_bot(), query.message.chat_id, uid, tx_hash)


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("BAGENT claim bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
