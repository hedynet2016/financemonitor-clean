#!/usr/bin/env bash
# render_start.sh — Render 啟動腳本
set -e

echo "[Render] Starting WorkBuddy..."

# 1. 從環境變數產生 config.json
python scripts/generate_config.py

# 2. 啟動 Web UI（內含排程器）
python webui.py
