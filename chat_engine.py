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
Stdlib + requests. Run: python3 chat_engine.py
"""
import json, sqlite3, os, uuid, time, hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import requests

PORT = 8001
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "chat.db")
PRPO_ENV = "/home/ubuntu/prpo_ai/.env"

# Treasury (same as ad engine)
TREASURY = {
    "solana": "GhFbGgNxERN6pQ7boSFLFJuPwXJuvJ8Tx7EgoJ9LV2Aw",
    "base":   "0x57eec52d76a4a78d4562fc2564101a4bd2e3f357",
}
# USDC contracts
USDC_CONTRACTS = {
    "base":   "0x833dBe171C8B6D5C6B6B9d225B2f7Cf6b5a9e2bA",  # Base USDC
    "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # Solana USDC
}
MSG_PRICE_USDC = 0.1  # $0.1 per message

def load_env():
    env = {}
    try:
        for line in open(PRPO_ENV):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except Exception:
        pass
    return env

ENV = load_env()
NVIDIA_API_KEY = ENV.get("NVIDIA_API_KEY", "")
NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_MODEL = "nvidia/nemotron-3-super-120b-a12b"

# CoinGecko (live market data for signals + stats)
COINGECKO_API_KEY = ENV.get("COINGECKO_API_KEY", "")
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
# curated watchlist -> (coingecko id, friendly pair, default network)
WATCHLIST = [
    ("bitcoin", "BTC/USDT", "Ethereum"),
    ("ethereum", "ETH/USDT", "Ethereum"),
    ("solana", "SOL/USDT", "Solana"),
    ("pepe", "PEPE/WETH", "Ethereum"),
    ("dogecoin", "DOGE/USDT", "BSC"),
    ("arbitrum", "ARB/USDT", "Arbitrum"),
    ("bonk", "BONK/USDC", "Solana"),
    ("chainlink", "LINK/USDT", "Ethereum"),
]
# network -> chain id used by CoinGecko (for reference; ids above already pick the right chain)
CG_NETWORK = {"Solana": "solana", "Ethereum": "ethereum", "BSC": "binance-smart-chain",
              "Base": "base", "Arbitrum": "arbitrum-one", "Polygon": "polygon-pos"}

def coingecko_markets():
    """Fetch live markets from CoinGecko using the server-side demo key.
    Returns a list of dicts with pair/net/price/chg/vol/sig/conf/reason."""
    ids = ",".join(c[0] for c in WATCHLIST)
    url = (f"{COINGECKO_BASE}/coins/markets?vs_currency=usd&ids={ids}"
           f"&order=market_cap_desc&per_page=50&page=1&price_change_percentage=24h")
    headers = {}
    if COINGECKO_API_KEY:
        headers["x_cg_demo_api_key"] = COINGECKO_API_KEY
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return {"error": f"coingecko {r.status_code}", "retry_after": r.headers.get("Retry-After")}
        rows = r.json()
    except Exception as e:
        return {"error": str(e)}
    by_id = {c[0]: (c[1], c[2]) for c in WATCHLIST}
    out = []
    for m in rows:
        cid = m.get("id")
        if cid not in by_id:
            continue
        pair, net = by_id[cid]
        chg = m.get("price_change_percentage_24h") or 0
        price = m.get("current_price") or 0
        vol = m.get("total_volume") or 0
        # simple heuristic signal
        if chg >= 4 and vol >= 5_000_000:
            sig, conf, reason = "BUY", min(92, 60 + int(abs(chg) * 2)), "Strong 24h momentum + volume"
        elif chg <= -4:
            sig, conf, reason = "SELL", min(88, 60 + int(abs(chg) * 2)), "24h downtrend — consider exit"
        else:
            sig, conf, reason = "HOLD", 55, "Consolidation — range bound"
        out.append({
            "pair": pair, "net": net,
            "price": f"${price:,.6f}".rstrip("0").rstrip(".") if price < 1 else f"${price:,.2f}",
            "chg": f"{'+' if chg>=0 else ''}{chg:.2f}%",
            "vol": f"${vol/1e6:.1f}M",
            "sig": sig, "conf": conf, "reason": reason,
            "chgClass": "up" if chg >= 0 else "down",
            "raw_price": price, "raw_chg": chg,
        })
    return out

def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id TEXT PRIMARY KEY, balance REAL DEFAULT 0, email TEXT, created INTEGER,
            wallet_address TEXT, network TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS msgs(
        id TEXT PRIMARY KEY, user_id TEXT, role TEXT, content TEXT, ts INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS topups(
        id TEXT PRIMARY KEY, user_id TEXT, network TEXT, amount REAL, status TEXT, ts INTEGER,
        wallet_address TEXT, tx_hash TEXT)""")
    c.commit(); return c

