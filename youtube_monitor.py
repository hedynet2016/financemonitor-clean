#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""youtube_monitor.py — 推播區塊的 YouTube 影片來源（2026-11-12 新增）

需求（Ben）：
  1. 各推播區塊除文字媒體外，附上 YouTube 相關影片
  2. 觀看次數門檻：**優先 ≥100 萬**；該區塊無百萬影片時自動放寬到 **≥30 萬**
     （實測：近 7 天財經影片僅約 1% 破百萬，若硬性 100 萬多數區塊每天都會空白）
  3. 標題翻成繁體中文 + 附影片超連結

資料取得（免 API key）：
  抓 https://www.youtube.com/results?search_query=…&sp=EgQIBRAB（影片 + 本週上傳）
  頁面內的 `ytInitialData` JSON，解析 videoRenderer 的
  title / viewCountText / publishedTimeText / ownerText / videoId。
  → 0 成本、無配額，但屬頁面爬取：Render 若被 Google 攔（429/驗證頁）會靜默降級。

設計：
  - 每區塊 1~2 組查詢字（見 BLOCK_QUERIES），結果合併後去重
  - 門檻兩層：≥100 萬（標 🚀）優先；否則 ≥30 萬；都不到就不顯示該子區塊
  - 依觀看數排序取前 MAX_ITEMS 支
  - 6 小時檔案快取（07:00 與 08:00 兩次推播共用，少一輪請求）
  - 同一則訊息內跨區塊去重（exclude_ids）
  - 停用開關：環境變數 YOUTUBE_VIDEOS=0

對外 API：
  fetch_block(key, translate_fn=None, exclude_ids=None) -> List[Dict]
  format_section(videos, title='YouTube 熱門影片') -> str（HTML，供 Telegram）
  diagnose(key='earnings') -> Dict（線上診斷用）
  BLOCK_KEYS / BLOCK_QUERIES
