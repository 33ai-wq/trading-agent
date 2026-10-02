# Trading Agent Audit Report
**Date**: 2026-08-30
**Repo**: 33ai-wq/trading-agent (cloned to BossyFactory/trading-agent)
**Status**: Critical bugs found — deployment will fail

---

## 🔴 CRITICAL BUGS (Block Deployment)

### 1. Missing Backend Endpoints (Frontend calls non-existent APIs)
| Frontend calls | Backend has? | Impact |
|---|---|---|
| `POST /api/chat/verify-payment` | ❌ NO | Wallet payment verification broken |
| `POST /api/chat/pay` | ❌ NO | Can't get payment payload for signing |
| `POST /api/chat/order` | ✅ YES | Works but frontend uses `doTopup()` not integrated |

**Evidence**: `index.html` lines 696, 710, 868 call these endpoints. `chat_engine.py` only has `/api/chat`, `/api/chat/balance`, `/api/chat/order`, `/api/chat/check-payments`, `/api/markets`, `/api/chat/health`.

### 2. Solana Treasury Address Mismatch
| File | Address | Match? |
|---|---|---|
| `chat_engine.py` line 23 | `GhFbGgNxERN6pQ7boSFLFJuPwXJuvJ8Tx7EgoJ9LV2Aw` | ❌ NO |
| `index.html` line 652 | `GhFbGgNxERN6pQ7boSFLFJuPwXJuvJ8Tx7EgoJ9LV1x` | ❌ NO |

**Diff**: Last 2 chars differ (`Aw` vs `1x`). Payment detection will NEVER match on Solana.

### 3. Frontend Payment Flow Broken
- `payUsdc()` calls `/api/chat/pay` → expects `{requires_wallet_signature, to_address, amount_base, amount_lamports}` → signs & broadcasts → polls `/api/chat/verify-payment`
- Neither endpoint exists → **payment flow completely broken**

---

## 🟡 HIGH PRIORITY

### 4. No Wallet Address Sync to Backend
Frontend sends `wallet_address` in `/api/chat` but:
- Balance check doesn't use it (only `user_id`)
- Order creation doesn't link wallet
- No way to verify payment belongs to user

### 5. Demo Mode Fallback Returns Fake Replies
`chat_engine.py` line 123: If `NVIDIA_API_KEY` missing, returns `[DEMO MODE]...` — but key IS in `.env`. If key invalid/expired, users get fake replies silently.

### 6. Cron Path Hardcoded to localhost
`deploy.sh` line 30: `curl -s -X POST http://127.0.0.1:8001/api/chat/check-payments`
- Works for local, but if deployed behind nginx proxy, may need different host

### 7. USDC Contract Address Hardcoded (Base)
`index.html` line 746: `0x833dBe171C8B6D5C6B6B9d225B2f7Cf6b5a9e2bA` — Base USDC. OK if only Base, but should be configurable.

---

## 🟢 MEDIUM / NICE TO HAVE

### 8. No Input Validation on Trade Size/SL/TP
Frontend sends raw values to backend. No sanitization.

### 9. Chat History Unbounded
`msgs` table grows forever. No retention policy.

### 10. No Rate Limiting on Chat Endpoint
Could be abused for free AI calls if balance check bypassed.

### 11. Single-threaded HTTPServer
Python `HTTPServer` is single-threaded. Concurrent users will queue.

### 12. No HTTPS / TLS
Running on HTTP only. Production needs nginx + SSL.

---

## 📋 FIXES NEEDED (Priority Order)

1. **Add `/api/chat/pay` endpoint** — returns payment payload for wallet signing
2. **Add `/api/chat/verify-payment` endpoint** — checks on-chain + credits balance
3. **Fix Solana treasury address** — unify between frontend/backend
4. **Wire wallet_address through balance/order/verify flows**
5. **Add demo mode warning to UI** so users know when AI is fake
6. **Consider upgrading to FastAPI/uvicorn** for concurrency

---

## 🚀 MIGRATION TO xhagents.xyz NOTES

- Current deploy path: `/var/www/nomad7/trading/` → xhagents.xyz likely wants subpath or subdomain
- Treasury addresses must match production wallets
- Need to add x402 payment header support (current is custom USDC memo)
- Consider using `x402` middleware instead of custom balance system
