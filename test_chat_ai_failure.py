#!/usr/bin/env python3
"""Regression test: an empty model answer is a FAILURE, never a paid success.

Why this file exists: the chat backend treated an HTTP 200 whose content string was
empty as a successful reply ("(empty reply)"). NIM_MODEL is a reasoning model, and its
thinking tokens come out of the same max_tokens budget, so the old 500-token budget
could be consumed entirely by reasoning: a paying buyer got "(empty reply)" while the
call still settled on-chain, and the response even said charged:false.

Run: ./venv/bin/python test_chat_ai_failure.py      (from /home/ubuntu/prpo_ai)
It makes one real (free) NIM call plus two faked-failure calls; no payment happens.
"""
import sys

sys.path.insert(0, "/home/ubuntu/prpo_ai/BossyFactory/trading-agent")

import chat_engine as ce  # noqa: E402
from chat_engine import nim_reply  # noqa: E402

SYS = ("You are XH Agent - a crypto/DeFi trading assistant. "
       "LIVE MARKET: BTC $0. RULES: reply directly, under 140 words.")

ok_all = True

print("NIM_MODEL:", ce.NIM_MODEL, "| key set:", bool(ce.NVIDIA_API_KEY))
if not ce.NVIDIA_API_KEY:
    print("SKIP: NVIDIA_API_KEY not configured")
    sys.exit(0)

print("\n1) a real call must return text")
txt, ok = nim_reply(SYS, "What is BTC doing right now?", [])
print("   ok:", ok, "| chars:", len(txt), "| head:", txt[:80])
ok_all &= bool(ok and txt.strip() and "empty reply" not in txt)

print("\n2) a 200 with empty content must be retried with a bigger budget, not reported")
real_post = ce.requests.post


class FakeEmpty:
    status_code = 200

    def json(self):
        return {"choices": [{"finish_reason": "length",
                             "message": {"content": "", "reasoning_content": "thinking..."}}]}


seen = []
_count = {"n": 0}


def flaky(*a, **kw):
    _count["n"] += 1
    seen.append(kw["json"]["max_tokens"])
    if _count["n"] == 1:
        return FakeEmpty()
    return real_post(*a, **kw)


ce.requests.post = flaky
txt2, ok2 = nim_reply(SYS, "What is BTC doing right now?", [])
ce.requests.post = real_post
print("   budgets tried:", seen, "| ok:", ok2, "| chars:", len(txt2))
ok_all &= bool(ok2 and seen == [1600, 3200])

print("\n3) a model that never returns text must be ok=False (so nothing is settled)")
ce.requests.post = lambda *a, **kw: FakeEmpty()
txt3, ok3 = nim_reply(SYS, "What is BTC doing right now?", [])
ce.requests.post = real_post
print("   ok:", ok3, "| reply:", txt3[:90])
ok_all &= (ok3 is False)

print("\nRESULT:", "PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
