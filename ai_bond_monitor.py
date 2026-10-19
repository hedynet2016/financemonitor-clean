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
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ── 設定 ─────────────────────────────────────────────────────────────
BT_BASE = "https://bondterminal.com/api/v1"
BT_LANDING_URL = "https://bondterminal.com/api/landing/top-bonds"
BT_API_KEY_ENV = "BONDTERMINAL_API_KEY"
YTW_THRESHOLD = 10.0          # 殖利率門檻：>10% 才列出
CACHE_FILE = "ai_bond_cache.json"
CACHE_TTL_HOURS = 12          # 08:00 每日推播，12 小時快取足夠
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN = 0.3           # 有金鑰時逐一搜尋的節流（限速 60 req/min）

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
    """主模式：逐一搜尋 AI 相關發行人，篩 YTW > 10% 的美元公司債。"""
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
        try:
            r = session.post(f"{BT_BASE}/bonds/search", json=payload,
                             timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                rows = r.json().get("rows") or []
                all_rows.extend(rows)
            else:
                errors.append(f"{entry['ticker']}:{r.status_code}")
                logger.warning("[AI_BOND] 搜尋 %s 失敗 HTTP %d",
                               entry["ticker"], r.status_code)
        except Exception as e:
            errors.append(f"{entry['ticker']}:{type(e).__name__}")
            logger.warning("[AI_BOND] 搜尋 %s 異常：%s", entry["ticker"], e)
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
    api_key = os.environ.get(BT_API_KEY_ENV, "").strip()
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
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = fetch_ai_high_yield_bonds(force=True)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])
    print("-" * 40)
    print(format_section(data))
