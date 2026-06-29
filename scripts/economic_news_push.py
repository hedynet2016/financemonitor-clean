#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
economic_news_push.py - 定時推送經濟指標相關新聞到 Telegram / Discord
設計給 render_scheduler.py 或 cron 呼叫
用法: python scripts/economic_news_push.py
"""
import json
import sys
import os
import requests
import feedparser
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
CONFIG_FILE = BASE_DIR / "config.json"

# 經濟指標關鍵字（與 news_monitor.py 一致）
ECONOMIC_KEYWORDS = [
    # 英文
    "cpi", "consumer price index", "inflation",
    "ppi", "producer price index",
    "unemployment rate", "jobless claims", "non-farm payroll",
    "employment report", "jobs report",
    "fed", "federal reserve", "fomc", "interest rate", "rate hike", "rate cut",
    "monetary policy", "central bank",
    "ecb", "european central bank", "boj", "bank of japan",
    "boe", "bank of england", "pboc", "people's bank of china",
    # 中文
    "消費者物價指數", "通膨", "通貨膨脹",
    "生產者物價指數",
    "失業率", "非農就業", "就業報告",
    "聯準會", "FOMC", "利率", "升息", "降息",
    "央行", "貨幣政策", "歐洲央行", "日本央行", "英國央行",
]

RSS_SOURCES = [
    ("CNBC",        "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("Bloomberg",   "https://feeds.bloomberg.com/markets/news.rss"),
    ("WSJ",         "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Yahoo Fin",   "https://finance.yahoo.com/news/rssindex"),
    ("FT",          "https://www.ft.com/?format=rss"),
    ("Seeking Alpha","https://seekingalpha.com/market_currents.xml"),
    ("TechNews",    "https://technews.tw/feed/"),
    ("MacroMicro",  "https://www.macromicro.me/feeds/latest-articles"),
    ("Digitimes",   "https://www.digitimes.com.tw/rss.asp"),
]


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def kw_match(text: str) -> bool:
    tl = text.lower()
    return any(kw.lower() in tl for kw in ECONOMIC_KEYWORDS)


def fetch_economic_news(max_per_source: int = 3,
                        hours_back: int = 24) -> list:
    """從 RSS 來源抓取經濟指標相關新聞"""
    results = []
    cutoff = datetime.now() - timedelta(hours=hours_back)

    for source_name, rss_url in RSS_SOURCES:
        try:
            feed = feedparser.parse(rss_url)
            count = 0
            for entry in feed.entries:
                if count >= max_per_source:
                    break
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                link = entry.get("link", "")

                if kw_match(title + " " + summary):
                    results.append({
                        "source": source_name,
                        "title": title.strip(),
                        "link": link,
                    })
                    count += 1
        except Exception as e:
            print(f"  [WARN] {source_name} 抓取失敗: {e}")

    # 去重（同一 link 只留第一筆）
    seen = set()
    unique = []
    for item in results:
        if item["link"] not in seen:
            seen.add(item["link"])
            unique.append(item)
    return unique


def format_message(news_items: list) -> str:
    if not news_items:
        return ""
    now_str = datetime.now().strftime("%Y-%m-%d")
    lines = [f"🏛️ 經濟指標相關新聞 {now_str}", "=" * 40]
    for item in news_items[:15]:
        lines.append(f"【{item['source']}】{item['title']}")
        lines.append(item["link"])
        lines.append("")
    return "\n".join(lines)


def send_telegram(message: str, token: str, chat_id: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def send_discord(message: str, webhook_url: str):
    # Discord 單則上限 2000 字，超過拆分
    max_len = 1900
    chunks = []
    while len(message) > max_len:
        chunks.append(message[:max_len])
        message = message[max_len:]
    chunks.append(message)
    for chunk in chunks:
        r = requests.post(webhook_url, json={"content": chunk}, timeout=20)
        r.raise_for_status()


def main():
    config = load_config()
    tg = config.get("telegram", {})
    dc = config.get("discord", {})

    token = tg.get("bot_token", "")
    chat_id = tg.get("chat_id", "")
    webhook = dc.get("webhook_url", "")

    if not token and not webhook:
        print("[ERROR] Telegram / Discord 皆未設定")
        sys.exit(1)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] 開始抓取經濟指標新聞 ...")

    items = fetch_economic_news()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 共找到 {len(items)} 則相關新聞")

    if not items:
        print("[INFO] 本時段無經濟指標相關新聞，跳過推播")
        sys.exit(0)

    msg = format_message(items)

    if token and chat_id:
        try:
            send_telegram(msg, token, chat_id)
            print(f"  ✅ 已推播到 Telegram ({len(items)} 則)")
        except Exception as e:
            print(f"  ❌ Telegram 推播失敗: {e}")

    if webhook:
        try:
            send_discord(msg, webhook)
            print(f"  ✅ 已推播到 Discord ({len(items)} 則)")
        except Exception as e:
            print(f"  ❌ Discord 推播失敗: {e}")

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 完成")


if __name__ == "__main__":
    main()
