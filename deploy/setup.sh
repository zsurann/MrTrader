#!/bin/bash
# One-shot setup on a fresh Debian VM: desk + dashboard as a service, phone access via Tailscale (private).
set -e
sudo apt-get update -qq && sudo apt-get install -y -qq git python3 curl
[ -d ~/MrTrader ] || git clone -b claude/brave-turing-8snpk9 https://github.com/zsurann/MrTrader.git ~/MrTrader
cd ~/MrTrader && git pull -q
sudo tee /etc/systemd/system/mrtrader.service >/dev/null <<UNIT
[Unit]
Description=MrTrader desk dashboard
After=network-online.target
Wants=network-online.target
[Service]
User=$USER
WorkingDirectory=$HOME/MrTrader
ExecStart=/usr/bin/python3 dashboard.py
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now mrtrader
sudo systemctl restart mrtrader
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
echo ">>> Apri il link qui sotto e accedi a Tailscale (stesso account che userai sul telefono):"
sudo tailscale up
sudo tailscale serve --bg 8050
echo ">>> FATTO. Indirizzo per il telefono:"
sudo tailscale serve status | grep -o 'https://[^ ]*' | head -1
