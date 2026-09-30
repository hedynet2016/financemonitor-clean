# -*- coding: utf-8 -*-
"""
投資熱門話題收集模組（[2026-09-30] 區塊：投資熱門話題）

用途:
  08:00 新聞推播「投資熱門話題」區塊的資料收集。

來源（匿名存取、免 API key）:
  Reddit — 美股×AI 相關板（wallstreetbets/stocks/investing/StockMarket/
  artificial/singularity）的 search RSS（sort=top&t=day），
  RSS 排序即當日熱度排名，剔除置頂公告後各板輪流合併取前 N 名

技術評估結論（2026-09-30）:
  - Reddit .json 匿名回 403，但 RSS (.rss) 可匿名（間歇 429，需重試）；
    search RSS 支援 sort=top&t=day 直接回傳當日熱門排序（RSS 無票數欄位，
    僅能以排序代表排名）
  - PTT 於 Render 資料中心 IP 遭全面 403 封鎖（2026-09-30 實測，requests/
    curl/公開代理/Google 跳板皆失敗）→ 使用者決策放棄 PTT 來源
  - Threads（JS 登入殼、無匿名熱門端點）與 X（登入牆、Nitter 已死、API 付費）
    技術上不可行，不納入

使用方式:
    from topics_monitor import fetch_hot_topics
    topics = fetch_hot_topics()
    # {"reddit": [ {title, url, sub}, ... ]}  前 10 名
"""

import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 美股 × AI 相關板（依主題相關性排序，越前面越優先抓取）
REDDIT_SUBS = (
    "wallstreetbets",   # 美股討論主戰場
    "stocks",           # 個股/大盤討論
    "investing",        # 投資綜合
    "StockMarket",      # 大盤動態
    "artificial",       # AI 話題
    "singularity",      # AI 前沿話題
)
REDDIT_TOP_N = 10            # 合併後取前 N 名
REDDIT_PER_SUB = 4           # 每個板先取前 N 名再輪流合併
REDDIT_RETRIES = 4           # RSS 遇 429 的重試次數
REDDIT_RETRY_WAIT = (5, 15, 30)   # 重試間隔秒數

TIMEOUT = 20

# Reddit 置頂公告標題特徵（排除，非熱門話題）
_STICKY_PATTERNS = (
    "daily discussion", "rate my portfolio", "weekly thread",
    "weekend discussion", "megathread", "status check",
)


def _get(url, headers=None, **kw):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    return requests.get(url, headers=h, timeout=TIMEOUT, **kw)


# ── Reddit ─────────────────────────────────────────────────────────

_RD_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_RD_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_RD_LINK = re.compile(r'<link[^>]*href="([^"]+)"')


def _fetch_reddit_sub_rss(sub, retries=REDDIT_RETRIES):
    """單一板當日熱門 RSS（sort=top&t=day），遇 429 重試。"""
    url = ("https://www.reddit.com/r/%s/search/.rss"
           "?q=*&sort=top&t=day&restrict_sr=1" % sub)
    last_err = None
    for i in range(retries):
        try:
            r = _get(url, headers={"Accept": "application/rss+xml"})
            if r.status_code == 200 and r.text.strip():
                return r.text
            last_err = "HTTP %s" % r.status_code
            if r.status_code != 429:
                break
        except Exception as e:
            last_err = str(e)
        if i < retries - 1:
            time.sleep(REDDIT_RETRY_WAIT[min(i, len(REDDIT_RETRY_WAIT) - 1)])
    raise RuntimeError("reddit r/%s RSS 失敗: %s" % (sub, last_err))


def _parse_reddit_rss(rss_text, sub):
    """解析 RSS entries → [{title,url,sub}]（剔除置頂公告）"""
    import html as _html
    out = []
    for entry in _RD_ENTRY.findall(rss_text):
        tm = _RD_TITLE.search(entry)
        lm = _RD_LINK.search(entry)
        if not tm or not lm:
            continue
        title = _html.unescape(tm.group(1)).strip()
        if any(p in title.lower() for p in _STICKY_PATTERNS):
            continue
        url = lm.group(1).split("?")[0]
        out.append({"title": title, "url": url, "sub": sub})
    return out


def fetch_reddit_hot(subs=REDDIT_SUBS, per_sub=REDDIT_PER_SUB,
                     top_n=REDDIT_TOP_N, retries=REDDIT_RETRIES):
    """Reddit 多板當日熱門合併前 N 名（各板輪流取，排序=熱度排名）。

    板間節流 2 秒；板數多時若候選已足（>= top_n + 5）提前停止，
    降低連續請求觸發 429 的機率。
    """
    merged = []
    per_board = {}
    for sub in subs:
        try:
            rss = _fetch_reddit_sub_rss(sub, retries=retries)
            per_board[sub] = _parse_reddit_rss(rss, sub)[:per_sub]
            logger.info("[Topics] Reddit r/%s: %d 篇候選",
                        sub, len(per_board[sub]))
        except Exception as e:
            logger.warning("[Topics] %s", e)
            per_board[sub] = []
        # 候選已足時提前結束，減少後續板請求
        if sum(len(v) for v in per_board.values()) >= top_n + 5:
            break
        time.sleep(2)  # 板間節流，降低 429 機率
    # 輪流合併: 板1#1, 板2#1, ..., 板1#2, 板2#2 ...
    for rank in range(per_sub):
        for sub in subs:
            lst = per_board.get(sub) or []
            if rank < len(lst):
                merged.append(lst[rank])
    return merged[:top_n]


# ── 整合入口 ───────────────────────────────────────────────────────

def fetch_hot_topics():
    """收集 Reddit 美股×AI 當日熱門話題前 10 名。"""
    topics = {"reddit": []}
    try:
        topics["reddit"] = fetch_reddit_hot()
    except Exception as e:
        logger.error("[Topics] Reddit fetch failed: %s", e)
    return topics


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s")
    print(json.dumps(fetch_hot_topics(), ensure_ascii=False, indent=2))
