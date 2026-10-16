#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sentiment_index.py — 自訂多空評分指標（2026-10-16 新增）

使用者自訂的五項指標，每項 5 / 10 / 15 分，合計 25 ~ 75 分（基準 50 分）：

  1. 最新美股表現        市值前 20 大公司平均漲跌幅
                         跌 > 3% → 5 分；漲 > 3% → 15 分；其他 → 10 分
  2. 最新美國 CPI 表現   實際 vs 市場預期
                         超出預期 → 5 分；低於預期 → 15 分；無最新數據 → 10 分
  3. 最新美國非農就業    實際 vs 市場預期（同上）
  4. 最新國際油價表現    Brent 布蘭特原油單日漲跌幅（Yahoo BZ=F）
                         漲 > 3% → 5 分（超出預期，通膨壓力，偏空）
                         跌 > 3% → 15 分（低於預期，偏多）；其他 → 10 分
  5. 前一日台股表現      加權指數漲跌點數
                         漲 > 500 點 → 5 分；跌 > 500 點 → 15 分；其他 → 10 分

判定：合計 > 60 → 建議做多 🔴；< 50 → 建議做空 🟢；50 ~ 60 → 觀望 ⚪
比對：11:30 以台指期（TXF）漲跌比對方向命中率（見 format_hitrate_section）

資料來源（皆免 API key）：
  - 美股市值前 20 大： Yahoo predefined screener（most_actives 取 250 檔後依市值排序）
  - CPI / 非農：        TradingView 經濟行事曆（含 forecast / actual）
  - 布蘭特油價：        Yahoo BZ=F 日線（最新收盤 vs 前一交易日漲跌幅）
  - 前一日台股：        證交所 FMTQIK（發行量加權股價指數＋漲跌點數），Yahoo ^TWII 備援
  - WTI 油價參考：      Yahoo CL=F（僅供顯示，不計分）
"""

import html
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

logger = logging.getLogger(__name__)

TPE = ZoneInfo('Asia/Taipei') if ZoneInfo else timezone(timedelta(hours=8))

# ── 常數 ────────────────────────────────────────────────────────────
STATE_FILE = 'sentiment_index_state.json'
SCORE_BEAR, SCORE_NEUTRAL, SCORE_BULL = 5, 10, 15
BASE_TOTAL = 50                  # 中性基準（僅用於顯示）
BULL_THRESHOLD = 60              # 總分 > 60 → 建議做多
BEAR_THRESHOLD = 50              # 總分 < 50 → 建議做空；50 ~ 60 → 觀望
TOTAL_MAX = 75
SENTI_MAX_AGE_DAYS = 45          # CPI / 非農：約 1.5 個月，確保月度指標下次公布前仍有效
HISTORY_MAX = 60                 # 命中率歷史保留筆數
HIT_STATS_N = 10                 # 顯示近 N 次命中率
HIT_BLOCK = 23                   # 11:30（block = hour*2 + minute//30）
US_TOP_N = 20

MOVEMENT_THRESHOLD_PCT = 3.0     # 美股前 20 大平均漲跌幅門檻（%）
OIL_MOVEMENT_THRESHOLD_PCT = 3.0 # Brent 布蘭特油價單日漲跌幅門檻（%）
TAIEX_THRESHOLD_PTS = 500.0      # 前一日台股漲跌門檻（點）

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
_HEADERS = {'User-Agent': _UA, 'Accept': 'application/json, text/plain, */*'}

TV_CAL_URL = 'https://economic-calendar.tradingview.com/events'
YAHOO_SCREENER_URL = 'https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved'
YAHOO_SPARK_URL = 'https://query1.finance.yahoo.com/v8/finance/spark'
TWSE_FMTQIK_URL = 'https://www.twse.com.tw/exchangeReport/FMTQIK'

# 同一公司多種股票代號：保留左邊（市值較大者）為代表，避免重複計權
SHARE_CLASS_DUP = {'GOOG': 'GOOGL', 'FOX': 'FOXA', 'NWS': 'NWSA'}

# TradingView 事件標題偏好（依序取第一個命中；採精確比對避免誤抓子項）
CPI_TITLES = ['CPI y/y', 'CPI m/m', 'CPI']
NFP_TITLES = ['Non Farm Payrolls', 'Nonfarm Payrolls', 'Non-Farm Payrolls']


# ── 通用工具 ────────────────────────────────────────────────────────
def now_tpe() -> datetime:
    return datetime.now(TPE)


def _num(v) -> Optional[float]:
    """把 '0.2%' / '90K' / 336.94 之類的值轉成 float，失敗回 None"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(',', '').replace('%', '').replace(' ', '')
    if not s or s in ('-', '—', 'N/A', 'n/a'):
        return None
    mult = 1.0
    tail = s[-1:]
    if tail in ('K', 'k'):
        mult, s = 1e3, s[:-1]
    elif tail in ('M', 'm'):
        mult, s = 1e6, s[:-1]
    elif tail in ('B', 'b'):
        mult, s = 1e9, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _fmt_num(v: Optional[float]) -> str:
    if v is None:
        return '—'
    if v == int(v) and abs(v) >= 100:
        return f'{int(v):,}'
    return f'{v:g}'


