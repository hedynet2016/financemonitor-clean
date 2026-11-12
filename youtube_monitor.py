#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""youtube_monitor.py — 推播區塊的 YouTube 影片來源（2026-11-12 新增）

需求（Ben）：
  1. 各推播區塊除文字媒體外，附上 YouTube 相關影片
  2. 觀看次數門檻：**優先 ≥100 萬**；該區塊無百萬影片時自動放寬到 **≥30 萬**
     （實測：近 7 天財經影片僅約 1% 破百萬，若硬性 100 萬多數區塊每天都會空白）
  3. 標題翻成繁體中文 + 附影片超連結

資料取得（雙模式，2026-11-12 起 API 優先）：
  A. 官方 API（環境變數 YOUTUBE_API_KEY 有值時，優先）
     search.list（order=viewCount + publishedAfter，真正依觀看數排序）
     → videos.list（part=statistics,snippet,contentDetails）取 viewCount／頻道／長度
     配額：search.list 每日 100 次（獨立桶）、其他端點合計 10,000 units。
     12 區塊 × 2 查詢 × 1 次 videos.list ≈ 36 units/輪，安全。
  B. 網頁抓取（無 key 或 API 失效時自動退回）
     抓 https://www.youtube.com/results?search_query=…&sp=EgQIBRAB
     解析 `ytInitialData` 的 videoRenderer。
     注意 sp 參數不是嚴格篩選，且無法依觀看數排序 → 命中率較差。

設計：
  - 每區塊 1~2 組查詢字（見 BLOCK_QUERIES），結果合併後去重
  - 門檻兩層：≥100 萬（標 🚀）優先；否則 ≥30 萬；都不到就不顯示該子區塊
  - 同級距內排序：正規財經／新聞媒體（TRUSTED_CHANNELS）優先，其次才看觀看數
    （避免點閱農場的假新聞以高點閱壓過 CNBC／Bloomberg）
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
from datetime import datetime, timedelta, timezone
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

# 官方 API（2026-11-12 起優先）
API_SEARCH_URL = 'https://www.googleapis.com/youtube/v3/search'
API_VIDEOS_URL = 'https://www.googleapis.com/youtube/v3/videos'
API_MAX_RESULTS = 25          # search 每次取 25 支候選（1 次 search = 1 次配額）
API_CHUNK = 50                # videos.list 一次最多 50 個 id
MIN_DURATION_SECS = 61        # 過濾 60 秒內的 Shorts

_quota = {'search': 0, 'other': 0}                    # 本輪 API 用量（僅記錄）
_api_state = {'failed': False, 'reason': ''}           # key 失效時整輪退回網頁模式

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
    'ipo':          ['initial public offering', 'ipo 上市'],
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

# 標題關鍵字驗證（僅對容易誤命的區塊啟用）
# 例：'spacex ipo' 會命中 SpaceX 自家頻道的火箭發射影片（287 萬點閱），與 IPO 無關
BLOCK_KEYWORDS: Dict[str, List[str]] = {
    'ipo': ['ipo', 'i.p.o', 'initial public offering', 'direct listing', 'spac',
            '上市', '掛牌', '首次公開發行', '公開發行', '新股'],
}

# 正規財經／新聞媒體（同級距內優先）——order=viewCount 會把點閱農場的假新聞排到最前面，
# 例：'黃仁勳將辭職？' 300 萬點閱（印度頻道）會被排在新聞台之前。
# 只影響「同一門檻級距內」的先後，不會用 30 萬的影片取代 100 萬的影片。
TRUSTED_CHANNELS = (
    'cnbc', 'bloomberg', 'yahoo finance', 'reuters', 'wall street journal', 'wsj',
    'financial times', 'fox business', 'cnn', 'bbc', 'sky news', 'abc news',
    'nbc', 'cbs', 'associated press', 'ap archive', 'thestreet', "barron's",
    'morningstar', 'marketwatch', 'investopedia', 'motley fool', 'schwab',
    'interactive brokers', 'td ameritrade', 'fidelity', 'vanguard',
    '財訊', '今周刊', '天下雜誌', '商周', '經濟日報', '工商時報', '鉅亨', '非凡',
    '三立', 'tvbs', '東森', '中天', '風傳媒', '先探', 'cmoney', '財經m平方',
    'technews', 'digitimes', '科技新報', '數位時代', '中央社', '公視', '台視', '民視',
    'nikkei', '日經', 'nhk',
)

# ── 快取 ────────────────────────────────────────────────────────────
_cache: Dict[str, Dict] = {}          # key -> {'ts': float, 'items': List[Dict]}
_lock = threading.Lock()
_cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'yt_cache.json')


def enabled() -> bool:
    """環境變數 YOUTUBE_VIDEOS=0 可整體停用"""
    return os.environ.get('YOUTUBE_VIDEOS', '1').strip() not in ('0', 'false', 'False', 'no')


def api_key() -> str:
    return (os.environ.get('YOUTUBE_API_KEY') or '').strip()


