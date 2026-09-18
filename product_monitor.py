#!/usr/bin/env python3
"""
雅虎拍賣商品追蹤監控模組

監控範圍（兩部分合併推播）：
  1. 三個固定賣場：樺仔二手電腦 / 點子3C 板橋店 / US3C
  2. 雙北地區（台北市・新北市）「與點子3C同類」的 3C 店鋪型賣家
     ─ 以相同 9 大關鍵字搜尋雅虎拍賣全站，篩選條件與固定賣場完全相同

共同過濾條件：
  - 價格 $2000 ~ $15000 元
  - 排除標題含「NG」商品
  - 刊登超過 7 天的商品自動排除
  - 同一商品 7 天內不重複推播
  - 推播上限 30 筆（依刊登日期新→舊排序）

使用方法：
  python product_monitor.py              # 一般模式
  python product_monitor.py --once      # 執行一次即結束（測試用）

技術方案：
  固定賣場 → 雅虎拍賣 GraphQL API（persisted query），無需瀏覽器；
             刊登日期由商品頁 isoredux-data 的 startTime 取得。
  雙北搜尋 → 雅虎拍賣搜尋頁（伺服器渲染），單次回應即含
             ec_location（地區）、ec_starttime（刊登時間）、
             ec_is_store_item（是否店鋪型）、ec_cat0（大分類）、
             ec_listprice（價格）、ec_auid（店鋪 ID），無需逐筆抓商品頁。
"""

import html
import json
import logging
import os
import re
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
DEDUP_DAYS = 7          # 同一商品 7 天內不重複推播
MAX_LISTING_DAYS = 7    # 刊登超過 7 天的商品自動排除
MAX_PUSH_ITEMS = 30     # 推播筆數上限（固定賣場 + 雙北同類店家）
STATE_FILE = "yahoo_state.json"

