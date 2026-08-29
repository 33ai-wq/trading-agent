#!/bin/bash
set -e
# deploy trading frontend
sudo mkdir -p /var/www/nomad7/trading
sudo cp /home/ubuntu/prpo_ai/BossyFactory/trading-agent/index.html /var/www/nomad7/trading/index.html
sudo chmod 644 /var/www/nomad7/trading/index.html
# nginx
sudo systemctl reload nginx
# chat engine service
sudo tee /etc/systemd/system/xh-chatengine.service >/dev/null <<'EOF'
[Unit]
Description=XH Agents AI Trading Chat Engine
After=network.target
[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/prpo_ai/BossyFactory/trading-agent
ExecStart=/home/ubuntu/prpo_ai/venv/bin/python chat_engine.py
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable xh-chatengine
sudo systemctl restart xh-chatengine
sleep 2
echo "chat service: $(sudo systemctl is-active xh-chatengine)"
# cron for topup payment scan
(crontab -l 2>/dev/null | grep -v 'chat/check-payments'; echo '*/5 * * * * curl -s -X POST http://127.0.0.1:8001/api/chat/check-payments >/dev/null 2>&1') | crontab -
echo "deployed"
