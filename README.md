# XH Agents — AI Trading Assistant (pay-per-message)

A paid chat for token and market analysis, plus a market-data panel. Live at
**https://xhagents.xyz/trading/**.

Ask it about a token, a pair or a strategy and it answers from a hosted LLM with live market data in
context. Each message costs **$0.10 USDC**. No token, no subscription, no sign-up.

---

## What this actually does

| | |
|---|---|
| Paid message | `POST /api/chat` — $0.10 USDC per message, two rails: wallet top-up credits, or a spec-compliant x402 payment on Base (`eip155:8453`) |
| Wallet top-up | Connect MetaMask (USDC on Base) or Phantom (USDC on Solana), send USDC to the treasury, and the server credits messages by verifying the transfer on-chain — sender, recipient, token, amount and tx hash are all bound, and one transfer can be claimed once |
| Market data | `GET /api/markets` — live price, 24h change, volume and a rule-based signal/confidence for 8 major pairs, from CoinGecko, refreshed server-side |
| Chat | hosted LLM with the market snapshot injected into the prompt; a failed model call is never charged |
| Health | `GET /api/chat/health` |

Buyer flow: connect wallet → top up (or pay per call via x402) → chat. The chat panel and the credits
flow live on the same page.

## What this repository does NOT contain — README correction, 2026-11-10

An earlier version of this README advertised features that are **not in the code**. They are listed
here so nobody has to discover the gap from the source:

- **No DexScreener integration.** Market data comes from CoinGecko (`/api/markets`).
- **No Jupiter swap, and no swap of any kind.** There is no swap instruction, no route request and no
  transaction builder anywhere in this repository.
- **No auto-trade bot.** No background scanner, no 30-second signal loop, no take-profit or stop-loss
  execution.
- **No rug-pull filter engine.** The activity log in the UI is a fixed, labelled sample (`DEMO …`) that
  shows the intended layout. The `SIM` button next to a signal draws a sample row with a randomly
  generated placeholder price; it sends nothing anywhere.
- **No wallet custody.** The page can *ask a browser wallet to sign a USDC transfer* for a top-up, and
  nothing else. No private key reaches this code.

The honest description of the product is one line: **a paid market-analysis chat with live market
data.**

## Running it locally

```bash
python3 chat_engine.py     # serves the API on :8001 and the page in this folder
```

Environment (a local `.env` is read by a shared loader; real environment variables win):

| variable | used for |
|---|---|
| LLM key (e.g. `NVIDIA_API_KEY`) | chat model calls — without it the engine answers in demo mode |
| `XH_LOOKBACK_BLOCKS` | how far back the on-chain payment scan looks |
| `XH_CHAT_VERIFY_PER_HOUR` | rate limit per user+IP on payment verification |
| `X402_STANDARD` | set to `0` to disable the standard x402 gate |
| `X402_FACILITATOR_URL` | x402 facilitator (defaults to the CDP facilitator on Base mainnet) |

Payment verification is delegated to a shared module (`xh_verify`), so the payable routes and the ad
engine use the same, tested rules.

## Files

| file | what it is |
|---|---|
| `chat_engine.py` | the whole backend: chat, balance, top-up orders, wallet payment verification, x402 gate, market data |
| `index.html` | the trading UI (market panel, chat, wallet connect, top-up) |
| `deploy.sh` | deploy helper for the host in front of the engine |
| `test_e2e.sh`, `test_chat_ai_failure.py` | end-to-end and failure-path checks |
| `AUDIT_REPORT.md` | the audit that produced the fixes in this repository (kept for history) |
| `LICENSE` | MIT |

## Disclaimer

Not financial advice. Market data is third-party data and can be delayed or wrong. This software does
not execute trades and cannot execute trades as shipped. Crypto assets are risky — never spend what
you cannot afford to lose.