def _score_label(score: int) -> str:
    return {SCORE_BULL: '15 分', SCORE_NEUTRAL: '10 分', SCORE_BEAR: '5 分'}.get(score, f'{score} 分')


# ── 1. 美股市值前 20 大 ─────────────────────────────────────────────
def fetch_us_top20() -> Dict:
    """取美股市值前 20 大公司的最新單日漲跌幅，回傳平均漲跌幅"""
    out = {'avg': None, 'items': [], 'source': None, 'error': None, 'count': 0}
    try:
        resp = requests.get(
            YAHOO_SCREENER_URL,
            params={'formatted': 'false', 'lang': 'en-US', 'region': 'US',
                    'scrIds': 'most_actives', 'count': '250'},
            headers={'User-Agent': _UA}, timeout=25)
        resp.raise_for_status()
        results = resp.json().get('finance', {}).get('result') or []
        if not results:
            out['error'] = 'Yahoo screener 回傳空結果'
            return out
        quotes = results[0].get('quotes') or []
    except Exception as e:
        out['error'] = f'Yahoo screener {type(e).__name__}: {e}'
        return out

    rows = []
    for q in quotes:
        if q.get('quoteType') != 'EQUITY':
            continue
        cap = q.get('marketCap') or 0
        chg = q.get('regularMarketChangePercent')
        if cap <= 0 or chg is None:
            continue
        rows.append({'symbol': str(q.get('symbol', '')).upper(),
                     'name': q.get('shortName') or q.get('symbol'),
                     'cap': float(cap), 'chg': float(chg)})
    if not rows:
        out['error'] = 'Yahoo screener 無有效市值資料'
        return out

    rows.sort(key=lambda r: r['cap'], reverse=True)
    picked, seen = [], set()
    for r in rows:
        primary = SHARE_CLASS_DUP.get(r['symbol'], r['symbol'])
        if primary in seen:
            continue
        seen.add(primary)
        picked.append(r)
        if len(picked) >= US_TOP_N:
            break

    out['items'] = picked
    out['count'] = len(picked)
    out['avg'] = sum(r['chg'] for r in picked) / len(picked)
    out['source'] = f'Yahoo Finance screener（市值前 {len(picked)} 大）'
    return out


def score_us_top20(data: Dict) -> Tuple[int, str]:
    avg = (data or {}).get('avg')
    if avg is None:
        return SCORE_NEUTRAL, '無法取得美股資料（視為無最新數據）'
    if avg < -MOVEMENT_THRESHOLD_PCT:
        return SCORE_BEAR, f'市值前 20 大平均跌幅 {avg:.2f}%（跌逾 3%）'
    if avg > MOVEMENT_THRESHOLD_PCT:
        return SCORE_BULL, f'市值前 20 大平均漲幅 {avg:.2f}%（漲逾 3%）'
    return SCORE_NEUTRAL, f'市值前 20 大平均漲跌 {avg:+.2f}%（未達 ±3%）'


