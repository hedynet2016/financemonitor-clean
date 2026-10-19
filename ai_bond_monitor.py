# -*- coding: utf-8 -*-
"""
AI 相關公司債高殖利率監控（2026-10-20 新增，掛在 08:00 AI 動能觀察區塊）

資料源：BondTerminal（bondterminal.com）
- 主模式（有金鑰）：API v1 `POST /api/v1/bonds/search`，逐一搜尋 AI 相關
  發行人，篩選殖利率（YTW）> 10% 的美元公司債。金鑰為免費自取
  （bondterminal.com/developers），設定於環境變數 BONDTERMINAL_API_KEY。
- 降級模式（無金鑰）：免金鑰 landing 端點
  `GET /api/landing/top-bonds?basis=cable`（7 天/30 天熱門債券榜），
  僅能涵蓋近期熱門標的（例如 2026-10 CoreWeave 信用事件期間 CRWV 全數上榜）。
  熱門榜未涵蓋的發行人會漏掉——此屬已知限制，補上免費金鑰即可完整掃描。

輸出：殖利率超過 10% 的 AI 相關公司（公司名稱＋美股代號＋債券明細）。
"""

import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional

import feedparser
import requests

logger = logging.getLogger(__name__)

# ── 設定 ─────────────────────────────────────────────────────────────
BT_BASE = "https://bondterminal.com/api/v1"
BT_LANDING_URL = "https://bondterminal.com/api/landing/top-bonds"
BT_API_KEY_ENV = "BT_API_KEY"                 # 使用者慣用名稱
BT_API_KEY_ENV_ALT = "BONDTERMINAL_API_KEY"   # 相容別名
YTW_THRESHOLD = 10.0          # 殖利率門檻：>10% 才列出
CDS_THRESHOLD_BPS = 150.0     # CDS 警示門檻：5Y CDS >150bps 才列入警示清單
CDS_MAX_AGE_DAYS = 7          # CDS 新聞時效：僅採近 7 天報導的數字
CACHE_FILE = "ai_bond_cache.json"
CACHE_TTL_HOURS = 12          # 08:00 每日推播，12 小時快取足夠
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN = 5.0           # 節流：保守放慢（免費層限速較嚴，429 時另有 Retry-After）

