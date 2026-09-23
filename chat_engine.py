#!/usr/bin/env python3
"""
XH Agents — AI Trading Assistant chat backend (pay-per-message, x402-native)
- POST /api/chat      : user sends message -> if balance>0 deduct 1, call NVIDIA NIM, return reply
- POST /api/chat/balance : check/top-up info for a user_id
- POST /api/chat/order    : create USDC top-up order (memo=user_id) -> invoice
- POST /api/chat/pay      : get USDC transfer payload for wallet signing
- POST /api/chat/verify-payment : verify on-chain USDC payment from wallet
- POST /api/chat/check-payments : scan treasury for top-up tx -> credit balance
- GET  /api/chat/health
- GET  /api/markets
Threaded server + CoinGecko cache (60s) + market data injected to AI prompt
Run: python3 chat_engine.py
"""
import json, sqlite3, os, sys, uuid, time, hashlib, threading, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import requests

PORT = 8001
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "chat.db")
PRPO_ENV = "/home/ubuntu/prpo_ai/.env"

# Shared, tested payment verification + idempotency (see /home/ubuntu/prpo_ai/xh_verify.py)
sys.path.insert(0, "/home/ubuntu/prpo_ai")
from xh_verify import find_incoming_usdc, find_incoming_solana_usdc, processed  # noqa: E402


def _shared_env(key, default=""):
    try:
        for line in open(PRPO_ENV):
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get(key, default)


LOOKBACK_BLOCKS = int(_shared_env("XH_LOOKBACK_BLOCKS", "7200"))
VERIFY_RATE_PER_HOUR = int(_shared_env("XH_CHAT_VERIFY_PER_HOUR", "30"))
_VERIFY_HITS = {}
_VERIFY_LOCK = threading.Lock()

