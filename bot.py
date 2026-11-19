import os, csv, io, sqlite3, logging, asyncio
from decimal import Decimal
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
USDT_ADDRESS = os.getenv("USDT_ADDRESS", "")  # Your receiving TRC20 address
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
DB = os.getenv("DB_PATH", "proxybot.db")
NETWORK = "TRC20"

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      tg_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS proxies(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      continent TEXT NOT NULL, country TEXT NOT NULL,
      host TEXT NOT NULL, port INTEGER NOT NULL,
      username TEXT, password TEXT, proxy_type TEXT DEFAULT 'HTTP',
      price REAL NOT NULL, sold INTEGER DEFAULT 0, expires TEXT
    );
    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER,
      proxy_id INTEGER, amount REAL, status TEXT DEFAULT 'pending',
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS deposits(
      id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER,
      amount REAL, txid TEXT UNIQUE, status TEXT DEFAULT 'pending',
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY, value TEXT
    );
    """)
    c.commit(); c.close()

def menu():
    kb=InlineKeyboardBuilder()
    items=[
        ("🛒 Buy Proxy","buy"),("💰 USDT Deposit","deposit"),
        ("📦 My Proxies","mine"),("💳 My Wallet","wallet"),
        ("🔍 Check Proxy","check"),("🎁 Referral","ref"),
        ("🆘 Support","support")
    ]
    for t,d in items: kb.button(text=t,callback_data=d)
    kb.adjust(2,2,2,1)
    return kb.as_markup()

def back():
    kb=InlineKeyboardBuilder(); kb.button(text="🔙 Back",callback_data="home")
    return kb.as_markup()

@dp.message(Command("start"))
async def start(m: Message):
    c=db(); c.execute("INSERT OR IGNORE INTO users(tg_id) VALUES(?)",(m.from_user.id,)); c.commit(); c.close()
    await m.answer("📋 <b>Proxy Selling Bot</b>\n\nChoose an option:",reply_markup=menu(),parse_mode="HTML")

@dp.callback_query(F.data=="home")
async def home(q: CallbackQuery):
    await q.message.edit_text("📋 <b>Proxy Selling Bot</b>",reply_markup=menu(),parse_mode="HTML")
    await q.answer()

@dp.callback_query(F.data=="deposit")
async def deposit(q: CallbackQuery):
    if not USDT_ADDRESS:
        text="⚠️ Deposit address is not configured yet."
    else:
        text=(f"💰 <b>USDT Deposit</b>\n\n"
              f"Network: <b>{NETWORK}</b>\n"
              f"Address:\n<code>{USDT_ADDRESS}</code>\n\n"
              "Send USDT only on the selected network.\n"
              "After sending, use /verify TXID to request automatic verification.")
    await q.message.edit_text(text,reply_markup=back(),parse_mode="HTML"); await q.answer()

async def tron_usdt_amount(txid):
    """Returns USDT amount received by USDT_ADDRESS for a TRC20 transfer, or None."""
    if not TRONGRID_API_KEY or not USDT_ADDRESS:
        return None
    url=f"https://api.trongrid.io/v1/transactions/{txid}"
    headers={"TRON-PRO-API-KEY":TRONGRID_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.get(url,headers=headers,timeout=20) as r:
            if r.status != 200: return None
            data=await r.json()
    txs=data.get("data") or []
    if not txs: return None
    tx=txs[0]
    if tx.get("ret", [{}])[0].get("contractRet") != "SUCCESS":
        return None
    contracts=tx.get("raw_data",{}).get("contract",[])
    for contract in contracts:
        val=contract.get("parameter",{}).get("value",{})
        if val.get("to_address") and val.get("amount") is None:
            # TRC20 TriggerSmartContract
            data_hex=val.get("data","")
            contract_addr=val.get("contract_address","")
            if len(data_hex) >= 72 and data_hex[:8].lower()=="a9059cbb":
                to_hex=data_hex[8+24:8+64]
                to_addr=tron_base58_from_hex(to_hex)
                raw_amount=int(data_hex[8+64:8+128],16) / 1_000_000
                if to_addr == USDT_ADDRESS and raw_amount > 0:
                    return raw_amount
    return None

def tron_base58_from_hex(h):
    # Uses Tron address derivation without external dependencies.
    import hashlib
    b=bytes.fromhex("41"+h)
    chk=hashlib.sha256(hashlib.sha256(b).digest()).digest()[:4]
    alphabet=b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n=int.from_bytes(b+chk,"big"); out=b""
    while n:
        n,rem=divmod(n,58); out=bytes([alphabet[rem]])+out
    pad=0
    for x in b+chk:
        if x==0: pad+=1
        else: break
    return (b"1"*pad+out).decode()

@dp.message(Command("verify"))
async def verify(m: Message):
    parts=m.text.split()
    if len(parts)!=2:
        await m.answer("Use: <code>/verify TRON_TRANSACTION_ID</code>",parse_mode="HTML"); return
    txid=parts[1]
    c=db()
    try:
        c.execute("INSERT INTO deposits(tg_id,txid) VALUES(?,?)",(m.from_user.id,txid))
        c.commit()
    except sqlite3.IntegrityError:
        pass
    c.close()
    amount=await tron_usdt_amount(txid)
    if amount is None:
        await m.answer("⏳ Transaction not verified yet. Make sure it is a successful TRC20 USDT transfer to the configured address, then try again.")
        return
    c=db()
    row=c.execute("SELECT status FROM deposits WHERE txid=?",(txid,)).fetchone()
    if row and row["status"]=="credited":
        c.close(); await m.answer("✅ This transaction was already credited."); return
    c.execute("UPDATE deposits SET amount=?,status='credited' WHERE txid=?",(amount,txid))
    c.execute("INSERT OR IGNORE INTO users(tg_id) VALUES(?)",(m.from_user.id,))
    c.execute("UPDATE users SET balance=balance+? WHERE tg_id=?",(amount,m.from_user.id))
    c.commit(); c.close()
    await m.answer(f"✅ Deposit verified.\n💵 Credited: <b>{amount:.6f} USDT</b>",parse_mode="HTML")

@dp.callback_query(F.data=="wallet")
async def wallet(q: CallbackQuery):
    c=db(); u=c.execute("SELECT balance FROM users WHERE tg_id=?",(q.from_user.id,)).fetchone(); c.close()
    await q.message.edit_text(f"💳 <b>Wallet</b>\nBalance: <code>{u['balance']:.6f} USDT</code>",reply_markup=back(),parse_mode="HTML"); await q.answer()

@dp.callback_query(F.data=="buy")
async def buy(q: CallbackQuery):
    c=db(); rows=c.execute("SELECT continent,COUNT(*) n FROM proxies WHERE sold=0 GROUP BY continent").fetchall(); c.close()
    kb=InlineKeyboardBuilder()
    for r in rows: kb.button(text=f"🌍 {r['continent']} ({r['n']})",callback_data=f"cont:{r['continent']}")
    kb.button(text="🔙 Back",callback_data="home"); kb.adjust(2)
    await q.message.edit_text("📦 <b>Available Stock</b>\nSelect continent:",reply_markup=kb.as_markup(),parse_mode="HTML"); await q.answer()

@dp.callback_query(F.data.startswith("cont:"))
async def cont(q: CallbackQuery):
    continent=q.data.split(":",1)[1]
    c=db(); rows=c.execute("SELECT country,COUNT(*) n FROM proxies WHERE sold=0 AND continent=? GROUP BY country",(continent,)).fetchall(); c.close()
    kb=InlineKeyboardBuilder()
    for r in rows: kb.button(text=f"🌐 {r['country']} ({r['n']})",callback_data=f"country:{r['country']}")
    kb.button(text="🔙 Back",callback_data="buy"); kb.adjust(2)
    await q.message.edit_text(f"🌍 <b>{continent}</b>\nSelect country:",reply_markup=kb.as_markup(),parse_mode="HTML"); await q.answer()

@dp.callback_query(F.data.startswith("country:"))
async def country(q: CallbackQuery):
    country=q.data.split(":",1)[1]
    c=db(); rows=c.execute("SELECT id,proxy_type,price,expires FROM proxies WHERE sold=0 AND country=? ORDER BY id LIMIT 50",(country,)).fetchall(); c.close()
    kb=InlineKeyboardBuilder()
    for r in rows: kb.button(text=f"🟢 {r['proxy_type']} — {r['price']:.2f} USDT",callback_data=f"order:{r['id']}")
    kb.button(text="🔙 Back",callback_data="buy"); kb.adjust(1)
    await q.message.edit_text(f"🌐 <b>{country}</b>\nSelect a proxy:",reply_markup=kb.as_markup(),parse_mode="HTML"); await q.answer()

@dp.callback_query(F.data.startswith("order:"))
async def order(q: CallbackQuery):
    pid=int(q.data.split(":")[1]); uid=q.from_user.id
    c=db(); p=c.execute("SELECT * FROM proxies WHERE id=? AND sold=0",(pid,)).fetchone(); u=c.execute("SELECT balance FROM users WHERE tg_id=?",(uid,)).fetchone()
    if not p: c.close(); await q.answer("Proxy unavailable.",show_alert=True); return
    if not u or u["balance"] < p["price"]: c.close(); await q.answer("Insufficient balance. Deposit USDT first.",show_alert=True); return
    c.execute("UPDATE users SET balance=balance-? WHERE tg_id=?",(p["price"],uid))
    c.execute("UPDATE proxies SET sold=1 WHERE id=?",(pid,))
    c.execute("INSERT INTO orders(tg_id,proxy_id,amount,status) VALUES(?,?,?,'paid')",(uid,pid,p["price"]))
    c.commit(); c.close()
    auth=f"\nUsername: <code>{p['username']}</code>\nPassword: <code>{p['password']}</code>" if p["username"] else ""
    await q.message.edit_text(f"✅ <b>Purchase successful</b>\n\n🌐 {p['country']}\n🔌 {p['proxy_type']}\n🖥 <code>{p['host']}:{p['port']}</code>{auth}\n📅 Expires: {p['expires'] or 'N/A'}",parse_mode="HTML")
    await q.answer()

@dp.callback_query(F.data=="mine")
async def mine(q: CallbackQuery):
    c=db(); rows=c.execute("""SELECT p.* FROM proxies p JOIN orders o ON o.proxy_id=p.id WHERE o.tg_id=? ORDER BY o.id DESC""",(q.from_user.id,)).fetchall(); c.close()
    if not rows: text="📦 No purchased proxies."
    else:
        text="📦 <b>My Proxies</b>\n\n"
        for p in rows:
            auth=f":{p['username']}:{p['password']}" if p["username"] else ""
            text+=f"🌐 {p['country']} | {p['proxy_type']}\n<code>{p['host']}:{p['port']}{auth}</code>\nExpires: {p['expires'] or 'N/A'}\n\n"
    await q.message.edit_text(text,reply_markup=back(),parse_mode="HTML"); await q.answer()

@dp.callback_query(F.data=="check")
async def check(q: CallbackQuery):
    await q.message.edit_text("🔍 Basic proxy checker placeholder.\nFor production, connect your authorized proxy-check provider/API.",reply_markup=back()); await q.answer()

@dp.callback_query(F.data=="support")
async def support(q: CallbackQuery):
    await q.message.edit_text("🆘 Support\nContact the bot administrator.",reply_markup=back()); await q.answer()

@dp.callback_query(F.data=="ref")
async def ref(q: CallbackQuery):
    me=await bot.get_me()
    await q.message.edit_text(f"🎁 Referral\nhttps://t.me/{me.username}?start=ref_{q.from_user.id}",reply_markup=back()); await q.answer()

# ---------- Admin ----------
def is_admin(m): return m.from_user.id == ADMIN_ID

@dp.message(Command("admin"))
async def admin(m: Message):
    if not is_admin(m): return
    kb=InlineKeyboardBuilder()
    for t,d in [("📊 Stats","astats"),("📦 Stock","astock"),("➕ Add Proxy","helpadd"),("📥 CSV Template","template"),("💳 Deposits","adeposits")]:
        kb.button(text=t,callback_data=d)
    kb.adjust(2)
    await m.answer("🛠 <b>Admin Panel</b>",reply_markup=kb.as_markup(),parse_mode="HTML")

@dp.callback_query(F.data=="astats")
async def astats(q: CallbackQuery):
    if q.from_user.id!=ADMIN_ID: return
    c=db()
    users=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    stock=c.execute("SELECT COUNT(*) n FROM proxies WHERE sold=0").fetchone()["n"]
    sold=c.execute("SELECT COUNT(*) n FROM proxies WHERE sold=1").fetchone()["n"]
    dep=c.execute("SELECT COALESCE(SUM(amount),0) x FROM deposits WHERE status='credited'").fetchone()["x"]
    c.close()
    await q.message.edit_text(f"📊 <b>Stats</b>\nUsers: {users}\nAvailable: {stock}\nSold: {sold}\nVerified deposits: {dep:.2f} USDT",reply_markup=back(),parse_mode="HTML"); await q.answer()

@dp.callback_query(F.data=="astock")
async def astock(q: CallbackQuery):
    if q.from_user.id!=ADMIN_ID: return
    c=db(); rows=c.execute("SELECT continent,country,COUNT(*) n FROM proxies WHERE sold=0 GROUP BY continent,country ORDER BY continent,country").fetchall(); c.close()
    text="📦 <b>Stock</b>\n\n"
    for r in rows: text+=f"🌍 {r['continent']} / {r['country']}: {r['n']}\n"
    await q.message.edit_text(text or "No stock.",reply_markup=back(),parse_mode="HTML"); await q.answer()

@dp.callback_query(F.data=="helpadd")
async def helpadd(q: CallbackQuery):
    if q.from_user.id!=ADMIN_ID: return
    await q.message.edit_text("➕ Add one proxy:\n<code>/addproxy Asia|Thailand|1.2.3.4|8080|user|pass|HTTP|0.10|2026-09-19</code>\n\nOr upload CSV using /importcsv",reply_markup=back(),parse_mode="HTML"); await q.answer()

@dp.message(Command("addproxy"))
async def addproxy(m: Message):
    if not is_admin(m): return
    raw=m.text.partition(" ")[2]
    try:
        continent,country,host,port,user,password,ptype,price,expires=raw.split("|")
        c=db(); c.execute("""INSERT INTO proxies(continent,country,host,port,username,password,proxy_type,price,expires) VALUES(?,?,?,?,?,?,?,?,?)""",
                           (continent,country,host,int(port),user,password,ptype,float(price),expires)); c.commit(); c.close()
        await m.answer("✅ Proxy added.")
    except Exception:
        await m.answer("Format:\n/addproxy Asia|Thailand|1.2.3.4|8080|user|pass|HTTP|0.10|2026-09-19")

@dp.callback_query(F.data=="template")
async def template(q: CallbackQuery):
    if q.from_user.id!=ADMIN_ID: return
    data=b"continent,country,host,port,username,password,proxy_type,price,expires\nAsia,Thailand,1.2.3.4,8080,user,pass,HTTP,0.10,2026-09-19\n"
    await q.message.answer_document(BufferedInputFile(data,filename="proxy_template.csv")); await q.answer()

@dp.message(Command("importcsv"))
async def importcsv(m: Message):
    if not is_admin(m): return
    if not m.document:
        await m.answer("Reply to a CSV file with /importcsv, or send /importcsv as a caption.")
        return
    f=await bot.get_file(m.document.file_id)
    buf=io.BytesIO(); await bot.download_file(f.file_path,buf); buf.seek(0)
    text=buf.read().decode("utf-8-sig")
    reader=csv.DictReader(io.StringIO(text))
    required={"continent","country","host","port","username","password","proxy_type","price","expires"}
    if not required.issubset(set(reader.fieldnames or [])):
        await m.answer("❌ CSV columns are incorrect. Use /admin → CSV Template."); return
    c=db(); count=0
    for row in reader:
        try:
            c.execute("""INSERT INTO proxies(continent,country,host,port,username,password,proxy_type,price,expires) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (row["continent"],row["country"],row["host"],int(row["port"]),row["username"],row["password"],row["proxy_type"],float(row["price"]),row["expires"]))
            count+=1
        except Exception as e:
            logging.warning("CSV row skipped: %s",e)
    c.commit(); c.close()
    await m.answer(f"✅ Imported {count} proxies.")

