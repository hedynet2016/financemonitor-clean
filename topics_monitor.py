# -*- coding: utf-8 -*-
"""
投資熱門話題收集模組（[2026-09-30] 區塊：投資熱門話題）

用途:
  08:00 新聞推播「投資熱門話題」區塊的資料收集。

來源（皆為匿名存取、免 API key）:
  1. PTT Stock 板    — 抓近幾頁文章列表，取「今日」（台北時間）推文數前 N 名
  2. Reddit          — r/wallstreetbets + r/stocks 的 search RSS（sort=top&t=day），
                       RSS 排序即當日熱度排名，剔除置頂公告後取前 N 名

技術評估結論（2026-09-30）:
  - PTT 匿名 GET 可行（HTTP 200，HTML 含標題/推文數/日期/連結）
  - Reddit .json 匿名回 403，但 RSS (.rss) 可匿名（間歇 429，需重試）；
    search RSS 支援 sort=top&t=day 直接回傳當日熱門排序（RSS 無票數欄位，
    僅能以排序代表排名）
  - Threads（JS 登入殼、無匿名熱門端點）與 X（登入牆、Nitter 已死、API 付費）
    技術上不可行，不納入

使用方式:
    from topics_monitor import fetch_hot_topics
    topics = fetch_hot_topics()
    # {"ptt": [ {title, url, push, date}, ... ],
    #  "reddit": [ {title, url, sub}, ... ]}
"""

import logging
import re
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PTT_BOARD = "Stock"
PTT_PAGES = 5                # 抓取頁數（每頁約 22 篇，5 頁足以涵蓋當日）
PTT_TOP_N = 3                # PTT 取前 N 名

REDDIT_SUBS = ("wallstreetbets", "stocks")
REDDIT_TOP_N = 3             # Reddit 合併後取前 N 名
REDDIT_PER_SUB = 5           # 每個板先取前 N 名再輪流合併
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


# ── PTT ────────────────────────────────────────────────────────────

_PT_ENT = re.compile(
    r'<div class="title">\s*(?:<a href="([^"]+)">([^<]+)</a>|([^<]*))')
_PT_PUSH = re.compile(r'<span class="hl f[1-4]">([^<]*)</span>')
_PT_DATE = re.compile(r'<div class="date">([^<]+)</div>')
_PT_PREV = re.compile(r'<a class="btn wide" href="(/bbs/\w+/index\d+\.html)">[^<]*上頁')


def _parse_ptt_page(html):
    """解析單頁文章列表 → [{title,url,push,date}]（已刪文略過）"""
    items = []
    for block in re.split(r'<div class="r-ent">', html)[1:]:
        m = _PT_ENT.search(block)
        if not m:
            continue
        href, t_link, t_plain = m.groups()
        title = (t_link or t_plain or "").strip()
        if not href or not title:
            continue  # 已刪除/未核准文章
        pm = _PT_PUSH.search(block)
        raw_push = (pm.group(1).strip() if pm else "")
        if raw_push == "爆":
            push = 100
        elif raw_push.isdigit():
            push = int(raw_push)
        else:
            push = 0
        dm = _PT_DATE.search(block)
        items.append({
            "title": title,
            "url": "https://www.ptt.cc" + href,
            "push": push,
            "date": dm.group(1).strip() if dm else "",
        })
    return items


def fetch_ptt_hot(board=PTT_BOARD, pages=PTT_PAGES, top_n=PTT_TOP_N):
    """PTT 板「今日」推文數前 N 名（台北時間）。無今日文章時改用已抓取全部文章。"""
    from pytz import timezone
    now = datetime.now(timezone("Asia/Taipei"))
    today = "%d/%d" % (now.month, now.day)

    url = "https://www.ptt.cc/bbs/%s/index.html" % board
    all_items = []
    for _ in range(pages):
        r = _get(url)
        r.raise_for_status()
        all_items.extend(_parse_ptt_page(r.text))
        prev = _PT_PREV.search(r.text)
        if not prev:
            break
        url = "https://www.ptt.cc" + prev.group(1)
        time.sleep(0.5)  # 禮貌性節流

    todays = [it for it in all_items if it["date"] == today]
    todays.sort(key=lambda x: x["push"], reverse=True)
    top = todays[:top_n]
    if len(top) < top_n:
        # 今日文章不足（如清晨）時以近幾日熱門補足
        rest = sorted(all_items, key=lambda x: x["push"], reverse=True)
        for it in rest:
            if len(top) >= top_n:
                break
            if it not in top:
                top.append(it)
    logger.info("[Topics] PTT %s: 今日(%s) %d 篇 / 共抓 %d 篇，取前 %d",
                board, today, len(todays), len(all_items), len(top))
    return top


# ── Reddit ─────────────────────────────────────────────────────────

_RD_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_RD_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_RD_LINK = re.compile(r'<link[^>]*href="([^"]+)"')


def _fetch_reddit_sub_rss(sub):
    """單一板當日熱門 RSS（sort=top&t=day），遇 429 重試。"""
    url = ("https://www.reddit.com/r/%s/search/.rss"
           "?q=*&sort=top&t=day&restrict_sr=1" % sub)
    last_err = None
    for i in range(REDDIT_RETRIES):
        try:
            r = _get(url, headers={
                "User-Agent": UA, "Accept": "application/rss+xml"})
            if r.status_code == 200 and r.text.strip():
                return r.text
            last_err = "HTTP %s" % r.status_code
            if r.status_code != 429:
                break
        except Exception as e:
            last_err = str(e)
        if i < REDDIT_RETRIES - 1:
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
                     top_n=REDDIT_TOP_N):
    """Reddit 多板當日熱門合併前 N 名（各板輪流取，排序=熱度排名）。"""
    merged = []
    per_board = {}
    for sub in subs:
        try:
            rss = _fetch_reddit_sub_rss(sub)
            per_board[sub] = _parse_reddit_rss(rss, sub)[:per_sub]
            logger.info("[Topics] Reddit r/%s: %d 篇候選",
                        sub, len(per_board[sub]))
        except Exception as e:
            logger.warning("[Topics] %s", e)
            per_board[sub] = []
        time.sleep(2)  # 板間節流，降低 429 機率
    # 輪流合併: wsb#1, stocks#1, wsb#2, stocks#2 ...
    for rank in range(per_sub):
        for sub in subs:
            lst = per_board.get(sub) or []
            if rank < len(lst):
                merged.append(lst[rank])
    return merged[:top_n]


# ── 整合入口 ───────────────────────────────────────────────────────

def fetch_hot_topics():
    """收集 PTT + Reddit 熱門話題。單一來源失敗不影響另一來源。"""
    topics = {"ptt": [], "reddit": []}
    try:
        topics["ptt"] = fetch_ptt_hot()
    except Exception as e:
        logger.error("[Topics] PTT fetch failed: %s", e)
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
