#!/bin/bash
# Install the US stock paper desk (stocks.py) as an always-on service next to MrTrader.
set -e
sudo tee /etc/systemd/system/mrtrader-stocks.service >/dev/null <<UNIT
[Unit]
Description=MrTrader US stock paper desk
After=network-online.target
Wants=network-online.target
[Service]
User=$USER
WorkingDirectory=$HOME/MrTrader
ExecStart=/usr/bin/python3 stocks.py
Restart=always
RestartSec=30
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now mrtrader-stocks
sudo systemctl restart mrtrader-stocks
sudo systemctl restart mrtrader
echo ">>> FATTO: desk azioni acceso."
