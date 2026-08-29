#!/usr/bin/env python3
"""
XH Agents — AI Trading Assistant chat backend (pay-per-message, x402-native)
- POST /api/chat      : user sends message -> if balance>0 deduct 1, call NVIDIA NIM, return reply
- POST /api/chat/balance : check/top-up info for a user_id
- POST /api/chat/order    : create USDC top-up order (memo=user_id) -> invoice
- POST /api/chat/check-payments : scan treasury for top-up tx -> credit balance
- GET  /api/chat/health
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
NIM_MODEL = "mistralai/mistral-7b-instruct-v0.3"  # free NIM; activate in NGC console if 404

def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id TEXT PRIMARY KEY, balance REAL DEFAULT 0, email TEXT, created INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS msgs(
        id TEXT PRIMARY KEY, user_id TEXT, role TEXT, content TEXT, ts INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS topups(
        id TEXT PRIMARY KEY, user_id TEXT, network TEXT, amount REAL, status TEXT, ts INTEGER)""")
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

# ---------- payment detection (reuse simple scan) ----------
SOL_RPC = "https://api.mainnet-beta.solana.com"
BASE_RPC = "https://mainnet.base.org"

def scan_topups():
    c = db()
    rows = c.execute("SELECT * FROM topups WHERE status='pending'").fetchall()
    cols = ["id","user_id","network","amount","status","ts"]
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
                blk = int(requests.post(BASE_RPC, json={"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}, timeout=8).json().get("result", "0x0"), 16)
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
            return self._j({"ok":True, "nim": bool(NVIDIA_API_KEY), "price": MSG_PRICE_USDC})
        return self._j({"error":"not found"},404)

    def do_POST(self):
        p = urlparse(self.path)
        L = int(self.headers.get("Content-Length",0)); body = self.rfile.read(L)
        try: data = json.loads(body or b"{}")
        except: return self._j({"error":"bad json"},400)
        if p.path == "/api/chat": return self._chat(data)
        if p.path == "/api/chat/balance": return self._balance(data)
        if p.path == "/api/chat/order": return self._order(data)
        if p.path == "/api/chat/check-payments": return self._j({"credited": scan_topups()})
        return self._j({"error":"not found"},404)

    def _balance(self, d):
        uid = d.get("user_id","")
        if not uid: return self._j({"error":"user_id required"},400)
        c = db(); c.execute("INSERT OR IGNORE INTO users(user_id,created) VALUES(?,?)",(uid,int(time.time())))
        bal = c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()[0]; c.close()
        return self._j({"user_id":uid,"balance":bal,"price_per_msg":MSG_PRICE_USDC})

    def _order(self, d):
        uid = d.get("user_id",""); net = d.get("network","")
        if not uid: return self._j({"error":"user_id required"},400)
        if net not in TREASURY: return self._j({"error":"bad network"},400)
        amt = float(d.get("amount",1))  # USDC top-up amount; 1 USDC = 10 messages
        oid = gen_id()
        c = db(); c.execute("INSERT OR IGNORE INTO users(user_id,created) VALUES(?,?)",(uid,int(time.time()))); c.execute("INSERT INTO topups VALUES(?,?,?,?,?,?)",(oid,uid,net,amt,"pending",int(time.time()))); c.commit(); c.close()
        return self._j({"order_id":oid,"network":net,"amount_usdc":amt,
            "treasury":TREASURY[net],"memo":oid,
            "credits":amt/MSG_PRICE_USDC,
            "note":f"Send {amt} USDC to treasury with memo {oid}. You get {amt/MSG_PRICE_USDC:.0f} messages."})

    def _chat(self, d):
        uid = d.get("user_id","anon")
        msg = (d.get("message") or "").strip()
        if not msg: return self._j({"error":"message required"},400)
        pair = d.get("pair","SOL/USDT"); net = d.get("network","Solana")
        strat = d.get("strategy","Momentum Scalp"); sl = d.get("sl","3"); tp = d.get("tp","6"); size = d.get("size","50")
        c = db(); c.execute("INSERT OR IGNORE INTO users(user_id,created) VALUES(?,?)",(uid,int(time.time())))
        bal = c.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()[0]
        if bal < 1:
            c.close()
            return self._j({"error":"insufficient_balance","balance":bal,
                "message":"Pay $0.1 per message. Top up via /api/chat/order (USDC to treasury, memo=order_id).",
                "topup_url": f"/trading/?topup=1"})
        c.execute("UPDATE users SET balance=balance-1 WHERE user_id=?",(uid,))
        # save user msg
        mid = gen_id()
        c.execute("INSERT INTO msgs VALUES(?,?,?,?,?)",(mid,uid,"user",msg,int(time.time())))
        history = [{"role":m[0],"content":m[1]} for m in c.execute("SELECT role,content FROM msgs WHERE user_id=? ORDER BY ts",(uid,)).fetchall()]
        c.commit(); c.close()
        system_prompt = f"""You are XH Agent — a professional crypto/DeFi trading AI assistant on XH Agents.
You monitor DEX data (DexScreener-style). Current config: Pair={pair}, Network={net}, Strategy={strat}, Size=${size}USDT, SL={sl}%, TP={tp}%.
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