@dp.callback_query(F.data=="adeposits")
async def adeposits(q: CallbackQuery):
    if q.from_user.id!=ADMIN_ID: return
    c=db(); rows=c.execute("SELECT id,tg_id,amount,txid,status,created_at FROM deposits ORDER BY id DESC LIMIT 20").fetchall(); c.close()
    text="💳 <b>Recent Deposits</b>\n\n"
    for r in rows: text+=f"#{r['id']} | {r['tg_id']} | {r['amount'] or 0:.6f} | {r['status']}\n<code>{r['txid']}</code>\n\n"
    await q.message.edit_text(text or "No deposits.",reply_markup=back(),parse_mode="HTML"); await q.answer()

@dp.message(Command("credit"))
async def credit(m: Message):
    if not is_admin(m): return
    try:
        uid,amount=m.text.split()[1:3]
        c=db(); c.execute("INSERT OR IGNORE INTO users(tg_id) VALUES(?)",(int(uid),))
        c.execute("UPDATE users SET balance=balance+? WHERE tg_id=?",(float(amount),int(uid))); c.commit(); c.close()
        await m.answer("✅ Balance credited.")
    except: await m.answer("Use: /credit TELEGRAM_ID AMOUNT")

async def main():
    if not BOT_TOKEN or not ADMIN_ID:
        raise RuntimeError("Set BOT_TOKEN and ADMIN_ID in .env")
    init_db()
    await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())