TREASURY = {
    "solana": "GhFbGgNxERN6pQ7boSFLFJuPwXJuvJ8Tx7EgoJ9LV2Aw",
    "base":   "0x6cb53f00a586f7704e1f7121c2e397b579eb3ed0",
}
USDC_CONTRACTS = {
    "base":   "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}
MSG_PRICE_USDC = 0.1
MSG_PRICE_ATOMIC = int(round(MSG_PRICE_USDC * 1_000_000))  # 0.1 USDC = 100000 atomic

# x402 pay-per-message gate (agent-to-agent). Reuses the x402_lib invoice/
# settlement logic already validated on b0x402. Lives alongside the legacy
# balance/top-up flow so the browser UI (/trading/) is unchanged.
# If a request carries an X-Payment header (tx_hash + nonce), payment is
# verified on Base and the reply is served without spending wallet balance.
sys.path.insert(0, "/home/ubuntu/prpo_ai")  # expose x402_lib package
try:
    from x402_lib import get_manager, verify_usdc_settlement
    _X402_OK = True
except Exception as e:  # noqa: BLE001
    print("x402_lib import failed:", e, flush=True)
    _X402_OK = False
X402_PRICE_ATOMIC = int(MSG_PRICE_USDC * 1_000_000)  # 0.1 USDC
X402_CHAIN = 8453  # Base mainnet


def parse_x402_payment_header(value: str) -> dict:
    parts = {}
    for chunk in value.split(","):
        chunk = chunk.strip()
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()
    return parts

def load_env():
    env = {}
    try:
        for line in open(PRPO_ENV):
            line=line.strip()
            if line and not line.startswith("#") and "=" in line:
                k,v=line.split("=",1)
                env[k.strip()]=v.strip()
    except Exception:
        pass
    return env

ENV = load_env()
NVIDIA_API_KEY = ENV.get("NVIDIA_API_KEY","")
NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_MODEL = "nvidia/nemotron-3-super-120b-a12b"

COINGECKO_API_KEY = ENV.get("COINGECKO_API_KEY","")
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
WATCHLIST = [
    ("bitcoin","BTC/USDT","Ethereum"),
    ("ethereum","ETH/USDT","Ethereum"),
    ("solana","SOL/USDT","Solana"),
    ("pepe","PEPE/WETH","Ethereum"),
    ("dogecoin","DOGE/USDT","BSC"),
    ("arbitrum","ARB/USDT","Arbitrum"),
    ("bonk","BONK/USDC","Solana"),
    ("chainlink","LINK/USDT","Ethereum"),
]

# CoinGecko cache 60s to avoid rate limit + blocking
_cg_cache = {"data": None, "ts": 0, "lock": threading.Lock()}

def coingecko_markets(force=False):
    now = time.time()
    with _cg_cache["lock"]:
        if not force and _cg_cache["data"] and (now - _cg_cache["ts"] < 60):
            return _cg_cache["data"]
    ids = ",".join(c[0] for c in WATCHLIST)
    url = f"{COINGECKO_BASE}/coins/markets?vs_currency=usd&ids={ids}&order=market_cap_desc&per_page=50&page=1&price_change_percentage=24h"
    headers = {}
    if COINGECKO_API_KEY:
        headers["x_cg_demo_api_key"] = COINGECKO_API_KEY
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            # return cached if exists else error
            if _cg_cache["data"]:
                return _cg_cache["data"]
            return {"error": f"coingecko {r.status_code}", "retry_after": r.headers.get("Retry-After")}
        rows = r.json()
    except Exception as e:
        if _cg_cache["data"]:
            return _cg_cache["data"]
        return {"error": str(e)}
    by_id = {c[0]:(c[1],c[2]) for c in WATCHLIST}
    out=[]
    for m in rows:
        cid=m.get("id")
        if cid not in by_id: continue
        pair,net=by_id[cid]
        chg=m.get("price_change_percentage_24h") or 0
        price=m.get("current_price") or 0
        vol=m.get("total_volume") or 0
        if chg >=4 and vol >=5_000_000:
            sig,conf,reason="BUY",min(92,60+int(abs(chg)*2)),"Strong 24h momentum + volume"
        elif chg <=-4:
            sig,conf,reason="SELL",min(88,60+int(abs(chg)*2)),"24h downtrend — consider exit"
        else:
            sig,conf,reason="HOLD",55,"Consolidation — range bound"
        out.append({
            "pair":pair,"net":net,
            "price": f"${price:,.6f}".rstrip("0").rstrip(".") if price<1 else f"${price:,.2f}",
            "chg": f"{'+' if chg>=0 else ''}{chg:.2f}%",
            "vol": f"${vol/1e6:.1f}M",
            "sig":sig,"conf":conf,"reason":reason,
            "chgClass":"up" if chg>=0 else "down",
            "raw_price":price,"raw_chg":chg,
        })
    with _cg_cache["lock"]:
        _cg_cache["data"]=out
        _cg_cache["ts"]=now
    return out

def coingecko_brief():
    """Compact market snapshot string for AI system prompt"""
    data = coingecko_markets()
    if isinstance(data, dict) and data.get("error"):
        return "CoinGecko feed unavailable."
    lines=[]
    for m in data:
        lines.append(f"{m['pair']} {m['price']} {m['chg']} {m['sig']}({m['conf']}%)")
    return "Live CoinGecko (60s cache): " + " | ".join(lines)

def db():
    c=sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.execute("CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY, balance REAL DEFAULT 0, email TEXT, created INTEGER, wallet_address TEXT, network TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS msgs(id TEXT PRIMARY KEY, user_id TEXT, role TEXT, content TEXT, ts INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS topups(id TEXT PRIMARY KEY, user_id TEXT, network TEXT, amount REAL, status TEXT, ts INTEGER, wallet_address TEXT, tx_hash TEXT)")
    c.commit()
    return c

def gen_id():
    return "XHC"+hashlib.sha1(uuid.uuid4().bytes).hexdigest()[:10].upper()

def nim_reply(system_prompt, user_msg, history):
    """Ask NVIDIA NIM for a reply. Returns (text, ok).

    NIM returns 503 "Service temporarily overloaded" every few calls (measured
    ~1 in 5), so transient statuses are retried with a short backoff. `ok=False`
    tells the caller to NOT charge the user for a failed reply.
    """
    if not NVIDIA_API_KEY:
        return "[DEMO MODE] NVIDIA_API_KEY not configured. Placeholder reply to: " + user_msg[:80], False
    allowed = {"system", "user", "assistant", "tool", "function"}
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-6:]:
        role = str(h.get("role", "")).lower()
        if role not in allowed:
            role = "assistant" if role else "user"      # never send an invalid role
        messages.append({"role": role, "content": h.get("content", "")})
    messages.append({"role": "user", "content": user_msg})
    payload = {"model": NIM_MODEL, "messages": messages, "max_tokens": 500, "temperature": 0.7}
    retry_status = (429, 500, 502, 503, 504)
    last = ""
    for attempt in range(3):
        try:
            r = requests.post(NIM_URL,
                              headers={"Authorization": f"Bearer {NVIDIA_API_KEY}", "Content-Type": "application/json"},
                              json=payload, timeout=30)
        except requests.exceptions.Timeout:
            last = "timeout"
            time.sleep(0.8 * (attempt + 1))
            continue
        except Exception as e:  # noqa: BLE001
            last = f"error: {e}"
            time.sleep(0.8 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                txt = r.json()["choices"][0]["message"]["content"]
                return (txt or "").strip() or "(empty reply)", True
            except Exception as e:  # noqa: BLE001
                return f"[AI error] {e}", False
        last = f"NIM {r.status_code}"
        if r.status_code not in retry_status:
            break
        time.sleep(0.8 * (attempt + 1))
    return f"[AI temporarily unavailable ({last}). You were NOT charged — please retry.]", False


SOL_RPC="https://api.mainnet-beta.solana.com"
BASE_RPC="https://mainnet.base.org"

def verify_user_payment(user_id, wallet_address, network):
    """Credit a user for a USDC payment that really landed in the treasury.

    Fixed 2026-09-23 (was exploitable): the old code credited +1 message on every
    call for ANY >=0.1 USDC transfer out of the user's wallet — no recipient check
    on Solana ("derivation check skipped"), none on Base either, and no tx binding,
    so one transfer bought unlimited messages. Now a payment must
      (a) be a USDC transfer INTO the treasury,
      (b) come from the user's own wallet,
      (c) be claimed exactly once (xh_verify.processed).
    Credits = floor(value / price), so paying 0.3 buys 3 messages in one go.
    """
    price = MSG_PRICE_ATOMIC
    try:
        if network == "solana":
            if not wallet_address:
                return False
            hits = find_incoming_solana_usdc(TREASURY["solana"], from_wallet=wallet_address,
                                            min_atomic=price, limit=25)
        else:
            if not wallet_address or not wallet_address.startswith("0x"):
                return False
            hits = find_incoming_usdc(TREASURY["base"], from_address=wallet_address,
                                      min_atomic=price, lookback_blocks=LOOKBACK_BLOCKS)
        for h in hits:
            credits = max(1, int(h["value_atomic"]) // price)
            if not processed.claim(h["tx_hash"], "chat-topup", user_id, h["value_atomic"]):
                continue                      # already credited somewhere
            c = db()
            try:
                c.execute("INSERT OR IGNORE INTO users(user_id,created,wallet_address,network) VALUES(?,?,?,?)",
                          (user_id, int(time.time()), wallet_address, network))
                c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (credits, user_id))
                c.execute("INSERT INTO topups VALUES(?,?,?,?,?,?,?,?)",
                          (gen_id(), user_id, network, MSG_PRICE_USDC * credits, "paid",
                           int(time.time()), wallet_address, h["tx_hash"]))
                c.commit()
            finally:
                c.close()
            print(f"[topup] {user_id} +{credits} credit(s) tx={h['tx_hash']}", flush=True)
            return True
        return False
    except Exception as e:  # noqa: BLE001
        print("verify err", e)
        return False


def scan_topups():
    """Re-check pending top-up rows with the verified matcher (same helpers, so the
    treasury-recipient check and one-tx-one-credit rule apply here too)."""
    c = db()
    rows = c.execute("SELECT * FROM topups WHERE status='pending' ORDER BY ts ASC").fetchall()
    cols = ["id","user_id","network","amount","status","ts","wallet_address","tx_hash"]
    activated = []
    for row in rows:
        o = dict(zip(cols, row))
        wallet = o.get("wallet_address") or ""
        want = int(round(float(o["amount"]) * 1_000_000))
        try:
            if o["network"] == "solana":
                hits = find_incoming_solana_usdc(TREASURY["solana"], from_wallet=wallet or None,
                                                 min_atomic=want, limit=25)
            else:
                hits = find_incoming_usdc(TREASURY["base"], from_address=wallet or None,
                                          min_atomic=want, lookback_blocks=LOOKBACK_BLOCKS)
        except Exception as e:  # noqa: BLE001
            print("scan err", e)
            continue
        for h in hits:
            if not processed.claim(h["tx_hash"], "chat-topup", o["user_id"], h["value_atomic"]):
                continue
            credits = max(1, int(h["value_atomic"]) // MSG_PRICE_ATOMIC)
            c.execute("UPDATE topups SET status='paid', tx_hash=? WHERE id=?", (h["tx_hash"], o["id"]))
            c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (credits, o["user_id"]))
            activated.append(o["id"])
            break
    c.commit(); c.close()
    return activated


class H(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"
    def _j(self,obj,code=200):
        body=json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers()
    def do_GET(self):
        p=urlparse(self.path)
        if p.path=="/api/chat/health":
            return self._j({"ok":True,"nim":bool(NVIDIA_API_KEY),"price":MSG_PRICE_USDC,"coingecko":bool(COINGECKO_API_KEY), "markets": len(coingecko_markets()) if isinstance(coingecko_markets(), list) else 0})
        if p.path=="/api/markets":
            data=coingecko_markets()
            if isinstance(data, dict) and data.get("error"):
                return self._j(data,502)
            return self._j({"ok":True,"source":"coingecko","markets":data})
        return self._j({"error":"not found"},404)
    def do_POST(self):
        p=urlparse(self.path)
        L=int(self.headers.get("Content-Length",0))
        body=self.rfile.read(L)
        try: data=json.loads(body or b"{}")
        except: return self._j({"error":"bad json"},400)
        if p.path=="/api/chat": return self._chat(data)
        if p.path=="/api/chat/invoice": return self._invoice(data)
        if p.path=="/api/chat/balance": return self._balance(data)
        if p.path=="/api/chat/order": return self._order(data)
        if p.path=="/api/chat/pay": return self._pay(data)
        if p.path=="/api/chat/verify-payment": return self._verify(data)
        if p.path=="/api/chat/check-payments": return self._j({"credited":scan_topups()})
        return self._j({"error":"not found"},404)
    def _balance(self,d):
        uid=d.get("user_id",""); wallet_addr=d.get("wallet_address",""); net=d.get("network","")
        if not uid: return self._j({"error":"user_id required"},400)
        c=db()
        try:
            c.execute("INSERT OR IGNORE INTO users(user_id,created,wallet_address,network) VALUES(?,?,?,?)",(uid,int(time.time()),wallet_addr,net))
            c.commit()
            bal=c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()
            bal=bal[0] if bal else 0
        finally:
            c.close()
        return self._j({"user_id":uid,"balance":bal,"price_per_msg":MSG_PRICE_USDC})
    def _order(self,d):
        uid=d.get("user_id",""); net=d.get("network","")
        if not uid: return self._j({"error":"user_id required"},400)
        if net not in TREASURY: return self._j({"error":"bad network"},400)
        amt=float(d.get("amount",1))
        oid=gen_id()
        c=db()
        try:
            c.execute("INSERT OR IGNORE INTO users(user_id,created,wallet_address,network) VALUES(?,?,?,?)",(uid,int(time.time()),"",net))
            c.execute("INSERT INTO topups VALUES(?,?,?,?,?,?,?,?)",(oid,uid,net,amt,"pending",int(time.time()),"", ""))
            c.commit()
        finally:
            c.close()
        return self._j({"order_id":oid,"network":net,"amount_usdc":amt,"treasury":TREASURY[net],"memo":oid,"credits":amt/MSG_PRICE_USDC, "note":f"Send {amt} USDC to treasury with memo {oid}. You get {amt/MSG_PRICE_USDC:.0f} messages."})
    def _pay(self,d):
        uid=d.get("user_id",""); wallet_addr=d.get("wallet_address",""); net=d.get("network","")
        if not uid or not wallet_addr or not net:
            return self._j({"error":"user_id, wallet_address, network required"},400)
        if net not in TREASURY: return self._j({"error":"bad network"},400)
        amount=d.get("amount", MSG_PRICE_USDC)
        c=db()
        try:
            c.execute("INSERT OR IGNORE INTO users(user_id,created,wallet_address,network) VALUES(?,?,?,?)",(uid,int(time.time()),wallet_addr,net))
            c.commit()
        finally:
            c.close()
        if net=="base":
            return self._j({"requires_wallet_signature":True,"network":"base","usdc_contract":USDC_CONTRACTS["base"],"to_address":TREASURY["base"],"amount_base":int(amount*1_000_000),"message":"Sign & send this USDC transfer via MetaMask"})
        else:
            return self._j({"requires_wallet_signature":True,"network":"solana","usdc_mint":USDC_CONTRACTS["solana"],"to_address":TREASURY["solana"],"amount_lamports":int(amount*1_000_000),"message":"Sign & send this USDC transfer via Phantom"})
    def _verify(self,d):
        uid=d.get("user_id",""); wallet_addr=d.get("wallet_address",""); net=d.get("network","")
        if not uid or not wallet_addr or not net:
            return self._j({"error":"user_id, wallet_address, network required"},400)
        ip = self.client_address[0] if self.client_address else "?"
        now = time.time()
        with _VERIFY_LOCK:
            key = f"{uid}:{ip}"
            hits = [t for t in _VERIFY_HITS.get(key, []) if now - t < 3600]
            if len(hits) >= VERIFY_RATE_PER_HOUR:
                _VERIFY_HITS[key] = hits
                return self._j({"error":"rate_limited","message":"too many verify attempts — wait a bit"},429)
            hits.append(now); _VERIFY_HITS[key] = hits
        paid=verify_user_payment(uid,wallet_addr,net)
        if paid:
            c=db()
            try:
                bal=c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()
                bal=bal[0] if bal else 0
            finally:
                c.close()
            return self._j({"ok":True,"credited":True,"balance":bal})
        else:
            c=db()
            try:
                b=c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()
                bal=b[0] if b else 0
            finally:
                c.close()
            return self._j({"ok":True,"credited":False,"balance":bal,
                            "message":"No NEW payment found. Send USDC to the treasury from this wallet, then check again."},402)
    def _invoice(self,d):
        # Issue an x402 invoice (pay-per-message, agent-to-agent).
        if not _X402_OK:
            return self._j({"error":"x402 not configured"},503)
        try:
            inv = get_manager().issue(
                seller_address=TREASURY["base"],
                amount_atomic=X402_PRICE_ATOMIC,
                token=USDC_CONTRACTS["base"],
                chain_id=X402_CHAIN,
                endpoint="/api/chat",
            )
        except Exception as e:
            return self._j({"error":f"issue failed: {e}"},502)
        return self._j({
            "error":"payment_required",
            "message":f"x402 payment required. Transfer {MSG_PRICE_USDC} USDC on Base to treasury then retry with X-Payment header.",
            "nonce":inv.nonce,
            "payout_address":inv.address,
            "chain_id":inv.chain_id,
            "token":inv.token,
            "amount_atomic":inv.amount_atomic,
            "amount_usdc":MSG_PRICE_USDC,
            "expires_at":int(inv.expires_at),
            "how_to_pay":(
                f"Transfer >= {MSG_PRICE_USDC} USDC to {inv.address} on Base "
                f"(chain 8453), then re-request /api/chat with header "
                f"X-Payment: tx_hash=<tx_hash>,nonce={inv.nonce}"
            ),
        }, 402)

    def _chat(self,d):
        uid=d.get("user_id","anon")
        msg=(d.get("message") or "").strip()
        if not msg: return self._j({"error":"message required"},400)
        # ── x402 pay-per-message gate ──────────────────────────────────
        # If the client sends an X-Payment header, verify the USDC transfer
        # and serve the reply WITHOUT spending wallet balance (agent-to-agent).
        # Reads both official spelling and legacy b0x402 header.
        payment_hdr = self.headers.get("x-payment") or self.headers.get("payment-signature")
        if payment_hdr:
            parsed = parse_x402_payment_header(payment_hdr)
            tx_hash = parsed.get("tx_hash",""); nonce=parsed.get("nonce","")
            if not tx_hash or not nonce:
                return self._j({"error":"bad_request","message":"X-Payment requires tx_hash and nonce"},400)
            if not _X402_OK:
                return self._j({"error":"x402 not configured"},503)
            inv = get_manager().get(nonce)
            if inv is None:
                return self._j({"error":"bad_request","message":"nonce not found or already used"},400)
            ok, reason = verify_usdc_settlement(
                tx_hash=tx_hash,
                expected_recipient=inv.address,
                expected_amount_atomic=inv.amount_atomic,
                expected_token=inv.token,
            )
            if not ok:
                return self._j({"error":"payment_required","message":f"payment failed: {reason}"},402)
            ok2, reason2, _pop = get_manager().verify_and_pop(nonce, tx_hash)
            if not ok2:
                return self._j({"error":"payment_required","message":f"nonce already used: {reason2}"},402)
            # Paid via x402 — skip balance gate below.
            x402_paid=True
        else:
            x402_paid=False
        pair=d.get("pair","SOL/USDT"); net=d.get("network","Solana")
        strat=d.get("strategy","Momentum Scalp"); sl=d.get("sl","3"); tp=d.get("tp","6"); size=d.get("size","50")
        wallet_addr=d.get("wallet_address","")
        c=db()
        try:
            c.execute("INSERT OR IGNORE INTO users(user_id,created,wallet_address,network) VALUES(?,?,?,?)",(uid,int(time.time()),wallet_addr,net))
            bal=c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()
            bal=bal[0] if bal else 0
            if wallet_addr:
                # quick verify without blocking too long - use cached check, real verify happens via /verify-payment
                # but also try fast path if balance zero
                pass
            if (not x402_paid) and bal <1:
                c.close()
                return self._j({"error":"insufficient_balance","balance":bal,"message":"Pay $0.1 per message. Connect wallet & pay via /api/chat/pay (USDC to treasury).","topup_url":"/trading/?topup=1"})
            # NOTE: the credit is NOT spent here. It is spent only after the model
            # answers, so a failed AI call never costs the user a message.
            mid=gen_id()
            c.execute("INSERT INTO msgs VALUES(?,?,?,?,?)",(mid,uid,"user",msg,int(time.time())))
            # The DB stores our own roles ("user"/"agent"); the chat API only accepts
            # system/user/assistant/tool/function. Sending "agent" made EVERY follow-up
            # message fail with 400 unknown variant, i.e. the assistant only ever
            # answered the first message of a conversation.
            _role_map={"agent":"assistant","assistant":"assistant","user":"user","system":"system"}
            history=[]
            for _m in c.execute("SELECT role,content FROM msgs WHERE user_id=? ORDER BY ts",(uid,)).fetchall():
                _r=_role_map.get(str(_m[0]),"assistant")
                history.append({"role":_r,"content":_m[1]})
            c.commit()
        finally:
            try: c.close()
            except: pass
        # inject live CoinGecko snapshot into system prompt
        market_brief=coingecko_brief()
        system_prompt=f"""You are XH Agent — a professional crypto/DeFi trading AI assistant on XH Agents.
You monitor DEX data (DexScreener-style). Current config: Pair={pair}, Network={net}, Strategy={strat}, Size=${size}USDT, SL={sl}%, TP={tp}%.
LIVE MARKET (CoinGecko, update 60s): {market_brief}
User wallet: {wallet_addr or 'not connected'}.
RULES: Reply DIRECTLY to the user. Do NOT show your reasoning, planning, or internal thoughts. Do NOT count words out loud. Just give the answer.
Respond concisely (under 140 words), professional trading language, mix English/Bahasa Indonesia if fitting.
Give actionable insights: entry/exit, risk, liquidity, rug signals. Use 🟢BUY 🔴SELL 🟡HOLD sparingly. Cite CoinGecko prices when relevant.
DISCLAIMER: not financial advice."""
        reply, ok = nim_reply(system_prompt, msg, history)
        c=db()
        try:
            if ok and not x402_paid:
                # charge exactly one credit, now that the user has a real answer
                c.execute("UPDATE users SET balance=balance-1 WHERE user_id=? AND balance>=1",(uid,))
                new_bal = max(0.0, float(bal) - 1)
            c.execute("INSERT INTO msgs VALUES(?,?,?,?,?)",(gen_id(),uid,"agent",reply,int(time.time())))
            c.commit()
        finally:
            c.close()
        if not ok:
            print(f"[chat] AI failure for {uid} — not charged ({reply[:60]})", flush=True)
        # Report the balance straight from the database: one source of truth, so a
        # failed call can never report a balance that was never charged.
        c=db()
        try:
            row=c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()
            final_bal=float(row[0]) if row else 0.0
        finally:
            c.close()
        return self._j({"reply":reply,"balance":final_bal,"charged":bool(ok and not x402_paid),
                        "price_per_msg":MSG_PRICE_USDC})
    def log_message(self,*a): pass

if __name__=="__main__":
    db()
    print(f"Chat engine on {PORT} NIM:{bool(NVIDIA_API_KEY)} CG:{bool(COINGECKO_API_KEY)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0",PORT), H).serve_forever()
