#!/usr/bin/env python3
"""
雅虎拍賣商品追蹤監控模組
監控三個賣場（樺仔/點子3C/US3C）的九大關鍵字商品
價格 $2000 ~ $15000 元，排除標題含「NG」商品，七天內同一商品不重複推播

使用方法：
  python product_monitor.py              # 一般模式（Firefox headless）
  python product_monitor.py --once      # 執行一次即結束（測試用）
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ── 設定 ──────────────────────────────────────────────────────────────
SHOPS = [
    {"name": "樺仔二手電腦", "booth_id": "Y2311650079"},
    {"name": "點子3C",       "booth_id": "Y9756173709"},
    {"name": "US3C",          "booth_id": "Y9753752632"},
]

KEYWORDS = ["NUC", "Airpod", "Apple Watch", "MacBook", "iMac",
            "Acer", "MSI", "Lenovo", "Surface"]

MAX_PRICE = 15000
MIN_PRICE = 2000
DEDUP_DAYS = 7
STATE_FILE = "yahoo_state.json"


class ProductMonitor:
    """雅虎拍賣商品監控器"""

    def __init__(self, config: dict, notification_sender=None):
        self.config = config
        self.notification_sender = notification_sender
        self.state = self._load_state()

    # ── 狀態讀寫（去重） ────────────────────────────────────────────
    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"sent": {}}

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save state: %s", e)

    def _clean_old_state(self):
        cutoff = (datetime.now() - timedelta(days=DEDUP_DAYS)).strftime("%Y-%m-%d")
        self.state["sent"] = {
            k: v for k, v in self.state.get("sent", {}).items()
            if v >= cutoff
        }

    def _product_key(self, name, price):
        return "%s|%s" % (name.strip(), price)

    def _is_duplicate(self, name, price):
        key = self._product_key(name, price)
        last = self.state.get("sent", {}).get(key, "")
        if not last:
            return False
        try:
            return (datetime.now() - datetime.strptime(last, "%Y-%m-%d")).days < DEDUP_DAYS
        except Exception:
            return False

    def _mark_sent(self, name, price):
        key = self._product_key(name, price)
        today = datetime.now().strftime("%Y-%m-%d")
        self.state.setdefault("sent", {})[key] = today

    # ── 核心爬蟲邏輯 ───────────────────────────────────────────────
    def _scrape_shop(self, browser, booth_id, shop_name, keyword):
        """
        使用 Playwright 爬取雅虎拍賣店內搜尋結果。
        搜尋 URL 格式：/booth/search?p=關鍵字&seller=攤商ID&sort=new
        價格格式：$69,360（在商品卡 innerText 中）
        標題來源：商品圖片 img[alt] 屬性
        """
        products = []
        page = browser.new_page()

        try:
            import urllib.parse
            keyword_enc = urllib.parse.quote(keyword)
            url = (
                "https://tw.bid.yahoo.com/booth/search"
                "?p=%s&seller=%s&sort=new"
                % (keyword_enc, booth_id)
            )
            logger.info("[%s] 開啟：%s", shop_name, url)
            page.goto(url, timeout=60000, wait_until="domcontentloaded")

            # 等待 JS 渲染
            time.sleep(8)
            # 微捲動觸發懶加載
            page.evaluate("window.scrollTo(0, 300)")
            time.sleep(2)

            # 從 DOM 擷取所有商品卡資訊
            items_js = page.evaluate("""() => {
                const results = [];
                const links = document.querySelectorAll('a[href*="/item/"]');
                const seen = new Set();
                for (const a of links) {
                    const href = a.getAttribute('href');
                    if (!href) continue;
                    const mm = href.match(/\\/item\\/(\\d+)/);
                    if (!mm || seen.has(mm[1])) continue;
                    seen.add(mm[1]);

                    // 找商品卡容器
                    const li = a.closest('li[role="gridcell"]') || a.closest('li') || a.parentElement;
                    if (!li) continue;

                    // 標題：從圖片 alt 取得
                    let title = '';
                    const img = li.querySelector('img[alt]');
                    if (img && img.alt && img.alt.length > 5) {
                        title = img.alt;
                    }
                    if (!title) {
                        title = a.getAttribute('aria-label') || a.getAttribute('title') || '';
                    }
                    if (!title) {
                        title = (a.innerText || '').trim().substring(0, 100);
                    }

                    // 價格：從卡片文字找 $xx,xxx 格式（用 indexOf + 手動提取，避開 regex 轉義問題）
                    let price = 0;
                    const cardText = (li.innerText || a.innerText || '');
                    // 找 '$' 後面的數字（含逗號）
                    const dollarIdx = cardText.indexOf('$');
                    if (dollarIdx >= 0) {
                        let numStr = '';
                        let j = dollarIdx + 1;
                        while (j < cardText.length && (cardText[j] === ',' || (cardText[j] >= '0' && cardText[j] <= '9'))) {
                            numStr += cardText[j];
                            j++;
                        }
                        if (numStr) {
                            price = parseInt(numStr.replace(/,/g, ''), 10);
                        }
                    }

                    results.push({
                        item_id: mm[1],
                        name: title.substring(0, 100),
                        price: price,
                        url: 'https://tw.bid.yahoo.com/item/' + mm[1]
                    });
                }
                return results;
            }""")

            logger.info("[%s] JS 擷取到 %d 筆資料", shop_name, len(items_js))

            for item in items_js:
                if item["name"] and item["url"]:
                    products.append({
                        "shop": shop_name,
                        "name": item["name"],
                        "price": item["price"],
                        "url": item["url"],
                        "keyword": keyword,
                    })

        except Exception as e:
            logger.warning("[%s] 爬取失敗：%s", shop_name, e)
        finally:
            try:
                page.close()
            except Exception:
                pass

        return products

    # ── 主流程 ────────────────────────────────────────────────────
    def run(self):
        logger.info("========== 商品追蹤開始 ==========")
        self._clean_old_state()

        from playwright.sync_api import sync_playwright

        all_products = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        with sync_playwright() as pw:
            browser = pw.firefox.launch(headless=True)
            logger.info("已啟動 Firefox headless")

            for shop in SHOPS:
                shop_products = []
                for keyword in KEYWORDS:
                    logger.info("[%s] 搜尋：%s", shop["name"], keyword)
                    items = self._scrape_shop(
                        browser, shop["booth_id"], shop["name"], keyword
                    )
                    for p in items:
                        if (p["price"] <= MAX_PRICE
                                and p["price"] >= MIN_PRICE
                                and "NG" not in p["name"]
                                and not self._is_duplicate(p["name"], p["price"])):
                            p["keyword"] = keyword
                            shop_products.append(p)
                    time.sleep(1.5)

                # 依商品 ID 排序（新→舊，ID 越大越新）
                shop_products.sort(
                    key=lambda x: int(x["url"].rstrip("/").split("/")[-1]),
                    reverse=True,
                )
                all_products.extend(shop_products)

            browser.close()

        if not all_products:
            logger.info("沒有符合條件的新商品")
            logger.info("========== 商品追蹤結束 ==========")
            return 0

        # 產生推播訊息
        lines = [
            "🔍 商品追蹤（%s）" % now_str,
            "共找到 %d 筆符合條件的商品（價格 $%s ~ $%s）"
            % (len(all_products), format(MIN_PRICE, ","), format(MAX_PRICE, ",")),
            "─" * 30,
        ]

        for i, p in enumerate(all_products[:20], 1):
            lines.append(
                "%d. [%s] %s\n"
                "   💰 $%s  |  關鍵字：%s\n"
                "   🔗 %s"
                % (
                    i,
                    p["shop"],
                    p["name"][:50],
                    format(p["price"], ",") if p["price"] else "?",
                    p["keyword"],
                    p["url"],
                )
            )
            self._mark_sent(p["name"], p["price"])

        message = "\n".join(lines)
        logger.info("準備推播 %d 筆商品", min(len(all_products), 20))

        # 推播到 Telegram + Discord（使用 events_webhook）
        sent = 0
        try:
            discord_webhook = (
                self.config.get("discord", {})
                .get("events_webhook_url", "")
            )
            if self.notification_sender:
                result = self.notification_sender.send_to_all(
                    message, discord_webhook=discord_webhook
                )
                if isinstance(result, dict):
                    sent = sum(1 for v in result.values() if v)
                else:
                    sent = 0
            else:
                from notification_sender import NotificationSender
                ns = NotificationSender(self.config)
                result = ns.send_to_all(
                    message, discord_webhook=discord_webhook
                )
                if isinstance(result, dict):
                    sent = sum(1 for v in result.values() if v)
                else:
                    sent = 0
        except Exception as e:
            logger.error("推播失敗：%s", e)

        self._save_state()
        logger.info(
            "========== 商品追蹤結束（推播 %d 筆）==========",
            sent,
        )
        return sent


# ── 獨立執行 ──────────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once", action="store_true", help="執行一次即結束（預設行為）"
    )
    args = parser.parse_args()

    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    from notification_sender import NotificationSender
    ns = NotificationSender(config)
    monitor = ProductMonitor(config, ns)
    monitor.run()


if __name__ == "__main__":
    main()
