#!/usr/bin/env python3
"""
YouTube 訪談影片追蹤模組（區塊 ⑬:ELON & JENSEN Interview）

搜尋 YouTube 關鍵字「Elon Musk interview」/「Jensen Huang interview」，
篩選條件：
  - 片長超過 20 分鐘
  - 有中文字幕可選（手動 CC 或自動字幕皆可）
  - 觀賞人次成長最快（以前次執行快照計算成長率；首次執行改用
    觀看數/上架時數 作為替代排名，並於推播中註明）

技術方案：yt-dlp（免 API key）
  - 第一階段:ytsearch 平面搜尋(含 duration/view_count/timestamp)預篩
  - 第二階段:對候選影片抓完整資訊,取得 subtitles/automatic_captions
    檢查中文字幕(zh 開頭語系,含 zh-Hant/zh-Hans/zh-TW)
  - 觀看數快照存於 youtube_growth.json,供跨日成長率計算

使用方法:
  from youtube_monitor import fetch_top_interviews
  vids = fetch_top_interviews()
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── 設定 ──────────────────────────────────────────────────────────────
SEARCH_QUERIES = ["Elon Musk interview", "Jensen Huang interview"]
MIN_DURATION_SEC = 1200        # 片長須超過 20 分鐘
SEARCH_RESULTS = 40            # 每個關鍵字平面搜尋筆數
FULL_FETCH_LIMIT = 12          # 每個關鍵字進入第二階段(完整資訊)的候選上限
TOP_N = 3                      # 推播前 N 名
GROWTH_FILE = "youtube_growth.json"
SNAPSHOT_MAX_AGE_DAYS = 21     # 快照保留天數
SNAPSHOT_MIN_HOURS = 4         # 計算成長率的最小快照間隔

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _yt_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "http_headers": {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
    }
    if extra:
        opts.update(extra)
    return opts


def _zh_subtitle_type(info):
    """檢查是否有中文字幕。回傳 'CC' / 'auto' / None"""
    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    for k in subs:
        if k.lower().startswith("zh"):
            return "CC"
    for k in autos:
        if k.lower().startswith("zh"):
            return "auto"
    return None


def _fmt_upload_date(raw):
    """'20260920' → '2026-09-20';無法解析時原樣回傳"""
    raw = raw or ""
    if len(raw) == 8 and raw.isdigit():
        return "%s-%s-%s" % (raw[:4], raw[4:6], raw[6:])
    return raw


def _flat_search(query, n=SEARCH_RESULTS):
    """第一階段:平面搜尋,回傳 entries(含 duration/view_count/timestamp)"""
    import yt_dlp
    with yt_dlp.YoutubeDL(_yt_opts({"extract_flat": "in_playlist"})) as ydl:
        r = ydl.extract_info("ytsearch%d:%s" % (n, query), download=False)
    return list(r.get("entries") or [])


def _full_info(video_id):
    """第二階段:抓單一影片完整資訊(字幕/觀看數)"""
    import yt_dlp
    url = "https://www.youtube.com/watch?v=%s" % video_id
    with yt_dlp.YoutubeDL(_yt_opts()) as ydl:
        return ydl.extract_info(url, download=False)


def _load_growth():
    try:
        if os.path.exists(GROWTH_FILE):
            with open(GROWTH_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("[YT] 快照讀取失敗,將重建:%s", e)
    return {}


def _save_growth(snap):
    try:
        with open(GROWTH_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning("[YT] 快照寫入失敗:%s", e)


def fetch_top_interviews(queries=None, top_n=TOP_N,
                         per_query_limit=FULL_FETCH_LIMIT,
                         quick=False):
    """抓取並排名訪談影片。

    Args:
        queries: 搜尋關鍵字清單,預設 SEARCH_QUERIES
        top_n: 回傳前 N 名
        per_query_limit: 每個關鍵字進入第二階段的候選上限
            (診斷端點可用較小值縮短耗時)
        quick: True 時僅執行第一階段平面搜尋(不抓字幕/不更新快照),
            供診斷端點快速確認 Render 對 YouTube 的連線能力

    Returns:
        list[dict]: 每筆含 title / title_zh(由呼叫端填) / url / video_id /
        channel / duration_min / view_count / upload_date / growth /
        growth_hours / subtitle_type / keywords / rate(次/小時)
    """
    if queries is None:
        queries = SEARCH_QUERIES

    now = time.time()
    snap = _load_growth()
    prev_snap = json.loads(json.dumps(snap))  # 深拷貝作為本輪基準

    candidates = {}
    for q in queries:
        try:
            entries = _flat_search(q)
        except Exception as e:
            logger.error("[YT] 平面搜尋失敗 [%s]:%s", q, e)
            continue
        logger.info("[YT] %s:平面搜尋 %d 筆", q, len(entries))

        # 預篩:片長 > 20 分鐘,依 觀看數/上架時數 排序取候選
        pool = []
        for e in entries:
            dur = e.get("duration") or 0
            if dur <= MIN_DURATION_SEC:
                continue
            views = e.get("view_count") or 0
            ts = e.get("timestamp") or 0
            hours = max((now - ts) / 3600.0, 1.0) if ts else 1.0
            pool.append((views / hours, e))
        pool.sort(key=lambda x: x[0], reverse=True)

        picked = 0
        for _, e in pool:
            vid = e.get("id")
            if not vid or vid in candidates:
                continue
            if quick:
                candidates[vid] = {
                    "video_id": vid,
                    "title": e.get("title") or "",
                    "url": "https://www.youtube.com/watch?v=%s" % vid,
                    "channel": e.get("channel") or "",
                    "duration_min": round((e.get("duration") or 0) / 60),
                    "view_count": e.get("view_count") or 0,
                    "keywords": [q],
                }
            else:
                # 暫存平面欄位,第二階段補齊
                candidates[vid] = {
                    "video_id": vid,
                    "title": e.get("title") or "",
                    "url": "https://www.youtube.com/watch?v=%s" % vid,
                    "channel": e.get("channel") or "",
                    "duration_min": round((e.get("duration") or 0) / 60),
                    "view_count": e.get("view_count") or 0,
                    "timestamp": e.get("timestamp") or 0,
                    "keywords": [q],
                    "_need_full": True,
                }
            picked += 1
            if picked >= per_query_limit:
                break
        logger.info("[YT] %s:片長>20min 候選 %d 筆", q, picked)

    if quick:
        logger.info("[YT] quick 模式:共 %d 筆候選(未抓字幕)", len(candidates))
        return list(candidates.values())

    # ── 第二階段:完整資訊(字幕檢查) ─────────────────────────────
    passed = []
    for vid, c in candidates.items():
        if not c.pop("_need_full", False):
            passed.append(c)
            continue
        try:
            info = _full_info(vid)
        except Exception as e:
            logger.warning("[YT] 完整資訊失敗 %s:%s", vid, e)
            continue
        zh = _zh_subtitle_type(info)
        if not zh:
            logger.info("[YT] 排除(無中文字幕):%s", (info.get("title") or "")[:40])
            continue
        c.update({
            "title": info.get("title") or c["title"],
            "channel": info.get("channel") or info.get("uploader") or c["channel"],
            "view_count": info.get("view_count") or c["view_count"],
            "duration_min": round((info.get("duration") or 0) / 60),
            "upload_date": _fmt_upload_date(info.get("upload_date")),
            "timestamp": info.get("timestamp") or c.get("timestamp") or 0,
            "subtitle_type": zh,
        })
        passed.append(c)
        time.sleep(0.5)  # 禮貌性間隔

    logger.info("[YT] 中文字幕篩選後:%d 筆", len(passed))

    # ── 成長率計算(以前次快照為基準) ────────────────────────────
    for c in passed:
        vid = c["video_id"]
        views = c["view_count"]
        prev = prev_snap.get(vid)
        growth, ghours = None, None
        if prev and isinstance(prev.get("views"), int):
            hours = (now - prev.get("ts", 0)) / 3600.0
            if hours >= SNAPSHOT_MIN_HOURS:
                growth = views - prev["views"]
                ghours = hours
        c["growth"] = growth
        c["growth_hours"] = ghours
        ts_up = c.get("timestamp") or 0
        hours_up = max((now - ts_up) / 3600.0, 6.0) if ts_up else 720.0
        c["rate"] = (growth / ghours) if (growth is not None and ghours) \
            else (views / hours_up)
        c["rate_is_growth"] = growth is not None

        # 更新快照
        snap[vid] = {"views": views, "ts": now, "title": c["title"][:80]}

    # 清理過期快照
    cutoff = now - SNAPSHOT_MAX_AGE_DAYS * 86400
    for k in list(snap.keys()):
        if snap[k].get("ts", 0) < cutoff:
            del snap[k]
    _save_growth(snap)

    # ── 排名:有成長率者優先(依 次/小時),其餘以觀看速度遞補 ──────
    with_growth = sorted(
        [c for c in passed if c["rate_is_growth"]],
        key=lambda x: x["rate"], reverse=True)
    without = sorted(
        [c for c in passed if not c["rate_is_growth"]],
        key=lambda x: x["rate"], reverse=True)
    ranked = with_growth + without
    ranked = ranked[:top_n]

    first_run = not any(c["rate_is_growth"] for c in ranked)
    for c in ranked:
        c["first_run"] = first_run
        c["keywords"] = list(dict.fromkeys(c.get("keywords") or []))

    mode = "成長率(次/小時)" if not first_run else "首次執行:觀看數/上架時數"
    logger.info("[YT] 排名完成(%s),前 %d 名:%s", mode, len(ranked),
                [c["title"][:30] for c in ranked])
    return ranked


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    for i, v in enumerate(fetch_top_interviews(), 1):
        print("\n#%d %s" % (i, v["title"]))
        print("   %s | %s | %d 分鐘 | 字幕:%s" % (
            v["channel"], v["url"], v["duration_min"], v["subtitle_type"]))
        print("   觀看 %s | growth %s (前次快照 %.1f 小時前) | rate %.0f/h" % (
            format(v["view_count"], ","),
            format(v["growth"], ",") if v["growth"] is not None else "N/A",
            v["growth_hours"] or -1, v["rate"]))
