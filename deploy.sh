#!/bin/bash
set -e

KEY="/c/Users/user/Desktop/Anil-StockManagement/anil-StockExecutionManagement-key.pem"
HOST="ec2-user@35.170.43.44"
SSH="ssh -i $KEY -o StrictHostKeyChecking=no"

echo "[deploy] Pulling latest code..."
$SSH $HOST "cd /home/ec2-user/app && git pull origin main"

echo "[deploy] Restarting service..."
$SSH $HOST "sudo systemctl restart stockapi"

sleep 3

echo "[deploy] Service status:"
$SSH $HOST "sudo systemctl status stockapi --no-pager"