def mode() -> str:
    """'api'：走官方 Data API v3；'page'：網頁抓取（無 key 或 key 失效）"""
    return 'api' if api_key() and not _api_state['failed'] else 'page'


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


# ── 官方 API 模式（YOUTUBE_API_KEY 有值時優先）──────────────────────────
def _api_get(url: str, params: Dict) -> Optional[dict]:
    """呼叫 YouTube Data API；key 失效／配額用完時標記本輪退回網頁模式"""
    try:
        r = requests.get(url, params={**params, 'key': api_key()},
                         timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return r.json()
        reason, msg = '', r.text[:120]
        try:
            err = r.json().get('error') or {}
            reason = ((err.get('errors') or [{}])[0].get('reason')
                      or err.get('status') or '')
            msg = err.get('message') or msg
        except Exception:
            pass
        logger.warning(f"[YouTube/API] HTTP {r.status_code} {reason}: {msg[:120]}")
        if reason in ('keyInvalid', 'quotaExceeded', 'dailyLimitExceeded',
                      'ipRefererBlocked', 'accessNotConfigured', 'forbidden'):
            _api_state['failed'] = True
            _api_state['reason'] = f'HTTP {r.status_code} {reason}'
            logger.warning("[YouTube/API] 本輪改用網頁抓取模式")
        return None
    except Exception as e:
        logger.warning(f"[YouTube/API] 請求失敗：{type(e).__name__}: {e}")
        return None


def _search_api(query: str, days: int) -> List[str]:
    """search.list 取影片 id（order=viewCount 才是真正依觀看數排序）"""
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    _quota['search'] += 1
    j = _api_get(API_SEARCH_URL, {
        'part': 'snippet', 'q': query, 'type': 'video',
        'order': 'viewCount', 'publishedAfter': after,
        'maxResults': API_MAX_RESULTS,
    })
    if not j:
        return []
    ids = [(i.get('id') or {}).get('videoId') for i in j.get('items', [])]
    ids = [i for i in ids if i]
    logger.info(f"[YouTube/API] '{query}'（{days} 天內）：{len(ids)} 支候選")
    return ids


def _iso_duration_secs(dur: str) -> int:
    """'PT1H2M3S' → 秒數"""
    if not dur:
        return 0
    m = re.match(r'^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', str(dur))
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _iso_age(published_at: str) -> Optional[float]:
    """'2026-11-10T19:48:24Z' → 距今天數"""
    if not published_at:
        return None
    try:
        dt = datetime.strptime(str(published_at).replace('Z', '+0000'),
                               '%Y-%m-%dT%H:%M:%S%z')
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        return None


def _zh_from_days(days: Optional[float]) -> str:
    if days is None:
        return ''
    if days < 1 / 24:
        return f'{max(1, int(days * 1440))} 分鐘前'
    if days < 1:
        return f'{int(days * 24)} 小時前'
    if days < 7:
        return f'{int(days)} 天前'
    if days < 30:
        return f'{int(days / 7)} 週前'
    if days < 365:
        return f'{int(days / 30)} 個月前'
    return f'{int(days / 365)} 年前'


def _api_to_video(it: dict) -> Optional[Dict]:
    """videos.list 單筆 → 本模組統一結構；直播／Shorts 回 None"""
    try:
        sn = it.get('snippet') or {}
        st = it.get('statistics') or {}
        if sn.get('liveBroadcastContent') in ('live', 'upcoming'):
            return None
        dur = _iso_duration_secs((it.get('contentDetails') or {}).get('duration', ''))
        if dur and dur < MIN_DURATION_SECS:
            return None
        vid = it.get('id') or ''
        title = sn.get('title') or ''
        if not vid or not title:
            return None
        views = int(st.get('viewCount') or 0)
        age = _iso_age(sn.get('publishedAt', ''))
        return {
            'id': vid,
            'title': title,
            'views': views,
            'views_text': f'{views:,} views',
            'published': str(sn.get('publishedAt', ''))[:10],
            'published_zh': _zh_from_days(age),
            'age_days': age,
            'channel': sn.get('channelTitle', ''),
            'live': False,
            'url': f'https://www.youtube.com/watch?v={vid}',
        }
    except Exception:
        return None


def _stats_api(ids: List[str]) -> List[Dict]:
    """videos.list 補觀看次數（search.list 不含 statistics）"""
    out: List[Dict] = []
    for i in range(0, len(ids), API_CHUNK):
        chunk = ids[i:i + API_CHUNK]
        _quota['other'] += 1
        j = _api_get(API_VIDEOS_URL, {
            'part': 'statistics,snippet,contentDetails',
            'id': ','.join(chunk),
        })
        if not j:
            continue
        for it in j.get('items', []):
            v = _api_to_video(it)
            if v:
                out.append(v)
    return out


def _is_trusted(channel: str) -> bool:
    """是否為正規財經／新聞媒體（同級距內排序用）"""
    c = str(channel or '').lower()
    if not c:
        return False
    return any(t in c for t in TRUSTED_CHANNELS)


def _has_keyword(key: str, video: Dict) -> bool:
    """標題／頻道是否含該區塊的必備關鍵字（未設定關鍵字的區塊一律通過）"""
    kws = BLOCK_KEYWORDS.get(key)
    if not kws:
        return True
    text = f"{video.get('title', '')} {video.get('channel', '')}".lower()
    return any(k in text for k in kws)


def _apply_keyword_filter(key: str, items: List[Dict]) -> List[Dict]:
    """套用區塊關鍵字驗證；若全數不符，只留下正規媒體（避免完全無關的影片）"""
    if key not in BLOCK_KEYWORDS:
        return items
    kept = [v for v in items if _has_keyword(key, v)]
    if kept:
        return kept
    trusted = [v for v in items if _is_trusted(v.get('channel'))]
    logger.info(f"[YouTube] '{key}' 關鍵字全數不符，僅保留正規媒體 {len(trusted)} 支")
    return trusted


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
            # 同為百萬級距：正規媒體優先，其次才是觀看數
            return sorted(strong, key=lambda v: (_is_trusted(v.get('channel')), v['views']),
                          reverse=True)
        mid = [v for v in pool if v['views'] >= MIN_VIEWS]
        for v in mid:
            v['tier'] = 'hot'
        return sorted(mid, key=lambda v: (_is_trusted(v.get('channel')), v['views']),
                      reverse=True)

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

    if mode() == 'api':
        merged: Dict[str, Dict] = {}
        ids: List[str] = []
        for q in queries:
            ids.extend(_search_api(q, RECENT_DAYS))
        if ids:
            for v in _stats_api(sorted(set(ids))):
                merged.setdefault(v['id'], v)
        all_items = sorted(merged.values(), key=lambda v: v['views'], reverse=True)
        items = _apply_keyword_filter(key, all_items)
        picked = _select(items)[:MAX_ITEMS]

        # 近 7 天無合格影片 → 放寬到 30 天重搜一次（API 才有 publishedAfter）
        if not picked and mode() == 'api':
            ids2: List[str] = []
            for q in queries:
                ids2.extend(_search_api(q, FALLBACK_DAYS))
            if ids2:
                merged2: Dict[str, Dict] = {}
                for v in _stats_api(sorted(set(ids2))):
                    merged2.setdefault(v['id'], v)
                items2 = _apply_keyword_filter(
                    key, sorted(merged2.values(), key=lambda v: v['views'], reverse=True))
                picked = _select(items2)[:MAX_ITEMS]
        if picked:
            with _lock:
                _cache[key] = {'ts': time.time(), 'items': picked, 'mode': 'api'}
            return picked
        # API 抓到 0 支（可能被擋）→ 繼續往下用網頁模式試一次
        logger.info(f"[YouTube] '{key}' API 無合格影片，改用網頁抓取")

    merged3: Dict[str, Dict] = {}
    for q in queries:
        for v in _search_once(q):
            merged3.setdefault(v['id'], v)
    items3 = _apply_keyword_filter(
        key, sorted(merged3.values(), key=lambda v: v['views'], reverse=True))
    picked = _select(items3)[:MAX_ITEMS]
    with _lock:
        _cache[key] = {'ts': time.time(), 'items': picked, 'mode': 'page'}
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
    logger.info(f"[YouTube] 預抓 {len(todo)} 個區塊（模式：{mode()}）：{', '.join(todo)}")
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            list(ex.map(lambda k: _fetch_block_raw(k), todo))
    except Exception as e:
        logger.warning(f"[YouTube] 預抓失敗：{e}")
    if mode() == 'api':
        logger.info(f"[YouTube] API 配額用量：search {_quota['search']} 次 / "
                    f"其他 {_quota['other']} units")
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
    info: Dict = {'enabled': enabled(), 'block': key, 'mode': mode(),
                  'api_key_set': bool(api_key()),
                  'queries': BLOCK_QUERIES.get(key, []),
                  'thresholds': {'prefer': PREFER_VIEWS, 'min': MIN_VIEWS},
                  'cache_path': _cache_path,
                  'cache_exists': os.path.exists(_cache_path)}
    if not enabled():
        info['error'] = 'YOUTUBE_VIDEOS=0（已停用）'
        return info
    raw: List[Dict] = []
    if mode() == 'api':
        ids: List[str] = []
        for q in info['queries']:
            got = _search_api(q, RECENT_DAYS)
            info.setdefault('per_query', {})[q] = len(got)
            ids.extend(got)
        raw = _stats_api(sorted(set(ids)))
        info['api_failed'] = _api_state['failed']
        if _api_state['reason']:
            info['api_error'] = _api_state['reason']
    else:
        for q in info['queries']:
            got = _search_once(q)
            info.setdefault('per_query', {})[q] = len(got)
            raw.extend(got)
    info['quota_used'] = dict(_quota)
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
