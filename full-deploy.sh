#!/bin/bash
set -e

git pull
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt
sudo cp deploy/discord-birthday.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable discord-birthday
sudo systemctl restart discord-birthday
journalctl -u discord-birthday -f
