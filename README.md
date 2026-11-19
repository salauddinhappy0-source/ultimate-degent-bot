# BAGENT Claim Telegram Bot Version 5

**Bot: Ultimate Crypto — @ultimate_dagent_bot** (user er pochondo, 2026-11-19)

@MR_COMPANY_2ND_BOT er moto bot — Binance Agentic Wallet connect kore
Flap er free BAGENT airdrop claim kore. Ekta value-0 transaction sign kore,
gas chara ar kono khoroch nai. Kono buy, swap, approval ba transfer kore na.

## Version 5 e ki notun (2026-11-20)

- **Claim preview er asol karon pawa gese:** `baw wallet address` Solana
  address prothome dey — bot oitai `--from` hishebe pathaito, tai BSC
  contract preview "Invalid contract-call parameters" dito. Ekhon bot
  shobshomoy **BSC (chain 56) EVM address** ney — claim preview ar status
  duitai thik.
- **Dashboard sajano:** Wallet (BNB/EVM address) → Agent (`salauddin7.ai`,
  age khali `salauddin7` dekhato) → BNB amount → BAGENT.
- **One-click copy:** address backtick er moddhe thake — ek click e copy
  hoy. Check status e **📋 Copy wallet address** button o ache.
- **Ekta dashboard:** /start, connect, Check status — sob ekta message
  ei update hoy; ager dashboard copy muche jay. Barbar connect korleo
  dashboard ar jome na. **/start dile ager dashboard muche giye niche
  notun kore ashe** (upore edit hoye thake na).
- **New QR button main keyboard theke sorano:** New QR ekhon shudhu QR
  photo er niche (ar expire notice e) thake — main keyboard e ar nai.

## Version 4 e ki notun (2026-11-20)

- **Auto-claim (watch):** Claim chaple round khola na thakle bot nijei
  watch shuru kore — proti 5 minute e preview check kore, round kholar
  sathe sathe nijei claim kore felbe (clean preview holei, value 0, kono
  approval/authority change chara). ⏹ Stop watching chaple bondho hobe.
  24 ghonta por nijei theme jabe, wallet disconnect holeo theme jabe.
- **Check status e claim dekha jay:** Check status e ekhon "Claim
  (BAGENT)" line thake — claiming / success / watching / not claimed.
  Nicher **🎁 BAGENT** button e click dilei claim er detail dekhabe:
  status, tx hash, BscScan link, BAGENT balance.
- **QR ar thake na:** Connect hole QR photo sathe sathe muche jay —
  age connect howar poreo QR theke jeto, oita thik kora hoise. Cancel,
  expire, notun QR — sob khetrei purono QR muche jay.
- **Claim preview fix:** vault address er checksum vul chilo bole
  preview "Invalid contract-call parameters" dito — thik kora hoise.

**Channel bot:** je keu use korte pare — prottek user er nijer alada
wallet session thake (`baw-sessions/<telegram-id>/`), tai ekjon er wallet
kokhono arekjon er sathe mishbe na. Prottek er claim tar nijer wallet ei jay.

## Bot ta ki kore

| Button | Kaj |
|---|---|
| 🔗 Connect Wallet | `baw auth signin` diye pairing code + QR dekhay — QR er niche button thake: Open Binance App / Check now / New QR / Cancel (MR_COMPANY_2ND_BOT er moto). Binance App diye scan kore Confirm korlei bot nije theke **auto-verify** kore "Wallet connected" janabe — `confirm` likhte hoy na (likhleo cholbe, fallback hishebe). |
| ✅ Check now (QR er niche) | Ekhoni ekbar verify check kore — App e Confirm kore thakle sathe sathe "Wallet connected" janabe. |
| 🔄 New QR (QR er niche) | Notun pairing code + QR dekhay — ager code expire hole ba harale eita chaplei hobe. Main keyboard e ei button ar nai. |
| ❌ Cancel (QR er niche) | Choloman sign-in batil kore — QR muche jay, kichu connect hoy na. |
| 🎁 Claim BAGENT | `claimAirdrop(0)` er preview dekhay (value 0, kono approval/authority change nai kina), Yes chaple execute kore, receipt + BAGENT balance janay. Round khola na thakle **auto-claim watch** shuru hoy — round kholar sathe sathe bot nijei claim kore felbe (⏹ Stop watching chaple bondho). |
| 📊 Check status | Wallet (BNB address, ek click e copy hoy), Agent ID (`.ai` soho), BNB + BAGENT balance, last claim tx, ar **Claim (BAGENT)** line. Chaplei sathe sathe "Checking…" dekhay, tarpor result ashe. Nicher **🎁 BAGENT** button e click dile claim er detail dekhabe (claiming / success, tx hash, BscScan link). **📋 Copy wallet address** button e click dile address alada kore code block e ashbe — ek tap e copy. |
| 🔌 Disconnect | `baw auth signout` — session shesh kore. |

## Asol trick ta ki chilo