# ── AI 相關發行人清單（搜尋關鍵字, 美股代號, 顯示名稱）─────────────────
# 內含大型股（債殖利率僅 4~6%，會被 10% 門檻自然濾掉，但保留以利
# 未來信用惡化時及早示警）
AI_BOND_UNIVERSE: List[Dict] = [
    {"q": "CoreWeave",        "ticker": "CRWV", "zh": "CoreWeave（AI 雲）"},
    {"q": "Nebius Group",     "ticker": "NBIS", "zh": "Nebius（AI 雲）"},
    {"q": "Oracle",           "ticker": "ORCL", "zh": "Oracle"},
    {"q": "Core Scientific",  "ticker": "CORZ", "zh": "Core Scientific（AI 算力）"},
    {"q": "Applied Digital",  "ticker": "APLD", "zh": "Applied Digital（AI 資料中心）"},
    {"q": "TeraWulf",         "ticker": "WULF", "zh": "TeraWulf（AI 算力）"},
    {"q": "Cipher Mining",    "ticker": "CIFR", "zh": "Cipher Mining（AI 算力）"},
    {"q": "Hut 8",            "ticker": "HUT",  "zh": "Hut 8（AI 算力）"},
    {"q": "IREN",             "ticker": "IREN", "zh": "IREN（AI 算力）"},
    {"q": "Galaxy Digital",   "ticker": "GLXY", "zh": "Galaxy Digital（AI/加密）"},
    {"q": "Super Micro",      "ticker": "SMCI", "zh": "Super Micro（AI 伺服器）"},
    {"q": "Dell Technologies","ticker": "DELL", "zh": "Dell（AI 伺服器）"},
    {"q": "Tesla",            "ticker": "TSLA", "zh": "Tesla（AI/機器人）"},
    {"q": "Intel",            "ticker": "INTC", "zh": "Intel（AI 晶片）"},
    {"q": "Microsoft",        "ticker": "MSFT", "zh": "Microsoft"},
    {"q": "Meta Platforms",   "ticker": "META", "zh": "Meta"},
    {"q": "Alphabet",         "ticker": "GOOGL", "zh": "Alphabet"},
    {"q": "Amazon.com",       "ticker": "AMZN", "zh": "Amazon"},
    {"q": "Apple",            "ticker": "AAPL", "zh": "Apple"},
    {"q": "NVIDIA",           "ticker": "NVDA", "zh": "NVIDIA"},
    {"q": "IBM",              "ticker": "IBM",  "zh": "IBM（AI 企業服務）"},
    {"q": "Palantir",         "ticker": "PLTR", "zh": "Palantir（AI 軟體）"},
    {"q": "Broadcom",         "ticker": "AVGO", "zh": "Broadcom（AI 晶片）"},
    {"q": "SoftBank Group",   "ticker": "SFTBY", "zh": "SoftBank（AI 投資）"},
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# 進程內快取
_mem_cache: Optional[Dict] = None
_mem_cache_time: Optional[datetime] = None


def _load_file_cache() -> Optional[Dict]:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = datetime.fromisoformat(data.get("fetched_at"))
        age_h = (datetime.now() - ts).total_seconds() / 3600
        if age_h < CACHE_TTL_HOURS:
            return data
    except Exception:
        pass
    return None


def _save_file_cache(data: Dict) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8", newline="") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("[AI_BOND] 快取檔寫入失敗：%s", e)


def _yield_of(row: Dict) -> Optional[float]:
    """取列上最適殖利率：優先 YTW，其次 leadYield／YTM。"""
    for k in ("ytw", "leadYield", "ytm"):
        v = row.get(k)
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _match_issuer(row_issuer: str, keyword: str) -> bool:
    """確認搜尋命中列的 issuer 確實包含關鍵字（避免文字搜尋誤配）。"""
    if not row_issuer:
        return False
    kw_words = [w.lower() for w in keyword.split() if w]
    issuer_l = row_issuer.lower()
    # 關鍵字任一單詞命中即算（"Amazon.com" → "amazon"；"Hut 8" → "hut 8"）
    return any(w in issuer_l for w in kw_words)


def _collect_search_rows(rows: List[Dict], universe: List[Dict]) -> Dict[str, Dict]:
    """把搜尋列依發行人彙整（每公司取 YTW 最高的一筆明細＋全部超標債券）。"""
    companies: Dict[str, Dict] = {}
    for row in rows:
        if row.get("currency") != "USD":
            continue
        if row.get("issuerType") != "corporate":
            continue
        y = _yield_of(row)
        if y is None or y <= YTW_THRESHOLD:
            continue
        for entry in universe:
            if _match_issuer(row.get("issuer", ""), entry["q"]):
                key = entry["ticker"]
                comp = companies.setdefault(key, {
                    "name": entry["zh"], "ticker": entry["ticker"],
                    "bonds": [], "max_ytw": 0.0,
                })
                comp["bonds"].append({
                    "ticker": row.get("ticker", row.get("isin", "")),
                    "coupon": row.get("couponRate"),
                    "maturity": row.get("maturityDate", ""),
                    "price": row.get("price"),
                    "ytw": y,
                })
                comp["max_ytw"] = max(comp["max_ytw"], y)
                break
    return companies


def _fetch_with_key(api_key: str) -> Dict:
    """主模式：逐一搜尋 AI 相關發行人，篩 YTW > 10% 的美元公司債。

    429 時依 Retry-After 等待後重試一次；仍失敗則記錄錯誤繼續下一家
    （免費層每日 200 read units、每分鐘請求數有限，故搜尋排序高風險優先）。
    """
    session = requests.Session()
    session.headers.update(_HEADERS)
    session.headers["Authorization"] = f"Bearer {api_key}"
    all_rows: List[Dict] = []
    errors = []
    for entry in AI_BOND_UNIVERSE:
        payload = {
            "q": entry["q"],
            "filters": {"issuerType": ["corporate"], "ytwMin": YTW_THRESHOLD},
            "page": {"size": 25},
        }
        for attempt in (1, 2):
            try:
                r = session.post(f"{BT_BASE}/bonds/search", json=payload,
                                 timeout=REQUEST_TIMEOUT)
                if r.status_code == 200:
                    rows = r.json().get("rows") or []
                    all_rows.extend(rows)
                    break
                if r.status_code == 429:
                    wait = 60.0
                    try:
                        wait = float(r.headers.get("Retry-After", 60))
                    except (TypeError, ValueError):
                        pass
                    logger.warning("[AI_BOND] %s 429 限流，等待 %.0fs 後重試",
                                   entry["ticker"], wait)
                    time.sleep(min(wait + 1, 90))
                    continue
                errors.append(f"{entry['ticker']}:{r.status_code}")
                logger.warning("[AI_BOND] 搜尋 %s 失敗 HTTP %d",
                               entry["ticker"], r.status_code)
                break
            except Exception as e:
                errors.append(f"{entry['ticker']}:{type(e).__name__}")
                logger.warning("[AI_BOND] 搜尋 %s 異常：%s", entry["ticker"], e)
                break
        time.sleep(SLEEP_BETWEEN)
    companies = _collect_search_rows(all_rows, AI_BOND_UNIVERSE)
    return {
        "available": True, "source": "search", "asof": None,
        "companies": companies, "errors": errors, "note": None,
    }


def _fetch_landing() -> Dict:
    """降級模式：免金鑰熱門榜，比對 AI 發行人代號。"""
    r = requests.get(BT_LANDING_URL, params={"basis": "cable"},
                     headers=_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    ticker_map = {e["ticker"]: e for e in AI_BOND_UNIVERSE}
    companies: Dict[str, Dict] = {}
    seen = set()
    for bucket in ("bonds7d", "bonds30d"):
        for row in data.get(bucket) or []:
            if row.get("issuerType") != "corporate":
                continue
            y = _yield_of(row)
            if y is None or y <= YTW_THRESHOLD:
                continue
            disp = str(row.get("displayName", ""))
            bt_ticker = disp.split()[0] if disp else ""
            entry = ticker_map.get(bt_ticker)
            if not entry:
                continue
            key = row.get("id") or disp
            if key in seen:
                continue
            seen.add(key)
            comp = companies.setdefault(entry["ticker"], {
                "name": entry["zh"], "ticker": entry["ticker"],
                "bonds": [], "max_ytw": 0.0,
            })
            comp["bonds"].append({
                "ticker": disp,
                "coupon": None,
                "maturity": "",
                "price": row.get("price"),
                "ytw": y,
            })
            comp["max_ytw"] = max(comp["max_ytw"], y)
    asof = data.get("viewDataThrough") or data.get("rankingAsOf") or ""
    return {
        "available": True, "source": "landing", "asof": asof,
        "companies": companies, "errors": [],
        "note": "無金鑰降級模式（熱門榜，僅涵蓋近期熱門債券）",
    }


# ── CDS 警示（2026-10-20 新增）─────────────────────────────────────
# 免費無單一公司 CDS 行情 API（ICE/S&P Global/LSEG 皆需授權），故採
# Bing News RSS 查詢式擷取：各 AI 發行人近 7 天報導標題與摘要中的 5Y
# CDS bps 數字（Reuters/Bloomberg/FT 等常報導 CDS 水位），取最高值判斷。
# 註：Google News RSS 的 summary 只有標題重複、無內文數字，且新版文章
# ID 無法解碼轉址，故不用；Bing RSS 摘要含內文片段（實測有 bps 數字）。
_BING_CDS_URL = ("https://www.bing.com/news/search?q={query}&format=rss")
# 「水位」數字：to/at/record/above/past 等接續的 bps；例如
# "widened ... to a record 288 bps"、"explodes above 1,000 basis points"
_CDS_LEVEL_RE = re.compile(
    r'(?:to|at|hit|hits|reached|record of|record close of|close of|above|'
    r'past|over|around|about|near|exceeds|explodes? above|trade[sd]? (?:at|around))\s+'
    r'(?:a\s+record\s+|a\s+|over\s+)?'
    r'([\d,]+(?:\.\d+)?)\s*(?:bp|bps|basis points?)\b', re.I)
# 任意 bps 數字（備援：取最大值）
_CDS_ANY_RE = re.compile(r'([\d,]+(?:\.\d+)?)\s*(?:bp|bps|basis points?)\b', re.I)
# 百分點格式：如 "surged to 2.03 percentage points"（×100 轉 bps）
_CDS_PCT_RE = re.compile(r'([\d.]+)\s*percentage points?\b', re.I)
_CDS_MENTION_RE = re.compile(r'credit default swap|\bCDS\b|default insurance', re.I)
_CDS_MIN, _CDS_MAX = 20.0, 5000.0    # 合理 5Y CDS 範圍（濾掉誤配；IG 公司可低於 100）
# 摘要中公司簡稱對照（q 全名常與新聞簡稱不同）
_CDS_MATCH_OVERRIDE = {
    "Nebius Group": "Nebius", "Dell Technologies": "Dell",
    "Meta Platforms": "Meta", "Amazon.com": "Amazon",
    "SoftBank Group": "SoftBank", "Super Micro": "Super Micro",
}


def _company_segments(text: str, match: str) -> List[str]:
    """取出含公司簡稱的最小文字片段。

    先切句子，再以逗號/分號/and 切分句內片段——摘要常一句併列多家
    公司的 CDS 數字（如 "Oracle CDS around 200 bps, above Nvidia at
    78 bps"），片段級歸屬才能避免誤配。
    """
    sents = re.split(r'(?<=[.!?])\s+', text)
    segs: List[str] = []
    for s in sents:
        segs.extend(re.split(r',|;|\band\b', s, flags=re.I))
    return [s for s in segs if match.lower() in s.lower()]


def _company_scopes(text: str, match: str) -> List[str]:
    """（保留供測試）公司歸屬掃描範圍：片段＋含公司句子。"""
    scopes: List[str] = list(_company_segments(text, match))
    for s in re.split(r'(?<=[.!?])\s+', text):
        if match.lower() in s.lower():
            scopes.append(s)
    return scopes


def _extract_cds_bps(text: str, match: Optional[str] = None,
                     allow_proximity: bool = False,
                     other_names: Optional[List[str]] = None) -> Optional[float]:
    """從新聞文字擷取 CDS 水位（bps）。

    擷取層級（依序嘗試，先命中先回傳）：
    1. 片段歸屬：含公司簡稱的片段內取數字（最嚴謹）。
    2. 鄰近視窗（allow_proximity=True，用於文章全文）：公司名出現位置
       前後 250 字元內的「水位型態」數字；向前視窗若跨過其他公司名則
       截斷，避免把別家數字配給本公司（代名詞指涉如 "the company's
       five-year CDS ... to a record 288 bps" 靠此層命中）。
    3. 無 match 時：全文掃描（水位型態優先，再取最大值）。
    """

    def _scan(scope: str) -> List[float]:
        nums = []
        for rx in (_CDS_LEVEL_RE, _CDS_PCT_RE, _CDS_ANY_RE):
            for m in rx.finditer(scope):
                try:
                    v = float(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                if rx is _CDS_PCT_RE:
                    v *= 100.0
                if _CDS_MIN <= v <= _CDS_MAX:
                    nums.append(v)
            if nums:
                break   # 水位型態已命中即不再取雜訊
        return nums

    if not match:
        nums = _scan(text)
        return max(nums) if nums else None

    # 1) 片段歸屬
    segs = _company_segments(text, match)
    for scope in segs:
        nums = _scan(scope)
        if nums:
            return max(nums)

    # 2) 鄰近視窗（僅全文模式）
    if allow_proximity:
        others = [n.lower() for n in (other_names or []) if n and n != match]
        for m in re.finditer(re.escape(match), text, re.I):
            after = text[m.end():m.end() + 250]
            nums = _scan(after)
            if nums:
                return max(nums)
            before = text[max(0, m.start() - 250):m.start()]
            if others:
                cut = max((before.lower().rfind(n) for n in others),
                          default=-1)
                if cut >= 0:
                    before = before[cut:]
            nums = _scan(before)
            if nums:
                return max(nums)
    return None


def _strip_html(raw: str) -> str:
    """去除 script/style 與標籤，回傳純文字（含 html unescape）。"""
    import html as _html
    txt = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', raw)
    txt = re.sub(r'<[^>]+>', ' ', txt)
    return _html.unescape(txt)


def _resolve_bing_link(link: str) -> str:
    """解碼 Bing apiclick 轉址，取 url= 參數中的原文網址。"""
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        u = (qs.get("url") or [None])[0]
        if u and u.startswith("http"):
            return u
    except Exception:
        pass
    return link


def _fetch_page_cds_bps(url: str, match: str,
                        session: requests.Session,
                        other_names: List[str]) -> Optional[float]:
    """抓文章頁全文，擷取含公司名句子中的 CDS bps（摘要截斷時的備援）。"""
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        # 500KB 上限：Yahoo 等站 HTML 前段多為 JS/JSON，內文較後（實測
        # 200KB 會截掉文章本體）
        text = _strip_html(r.text[:500_000])
        return _extract_cds_bps(text, match, allow_proximity=True,
                                other_names=other_names)
    except Exception as e:
        logger.warning("[AI_CDS] 頁面抓取失敗 %s：%s", url[:60], e)
        return None


_CDS_PAGE_HINT_RE = re.compile(
    r'record|high|surge|spike|jump|widened|bps|basis point', re.I)
_CDS_SKIP_DOMAINS = ("msn.com", "news.google.com")   # JS 頁/轉址頁，抓不到全文


def _fetch_cds_alerts() -> Dict:
    """逐家搜尋 Bing News，擷取 CDS >150bps 的 AI 相關公司。

    擷取順序：RSS 標題＋摘要（片段級歸屬）→ 無數字時抓原文頁（每家
    最多 2 篇）全文擷取（片段→鄰近視窗）。取近 7 天報導中的最高值
    （警示取向）。回傳 {"alerts": [...], "errors": [...], "checked": N}
    """
    alerts: List[Dict] = []
    errors: List[str] = []
    session = requests.Session()
    session.headers.update({"User-Agent": _HEADERS["User-Agent"]})
    now = datetime.now()
    other_names = [(_CDS_MATCH_OVERRIDE.get(e["q"]) or e["q"].split()[0])
                   for e in AI_BOND_UNIVERSE]
    for entry in AI_BOND_UNIVERSE:
        match = _CDS_MATCH_OVERRIDE.get(entry["q"]) or entry["q"].split()[0]
        candidates: List[Dict] = []   # {bps, link, title, published}
        page_budget = 3
        for query in (f'"{match}" credit default swap', f'"{match}" CDS'):
            url = _BING_CDS_URL.format(query=urllib.parse.quote(query))
            try:
                feed = feedparser.parse(url)
            except Exception as e:
                errors.append(f"{entry['ticker']}:{type(e).__name__}")
                logger.warning("[AI_CDS] %s 查詢失敗：%s", entry["ticker"], e)
                continue
            for item in feed.entries[:20]:
                title = item.get("title", "")
                summary = _strip_html(item.get("description", "") or "")
                combined = f"{title}. {summary}"
                pub_raw = item.get("published_parsed")
                pub_str = (item.get("published") or "")[:40]
                link = _resolve_bing_link(item.get("link", ""))
                try:
                    pub_dt = (datetime(*pub_raw[:6]) if pub_raw else None)
                except Exception:
                    pub_dt = None
                if pub_dt and (now - pub_dt).days > CDS_MAX_AGE_DAYS:
                    continue
                if not _CDS_MENTION_RE.search(combined):
                    continue
                bps = _extract_cds_bps(combined, match)
                if (bps is None and page_budget > 0 and link
                        and _CDS_PAGE_HINT_RE.search(title)
                        and not any(d in link for d in _CDS_SKIP_DOMAINS)):
                    page_budget -= 1
                    bps = _fetch_page_cds_bps(link, match, session,
                                              other_names)
                    time.sleep(0.3)
                if bps is not None:
                    candidates.append({"bps": bps, "link": link,
                                       "title": title, "published": pub_str})
        if candidates:
            top = max(candidates, key=lambda c: c["bps"])
            if top["bps"] > CDS_THRESHOLD_BPS:
                alerts.append({
                    "name": entry["zh"], "ticker": entry["ticker"],
                    "bps": top["bps"], "title": top["title"],
                    "link": top["link"], "published": top["published"],
                })
        time.sleep(0.4)   # Bing RSS 節流
        time.sleep(0.4)   # Bing RSS 節流
    alerts.sort(key=lambda a: a["bps"], reverse=True)
    return {"alerts": alerts, "errors": errors,
            "checked": len(AI_BOND_UNIVERSE)}


def format_cds_section(cds: Optional[Dict]) -> str:
    """組出 CDS 警示子區塊（< 一律用 HTML 實體）。"""
    lines = ["", "🛡️ <b>AI 相關公司 CDS 警示（5Y &gt; 150bps）</b>"]
    if cds is None:
        lines.append("⚠️ CDS 資料暫時無法取得")
        return "\n".join(lines) + "\n"
    alerts = cds.get("alerts") or []
    if not alerts:
        lines.append("📭 目前無 CDS 超過 150bps 的 AI 相關公司")
    else:
        for a in alerts:
            bps_str = f"{a['bps']:,.0f} bps"
            if a.get("link"):
                lines.append(
                    f"　🔴 <b>{a['name']}（{a['ticker']}）</b>"
                    f"<a href=\"{a['link']}\">{bps_str}</a>"
                )
            else:
                lines.append(
                    f"　🔴 <b>{a['name']}（{a['ticker']}）</b>{bps_str}"
                )
    lines.append("　📍 來源：Bing News 近 7 天報導（Reuters/Bloomberg/FT 等）")
    if cds.get("errors"):
        lines.append(f"　ℹ️ 查詢失敗：{', '.join(cds['errors'][:5])}")
    return "\n".join(lines) + "\n"


def fetch_ai_high_yield_bonds(force: bool = False) -> Dict:
    """取得殖利率 >10% 的 AI 相關公司債清單（含 12 小時快取）。

    回傳 dict：available/source/asof/companies/errors/note；
    companies 為 {ticker: {name, ticker, bonds:[…], max_ytw}}。
    """
    global _mem_cache, _mem_cache_time
    if not force:
        if _mem_cache and _mem_cache_time and (
            datetime.now() - _mem_cache_time
        ).total_seconds() < CACHE_TTL_HOURS * 3600:
            return _mem_cache
        file_cache = _load_file_cache()
        if file_cache:
            _mem_cache, _mem_cache_time = file_cache, datetime.now()
            return file_cache

    result: Dict
    api_key = (os.environ.get(BT_API_KEY_ENV)
               or os.environ.get(BT_API_KEY_ENV_ALT) or "").strip()
    if api_key:
        try:
            result = _fetch_with_key(api_key)
        except Exception as e:
            logger.error("[AI_BOND] 主模式失敗：%s", e)
            result = {"available": False, "source": "error", "asof": None,
                      "companies": {}, "errors": [str(e)],
                      "note": "主模式失敗，且未降級（已設定金鑰）"}
    else:
        try:
            result = _fetch_landing()
        except Exception as e:
            logger.error("[AI_BOND] 降級模式（熱門榜）失敗：%s", e)
            result = {"available": False, "source": "error", "asof": None,
                      "companies": {}, "errors": [str(e)],
                      "note": "BondTerminal 連線失敗"}

    # ── CDS 警示（同快取週期一起抓，[2026-10-20] 新增）─────────────
    try:
        result["cds"] = _fetch_cds_alerts()
    except Exception as e:
        logger.error("[AI_CDS] CDS 擷取失敗：%s", e)
        result["cds"] = None

    result["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    _mem_cache, _mem_cache_time = result, datetime.now()
    _save_file_cache(result)
    return result


def format_section(ai_bond: Optional[Dict]) -> str:
    """組出 AI 相關公司債子區塊（附加在 AI 動能觀察內）。

    輸出含 <> 一律用 HTML 實體，避免 Telegram HTML 解析失敗。
    """
    lines = ["", "⛓️ <b>AI 相關公司債（殖利率 &gt; 10%）</b>"]
    if not ai_bond or not ai_bond.get("available"):
        note = (ai_bond or {}).get("note") or "資料暫時無法取得"
        lines.append(f"⚠️ {note}")
        return "\n".join(lines) + "\n"

    companies = ai_bond.get("companies") or {}
    if not companies:
        lines.append("📭 目前追蹤清單內無殖利率超過 10% 的 AI 相關公司債")
    else:
        for comp in sorted(companies.values(),
                           key=lambda c: c["max_ytw"], reverse=True):
            best = max(comp["bonds"], key=lambda b: b["ytw"])
            y = best["ytw"]
            bond_label = best.get("ticker") or ""
            price = best.get("price")
            price_str = f"、價格 {float(price):.2f}" if price else ""
            lines.append(
                f"　🔴 <b>{comp['name']}（{comp['ticker']}）</b>"
                f"最高 <b>{y:.2f}%</b>（{bond_label}{price_str}）"
            )
            # 同公司其他超標債券（最多再列 2 筆）
            others = [b for b in comp["bonds"]
                      if (b.get("ticker") or "") != best.get("ticker")]
            for b in sorted(others, key=lambda x: x["ytw"], reverse=True)[:2]:
                lines.append(f"　　　　{b['ticker']}：{b['ytw']:.2f}%")
    src = "BondTerminal" + (
        "（免費熱門榜）" if ai_bond.get("source") == "landing" else ""
    )
    asof = ai_bond.get("asof") or (ai_bond.get("fetched_at") or "")[:10]
    if asof:
        lines.append(f"　📍 資料日期：{asof}｜來源：{src}")
    if ai_bond.get("note"):
        lines.append(f"　ℹ️ {ai_bond['note']}")
    out = "\n".join(lines) + "\n"
    # [2026-10-20] CDS 警示子區塊接在債券子區塊之後
    try:
        out += format_cds_section(ai_bond.get("cds"))
    except Exception as e:
        logger.warning("[AI_CDS] 區塊組裝失敗：%s", e)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = fetch_ai_high_yield_bonds(force=True)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])
    print("-" * 40)
    print(format_section(data))