"""
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional, Set

import requests

logger = logging.getLogger(__name__)

# ── 參數 ────────────────────────────────────────────────────────────
PREFER_VIEWS = 1_000_000      # 第一層：百萬點閱
MIN_VIEWS = 300_000           # 第二層：30 萬點閱
MAX_ITEMS = 2                 # 每區塊最多顯示幾支
CACHE_TTL_SECS = 6 * 3600     # 檔案快取有效期
REQUEST_TIMEOUT = 20
MAX_WORKERS = 4               # 併發抓取（避免被 Google 限流）

SP_THIS_WEEK = 'EgQIBRAB'     # 篩選：僅影片 + 本週上傳（僅作偏向，YouTube 不嚴格）
RECENT_DAYS = 7               # 首選時間窗
FALLBACK_DAYS = 30            # 首選窗內無合格影片時放寬到 30 天
SEARCH_URL = 'https://www.youtube.com/results'

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

# 各推播區塊的查詢字（key 對應 news_monitor 的區塊）
BLOCK_QUERIES: Dict[str, List[str]] = {
    'top_news':     ['stock market today', 'nvidia stock news'],
    'filings_13f':  ['warren buffett stock', 'berkshire hathaway 13f'],
    'earnings':     ['nvidia earnings', 'tsmc earnings'],
    'ipo':          ['spacex ipo', 'ipo stock 2026'],
    'politician':   ['congress stock trades', 'pelosi stock trades'],
    'ai_momentum':  ['ai data center', 'nvidia ai chip'],
    'economic':     ['fed rate cut', 'cpi inflation report'],
    'calendar':     ['fomc meeting', 'fomc rate decision'],
    'sentiment':    ['台股 分析', 'stock market outlook'],
    'hot_topics':   ['wallstreetbets', 'ai stocks to buy'],
    'sox':          ['semiconductor stocks', 'tsmc stock'],
    'events':       ['computex', 'ai conference'],
}

BLOCK_KEYS = tuple(BLOCK_QUERIES.keys())

# ── 快取 ────────────────────────────────────────────────────────────
_cache: Dict[str, Dict] = {}          # key -> {'ts': float, 'items': List[Dict]}
_lock = threading.Lock()
_cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'yt_cache.json')


def enabled() -> bool:
    """環境變數 YOUTUBE_VIDEOS=0 可整體停用"""
    return os.environ.get('YOUTUBE_VIDEOS', '1').strip() not in ('0', 'false', 'False', 'no')


def _load_cache() -> None:
    if _cache:
        return
    try:
        if os.path.exists(_cache_path):
            with open(_cache_path, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                _cache.update({k: v for k, v in data.items() if isinstance(v, dict)})
                logger.info(f"[YouTube] 載入快取 {len(_cache)} 個區塊")
    except Exception as e:
        logger.debug(f"[YouTube] 快取載入失敗（忽略）：{e}")


def _save_cache() -> None:
    try:
        tmp = f'{_cache_path}.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            json.dump(_cache, f, ensure_ascii=False)
        os.replace(tmp, _cache_path)
    except Exception as e:
        logger.debug(f"[YouTube] 快取寫入失敗（忽略）：{e}")


# ── 解析 ────────────────────────────────────────────────────────────
def _parse_views(text: str) -> int:
    """'1,234,567 views' / '1.2M views' / '12萬次' → int"""
    if not text:
        return 0
    raw = str(text).replace(',', '').strip()
    m = re.search(r'([\d.]+)\s*([KMB萬億]?)', raw, re.I)
    if not m:
        return 0
    try:
        num = float(m.group(1))
    except ValueError:
        return 0
    unit = m.group(2).upper()
    mult = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000,
            '萬': 10_000, '億': 100_000_000}.get(unit, 1)
    return int(num * mult)


def _extract_initial_data(html_text: str) -> Optional[dict]:
    if not html_text:
        return None
    for pat in (r'var\s+ytInitialData\s*=\s*(\{.*?\});\s*</script>',
                r'window\["ytInitialData"\]\s*=\s*(\{.*?\});\s*</script>'):
        m = re.search(pat, html_text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
    return None


def _age_days(published_text: str) -> Optional[float]:
    """'4h ago' / '12d ago' / '2w ago' / '1mo ago' / 'Streamed 3mo ago' → 天數"""
    if not published_text:
        return None
    t = str(published_text).lower().replace('streamed', '').replace('premiered', '').strip()
    m = re.search(r'(\d+)\s*(second|minute|hour|day|week|month|year|mo|s|h|d|w|y)s?\s*ago', t)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    factor = {'second': 1 / 86400, 's': 1 / 86400,
              'minute': 1 / 1440, 'hour': 1 / 24, 'h': 1 / 24,
              'day': 1, 'd': 1,
              'week': 7, 'w': 7,
              'month': 30, 'mo': 30,
              'year': 365, 'y': 365}.get(unit, 1)
    return n * factor


def _zh_age(published_text: str) -> str:
    """'3w ago' → '3 週前'；無法解析時回原文"""
    if not published_text:
        return ''
    t = str(published_text).lower().replace('streamed', '').replace('premiered', '').strip()
    m = re.search(r'(\d+)\s*(second|minute|hour|day|week|month|year|mo|s|h|d|w|y)s?\s*ago', t)
    if not m:
        return str(published_text)
    n, unit = m.group(1), m.group(2)
    zh = {'second': '秒', 's': '秒', 'minute': '分鐘', 'hour': '小時', 'h': '小時',
          'day': '天', 'd': '天', 'week': '週', 'w': '週',
          'month': '個月', 'mo': '個月', 'year': '年', 'y': '年'}.get(unit, '')
    return f'{n} {zh}前' if zh else str(published_text)


def _walk_video_renderers(node, out: List[Dict]) -> None:
    if isinstance(node, dict):
        vr = node.get('videoRenderer')
        if isinstance(vr, dict):
            try:
                vid = vr.get('videoId') or ''
                title = ''.join(r.get('text', '') for r in
                                (vr.get('title') or {}).get('runs', [])) or ''
                views_txt = ((vr.get('viewCountText') or {}).get('simpleText')
                             or ''.join(r.get('text', '') for r in
                                        (vr.get('viewCountText') or {}).get('runs', [])))
                published = (vr.get('publishedTimeText') or {}).get('simpleText', '')
                channel = (((vr.get('ownerText') or {}).get('runs') or [{}])[0]).get('text', '')
                live = bool(vr.get('isLiveNow'))
                if vid and title:
                    out.append({
                        'id': vid,
                        'title': title,
                        'views': _parse_views(views_txt),
                        'views_text': views_txt,
                        'published': published,
                        'published_zh': _zh_age(published),
                        'age_days': _age_days(published),
                        'channel': channel,
                        'live': live,
                        'url': f'https://www.youtube.com/watch?v={vid}',
                    })
            except Exception:
                pass
        for v in node.values():
            _walk_video_renderers(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_video_renderers(v, out)


def _search_once(query: str, sp: str = SP_THIS_WEEK) -> List[Dict]:
    """單一關鍵字搜尋（本週上傳），回傳影片清單"""
    try:
        r = requests.get(SEARCH_URL, params={'search_query': query, 'sp': sp},
                         headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.warning(f"[YouTube] '{query}' HTTP {r.status_code}")
            return []
        data = _extract_initial_data(r.text)
        if not data:
            if 'consent' in r.text[:5000].lower():
                logger.warning(f"[YouTube] '{query}' 被導向同意頁（IP 可能受限）")
            else:
                logger.warning(f"[YouTube] '{query}' 無法解析 ytInitialData")
            return []
        out: List[Dict] = []
        _walk_video_renderers(data, out)
        logger.info(f"[YouTube] '{query}': {len(out)} 支影片")
        return [v for v in out if not v['live']]
    except Exception as e:
        logger.warning(f"[YouTube] '{query}' 失敗：{type(e).__name__}: {e}")
        return []


def _select(items: List[Dict]) -> List[Dict]:
    """時間窗 × 觀看門檻的兩層挑選

    1) 先看近 RECENT_DAYS 天內：有 ≥100 萬就取（標 boom），否則取 ≥30 萬（hot）
    2) 近 7 天都不到 30 萬 → 放寬到 FALLBACK_DAYS 天，重跑同一規則
    3) 都不到就回空（該子區塊不顯示）
    """
    def pick(pool: List[Dict]) -> List[Dict]:
        strong = [v for v in pool if v['views'] >= PREFER_VIEWS]
        if strong:
            for v in strong:
                v['tier'] = 'boom'
            return strong
        mid = [v for v in pool if v['views'] >= MIN_VIEWS]
        for v in mid:
            v['tier'] = 'hot'
        return mid

    recent = [v for v in items
              if v.get('age_days') is not None and v['age_days'] <= RECENT_DAYS]
    picked = pick(recent)
    if picked:
        return picked

    older = [v for v in items
             if v.get('age_days') is not None and v['age_days'] <= FALLBACK_DAYS]
    return pick(older)


def _fetch_block_raw(key: str) -> List[Dict]:
    """抓取並快取某區塊（未翻譯）"""
    queries = BLOCK_QUERIES.get(key, [])
    if not queries:
        return []
    merged: Dict[str, Dict] = {}
    for q in queries:
        for v in _search_once(q):
            merged.setdefault(v['id'], v)
    items = sorted(merged.values(), key=lambda v: v['views'], reverse=True)
    picked = _select(items)[:MAX_ITEMS]
    with _lock:
        _cache[key] = {'ts': time.time(), 'items': picked}
    return picked


def prefetch(keys: Iterable[str]) -> None:
    """批次預抓（07:00/08:00 推播前呼叫）；有快取則跳過"""
    if not enabled():
        return
    _load_cache()
    todo = []
    now = time.time()
    for k in keys:
        c = _cache.get(k)
        if c and isinstance(c.get('items'), list) and now - c.get('ts', 0) < CACHE_TTL_SECS:
            continue
        todo.append(k)
    if not todo:
        logger.info("[YouTube] 全部區塊命中快取")
        return
    logger.info(f"[YouTube] 預抓 {len(todo)} 個區塊：{', '.join(todo)}")
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            list(ex.map(lambda k: _fetch_block_raw(k), todo))
    except Exception as e:
        logger.warning(f"[YouTube] 預抓失敗：{e}")
    _save_cache()


def _fmt_views(n: int) -> str:
    if n >= 100_000_000:
        return f'{n / 100_000_000:.1f} 億'
    if n >= 10_000:
        return f'{n / 10_000:.1f} 萬'
    return f'{n:,}'


def fetch_block(key: str,
                translate_fn: Optional[Callable[[str], str]] = None,
                exclude_ids: Optional[Set[str]] = None) -> List[Dict]:
    """取得某區塊的影片（含中文標題）；無資料或停用時回傳 []"""
    if not enabled():
        return []
    _load_cache()
    c = _cache.get(key)
    if not c or not isinstance(c.get('items'), list):
        _fetch_block_raw(key)
        c = _cache.get(key) or {}
    items = list(c.get('items') or [])
    exclude_ids = exclude_ids or set()
    out = []
    for v in items:
        if v['id'] in exclude_ids:
            continue
        title_zh = v['title']
        if translate_fn:
            try:
                t = translate_fn(v['title'])
                if t and any('\u4e00' <= ch <= '\u9fff' for ch in t):
                    title_zh = t
            except Exception:
                pass
        out.append({**v, 'title_zh': title_zh,
                    'published_zh': v.get('published_zh') or _zh_age(v.get('published', ''))})
    return out


def mark_used(videos: List[Dict], exclude_ids: Set[str]) -> None:
    """同一則訊息內跨區塊去重"""
    for v in videos:
        exclude_ids.add(v['id'])


def format_section(videos: List[Dict], title: str = 'YouTube 熱門影片') -> str:
    """格式化為 Telegram HTML 子區塊（無資料回傳空字串）"""
    if not videos:
        return ''
    import html as _html
    lines = [f'📺 <b>{title}</b>\n']
    for v in videos:
        icon = '🚀' if v.get('tier') == 'boom' else '🔥'
        t = _html.escape(v.get('title_zh') or v.get('title') or '', quote=False)
        meta = f"👁 {_fmt_views(v.get('views', 0))}"
        if v.get('channel'):
            meta += f"  ▪  {_html.escape(v['channel'], quote=False)}"
        when = v.get('published_zh') or v.get('published')
        if when:
            meta += f"  •  {_html.escape(when, quote=False)}"
        lines.append(f"{icon} <a href=\"{_html.escape(v['url'])}\">{t}</a>\n   {meta}\n")
    lines.append('\n')
    return ''.join(lines)


def diagnose(key: str = 'earnings') -> Dict:
    """線上診斷：實抓一次指定區塊並回報原始結果（不吃快取）"""
    info: Dict = {'enabled': enabled(), 'block': key,
                  'queries': BLOCK_QUERIES.get(key, []),
                  'thresholds': {'prefer': PREFER_VIEWS, 'min': MIN_VIEWS},
                  'cache_path': _cache_path,
                  'cache_exists': os.path.exists(_cache_path)}
    if not enabled():
        info['error'] = 'YOUTUBE_VIDEOS=0（已停用）'
        return info
    raw: List[Dict] = []
    for q in info['queries']:
        got = _search_once(q)
        info.setdefault('per_query', {})[q] = len(got)
        raw.extend(got)
    uniq = {v['id']: v for v in raw}
    ordered = sorted(uniq.values(), key=lambda v: v['views'], reverse=True)
    info['total_raw'] = len(raw)
    info['unique'] = len(ordered)
    info['over_1m'] = sum(1 for v in ordered if v['views'] >= PREFER_VIEWS)
    info['over_300k'] = sum(1 for v in ordered if v['views'] >= MIN_VIEWS)
    info['within_7d'] = sum(1 for v in ordered
                            if v.get('age_days') is not None and v['age_days'] <= RECENT_DAYS)
    info['top3'] = [{'title': v['title'][:60], 'views': v['views'],
                     'published': v['published'], 'age_days': v.get('age_days'),
                     'channel': v['channel']}
                    for v in ordered[:3]]
    info['picked'] = len(_select(ordered)[:MAX_ITEMS])
    return info


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    for k in ('earnings', 'sox'):
        d = diagnose(k)
        print(f"\n=== {k} ===")
        print(json.dumps(d, ensure_ascii=False, indent=2))
