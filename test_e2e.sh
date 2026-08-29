#!/bin/bash
echo "=== health ==="
curl -s https://xhagents.xyz/api/chat/health -w " [HTTP %{http_code}]\n"
echo "=== balance (new user) ==="
UID="testuser123"
curl -s -X POST https://xhagents.xyz/api/chat/balance -H 'Content-Type: application/json' --data "{\"user_id\":\"$UID\"}" -w " [HTTP %{http_code}]\n"
echo "=== chat without balance (should reject) ==="
curl -s -X POST https://xhagents.xyz/api/chat -H 'Content-Type: application/json' --data "{\"user_id\":\"$UID\",\"message\":\"analyze SOL\"}" -w " [HTTP %{http_code}]\n"
echo "=== topup order ==="
ORD=$(curl -s -X POST https://xhagents.xyz/api/chat/order -H 'Content-Type: application/json' --data "{\"user_id\":\"$UID\",\"amount\":1,\"network\":\"base\"}")
echo "$ORD"
OID=$(echo "$ORD" | /home/ubuntu/prpo_ai/venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['order_id'])")
echo "order_id=$OID"
echo "=== manually credit (simulate paid) ==="
cd /home/ubuntu/prpo_ai/BossyFactory/trading-agent && /home/ubuntu/prpo_ai/venv/bin/python -c "
import sqlite3
c=sqlite3.connect('chat.db')
c.execute(\"UPDATE topups SET status='paid' WHERE id='$OID'\")
c.execute(\"UPDATE users SET balance=balance+10 WHERE user_id='$UID'\")
c.commit(); print('credited 10 msgs')
"
echo "=== chat now (should reply) ==="
curl -s -X POST https://xhagents.xyz/api/chat -H 'Content-Type: application/json' --data "{\"user_id\":\"$UID\",\"message\":\"What is the signal for SOL/USDT?\",\"pair\":\"SOL/USDT\",\"network\":\"Solana\",\"strategy\":\"Momentum Scalp\",\"sl\":\"3\",\"tp\":\"6\",\"size\":\"50\"}" -w " [HTTP %{http_code}]\n"
echo "=== cleanup ==="
/home/ubuntu/prpo_ai/venv/bin/python -c "import sqlite3;c=sqlite3.connect('chat.db');c.execute(\"DELETE FROM users WHERE user_id='$UID'\");c.execute(\"DELETE FROM topups\");c.execute(\"DELETE FROM msgs\");c.commit();print('cleaned')"