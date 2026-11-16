# -*- coding: utf-8 -*-
"""
投資熱門話題收集模組（[2026-09-30] 區塊：投資熱門話題）

用途:
  07:00 早鳥新聞推播「投資熱門話題」區塊的資料收集。

來源（匿名存取、免 API key）:
  1. Google News RSS «社群熱議»（site:reddit.com 的華爾街板貼文）
     ＋ «中文財經熱門» 查詢
  2. Reddit 各板當日熱門 RSS —【2026-11-13 起失效，僅保留為備援】
     Reddit 已封鎖匿名存取：RSS/JSON/old.reddit 一律回 403 或 429
     （本機與 Render 皆同），因此改以上述來源為主。

歷史決策:
  - [2026-11-13] Reddit 封鎖後曾新增 Yahoo Finance 熱門搜尋 Top 8 作為主力；
    [2026-11-16] 依用戶指示取消「美股熱門搜尋 Top 8」子區塊並移除相關程式。
  - PTT 於 Render 資料中心 IP 遭全面 403 封鎖 → 使用者決策放棄 PTT 來源
  - Threads（JS 登入殼）與 X（登入牆、API 付費）技術上不可行

使用方式:
    from topics_monitor import fetch_hot_topics
    topics = fetch_hot_topics()
    # {"news":   [{title,url,source}],
    #  "reddit": [{title,url,sub}]}   ← 備援，通常為空
"""

import logging
import re
import time
from html import unescape as html_unescape

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
REDDIT_PER_SUB = 6           # 每個板先取前 N 名再輪流合併（板多遭 429 時，
                             # 成功板單板即可貢獻足夠名額補滿前 10）
REDDIT_RETRIES = 2           # [2026-11-13] 4→2：Reddit 已全面封鎖，不再久等
REDDIT_RETRY_WAIT = (3,)     # 重試間隔秒數（原本 5/15/30 會讓推播延遲數分鐘）

TIMEOUT = 20
REDDIT_TIMEOUT = 10          # Reddit 專用短逾時（封鎖時快速失敗）

# ── Google News RSS（社群熱議＋中文財經）──────────────────────────────
GNEWS_URL = 'https://news.google.com/rss/search'
GNEWS_N = 5                  # 社群／中文財經熱門顯示前 N 名

# Reddit 置頂公告標題特徵（排除，非熱門話題）
_STICKY_PATTERNS = (
    "daily discussion", "rate my portfolio", "weekly thread",
    "weekend discussion", "megathread", "status check",
)


def _get(url, headers=None, **kw):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    kw.setdefault("timeout", TIMEOUT)
    return requests.get(url, headers=h, **kw)


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
            r = _get(url, headers={"Accept": "application/rss+xml"},
                     timeout=REDDIT_TIMEOUT)
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
    blocked = False
    for sub in subs:
        if blocked:
            break                     # 403 = 全站封鎖，不必再試其他板
        try:
            rss = _fetch_reddit_sub_rss(sub, retries=retries)
            per_board[sub] = _parse_reddit_rss(rss, sub)[:per_sub]
            logger.info("[Topics] Reddit r/%s: %d 篇候選",
                        sub, len(per_board[sub]))
        except Exception as e:
            logger.warning("[Topics] %s", e)
            per_board[sub] = []
            if '403' in str(e):
                blocked = True
        # 候選已足時提前結束，減少後續板請求
        if sum(len(v) for v in per_board.values()) >= top_n + 5:
            break
        time.sleep(1)  # 板間節流，降低 429 機率
    # 輪流合併: 板1#1, 板2#1, ..., 板1#2, 板2#2 ...
    for rank in range(per_sub):
        for sub in subs:
            lst = per_board.get(sub) or []
            if rank < len(lst):
                merged.append(lst[rank])
    return merged[:top_n]


# ── Google News RSS（社群熱議＋中文財經）──────────────────────────────
GNEWS_QUERIES = (
    # (查詢字, hl, gl, ceid)
    ('site:reddit.com/r/wallstreetbets', 'en-US', 'US', 'US:en'),
    ('美股 盤勢 焦點', 'zh-TW', 'TW', 'TW:zh-Hant'),
)
_GN_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_GN_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_GN_LINK = re.compile(r"<link>(.*?)</link>", re.S)
_GN_SOURCE = re.compile(r"<source[^>]*>(.*?)</source>", re.S)

# 明顯是炫耀對帳單／純迷因的貼文，不適合放進財經推播
_JUNK_PATTERNS = (
    'i did it', 'i won the game', 'rate my', 'yolo', 'absolute cinematic',
    'daily discussion', 'weekly thread', 'megathread', 'gain porn', 'loss porn',
)


def _is_junk(title):
    t = str(title or '').lower()
    return any(p in t for p in _JUNK_PATTERNS)


def fetch_google_news_hot(n=GNEWS_N):
    """Google News RSS：Reddit 華爾街板貼文＋中文財經熱門（免 key、Render 可連）

    兩個查詢各自取候選後「輪流合併」，避免來源被第一個查詢吃光。
    """
    per_query, seen = [], set()
    for q, hl, gl, ceid in GNEWS_QUERIES:
        bucket = []
        try:
            r = _get(GNEWS_URL, params={'q': q, 'hl': hl, 'gl': gl, 'ceid': ceid},
                     timeout=TIMEOUT)
            if r.status_code != 200:
                logger.warning("[Topics] Google News '%s' HTTP %s", q, r.status_code)
                per_query.append(bucket)
                continue
        except Exception as e:
            logger.warning("[Topics] Google News '%s' 失敗: %s", q, e)
            per_query.append(bucket)
            continue
        for raw in _GN_ITEM.findall(r.text)[:n * 4]:
            tm, lm, sm = (_GN_TITLE.search(raw), _GN_LINK.search(raw),
                          _GN_SOURCE.search(raw))
            if not tm or not lm:
                continue
            title = html_unescape(tm.group(1)).strip()
            source = html_unescape(sm.group(1)).strip() if sm else ''
            if source and title.endswith(' - ' + source):
                title = title[:-(len(source) + 3)]
            key = title.lower()[:60]
            if not title or len(title) < 12 or key in seen or _is_junk(title):
                continue
            seen.add(key)
            bucket.append({'title': title, 'url': lm.group(1).strip(),
                           'source': source})
        logger.info("[Topics] Google News '%s': %d 篇候選", q, len(bucket))
        per_query.append(bucket)

    merged = []
    for rank in range(max((len(b) for b in per_query), default=0)):
        for bucket in per_query:
            if rank < len(bucket):
                merged.append(bucket[rank])
    return merged[:n]


# ── 整合入口 ───────────────────────────────────────────────────────

def fetch_hot_topics():
    """收集投資熱門話題：社群／中文財經新聞（Reddit 為備援）。

    [2026-11-16] 取消 Yahoo 熱門搜尋 Top 8，僅保留 Google News 主來源。
    """
    topics = {"news": [], "reddit": []}
    try:
        topics["news"] = fetch_google_news_hot()
    except Exception as e:
        logger.error("[Topics] Google News 例外: %s", e)
    if not topics["news"]:
        # 主力來源空時，才花時間試 Reddit（大概率已封鎖）
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
