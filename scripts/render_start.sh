#!/usr/bin/env bash
# render_start.sh — Render 啟動腳本
set -e

echo "[Render] Starting WorkBuddy..."
echo "[Render] Time: $(date)"
echo "[Render] Timezone: $(cat /etc/timezone 2>/dev/null || echo 'unknown')"
echo "[Render] TELEGRAM_BOT_TOKEN: $([ -n "$TELEGRAM_BOT_TOKEN" ] && echo 'SET' || echo 'NOT SET')"
echo "[Render] TELEGRAM_CHAT_ID: $([ -n "$TELEGRAM_CHAT_ID" ] && echo 'SET' || echo 'NOT SET')"
echo "[Render] DISCORD_WEBHOOK_URL: $([ -n "$DISCORD_WEBHOOK_URL" ] && echo 'SET' || echo 'NOT SET')"
echo "[Render] Python: $(python --version)"

# 1. 從環境變數產生 config.json
echo "[Render] Generating config.json..."
python scripts/generate_config.py
echo "[Render] config.json generated"

# 2. 啟動 Web UI（內含排程器）
echo "[Render] Starting Web UI + scheduler..."
python webui.py