def gen_id():
    return "XHC" + hashlib.sha1(uuid.uuid4().bytes).hexdigest()[:10].upper()

# ---------- NVIDIA NIM call ----------
def nim_reply(system_prompt, user_msg, history):
    if not NVIDIA_API_KEY:
        return "[DEMO MODE] NVIDIA_API_KEY not configured. This is a placeholder reply to: " + user_msg[:80]
    try:
        messages = [{"role":"system","content":system_prompt}]
        for h in history[-6:]:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role":"user","content":user_msg})
        r = requests.post(NIM_URL, headers={
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json"},
            json={"model": NIM_MODEL, "messages": messages, "max_tokens": 400, "temperature": 0.7},
            timeout=45)
        if r.status_code != 200:
            return f"[AI temporarily unavailable (NIM {r.status_code}). The model may need activation in NVIDIA NGC console, or try again shortly.]"
        j = r.json()
        return j["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        return "[AI engine timeout — NVIDIA NIM model is warming up. Please retry in a moment.]"
    except Exception as e:
        return f"[AI error] {e}"

# ---------- on-chain payment verification ----------
SOL_RPC = "https://api.mainnet-beta.solana.com"
BASE_RPC = "https://mainnet.base.org"

# Scan treasury for USDC transfer from specific wallet (for verify-payment)
def verify_user_payment(user_id, wallet_address, network):
    c = db()
    # check if user already has credited balance > 0
    bal = c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    if bal and bal[0] >= 1:
        c.close()
        return True
    
    # scan on-chain for USDC transfer from wallet_address to treasury
    paid = False
    try:
        if network == "solana":
            # get signatures for treasury, check memo or direct transfer
            # For USDC SPL, check token accounts... simpler: check if user wallet sent USDC to treasury
            # We'll check treasury SPL token account balance change or use getSignaturesForAddress on user wallet
            r = requests.post(SOL_RPC, json={"jsonrpc":"2.0","id":1,
                "method":"getSignaturesForAddress","params":[wallet_address,{"limit":20}]}, timeout=10).json()
            for s in (r.get("result") or [])[:20]:
                sig = s.get("signature")
                if not sig: continue
                tx = requests.post(SOL_RPC, json={"jsonrpc":"2.0","id":1,
                    "method":"getTransaction","params":[sig,{"encoding":"jsonParsed"}]}, timeout=10).json().get("result")
                if not tx: continue
                # check SPL token transfer instruction
                for ix in tx.get("transaction",{}).get("message",{}).get("instructions",[]):
                    if ix.get("program") == "spl-token":
                        info = ix.get("parsed",{}).get("info",{})
                        if info.get("destination") == TREASURY["solana"]:
                            # check amount
                            amt = int(info.get("amount","0"))
                            if amt >= int(MSG_PRICE_USDC * 1_000_000):  # 0.1 USDC = 100k base units
                                paid = True; break
                    elif ix.get("program") == "spl-memo":
                        # fallback: memo contains user_id
                        memo = ix.get("parsed",{}).get("info",{}).get("memo","")
                        if user_id in memo:
                            paid = True; break
                if paid: break
        else:
            # Base: scan Transfer events from user wallet to treasury
            blk = int(requests.post(BASE_RPC, json={"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}, timeout=10).json().get("result","0x0"), 16)
            fromb = max(0, blk-5000)
            logs = requests.post(BASE_RPC, json={"jsonrpc":"2.0","id":1,"method":"eth_getLogs",
                "params":[{"fromBlock":hex(fromb),"toBlock":"latest","address":USDC_CONTRACTS["base"],
                "topics":["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                         "0x"+wallet_address.lower().zfill(64)]}]}, timeout=15).json().get("result",[])
            for lg in (logs or []):
                data = lg.get("data","") or ""
                try:
                    val = int(data,16)/1e6 if data else 0
                except:
                    val = 0
                if val >= MSG_PRICE_USDC * 0.99:
                    paid = True; break
    except Exception as e:
        print("verify err", e)
    
    if paid:
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (MSG_PRICE_USDC/MSG_PRICE_USDC, user_id))
        c.execute("INSERT INTO topups VALUES(?,?,?,?,?,?,?,?)",
                  (gen_id(), user_id, network, MSG_PRICE_USDC, "paid", int(time.time()), wallet_address, ""))
        c.commit()
    c.close()
    return paid

# ---------- legacy scan_topups (for backward compat with cron) ----------
def scan_topups():
    c = db()
    rows = c.execute("SELECT * FROM topups WHERE status='pending'").fetchall()
    cols = ["id","user_id","network","amount","status","ts","wallet_address","tx_hash"]
    activated = []
    for row in rows:
        o = dict(zip(cols, row))
        paid = False
        try:
            if o["network"] == "solana":
                r = requests.post(SOL_RPC, json={"jsonrpc":"2.0","id":1,
                    "method":"getSignaturesForAddress","params":[TREASURY["solana"],{"limit":15}]}, timeout=8).json()
                for s in (r.get("result") or [])[:15]:
                    sig = s.get("signature")
                    if not sig: continue
                    tx = requests.post(SOL_RPC, json={"jsonrpc":"2.0","id":1,
                        "method":"getTransaction","params":[sig,{"encoding":"jsonParsed"}]}, timeout=8).json().get("result")
                    if not tx: continue
                    for ix in tx.get("transaction",{}).get("message",{}).get("instructions",[]):
                        memo = ix.get("parsed",{}).get("info",{}).get("memo","") if ix.get("program")=="spl-memo" else ""
                        if o["id"] in memo:
                            paid = True; break
                    if paid: break
            else:
                blk = int(requests.post(BASE_RPC, json={"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}, timeout=8).json().get("result","0x0"), 16)
                fromb = max(0, blk-2000)
                logs = requests.post(BASE_RPC, json={"jsonrpc":"2.0","id":1,"method":"eth_getLogs",
                    "params":[{"fromBlock":hex(fromb),"toBlock":"latest","address":TREASURY["base"],
                    "topics":["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"]}]}, timeout=10).json().get("result",[])
                for lg in (logs or []):
                    data = lg.get("data","") or ""
                    try: val = int(data[-64:],16)/1e6 if len(data)>=64 else 0
                    except: val = 0
                    if val >= o["amount"]*0.99:
                        paid = True; break
        except Exception as e:
            print("scan err", e)
        if paid:
            c.execute("UPDATE topups SET status='paid' WHERE id=?", (o["id"],))
            c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (o["amount"]/MSG_PRICE_USDC, o["user_id"]))
            activated.append(o["id"])
    c.commit(); c.close()
    return activated

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def _j(self, obj, code=200):
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*"); self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/api/chat/health":
            return self._j({"ok":True, "nim": bool(NVIDIA_API_KEY), "price": MSG_PRICE_USDC, "coingecko": bool(COINGECKO_API_KEY)})
        if p.path == "/api/markets":
            data = coingecko_markets()
            if isinstance(data, dict) and data.get("error"):
                return self._j(data, 502)
            return self._j({"ok":True, "source":"coingecko", "markets": data})
        return self._j({"error":"not found"},404)

    def do_POST(self):
        p = urlparse(self.path)
        L = int(self.headers.get("Content-Length",0)); body = self.rfile.read(L)
        try: data = json.loads(body or b"{}")
        except: return self._j({"error":"bad json"},400)
        if p.path == "/api/chat": return self._chat(data)
        if p.path == "/api/chat/balance": return self._balance(data)
        if p.path == "/api/chat/order": return self._order(data)
        if p.path == "/api/chat/pay": return self._pay(data)
        if p.path == "/api/chat/verify-payment": return self._verify(data)
        if p.path == "/api/chat/check-payments": return self._j({"credited": scan_topups()})
        return self._j({"error":"not found"},404)

    def _balance(self, d):
        uid = d.get("user_id",""); wallet_addr = d.get("wallet_address",""); net = d.get("network","")
        if not uid: return self._j({"error":"user_id required"},400)
        c = db(); c.execute("INSERT OR IGNORE INTO users(user_id,created,wallet_address,network) VALUES(?,?,?,?)",(uid,int(time.time()),wallet_addr,net)); c.commit(); c.close()
        bal = c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()[0]; c.close()
        return self._j({"user_id":uid,"balance":bal,"price_per_msg":MSG_PRICE_USDC})

    def _order(self, d):
        uid = d.get("user_id",""); net = d.get("network","")
        if not uid: return self._j({"error":"user_id required"},400)
        if net not in TREASURY: return self._j({"error":"bad network"},400)
        amt = float(d.get("amount",1))
        oid = gen_id()
        c = db(); c.execute("INSERT OR IGNORE INTO users(user_id,created,wallet_address,network) VALUES(?,?,?,?)",(uid,int(time.time()),"",net)); c.execute("INSERT INTO topups VALUES(?,?,?,?,?,?)",(oid,uid,net,amt,"pending",int(time.time()))); c.commit(); c.close()
        return self._j({"order_id":oid,"network":net,"amount_usdc":amt,
            "treasury":TREASURY[net],"memo":oid,
            "credits":amt/MSG_PRICE_USDC,
            "note":f"Send {amt} USDC to treasury with memo {oid}. You get {amt/MSG_PRICE_USDC:.0f} messages."})

    def _pay(self, d):
        """Return treasury details for wallet to sign & broadcast USDC transfer"""
        uid = d.get("user_id",""); wallet_addr = d.get("wallet_address",""); net = d.get("network","")
        if not uid or not wallet_addr or not net:
            return self._j({"error":"user_id, wallet_address, network required"},400)
        if net not in TREASURY:
            return self._j({"error":"bad network"},400)
        
        amount = d.get("amount", MSG_PRICE_USDC)
        c = db(); c.execute("INSERT OR IGNORE INTO users(user_id,created,wallet_address,network) VALUES(?,?,?,?)",
                          (uid,int(time.time()),wallet_addr,net)); c.commit(); c.close()
        
        if net == "base":
            # EVM: return USDC contract, treasury, amount in base units
            return self._j({
                "requires_wallet_signature": True,
                "network": "base",
                "usdc_contract": USDC_CONTRACTS["base"],
                "to_address": TREASURY["base"],
                "amount_base": int(amount * 1_000_000),  # 6 decimals
                "message": "Sign & send this USDC transfer via MetaMask"
            })
        else:
            # Solana: return treasury address, amount in lamports
            return self._j({
                "requires_wallet_signature": True,
                "network": "solana",
                "usdc_mint": USDC_CONTRACTS["solana"],
                "to_address": TREASURY["solana"],
                "amount_lamports": int(amount * 1_000_000),
                "message": "Sign & send this USDC transfer via Phantom"
            })

    def _verify(self, d):
        """Verify on-chain USDC payment from user wallet"""
        uid = d.get("user_id",""); wallet_addr = d.get("wallet_address",""); net = d.get("network","")
        if not uid or not wallet_addr or not net:
            return self._j({"error":"user_id, wallet_address, network required"},400)
        paid = verify_user_payment(uid, wallet_addr, net)
        if paid:
            c = db(); bal = c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()[0]; c.close()
            return self._j({"ok":True,"credited":True,"balance":bal})
        else:
            return self._j({"ok":True,"credited":False,"message":"Payment not yet detected on-chain"}, 402)

    def _chat(self, d):
        uid = d.get("user_id","anon")
        msg = (d.get("message") or "").strip()
        if not msg: return self._j({"error":"message required"},400)
        pair = d.get("pair","SOL/USDT"); net = d.get("network","Solana")
        strat = d.get("strategy","Momentum Scalp"); sl = d.get("sl","3"); tp = d.get("tp","6"); size = d.get("size","50")
        wallet_addr = d.get("wallet_address","")
        c = db(); c.execute("INSERT OR IGNORE INTO users(user_id,created,wallet_address,network) VALUES(?,?,?,?)",(uid,int(time.time()),wallet_addr,net))
        bal = c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()[0]
        # If wallet provided, verify on-chain payment
        if wallet_addr:
            paid = verify_user_payment(uid, wallet_addr, net.lower())
            if paid:
                bal = c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()[0]
        if bal < 1:
            c.close()
            return self._j({"error":"insufficient_balance","balance":bal,
                "message":"Pay $0.1 per message. Connect wallet & pay via /api/chat/pay (USDC to treasury).",
                "topup_url": f"/trading/?topup=1"})
        c.execute("UPDATE users SET balance=balance-1 WHERE user_id=?",(uid,))
        # save user msg
        mid = gen_id()
        c.execute("INSERT INTO msgs VALUES(?,?,?,?,?)",(mid,uid,"user",msg,int(time.time())))
        history = [{"role":m[0],"content":m[1]} for m in c.execute("SELECT role,content FROM msgs WHERE user_id=? ORDER BY ts",(uid,)).fetchall()]
        c.commit(); c.close()
        system_prompt = f"""You are XH Agent — a professional crypto/DeFi trading AI assistant on XH Agents.
You monitor DEX data (DexScreener-style). Current config: Pair={pair}, Network={net}, Strategy={strat}, Size=${size}USDT, SL={sl}%, TP={tp}%.
RULES: Reply DIRECTLY to the user. Do NOT show your reasoning, planning, or internal thoughts. Do NOT count words out loud. Just give the answer.
Respond concisely (under 120 words), professional trading language, mix English/Bahasa Indonesia if fitting.
Give actionable insights: entry/exit, risk, liquidity, rug signals. Use 🟢BUY 🔴SELL 🟡HOLD sparingly.
DISCLAIMER: not financial advice."""
        reply = nim_reply(system_prompt, msg, history)
        # save agent reply
        c = db(); c.execute("INSERT INTO msgs VALUES(?,?,?,?,?)",(gen_id(),uid,"agent",reply,int(time.time()))); c.commit(); c.close()
        return self._j({"reply": reply, "balance": bal-1, "price_per_msg": MSG_PRICE_USDC})

    def log_message(self, *a): pass

if __name__ == "__main__":
    db()
    print("Chat engine on", PORT, "NIM:", bool(NVIDIA_API_KEY))
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()