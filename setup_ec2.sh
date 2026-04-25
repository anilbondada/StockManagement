#!/bin/bash
set -e

sudo dnf update -y
sudo dnf install -y python3 python3-pip

cd /home/ec2-user/app
pip3 install -r requirements.txt

sudo tee /etc/systemd/system/stockapi.service > /dev/null <<EOF
[Unit]
Description=Stock Management API
After=network.target

[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/app
ExecStart=/usr/local/bin/uvicorn Main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable stockapi
sudo systemctl start stockapi
echo "Done. API running on http://35.173.222.119:8000"
