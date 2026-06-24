#!/usr/bin/env python3
"""
generate_config.py - 從環境變數生成 config.json
在 Render（或任何雲端平台）上執行時，敏感資訊不寫入 repo，
而是透過環境變數注入，啟動時由本腳本動態生成 config.json。
"""
import json
import os
import sys

def generate():
    config = {
        "fred_api_key": os.environ.get("FRED_API_KEY", ""),
        "telegram": {
            "enabled": os.environ.get("TELEGRAM_ENABLED", os.environ.get("TG_ENABLED", "true")).lower() == "true",
            "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", os.environ.get("TG_BOT_TOKEN", "")),
            "chat_id": os.environ.get("TELEGRAM_CHAT_ID", os.environ.get("TG_CHAT_ID", "")),
            "proxy": os.environ.get("TELEGRAM_PROXY", os.environ.get("TG_PROXY", ""))
        },
        "wechat": {
            "enabled": False,
            "webhook_url": "",
            "corp_id": "",
            "agent_id": "",
            "secret": ""
        },
        "line": {
            "enabled": False,
            "channel_access_token": "",
            "user_id": ""
        },
        "discord": {
            "enabled": os.environ.get("DISCORD_ENABLED", "true").lower() == "true",
            "webhook_url": os.environ.get("DISCORD_WEBHOOK_URL", os.environ.get("DISCORD_WEBHOOK", "")),
            "events_webhook_url": os.environ.get("DISCORD_EVENTS_WEBHOOK_URL", os.environ.get("DISCORD_EVENTS_WEBHOOK", ""))
        },
        "tickers": {
            "us_stocks": os.environ.get(
                "US_STOCKS",
                "AAPL,MSFT,GOOGL,GOOG,AMZN,META,NVDA,TSLA,JPM,BAC,WMT,DIS,NFLX,AMD,INTC,CSCO,PLTR,COIN,MSTR"
            ).split(","),
            "us_etfs": os.environ.get(
                "US_ETFS",
                "SPY,QQQ,IWM,DIA,GLD,VTI,VOO,IVV,XLF,XLK,XLE,XLV,XLI,XLU,XLRE,XLP,SOXL,TQQQ"
            ).split(","),
            "tw_stocks": os.environ.get(
                "TW_STOCKS",
                "2330.TW,2317.TW,2454.TW,2308.TW,2382.TW,2412.TW,2327.TW,2882.TW,2881.TW,2880.TW,2885.TW,2886.TW,2887.TW,2890.TW,2891.TW,2892.TW,4938.TW,3231.TW,3008.TW"
            ).split(","),
            "tw_etfs": os.environ.get(
                "TW_ETFS",
                "0050.TW,0051.TW,0052.TW,0053.TW,0054.TW,0055.TW,0056.TW,0057.TW,0058.TW,0060.TW,0061.TW,0062.TW"
            ).split(",")
        },
        "trading_hours": {
            "us_market": {
                "enabled": True,
                "timezone": "US/Eastern",
                "start_hour": 9,
                "start_minute": 30,
                "end_hour": 16,
                "end_minute": 0,
                "weekdays_only": True
            },
            "tw_market": {
                "enabled": True,
                "timezone": "Asia/Taipei",
                "start_hour": 9,
                "start_minute": 0,
                "end_hour": 13,
                "end_minute": 30,
                "weekdays_only": True
            }
        },
        "news": {
            "enabled": True,
            "timezone": "US/Eastern",
            "daily_hour": int(os.environ.get("NEWS_DAILY_HOUR", "8")),
            "events_hour": int(os.environ.get("NEWS_EVENTS_HOUR", "14")),
            "sources": [
                {
                    "name": "CNBC",
                    "rss_url": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
                    "base_url": "https://www.cnbc.com"
                },
                {
                    "name": "Wall Street Journal",
                    "rss_url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
                    "base_url": "https://www.wsj.com"
                }
            ]
        },
        "economic": {
            "enabled": False,
            "timezone": "US/Eastern",
            "daily_hour": 8,
            "check_interval": None,
            "high_priority_indicators": [
                "GDP", "Federal Funds Rate", "Unemployment Rate",
                "Non-Farm Payrolls", "CPI", "PCE"
            ],
            "medium_priority_indicators": [
                "Retail Sales", "PMI", "Housing Starts", "Consumer Confidence"
            ]
        }
    }

    # 寫入專案根目錄（scripts/ 的上一層），與 webui.py / integrated_monitor.py 讀取路徑一致
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = os.path.join(project_root, "config.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    # 驗證關鍵欄位
    missing = []
    if not config["telegram"]["bot_token"]:
        missing.append("TG_BOT_TOKEN")
    if not config["telegram"]["chat_id"]:
        missing.append("TG_CHAT_ID")
    if config["discord"]["enabled"] and not config["discord"]["webhook_url"]:
        missing.append("DISCORD_WEBHOOK")

    if missing:
        print(f"[WARN] 以下環境變數未設定: {', '.join(missing)}", file=sys.stderr)
        print("[WARN] 對應功能將無法正常運作", file=sys.stderr)
    else:
        print("[OK] config.json 生成完成，所有關鍵欄位已就緒")

    return config


if __name__ == "__main__":
    generate()