Flap page er contract `0xf713d440d84bd149e8940c2ae3932db67524e43b` te
`claimAirdrop` call korle revert hoy. Kaj kore vault proxy
`0x80CABb78d05205Bb0f7C395ec6326Bf1890E7a00` te `claimAirdrop(0)` call
korle — eita on-chain promanito (tx
`0x97f4e7c244ccb930ff963d274d84e5c82a646c4169893c5f6f5480fde3ec9143`,
2026-11-19 19:30 +06, 6,614,574.88 BAGENT). Bot ei contract tai use kore.

## Setup (VPS / server lagbe — bot 24/7 cholte hoy)

### 1. Telegram bot banano (2 minute)

1. Telegram e `@BotFather` khulo → `/newbot` likho.
2. Bot er nam dao, tarpor username dao (sheshe `bot` thakte hobe).
3. BotFather je **token** dibe, oita copy kore rakho.
4. `ALLOWED_IDS` optional: khali rakhle sobai bot use korte parbe
   (channel bot). Sudhu nirdishto lok der jonno chaile
   `@userinfobot` ke `/start` diye tader Telegram user ID jene comma
   diye bosao, jemon `5169944492,6365388844`.

### 2. Server toiri (Ubuntu VPS)

```bash
# Node.js (baw er jonno) + Python
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs python3 python3-pip python3-venv git

# Binance Agentic Wallet CLI
npm install -g @binance/agentic-wallet@1.12.0
baw --version   # cholle thik ache
```

### 3. Bot er code bosano

```bash
git clone <tomar repo> bagent-telegram-bot   # ba ei folder ta server e copy koro
cd bagent-telegram-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env    # TELEGRAM_BOT_TOKEN ar ALLOWED_IDS bosao
```

### 4. Chalano

```bash
# test kore dekho (Ctrl+C diye thamabe)
python3 bot.py

# 24/7 chalate systemd service (recommended)
sudo nano /etc/systemd/system/bagent-bot.service
```

`bagent-bot.service` er content:

```ini
[Unit]
Description=BAGENT claim Telegram bot
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/bagent-telegram-bot
ExecStart=/home/ubuntu/bagent-telegram-bot/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bagent-bot
sudo systemctl status bagent-bot
sudo journalctl -u bagent-bot -f   # log dekhte
```

### 5. Binance App e ja korte hobe (proti wallet e ekbar)

1. Binance App → Agentic Wallet → Settings → **Developer Mode ON** koro
   (na hole claim preview cholbe na).
2. Wallet e gas er jonno ektu BNB rakho (0.0002 BNB er motoi enough).

## Kivabe use korbe (user hishebe)

1. Bot e `/start` → **Connect Wallet**.
2. QR ta Binance App diye scan koro, code miliye **Confirm** chap.
3. Bot nijei **auto-verify** kore "Wallet connected" janabe — kichu likhte
   hoy na. Deri mone hole QR er nicher **Check now** chapte paro.
4. **Claim BAGENT** chap → preview dekhabe → **Yes, claim** chap.
5. Tx hash + BAGENT amount janabe. Kaaj shesh hole **Disconnect** chap.

## Security — age poro, tarpor chalao

- **Wallet connect mane bot er kache wallet er FULL control.** Binance App
  nijeo ei warning dey. Tai:
  - Claim er jonno alada wallet use koro, jekhane sudhu gas er BNB ar
    claim er token thakbe — main NFT/wallet kokhono connect koro na.
  - Kaaj shesh holei **Disconnect** chap.
  - Bot kokhono seed phrase / private key chabe na. Keu chaile oita scam.
- **Ekta user = ekta wallet session.** Prottek Telegram user er baw
  session alada folder e thake (`baw-sessions/<telegram-id>/`), tai ekjon
  er wallet arekjon er claim e jay na. Kintu ekjoner Telegram account
  diye onno keu Connect chaple session bodle jabe — nijer account kauke
  dio na.
- **Redeploy korle session chole jay.** Railway redeploy / notun build
  hole `baw-sessions/` muche jay — tokhon sobai ke abar Connect Wallet
  korte hobe. Tai proyojon chara redeploy koro na.
- Session 48 ghonta por expire hoy — tokhon abar Connect Wallet korte hoy.
- Pairing code 5 minute e expire hoy — QR ashar sathe sathe scan korte hoy.
  Code expire hole bot nijei message diye janabe, tokhon abar Connect Wallet chapte hoy.
  Notun QR asle ager QR message auto-delete hoye jay — sobsomoy sobcheye
  nicher (latest) QR tai scan koro, uporer purono ta na. Connect hoye
  geleo QR muche jay.
- **Auto-verify:** QR scan + App e Confirm korlei bot nije theke janabe —
  `confirm` likhte hoy na. QR er niche **Check now** (ekhoni check),
  **New QR** (notun code) ar **Cancel** (batil) button thake.

## File gulo

- `bot.py` — pura bot (python-telegram-bot v21, async).
- `requirements.txt` — Python dependency.
- `.env.example` — config er nomuna (`.env` baniye token bosate hoy).
- `state.json` — bot nije banay (last claim tx, pending QR id). Git e diba na.
