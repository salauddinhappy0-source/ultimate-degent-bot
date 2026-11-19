#!/usr/bin/env python3
"""
BAGENT claim Telegram bot — works like @MR_COMPANY_2ND_BOT.

What it does
------------
1. /start -> buttons: Connect Wallet / Claim BAGENT / Check status /
   Disconnect / Official Flap page.

   Every Telegram user has their own separate wallet session: connecting
   your wallet never touches another user's wallet, and your claim goes
   to your own wallet only.
2. Connect Wallet -> runs `baw auth signin`, shows the pairing code + QR
   (urlForWeb). The user scans it with the Binance App, taps Confirm in the
   app, then types `confirm` in the bot -> the bot runs
   `baw auth verify --qrCodeId <id>`.
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
0x80cabb78d05205bb0f7c395ec6326bf1890e7a00 — proven by the successful
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
VAULT_CONTRACT = "0x80cabb78d05205bb0f7c395ec6326bf1890e7a00"  # claim target (works)
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
    res = await baw(uid, "wallet", "status")
    status = _find(res, "status")
    connected = isinstance(status, str) and status.upper() == "CONNECTED"
    return connected, (status or "UNKNOWN")


async def wallet_address(uid: int) -> str | None:
    res = await baw(uid, "wallet", "address")
    data = res.get("data") or {}
    # data can be {"56": "0x...", ...} or {"address": "0x..."}
    addr = _find(data, "address")
    if addr:
        return addr
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str) and re.match(r"^0x[0-9a-fA-F]{40}$", v):
                return v
    return None


async def wallet_agent(uid: int) -> dict:
    res = await baw(uid, "wallet", "agent-id")
    data = res.get("data") or {}
    return {
        "claimed": data.get("claimed"),
        "handle": data.get("handle"),
        "agentId": data.get("agentId"),
    }


async def dev_mode_enabled(uid: int) -> bool | None:
    res = await baw(uid, "wallet", "settings")
    dm = _find(res, "devMode")
    if isinstance(dm, dict):
        return bool(dm.get("enabled"))
    return None


async def bnb_balance(uid: int) -> float | None:
    res = await baw(uid, "wallet", "balance", "--symbol", "BNB", "--binanceChainId", "56")
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
            res = _rpc("eth_call", [{"to": to, "data": data}, "latest"], rpc_url)
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
            res = _rpc("eth_getTransactionReceipt", [tx_hash], rpc_url)
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
                InlineKeyboardButton("🔌 Disconnect", callback_data="disconnect"),
            ],
            [InlineKeyboardButton("🌐 Official Flap page", url=FLAP_PAGE_URL)],
        ]
    )


WELCOME = (
    "I connect your Binance Agentic Wallet and claim the free BAGENT airdrop "
    "on Flap (BSC).\n\n"
    "I sign only one transaction: claimAirdrop(0) → official vault.\n"
    "Value: 0 BNB. No buys, swaps, token approvals, or transfers.\n"
    "Gas is the only cost.\n\n"
    "Tap Connect Wallet to begin.\n\n"
    "⚠️ Connecting gives this bot full control of your Agentic Wallet. "
    "Use a wallet that holds only what the claim needs + a little BNB for gas, "
    "and Disconnect after claiming. This bot never asks for your seed phrase "
    "or private key."
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


# -------------------------------------------------------------- handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(WELCOME, reply_markup=main_keyboard())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await guard(update):
        return
    action = query.data

    if action == "connect":
        await do_connect(query)
        return

    if action == "disconnect":
        await baw(query.from_user.id, "auth", "signout")
        set_user_state(query.message.chat_id, {"qrCodeId": None})
        await query.edit_message_text(
            "Disconnected from this bot.\nYour wallet and on-chain claim history "
            "are unchanged.",
            reply_markup=main_keyboard(),
        )
        return

    if action == "status":
        await do_status(query)
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


async def do_connect(query):
    uid = query.from_user.id
    connected, status = await wallet_status(uid)
    if connected:
        addr = await wallet_address(uid)
        agent = await wallet_agent(uid)
        await query.edit_message_text(
            f"Already connected.\nWallet: {addr}\nAgent: {agent.get('handle')}",
            reply_markup=main_keyboard(),
        )
        return

    res = await baw(uid, "auth", "signin")
    if not res.get("success"):
        await query.edit_message_text(
            f"Sign-in failed: {err_text(res)}", reply_markup=main_keyboard()
        )
        return
    data = res.get("data") or {}
    if data.get("status") == "ALREADY_CONNECTED":
        await query.edit_message_text(
            "Already connected.", reply_markup=main_keyboard()
        )
        return

    qr_id = data.get("qrCodeId")
    code = data.get("pairingCode")
    url = data.get("urlForWeb")
    if not qr_id or not code:
        await query.edit_message_text(
            "Sign-in failed: no pairing code returned.",
            reply_markup=main_keyboard(),
        )
        return

    set_user_state(query.message.chat_id, {"qrCodeId": qr_id})
    img = qrcode.make(url or code)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    caption = (
        "🔗 Connect your Binance Agentic Wallet\n\n"
        f"Pairing code: `{code}`\n\n"
        "1. Open your Binance App and scan the QR code below (or tap the link).\n"
        "2. Check the code matches, then tap Confirm in the Binance App.\n"
        "3. Type `confirm` here to finish.\n\n"
        "⏰ The code expires in about 5 minutes."
    )
    await query.message.reply_photo(
        photo=bio,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Open Binance login link", url=url)]]
            if url
            else []
        ),
    )
    await query.edit_message_text(
        "QR code sent below. Scan it, confirm in the Binance App, then type "
        "`confirm` here.",
        reply_markup=main_keyboard(),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text = (update.message.text or "").strip().lower()
    if text != "confirm":
        await update.message.reply_text(
            "Tap a button below, or type `confirm` after you approve the "
            "sign-in in the Binance App.",
            reply_markup=main_keyboard(),
        )
        return

    st = user_state(update.message.chat_id)
    qr_id = st.get("qrCodeId")
    if not qr_id:
        await update.message.reply_text(
            "No sign-in in progress. Tap Connect Wallet first.",
            reply_markup=main_keyboard(),
        )
        return

    await update.message.reply_text("Verifying your sign-in, please wait…")
    # auth verify blocks until the user confirms in the App (max ~5 min).
    res = await baw(update.message.from_user.id, "auth", "verify", "--qrCodeId", qr_id, timeout=330)
    if not res.get("success"):
        await update.message.reply_text(
            f"Sign-in did not complete: {err_text(res)}\n\n"
            "If the code expired, tap Connect Wallet for a fresh code.",
            reply_markup=main_keyboard(),
        )
        return

    connected, _ = await wallet_status(update.message.from_user.id)
    if not connected:
        await update.message.reply_text(
            "The App shows signed in but the wallet is still not connected "
            "here. Tap Connect Wallet for a fresh code.",
            reply_markup=main_keyboard(),
        )
        return

    addr = await wallet_address(update.message.from_user.id)
    agent = await wallet_agent(update.message.from_user.id)
    set_user_state(update.message.chat_id, {"qrCodeId": None, "wallet": addr})
    await update.message.reply_text(
        "✅ Wallet connected.\n"
        f"Wallet: {addr}\n"
        f"Agent: {agent.get('handle')}\n\n"
        "Tap Claim BAGENT when you are ready.",
        reply_markup=main_keyboard(),
    )


async def do_status(query):
    uid = query.from_user.id
    connected, status = await wallet_status(uid)
    addr = await wallet_address(uid) if connected else None
    agent = await wallet_agent(uid) if connected else {}
    st = user_state(query.message.chat_id)
    lines = [
        "Your status",
        f"Status: {'connected' if connected else status.lower()}",
        f"Wallet: {addr or '—'}",
        f"Agent: {agent.get('handle') or '—'}",
    ]
    if connected and addr:
        bnb = await bnb_balance(uid)
        bagent = await asyncio.to_thread(bagent_balance, addr)
        lines.append(f"BNB: {bnb if bnb is not None else '—'}")
        lines.append(f"BAGENT: {bagent if bagent is not None else '—'}")
    if st.get("last_tx"):
        lines.append(f"Last tx: {st['last_tx']}")
    if st.get("last_amount") is not None:
        lines.append(f"BAGENT received: {st['last_amount']}")
    lines.append(f"\nAirdrop opens: {AIRDROP_OPENS_UTC}")
    await query.edit_message_text("\n".join(lines), reply_markup=main_keyboard())


async def do_claim_start(query):
    uid = query.from_user.id
    connected, status = await wallet_status(uid)
    if not connected:
        await query.edit_message_text(
            "Wallet is not connected. Tap Connect Wallet first.",
            reply_markup=main_keyboard(),
        )
        return
    addr = await wallet_address(uid)
    if not addr:
        await query.edit_message_text(
            "Could not read the wallet address. Try Connect Wallet again.",
            reply_markup=main_keyboard(),
        )
        return

    dev = await dev_mode_enabled(uid)
    if dev is False:
        await query.edit_message_text(
            "Developer Mode is OFF in the Binance App.\n\n"
            "Turn it on first: Binance App → Agentic Wallet → Settings → "
            "Developer Mode, then tap Claim BAGENT again.",
            reply_markup=main_keyboard(),
        )
        return

    bnb = await bnb_balance(uid)
    if bnb is not None and bnb < MIN_BNB_FOR_GAS:
        await query.edit_message_text(
            f"Not enough BNB for gas (balance {bnb} BNB). Top up a little BNB "
            "and try again.",
            reply_markup=main_keyboard(),
        )
        return

    await query.edit_message_text("Simulating the claim, please wait…")
    res = await baw(uid,
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
    if not res.get("success"):
        await query.edit_message_text(
            "Claim preview failed — the airdrop round is probably not open yet.\n"
            f"Error: {err_text(res)}\n\n"
            "Nothing was sent and no gas was spent. Try again later.",
            reply_markup=main_keyboard(),
        )
        return

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

    receipt = await wait_receipt(tx_hash)
    addr = await wallet_address(query.from_user.id)
    amount = await asyncio.to_thread(bagent_balance, addr) if addr else None
    if receipt and receipt.get("status") == "0x1":
        if amount is not None:
            set_user_state(
                query.message.chat_id, {"last_amount": amount}
            )
        await query.message.reply_text(
            "✅ BAGENT claimed!\n"
            f"Tx: `{tx_hash}`\n"
            f"BAGENT balance now: {amount if amount is not None else '—'}\n\n"
            "Tip: tap Disconnect when you are done.",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
    elif receipt:
        await query.message.reply_text(
            f"⚠️ Transaction confirmed but FAILED on-chain (status 0).\n"
            f"Tx: `{tx_hash}`",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
    else:
        await query.message.reply_text(
            "Transaction sent but no receipt seen yet.\n"
            f"Tx: `{tx_hash}`\nCheck it on BscScan.",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )


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