# ── 2/3/4. 經濟事件（實際 vs 市場預期）────────────────────────────
def _tv_events(days_back: int = 75, days_fwd: int = 2) -> List[Dict]:
    """TradingView 經濟行事曆（美國），含 forecast / actual"""
    now = datetime.now(timezone.utc)
    resp = requests.get(
        TV_CAL_URL,
        params={'from': (now - timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                'to': (now + timedelta(days=days_fwd)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                'countries': 'US'},
        headers={'User-Agent': _UA, 'Accept': 'application/json',
                 'Origin': 'https://www.tradingview.com',
                 'Referer': 'https://www.tradingview.com/'},
        timeout=25)
    resp.raise_for_status()
    return resp.json().get('result') or []


def _parse_event_date(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _pick_event(events: List[Dict], titles: List[str], max_age_days: int) -> Optional[Dict]:
    """在 max_age_days 內取最近一次符合標題的已公布事件"""
    now = datetime.now(timezone.utc)
    limit = now - timedelta(days=max_age_days)
    cands = []
    for e in events:
        title = str(e.get('title', '')).strip()
        if title not in titles:
            continue
        if str(e.get('currency', '')).upper() != 'USD':
            continue
        dt = _parse_event_date(e.get('date'))
        if dt is None or dt > now or dt < limit:
            continue
        cands.append((dt, e))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    dt, ev = cands[-1]
    return {'title': ev.get('title'), 'dt': dt,
            'actual': _num(ev.get('actual')), 'forecast': _num(ev.get('forecast')),
            'previous': _num(ev.get('previous')), 'source': ev.get('source'),
            'raw_actual': ev.get('actual'), 'raw_forecast': ev.get('forecast')}


def _score_surprise(ev: Optional[Dict]) -> Tuple[int, str]:
    """實際 vs 市場預期：超出 → 5 分；低於 → 15 分；無資料/符合 → 10 分"""
    if not ev:
        return SCORE_NEUTRAL, '無最新數據'
    a, f = ev.get('actual'), ev.get('forecast')
    if a is None or f is None:
        return SCORE_NEUTRAL, '無最新數據（缺實際或預期值）'
    if a > f:
        return SCORE_BEAR, '超出市場預期'
    if a < f:
        return SCORE_BULL, '低於市場預期'
    return SCORE_NEUTRAL, '符合市場預期'


def fetch_econ_events() -> Dict:
    out = {'events': [], 'source': None, 'error': None}
    try:
        out['events'] = _tv_events()
        out['source'] = 'TradingView 經濟行事曆'
    except Exception as e:
        out['error'] = f'TradingView {type(e).__name__}: {e}'
    return out


# ── 5. 前一日台股 ──────────────────────────────────────────────────
def fetch_prev_taiex() -> Dict:
    """前一個交易日加權指數收盤與漲跌點數（證交所 FMTQIK，Yahoo 備援）"""
    out = {'date': None, 'close': None, 'change': None, 'source': None, 'error': None}
    try:
        resp = requests.get(TWSE_FMTQIK_URL,
                            params={'response': 'json',
                                    'date': now_tpe().strftime('%Y%m%d')},
                            headers={'User-Agent': _UA}, timeout=20)
        resp.raise_for_status()
        data = resp.json().get('data') or []
        today_roc = _roc_date(now_tpe())
        rows = []
        for r in data:
            if len(r) < 6:
                continue
            rows.append({'roc': str(r[0]).strip(),
                         'close': _num(str(r[4]).replace(',', '')),
                         'change': _num(str(r[5]).replace(',', ''))})
        prev = [r for r in rows if r['roc'] < today_roc and r['close'] is not None]
        if not prev:
            prev = [r for r in rows if r['close'] is not None]
        if prev:
            last = prev[-1]
            out.update({'date': _roc_to_display(last['roc']), 'close': last['close'],
                        'change': last['change'], 'source': '證交所 FMTQIK'})
            return out
        out['error'] = '證交所 FMTQIK 無資料'
    except Exception as e:
        out['error'] = f'證交所 FMTQIK {type(e).__name__}: {e}'

    # 備援：Yahoo ^TWII 日收盤序列
    try:
        resp = requests.get(YAHOO_SPARK_URL,
                            params={'symbols': '^TWII', 'range': '10d', 'interval': '1d'},
                            headers={'User-Agent': _UA}, timeout=20)
        resp.raise_for_status()
        d = resp.json().get('^TWII') or {}
        closes = [c for c in (d.get('close') or []) if c]
        if len(closes) >= 2:
            out.update({'date': None, 'close': closes[-1],
                        'change': closes[-1] - closes[-2], 'source': 'Yahoo ^TWII'})
            out['error'] = None
    except Exception as e:
        out.setdefault('error', '')
        out['error'] = (out['error'] or '') + f' | Yahoo ^TWII {type(e).__name__}: {e}'
    return out


def _roc_date(dt: datetime) -> str:
    """西元日期 → 民國年字串，如 115/10/16（與證交所格式一致，可直接字串比較）"""
    return f'{dt.year - 1911:03d}/{dt.month:02d}/{dt.day:02d}'


def _roc_to_display(roc: str) -> str:
    parts = roc.split('/')
    if len(parts) != 3:
        return roc
    try:
        return f'{int(parts[0]) + 1911}/{parts[1]}/{parts[2]}'
    except ValueError:
        return roc


def score_prev_taiex(data: Dict) -> Tuple[int, str]:
    """前一日台股：跌逾 500 點 → 15 分；漲逾 500 點 → 5 分；其他 → 10 分

    注意方向與第 1 項（美股）相反：此項為逆勢（跌深反彈）設計。
    """
    ch = (data or {}).get('change')
    if ch is None:
        return SCORE_NEUTRAL, '無法取得前一日台股資料（視為無最新數據）'
    if ch < -TAIEX_THRESHOLD_PTS:
        return SCORE_BULL, f'加權指數跌 {abs(ch):,.0f} 點（跌逾 500 點）'
    if ch > TAIEX_THRESHOLD_PTS:
        return SCORE_BEAR, f'加權指數漲 {ch:,.0f} 點（漲逾 500 點）'
    return SCORE_NEUTRAL, f'加權指數 {ch:+,.0f} 點（未達 ±500 點）'


# ── 4. 國際油價（Brent 布蘭特，單日漲跌幅 ±3%）───────────────────
def _ts_to_dt(ts) -> Optional[datetime]:
    """Yahoo spark 時間戳（秒或毫秒）→ UTC datetime"""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return None
    if t > 1e11:                      # 毫秒
        t /= 1000.0
    try:
        return datetime.fromtimestamp(t, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def fetch_brent_oil() -> Dict:
    """Brent 布蘭特原油最新收盤與相對前一交易日漲跌幅（Yahoo BZ=F 日線）"""
    out = {'symbol': 'BZ=F', 'label': 'Brent 布蘭特', 'date': None,
           'last': None, 'prev': None, 'pct': None, 'source': None, 'error': None}
    try:
        resp = requests.get(YAHOO_SPARK_URL,
                            params={'symbols': 'BZ=F', 'range': '1mo', 'interval': '1d'},
                            headers={'User-Agent': _UA}, timeout=20)
        resp.raise_for_status()
        d = resp.json().get('BZ=F') or {}
        closes = d.get('close') or []
        stamps = d.get('timestamp') or []
    except Exception as e:
        out['error'] = f'Yahoo BZ=F {type(e).__name__}: {e}'
        return out

    seq = [(t, c) for t, c in zip(stamps, closes) if c]
    if len(seq) < 2:
        out['error'] = 'Yahoo BZ=F 日線資料不足（<2 筆）'
        return out
    _, prev = seq[-2]
    ct, last = seq[-1]
    dt = _ts_to_dt(ct)
    out.update({'prev': float(prev), 'last': float(last),
                'pct': (float(last) / float(prev) - 1) * 100,
                'date': dt.astimezone(TPE).strftime('%Y-%m-%d') if dt else None,
                'source': 'Yahoo Finance BZ=F（Brent 布蘭特原油期貨）'})
    return out


def score_brent_oil(data: Dict) -> Tuple[int, str]:
    """Brent 布蘭特油價單日漲跌幅：
       漲逾 3% → 5 分（超出預期，通膨壓力，偏空）
       跌逾 3% → 15 分（低於預期，偏多）；其他（含未達 ±3%）→ 10 分
    """
    pct = (data or {}).get('pct')
    if pct is None:
        return SCORE_NEUTRAL, '無法取得 Brent 布蘭特油價（視為無最新數據）'
    if pct > OIL_MOVEMENT_THRESHOLD_PCT:
        return SCORE_BEAR, f'Brent 布蘭特單日漲 {pct:+.2f}%（漲逾 3%，通膨壓力）'
    if pct < -OIL_MOVEMENT_THRESHOLD_PCT:
        return SCORE_BULL, f'Brent 布蘭特單日跌 {pct:+.2f}%（跌逾 3%）'
    return SCORE_NEUTRAL, f'Brent 布蘭特單日漲跌 {pct:+.2f}%（未達 ±3%）'


# ── WTI 油價現貨（僅顯示，不計分）─────────────────────────────────
def fetch_oil_spot() -> List[Dict]:
    try:
        resp = requests.get(YAHOO_SPARK_URL,
                            params={'symbols': 'CL=F', 'range': '5d', 'interval': '1d'},
                            headers={'User-Agent': _UA}, timeout=20)
        resp.raise_for_status()
        j = resp.json()
    except Exception as e:
        logger.info('[sentiment] 油價現貨取得失敗：%s', e)
        return []
    labels = {'CL=F': 'WTI 西德州'}
    rows = []
    for sym, label in labels.items():
        d = j.get(sym) or {}
        closes = [c for c in (d.get('close') or []) if c]
        if not closes:
            continue
        pct = ((closes[-1] / closes[-2] - 1) * 100) if len(closes) >= 2 and closes[-2] else None
        rows.append({'label': label, 'last': closes[-1], 'pct': pct})
    return rows


# ── 主流程 ──────────────────────────────────────────────────────────
def decide_direction(total: Optional[int]) -> Optional[str]:
    """依合計分數決定建議方向：> 60 → 做多；< 50 → 做空；50 ~ 60 → 觀望"""
    if total is None:
        return None
    if total > BULL_THRESHOLD:
        return '做多'
    if total < BEAR_THRESHOLD:
        return '做空'
    return '觀望'


def fetch_sentiment_index(run_time: Optional[datetime] = None) -> Dict:
    """計算自訂多空評分指標（五項）"""
    run_time = run_time or now_tpe()
    idx = {'run_time': run_time.strftime('%Y-%m-%d %H:%M'),
           'date': run_time.strftime('%Y-%m-%d'),
           'items': [], 'total': None, 'direction': None,
           'error': None, 'debug': {}}

    # 1. 最新美股表現
    us = fetch_us_top20()
    s, why = score_us_top20(us)
    idx['items'].append({'key': 'us', 'name': '最新美股表現', 'score': s, 'why': why,
                         'data': us})

    # 2/3. CPI / 非農（實際 vs 市場預期）
    econ = fetch_econ_events()
    idx['debug']['econ_source'] = econ.get('source')
    idx['debug']['econ_error'] = econ.get('error')
    events = econ['events']
    for key, name, titles in (
            ('cpi', '最新美國 CPI', CPI_TITLES),
            ('nfp', '最新美國非農就業', NFP_TITLES)):
        ev = _pick_event(events, titles, SENTI_MAX_AGE_DAYS) if events else None
        s, why = _score_surprise(ev)
        idx['items'].append({'key': key, 'name': name, 'score': s, 'why': why, 'data': ev})

    # 4. 國際油價（Brent 布蘭特單日漲跌幅 ±3%）
    oil = fetch_brent_oil()
    s, why = score_brent_oil(oil)
    idx['items'].append({'key': 'oil', 'name': '最新國際油價（Brent 布蘭特）',
                         'score': s, 'why': why, 'data': oil})

    # 5. 前一日台股表現
    tw = fetch_prev_taiex()
    s, why = score_prev_taiex(tw)
    idx['items'].append({'key': 'tw', 'name': '前一日台股表現', 'score': s, 'why': why,
                         'data': tw})

    idx['oil_spot'] = fetch_oil_spot()

    idx['total'] = sum(i['score'] for i in idx['items'])
    idx['direction'] = decide_direction(idx['total'])
    return idx


# ── 狀態檔（08:00 寫入 / 11:30 比對）──────────────────────────────
def _state_path(path: Optional[str] = None) -> str:
    if path:
        return path
    return STATE_FILE


def load_state(path: Optional[str] = None) -> Dict:
    try:
        with open(_state_path(path), 'r', encoding='utf-8') as f:
            st = json.load(f)
        if not isinstance(st, dict):
            return {'signals': {}, 'history': []}
        st.setdefault('signals', {})
        st.setdefault('history', [])
        return st
    except Exception:
        return {'signals': {}, 'history': []}


def save_state(state: Dict, path: Optional[str] = None) -> None:
    try:
        st = dict(state)
        st['history'] = list(st.get('history', []))[-HISTORY_MAX:]
        # signals 僅保留最近 30 天，避免無限成長
        sigs = st.get('signals', {})
        if len(sigs) > 30:
            for k in sorted(sigs.keys())[:-30]:
                sigs.pop(k, None)
        with open(_state_path(path), 'w', encoding='utf-8', newline='') as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning('[sentiment] 狀態檔寫入失敗：%s', e)


def save_signal(idx: Dict, path: Optional[str] = None) -> None:
    """08:00 記錄當日評分與建議方向"""
    if not idx or idx.get('total') is None:
        return
    st = load_state(path)
    st['signals'][idx['date']] = {
        'date': idx['date'], 'run_time': idx.get('run_time'),
        'total': idx['total'], 'direction': idx['direction'],
        'items': [{'key': i['key'], 'name': i['name'], 'score': i['score'], 'why': i['why']}
                  for i in idx['items']],
    }
    save_state(st, path)


def get_signal(date_str: str, path: Optional[str] = None) -> Optional[Dict]:
    return (load_state(path).get('signals') or {}).get(date_str)


def evaluate_signal(date_str: str, ref: float, last: float,
                    path: Optional[str] = None) -> Optional[Dict]:
    """依 08:00 建議方向計算當日命中結果（不寫檔）

    命中定義：建議做多且台指期上漲，或建議做空且台指期下跌；
    觀望（50 ~ 60 分）不列入命中率。模擬損益以昨結算為基準、小台×5＋微台×5。
    """
    sig = get_signal(date_str, path)
    if not sig:
        return None
    direction = sig.get('direction')
    diff = last - ref
    hit = None
    if direction == '做多':
        hit = diff > 0
    elif direction == '做空':
        hit = diff < 0
    pnl = diff * 50 * 5 + diff * 10 * 5   # 小台每點 50 元、微台每點 10 元，各 5 口
    if direction == '做空':
        pnl = -pnl
    return {'date': date_str, 'total': sig.get('total'), 'direction': direction,
            'ref': ref, 'last': last, 'diff': diff, 'hit': hit,
            'pnl': (None if hit is None else pnl)}


def record_hit(date_str: str, ref: float, last: float,
               path: Optional[str] = None) -> Optional[Dict]:
    """11:30 比對並寫入命中歷史（同一日僅記錄第一次，回傳本次計算結果）"""
    rec = evaluate_signal(date_str, ref, last, path)
    if rec is None:
        return None
    st = load_state(path)
    if not any(h.get('date') == date_str for h in st['history']):
        rec = dict(rec)
        rec['recorded_at'] = now_tpe().strftime('%Y-%m-%d %H:%M')
        st['history'].append(rec)
        save_state(st, path)
    return rec


def hit_stats(n: int = HIT_STATS_N, path: Optional[str] = None) -> Dict:
    """近期命中率（僅計入方向非中性者）"""
    hist = [h for h in (load_state(path).get('history') or []) if h.get('hit') is not None]
    hist = hist[-n:]
    hits = sum(1 for h in hist if h.get('hit'))
    total = len(hist)
    return {'hits': hits, 'total': total,
            'rate': (hits / total * 100) if total else None,
            'records': hist}


# ── Telegram 區塊（08:00）──────────────────────────────────────────
def format_section(idx: Dict) -> str:
    section = f"\n{'='*40}\n"
    section += "🧭 <b>自訂多空評分指標</b>（每日 08:00）\n"
    section += f"{'='*40}\n\n"
    if not idx or not idx.get('items'):
        section += "📭 目前無法計算評分\n"
        section += f"{'='*40}\n"
        return section

    section += f"🗓 資料截止：{idx.get('run_time', '—')}（台灣時間）\n\n"
    emoji = {'us': '1️⃣', 'cpi': '2️⃣', 'nfp': '3️⃣', 'oil': '4️⃣', 'tw': '5️⃣'}
    for it in idx['items']:
        key, data = it['key'], it.get('data') or {}
        section += f"{emoji.get(key, '▪️')} <b>{it['name']}</b>\n"
        if key == 'us':
            items = data.get('items') or []
            if items:
                worst = min(items, key=lambda r: r['chg'])
                best = max(items, key=lambda r: r['chg'])
                section += (f"   前 {len(items)} 大平均：<b>{data['avg']:+.2f}%</b>"
                            f"（{html.escape(str(worst['symbol']))} {worst['chg']:+.2f}% ／ "
                            f"{html.escape(str(best['symbol']))} {best['chg']:+.2f}%）\n")
                section += f"   資料：{html.escape(str(data.get('source') or '—'))}\n"
            else:
                section += f"   ⚠️ {html.escape(str(data.get('error') or '無資料'))[:120]}\n"
        elif key in ('cpi', 'nfp'):
            if data:
                pub = data['dt'].astimezone(TPE).strftime('%Y/%m/%d %H:%M')
                section += (f"   公布：{pub}（台灣時間）· 實際 "
                            f"<b>{_fmt_num(data.get('actual'))}</b> vs 預期 "
                            f"{_fmt_num(data.get('forecast'))}"
                            f"（前值 {_fmt_num(data.get('previous'))}）\n")
                if data.get('source'):
                    section += f"   來源：{html.escape(str(data['source']))}\n"
            else:
                section += "   📭 無最新數據（視為 10 分）\n"
        elif key == 'oil':
            if data.get('pct') is not None:
                date_disp = data.get('date') or '最新交易日'
                section += (f"   {date_disp} 收盤 <b>{data['last']:,.2f}</b> 美元"
                            f"（{data['pct']:+.2f}%；前一日 {data['prev']:,.2f}）\n")
                if data.get('source'):
                    section += f"   來源：{html.escape(str(data['source']))}\n"
            else:
                section += (f"   ⚠️ {html.escape(str(data.get('error') or '無油價資料'))[:120]}"
                            f"（視為 10 分）\n")
        elif key == 'tw':
            if data.get('change') is not None:
                date_disp = data.get('date') or '前一日'
                section += (f"   {date_disp} 加權指數 "
                            f"<b>{data['close']:,.2f}</b>（{data['change']:+,.2f} 點）\n")
            else:
                section += f"   ⚠️ {html.escape(str(data.get('error') or '無前一日資料'))[:120]}\n"
        section += f"   → 判定：{html.escape(it['why'])} → <b>{_score_label(it['score'])}</b>\n\n"

    spot = idx.get('oil_spot') or []
    if spot:
        txt = ' · '.join(
            f"{r['label']} {r['last']:,.2f}"
            + (f"（{r['pct']:+.2f}%）" if r.get('pct') is not None else '')
            for r in spot)
        section += f"🛢 WTI 西德州原油參考（不計分）：{html.escape(txt)}\n\n"

    icon = {'做多': '🔴', '做空': '🟢', '觀望': '⚪', '中性觀望': '⚪'}
    section += "────────────\n"
    section += (f"📊 合計：<b>{idx['total']} 分</b>"
                f"（基準 {BASE_TOTAL} 分／滿分 {TOTAL_MAX} 分）\n")
    section += (f"🎯 建議：<b>{idx['direction']}</b> "
                f"{icon.get(idx['direction'], '')}"
                f"（&gt;{BULL_THRESHOLD} 做多／&lt;{BEAR_THRESHOLD} 做空／其餘觀望）\n")

    st = hit_stats()
    if st['total']:
        section += f"📈 近 {st['total']} 次命中率：<b>{st['rate']:.0f}%</b>"
        section += f"（{st['hits']}/{st['total']}）\n"
    section += "🕦 11:30 台股推播將以台指期比對方向命中率\n"
    section += f"{'='*40}\n"
    section += ("📐 評分：美股前 20 大｜CPI｜非農｜Brent 布蘭特油價｜前一日台股，"
                "各 5/10/15 分（合計 25~75）\n")
    section += "📋 來源: Yahoo Finance（BZ=F） / TradingView 經濟行事曆 / 台灣證交所\n"
    section += "🔴 偏多 ／ 🟢 偏空（台股慣例）\n"
    return section


# ── 11:30 命中率比對區塊（由 stock_monitor 呼叫）──────────────────
def format_hitrate_section(taifex: Dict, run_time: Optional[datetime] = None) -> str:
    """以台指期（TXF）漲跌比對 08:00 建議方向

    Args:
        taifex: stock_monitor.get_taiex_quotes() 取得的 TXF 報價
                {symbol,name,last,diff,diff_rate,ref,volume,date,time}
    """
    run_time = run_time or now_tpe()
    date_str = run_time.strftime('%Y-%m-%d')
    lines = ["", "🎯 <b>08:00 多空評分命中比對</b>"]
    sig = get_signal(date_str)
    if not sig:
        lines.append("　📭 今日無 08:00 評分紀錄（無法比對）")
        return "\n".join(lines) + "\n"

    if not taifex or taifex.get('last') is None:
        lines.append(f"　📊 08:00 評分 {sig.get('total')} 分 → 建議 {sig.get('direction')}")
        lines.append("　⚠️ 無法取得台指期報價，暫不比對")
        return "\n".join(lines) + "\n"

    ref = taifex.get('ref') or 0
    last = taifex.get('last')
    if ref <= 0:
        ref = last - (taifex.get('diff') or 0)
    rec = record_hit(date_str, ref, last)
    direction = sig.get('direction')
    lines.append(f"　📊 08:00 評分 <b>{sig.get('total')} 分</b> → 建議 <b>{direction}</b>")
    lines.append(f"　基準：昨結算 {ref:,.0f} → 11:30 <b>{last:,.0f}</b>"
                 f"（{last - ref:+,.0f} 點）")
    if rec and rec.get('hit') is True:
        lines.append("　✅ <b>命中</b>（方向一致）")
    elif rec and rec.get('hit') is False:
        lines.append("　❌ <b>未命中</b>（方向相反）")
    else:
        lines.append("　⚪ 觀望（50 ~ 60 分），不列入命中率")
    if rec and rec.get('pnl') is not None:
        pnl = rec['pnl']
        lines.append(f"　💰 依建議方向模擬損益（小台×5＋微台×5）："
                     f"<b>{'+' if pnl >= 0 else '−'}{abs(pnl):,.0f} 元</b>")
    st = hit_stats()
    if st['total']:
        lines.append(f"　📈 近 {st['total']} 次命中率：<b>{st['rate']:.0f}%</b>"
                     f"（{st['hits']}/{st['total']}）")
    return "\n".join(lines) + "\n"


# ── CLI（除錯用）──────────────────────────────────────────────────
def _cli():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')
    import re as _re
    idx = fetch_sentiment_index()
    txt = _re.sub(r'<[^>]+>', '', format_section(idx))
    print(txt)
    print('─' * 50)
    print(json.dumps(idx['debug'], ensure_ascii=False, indent=2))
    print('總分 =', idx['total'], '/ 建議 =', idx['direction'])


if __name__ == '__main__':
    _cli()
