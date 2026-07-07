#!/usr/bin/env python3
"""
雅虎拍賣商品追蹤監控模組
監控三個賣場（樺仔/點子3C/US3C）的九大關鍵字商品
價格 $2000 ~ $15000 元，排除標題含「NG」商品，七天內同一商品不重複推播

使用方法：
  python product_monitor.py              # 一般模式
  python product_monitor.py --once      # 執行一次即結束（測試用）

技術方案：直接呼叫雅虎拍賣 GraphQL API（persisted query），無需瀏覽器
"""

import json
import logging
import os
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

# ── GraphQL API 設定 ──────────────────────────────────────────────────
GQL_URL = "https://graphql.ec.yahoo.com/graphql"
GQL_SPACE_ID = "2092115390"
GQL_LISTINGS_HASH = (
    "0de637297197474cdd15b2f7635ab08d6b689d0e6e3974336d44e5d11b669782"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class ProductMonitor:
    """雅虎拍賣商品監控器（GraphQL API 版）"""

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

    # ── 核心：GraphQL API 查詢 ─────────────────────────────────────
    def _scrape_shop(self, booth_id, shop_name, keyword):
        """
        呼叫雅虎拍賣 GraphQL API 取得店內搜尋結果。
        使用 persisted query（SHA256 hash），不需瀏覽器。
        """
        products = []

        try:
            import requests

            payload = {
                "variables": {
                    "spaceId": GQL_SPACE_ID,
                    "storeId": booth_id,
                    "q": keyword,
                    "sort": "recommended",
                    "hits": 60,
                    "offset": 0,
                },
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": GQL_LISTINGS_HASH,
                    }
                },
            }

            headers = {
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": "https://tw.bid.yahoo.com",
                "Referer": (
                    "https://tw.bid.yahoo.com/booth/search"
                    "?p=%s&seller=%s&sort=new" % (keyword, booth_id)
                ),
            }

            logger.info("[%s] GraphQL 搜尋：%s", shop_name, keyword)
            resp = requests.post(GQL_URL, headers=headers, json=payload, timeout=30)

            if resp.status_code != 200:
                logger.warning("[%s] API 回傳 %d", shop_name, resp.status_code)
                return products

            data = resp.json()

            # 檢查錯誤
            if "errors" in data:
                errs = data["errors"]
                msg = errs[0].get("message", str(errs)) if errs else str(errs)
                logger.warning("[%s] GraphQL 錯誤：%s", shop_name, str(msg)[:200])
                return products

            # 解析商品列表
            result = data.get("data", {}).get("getAuctionProductsInStore") or {}
            total = result.get("totalCount", 0)
            raw_products = result.get("products") or []

            logger.info("[%s] API 回傳 %d 筆（totalCount=%d）", shop_name, len(raw_products), total)

            for p in raw_products:
                title = (p.get("title") or "").strip()
                if not title or len(title) < 3:
                    continue

                item_id = p.get("id", "")
                url = p.get("url") or ("https://tw.bid.yahoo.com/item/" + item_id)

                # 價格：currentPrice（直購價）> marketPrice（市價）> biddingPrice（競標價）
                price = 0
                for key in ("currentPrice", "marketPrice", "biddingPrice"):
                    val = p.get(key)
                    if val is not None:
                        try:
                            price = int(float(val))
                            break
                        except (ValueError, TypeError):
                            pass

                products.append({
                    "shop": shop_name,
                    "name": title[:100],
                    "price": price,
                    "url": url,
                    "keyword": keyword,
                    "item_id": item_id,
                })

        except Exception as e:
            logger.warning("[%s] GraphQL 查詢失敗：%s", shop_name, e)

        return products

    # ── 主流程 ────────────────────────────────────────────────────
    def run(self):
        logger.info("========== 商品追蹤開始 ==========")
        self._clean_old_state()

        all_products = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        for shop in SHOPS:
            shop_products = []
            for keyword in KEYWORDS:
                logger.info("[%s] 搜尋：%s", shop["name"], keyword)
                items = self._scrape_shop(
                    shop["booth_id"], shop["name"], keyword
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
                key=lambda x: int(x.get("item_id", 0) or 0),
                reverse=True,
            )
            all_products.extend(shop_products)

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