# ── 雙北地區「與點子3C同類」店家搜尋設定 ──────────────────────────────
# 以 9 大關鍵字搜尋雅虎拍賣全站，再依下列條件篩出雙北 3C 店鋪
AREA_SEARCH_URL = "https://tw.bid.yahoo.com/search/auction/product"
AREA_LOCATIONS = {"台北市", "新北市"}
# 雅虎拍賣 3C 相關大分類（ec_cat0）：只有這些分類才算「同類店家」
C3_CAT0_IDS = {
    "23336",       # 電腦、平板與周邊
    "2092042954",  # 手機、配件與通訊
    "2092101502",  # 家電與影音視聽
    "2092077887",  # 相機、攝影與周邊
    "2092107360",  # 電玩遊戲與主機
}

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

    # ── 刊登日期查詢 ────────────────────────────────────────────────
    def _fetch_listing_date(self, item_id):
        """
        從商品頁面取得刊登日期（startTime）。
        雅虎拍賣商品頁面的 isoredux-data 中包含 startTime（Unix timestamp）。
        回傳 datetime 物件或 None。
        """
        try:
            import requests

            url = "https://tw.bid.yahoo.com/item/%s" % item_id
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            if resp.status_code != 200:
                return None

            # 從 HTML 中提取 startTime
            m = re.search(r'"startTime"\s*:\s*(\d+)', resp.text)
            if m:
                ts = int(m.group(1))
                return datetime.utcfromtimestamp(ts)

        except Exception as e:
            logger.debug("取得刊登日期失敗 [%s]: %s", item_id, e)

        return None

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

    # ── 雙北地區同類店家（與點子3C同類的 3C 店鋪）───────────────────
    @staticmethod
    def _extract_isoredux(html_text):
        """
        從雅虎拍賣頁面取出 isoredux-data 的 JSON（伺服器端渲染的狀態資料）。
        取不到時回傳 None。
        """
        m = re.search(
            r'id="isoredux-data"[^>]*>(.*?)</script>',
            html_text,
            re.S,
        )
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _hit_price(hit):
        """從搜尋結果取價格：ec_listprice > ec_price > ec_buyprice。"""
        for key in ("ec_listprice", "ec_price", "ec_buyprice"):
            try:
                val = float(hit.get(key))
            except (TypeError, ValueError):
                continue
            if val > 0:
                return int(val)
        return 0

    def _search_area_stores(self, keyword):
        """
        以關鍵字搜尋雅虎拍賣全站，篩出「雙北地區 3C 店鋪型賣家」的商品。

        篩選條件（與固定賣場完全相同，另加地區／店鋪／分類限制）：
          - 商品所在地為台北市或新北市（ec_location）
          - 店鋪型賣家（ec_is_store_item == True），排除個人賣家
          - 商品大分類屬 3C（ec_cat0 在 C3_CAT0_IDS）
          - 賣家非既有三個固定賣場（避免重複）
          - 價格 MIN_PRICE ~ MAX_PRICE、標題不含 NG
          - 刊登於 MAX_LISTING_DAYS 天內（ec_starttime）
          - 同一商品 DEDUP_DAYS 天內不重複推播

        搜尋結果本身即含刊登時間，無需逐筆抓商品頁。
        """
        products = []
        known_booths = {s["booth_id"] for s in SHOPS}

        try:
            import requests

            headers = {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-TW,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            }
            # sort=new 讓最新上架的物件排在前面，提高抓到 7 天內新品的機率
            params = {"p": keyword, "sort": "new"}

            logger.info("[雙北搜尋] %s", keyword)
            resp = requests.get(
                AREA_SEARCH_URL, params=params, headers=headers, timeout=30
            )
            if resp.status_code != 200:
                logger.warning("[雙北搜尋] %s：HTTP %d", keyword, resp.status_code)
                return products

            state = self._extract_isoredux(resp.text)
            if not state:
                logger.warning("[雙北搜尋] %s：找不到 isoredux-data", keyword)
                return products

            hits = (
                state.get("search", {})
                .get("ecsearch", {})
                .get("hits", []) or []
            )
            logger.info("[雙北搜尋] %s：取得 %d 筆候選", keyword, len(hits))

            now_utc = datetime.utcnow()
            for hit in hits:
                # 1. 店鋪型賣家
                if str(hit.get("ec_is_store_item")) != "True":
                    continue
                # 2. 雙北地區
                location = (hit.get("ec_location") or "").strip()
                if location not in AREA_LOCATIONS:
                    continue
                # 3. 3C 大分類
                if str(hit.get("ec_cat0")) not in C3_CAT0_IDS:
                    continue
                # 4. 排除既有固定賣場
                auid = (hit.get("ec_auid") or "").strip()
                if auid in known_booths:
                    continue

                title = (hit.get("ec_title") or "").strip()
                if not title or len(title) < 3:
                    continue

                # 5. 價格區間 + 排除 NG
                price = self._hit_price(hit)
                if price < MIN_PRICE or price > MAX_PRICE:
                    continue
                if "NG" in title:
                    continue

                # 6. 刊登日期（ec_starttime 等同商品頁 startTime）
                listing_date = None
                try:
                    ts = int(hit.get("ec_starttime") or 0)
                    if ts > 0:
                        listing_date = datetime.utcfromtimestamp(ts)
                except (ValueError, TypeError):
                    listing_date = None

                if listing_date is not None:
                    age_days = (now_utc - listing_date).days
                    if age_days > MAX_LISTING_DAYS:
                        continue

                # 7. 7 天內去重
                if self._is_duplicate(title, price):
                    continue

                url = hit.get("ec_item_url") or ""
                item_id = url.rstrip("/").split("/")[-1] if url else ""

                products.append({
                    "shop": (hit.get("ec_storename") or "雙北店家").strip(),
                    "name": title[:100],
                    "price": price,
                    "url": url or ("https://tw.bid.yahoo.com/item/" + item_id),
                    "keyword": keyword,
                    "item_id": item_id,
                    "location": location,
                    "listing_date": (
                        listing_date.strftime("%Y-%m-%d")
                        if listing_date else "?"
                    ),
                    "source": "雙北同類店家",
                })

        except Exception as e:
            logger.warning("[雙北搜尋] %s 失敗：%s", keyword, e)

        return products

    # ── 主流程 ────────────────────────────────────────────────────
    def run(self):
        logger.info("========== 商品追蹤開始 ==========")
        self._clean_old_state()

        all_products = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        now_utc = datetime.utcnow()

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
                        p["source"] = "固定賣場"
                        shop_products.append(p)
                time.sleep(1.5)

            # 依商品 ID 排序（新→舊，ID 越大越新）
            shop_products.sort(
                key=lambda x: int(x.get("item_id", 0) or 0),
                reverse=True,
            )
            all_products.extend(shop_products)

        # ── 雙北地區同類店家（與點子3C同類的 3C 店鋪）───────────────
        # 以相同 9 大關鍵字搜尋全站，篩出雙北店鋪型 3C 賣家。
        # 搜尋結果已含刊登日期，無需逐筆抓商品頁。
        area_products = []
        seen_ids = set()
        for keyword in KEYWORDS:
            items = self._search_area_stores(keyword)
            for p in items:
                # 同一商品可能命中多個關鍵字，以 item_id 去重
                iid = p.get("item_id") or p["name"]
                if iid in seen_ids:
                    continue
                seen_ids.add(iid)
                area_products.append(p)
            time.sleep(1.0)

        if area_products:
            logger.info("雙北同類店家：找到 %d 筆符合條件的商品", len(area_products))
        else:
            logger.info("雙北同類店家：無符合條件的商品")

        all_products.extend(area_products)

        # 過濾刊登日期：排除刊登超過 MAX_LISTING_DAYS 天的商品
        # 固定賣場商品已依 item ID 排序（新→舊），找到足量近期商品即停止；
        # 雙北商品已在搜尋階段帶入 listing_date，不需再抓商品頁。
        filtered_products = []
        skipped_old = 0
        max_fetch = 60  # 最多查詢 60 個商品頁面，避免過多請求
        fetch_count = 0
        for p in all_products:
            if len(filtered_products) >= MAX_PUSH_ITEMS or fetch_count >= max_fetch:
                break

            # 雙北搜尋已直接取得刊登日期，不重複抓商品頁
            if p.get("listing_date"):
                filtered_products.append(p)
                continue

            fetch_count += 1
            listing_dt = self._fetch_listing_date(p["item_id"])
            if listing_dt is None:
                # 無法取得刊登日期，保留商品（避免誤殺）
                p["listing_date"] = "?"
                filtered_products.append(p)
                continue

            age_days = (now_utc - listing_dt).days
            p["listing_date"] = listing_dt.strftime("%Y-%m-%d")

            if age_days > MAX_LISTING_DAYS:
                logger.info("排除舊商品 [%s] 刊登於 %s（%d天前）",
                            p["name"][:30], p["listing_date"], age_days)
                skipped_old += 1
                # 標記為已發送，避免後續重複檢查
                self._mark_sent(p["name"], p["price"])
            else:
                filtered_products.append(p)

            time.sleep(0.3)  # 避免過快請求

        # 依刊登日期新→舊排序（日期不明者排最後），同日再依 item ID 新→舊
        def _sort_key(x):
            d = x.get("listing_date", "?")
            has_date = 1 if d and d != "?" else 0
            return (
                has_date,
                d if has_date else "",
                int(x.get("item_id", 0) or 0) if str(x.get("item_id", "")).isdigit() else 0,
            )

        all_products = sorted(filtered_products, key=_sort_key, reverse=True)

        if skipped_old > 0:
            logger.info("已排除 %d 筆刊登超過 %d 天的舊商品", skipped_old, MAX_LISTING_DAYS)

        if not all_products:
            self._save_state()
            logger.info("========== 商品追蹤結束（無任何商品）==========")
            return 0

        # 產生推播訊息（HTML 格式，標題使用超連結）
        fixed_count = sum(1 for p in all_products if p.get("source") == "固定賣場")
        area_count = len(all_products) - fixed_count

        lines = [
            "🔍 <b>商品追蹤（%s）</b>" % now_str,
        ]

        lines.append(
            "共找到 %d 筆符合條件的商品（價格 $%s ~ $%s，刊登 %d 天內）"
            % (len(all_products), format(MIN_PRICE, ","),
               format(MAX_PRICE, ","), MAX_LISTING_DAYS)
        )
        lines.append(
            "　固定賣場 %d 筆 ｜ 雙北同類店家 %d 筆" % (fixed_count, area_count)
        )
        lines.append("─" * 30)

        for i, p in enumerate(all_products[:MAX_PUSH_ITEMS], 1):
            safe_title = html.escape(p["name"][:50])
            safe_url = html.escape(p["url"])
            location = (p.get("location") or "").strip()
            loc_part = "  |  📍 %s" % html.escape(location) if location else ""
            lines.append(
                '%d. <b><a href="%s">[%s] %s</a></b>\n'
                "   💰 $%s  |  📅 %s%s  |  關鍵字：%s"
                % (
                    i,
                    safe_url,
                    html.escape(p["shop"]),
                    safe_title,
                    format(p["price"], ",") if p["price"] else "?",
                    html.escape(p.get("listing_date", "?")),
                    loc_part,
                    html.escape(p["keyword"]),
                )
            )
            self._mark_sent(p["name"], p["price"])

        message = "\n".join(lines)
        logger.info("準備推播：雅虎拍賣 %d 筆（固定賣場 %d + 雙北同類店家 %d）",
                    min(len(all_products), MAX_PUSH_ITEMS),
                    fixed_count, area_count)

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
