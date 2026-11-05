#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[2026-11-06] 費半表現監控（費城半導體指數 SOX）

供 07:00 早鳥推播新增「費半表現」區塊：
  - 費城半導體指數（^SOX）收盤點數、漲跌點數與漲跌幅
  - 成分股（30 檔）漲跌幅前五名／後五名個股與收盤價

資料來源: Yahoo Finance（yfinance，日 K 收盤）
成分股清單: Nasdaq 官方 SOX 權重報告（2026-05 版，30 檔）
"""

import logging
import os
import tempfile
from datetime import datetime
from typing import Dict, List

import pandas as pd

# ── Patch yfinance cache directory（與 news_monitor 相同修補，冪等）──
_YF_SAFE_CACHE = os.path.join(tempfile.gettempdir(), 'yfinance_sandbox')
os.makedirs(_YF_SAFE_CACHE, exist_ok=True)
os.environ['YF_CACHE_DIR'] = _YF_SAFE_CACHE
try:
    import appdirs as _ad
    _ad.user_cache_dir = lambda: _YF_SAFE_CACHE
except ImportError:
    pass

import yfinance as yf

logger = logging.getLogger(__name__)

# 費城半導體指數代碼
SOX_INDEX_SYMBOL = '^SOX'

# SOX 成分股（Nasdaq 官方清單 2026-05，共 30 檔）
# 來源: https://www.nasdaq.com/docs/SOX
SOX_COMPONENTS: Dict[str, str] = {
    'NVDA': '輝達',
    'AVGO': '博通',
    'MU':   '美光',
    'INTC': '英特爾',
    'MRVL': '邁威爾',
    'AMD':  '超微',
    'TXN':  '德州儀器',
    'QCOM': '高通',
    'MPWR': 'Monolithic Power',
    'KLAC': 'KLA',
    'ADI':  '亞德諾',
    'LRCX': '泛林',
    'NXPI': '恩智浦',
    'TSM':  '台積電ADR',
    'AMAT': '應用材料',
    'ASML': '艾司摩爾',
    'COHR': 'Coherent',
    'TER':  '泰瑞達',
    'MCHP': '微晶片',
    'ON':   '安森美',
    'GFS':  '格芯',
    'ALAB': 'Astera Labs',
    'CRDO': 'Credo',
    'ARM':  '安謀',
    'ENTG': '英特格',
    'MTSI': 'MACOM',
    'NVMI': 'Nova',
    'RMBS': 'Rambus',
    'SWKS': '思佳訊',
    'QRVO': 'Qorvo',
}

# 批次下載取近 5 個交易日（保守取多日，避免除權息/停牌造成天數不足）
_HISTORY_PERIOD = '5d'


def _pick_last_two_closes(hist) -> tuple:
    """從日 K DataFrame 取最後兩個有效收盤價（跳過 NaN），回傳 (最新, 前一交易日)"""
    closes = hist['Close'].dropna()
    if len(closes) < 2:
        return None, None
    return float(closes.iloc[-1]), float(closes.iloc[-2])


def fetch_sox_performance() -> Dict:
    """取得 SOX 指數收盤與成分股漲跌幅排名。

    回傳:
    {
      'index': {'close', 'change', 'change_pct', 'date'},
      'gainers': [{'symbol','name','price','change_pct'}, ...],   # 漲幅前 5
      'losers':  [{'symbol','name','price','change_pct'}, ...],   # 跌幅前 5
      'component_count': 有效成分股檔數,
      'fetched_at': ISO 時間,
    }
    失敗時各欄位為空值，由呼叫端決定跳過。
    """
    out: Dict = {
        'index': None,
        'gainers': [],
        'losers': [],
        'component_count': 0,
        'fetched_at': datetime.now().isoformat(timespec='seconds'),
    }

    all_symbols = [SOX_INDEX_SYMBOL] + list(SOX_COMPONENTS.keys())
    logger.info("Fetching SOX index & %d components via yfinance...", len(SOX_COMPONENTS))

    df = yf.download(
        tickers=all_symbols,
        period=_HISTORY_PERIOD,
        interval='1d',
        group_by='ticker',
        auto_adjust=False,
        threads=True,
        progress=False,
    )
    if df is None or df.empty:
        logger.warning("SOX performance: yfinance download returned empty data")
        return out

    rows: List[Dict] = []

    # ── 指數收盤 ──
    try:
        hist = df[SOX_INDEX_SYMBOL] if isinstance(df.columns, pd.MultiIndex) else df
        last, prev = _pick_last_two_closes(hist)
        if last is not None and prev:
            out['index'] = {
                'close': last,
                'change': last - prev,
                'change_pct': (last - prev) / prev * 100,
                'date': hist.index[-1].strftime('%Y-%m-%d'),
            }
    except Exception as e:
        logger.warning("SOX index parse failed: %s", e)

    # ── 成分股漲跌幅 ──
    for sym, name in SOX_COMPONENTS.items():
        try:
            hist = df[sym] if isinstance(df.columns, pd.MultiIndex) else df
            last, prev = _pick_last_two_closes(hist)
            if last is None or not prev:
                continue
            rows.append({
                'symbol': sym,
                'name': name,
                'price': last,
                'change_pct': (last - prev) / prev * 100,
            })
        except Exception:
            # 單一成分股失敗不影響整體（如停牌、代碼異動）
            continue

    out['component_count'] = len(rows)
    rows.sort(key=lambda r: r['change_pct'], reverse=True)
    out['gainers'] = rows[:5]
    # 成分股取樣不足 10 檔時，漲跌名單會重疊，僅顯示漲幅前五
    out['losers'] = list(reversed(rows[-5:])) if len(rows) >= 10 else []
    logger.info("SOX performance: index=%s, components=%d", 
                bool(out['index']), len(rows))
    return out


def has_sox_content(data: Dict) -> bool:
    """判斷是否具備可推播內容（指數或至少 5 檔成分股有資料）"""
    if not data:
        return False
    return bool(data.get('index')) or len(data.get('gainers') or []) >= 5


def format_sox_section(data: Dict) -> str:
    """格式化費半表現區塊（Telegram HTML）"""
    section = f"\n{'='*40}\n"
    section += "🇺🇸 <b>費半表現</b>（費城半導體指數）\n"
    section += f"{'='*40}\n"

    idx = (data or {}).get('index')
    if idx:
        sign = '+' if idx['change'] >= 0 else ''
        section += (
            f"📊 指數收盤: <b>{idx['close']:,.2f}</b>"
            f"（{sign}{idx['change']:+,.2f} 點 / {sign}{idx['change_pct']:+.2f}%）\n"
            f"　交易日: {idx.get('date') or '最新'}\n"
        )
    else:
        section += "📭 指數資料暫時無法取得\n"

    gainers = (data or {}).get('gainers') or []
    losers = (data or {}).get('losers') or []

    if gainers:
        section += "\n📈 <b>漲幅前五</b>\n"
        for rank, r in enumerate(gainers, 1):
            url = f"https://finance.yahoo.com/quote/{r['symbol']}"
            section += (
                f"{rank}. <a href=\"{url}\">{r['symbol']} {r['name']}</a>"
                f"　${r['price']:,.2f}（{r['change_pct']:+.2f}%）\n"
            )
    if losers:
        section += "\n📉 <b>跌幅前五</b>\n"
        for rank, r in enumerate(losers, 1):
            url = f"https://finance.yahoo.com/quote/{r['symbol']}"
            section += (
                f"{rank}. <a href=\"{url}\">{r['symbol']} {r['name']}</a>"
                f"　${r['price']:,.2f}（{r['change_pct']:+.2f}%）\n"
            )
    if not gainers and not losers:
        section += "\n📭 成分股資料暫時無法取得\n"

    section += f"{'='*40}\n"
    section += (
        f"📋 來源: Yahoo Finance（SOX 成分股 30 檔，實際取得 "
        f"{(data or {}).get('component_count', 0)} 檔；收盤價，美東時間）\n"
    )
    return section
