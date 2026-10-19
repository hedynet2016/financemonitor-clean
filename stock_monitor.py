#!/usr/bin/env python3
"""
股票監控工具 - 每半小時交易量監控
Stock Monitor - Half-Hourly Volume Monitor (US + TW)
"""

# ── Patch yfinance cache directory ──
import os as _os
import tempfile as _tempfile
_YF_SAFE_CACHE = _os.path.join(_tempfile.gettempdir(), 'yfinance_sandbox')
_os.makedirs(_YF_SAFE_CACHE, exist_ok=True)
_os.environ['YF_CACHE_DIR'] = _YF_SAFE_CACHE

# Monkey-patch appdirs BEFORE yfinance is first imported
try:
    import appdirs as _ad
    _ad.user_cache_dir = lambda: _YF_SAFE_CACHE
except ImportError:
    pass

import yfinance as yf
import pandas as pd
import requests
import os
import re
from datetime import datetime, timedelta
import time
import json
import logging
from typing import List, Dict
import pytz
import urllib3
from notification_sender import NotificationSender

# Directly override yfinance cache class attributes (belt-and-suspenders)
import yfinance.cache as _yf_cache
_yf_cache._CookieDBManager._cache_dir = _YF_SAFE_CACHE

# Suppress yfinance ERROR logs (404 for delisted/renamed tickers is expected)
logging.getLogger('yfinance').setLevel(logging.WARNING)
_yf_cache._TzDBManager._cache_dir = _YF_SAFE_CACHE
_yf_cache._ISINDBManager._cache_dir = _YF_SAFE_CACHE

# 禁用SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 配置日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('stock_monitor.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ── 台指期監控設定（2026-09-23 新增）─────────────────────────────────
# 期交所 MIS API（免 key）：POST getQuoteList，取成交量最大的近月契約
TAIFEX_QUOTE_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
FUT_CIDS = ["TXF", "MXF", "TMF"]            # 大台／小台／微台
FUT_CONTRACT_SIZE = {"TXF": 200, "MXF": 50, "TMF": 10}  # 每點新台幣（期交所規格）
FUT_PNL_QTY = 5                              # 多單：小台與微台各五口
FUT_PNL_SHORT_QTY = 2                        # 空單：小台與微台各兩口（[2026-10-20] 使用者調整）
# 09:00 下單（進場價 = 當日 09:00 後第一次抓到的報價；多單與空單同進場價）
FUT_ENTRY_MINUTES = 9 * 60
# 損益試算顯示時段：09:30 ~ 11:30（以半小時 block 計：block = hour*2 + minute//30）
# block 19=09:30, 20=10:00, 21=10:30, 22=11:00, 23=11:30
FUT_PNL_BLOCKS = range(19, 24)
# [2026-10-16] 08:00 自訂多空評分指標的命中比對時點：11:30（block 23）
FUT_HIT_BLOCK = 23
FUT_STATE_FILE = "taiex_state.json"

# ── 台股即時排行設定（範圍固定為 config 監控清單：7 檔個股＋7 檔 ETF）──
# 2026-10-15 修正：2026-10-14 曾改為 TWSE/TPEx 全市場股池（前一日量前 260+90），
# 經使用者要求收回範圍至監控清單；資料來源維持證交所 MIS 即時行情 API
# 批次查詢，量價即時、每半小時重排（解決舊版 Yahoo 資料不動態的問題）。
TW_MIS_BATCH = 80                         # MIS API 每批查詢檔數
TW_VOL_TOP_N = 10                         # 成交量排行取前 N 名
TW_DROP_POOL = 50                         # 跌幅排行母體（成交量前 N 名）
TW_DROP_MIN = -3.0                        # 跌幅門檻（%）
TW_MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"


class StockMonitor:
    """股票監控類"""

    # 美股 ETF 廣泛監控池（動態依交易量排序取前 N 名）
    US_ETF_UNIVERSE = [
        # 大盤指數
        "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV",
        # 板塊 ETF
        "XLF", "XLK", "XLE", "XLV", "XLI", "XLU", "XLRE", "XLP", "XLB", "XLC", "XLY",
        # 槓桿/反向
        "SOXL", "TQQQ", "SQQQ", "SPXS", "SPXL", "UPRO", "SDS",
        # 波動率
        "UVXY", "VIXY",
        # 債券
        "TLT", "HYG", "LQD", "EMB", "AGG", "BND", "SHV", "IEF", "BIL",
        # 新興市場/國際
        "EEM", "EFA", "VEA", "VWO", "FXI", "KWEB", "ASHR", "INDA",
        "EWZ", "EWJ", "EWW", "RSX", "EWG", "EWQ", "EWU", "EEMS", "SCZ",
        # 半導體/科技
        "SMH", "XHB", "XSD", "FINX", "CLOU", "BOTZ", "ROBO", "AIQ",
        # 原物料/貴金屬/能源
        "XME", "XOP", "GDX", "GDXJ", "SLV", "USO", "UNG", "UCO",
        "DBC", "WEAT", "CORN", "SOYB",
        # ARK 系列
        "ARKK", "ARKG", "ARKW", "ARKF", "ARKQ",
        # 清潔能源
        "ICLN", "TAN", "PBW", "QCLN",
        # 生技/醫療
        "XBI", "IBB", "PJP",
        # 金融
        "FNCL", "KRE", "KBE", "IAI",
        # 航空/國防/運輸
        "JETS", "PPA", "ITA",
        # 主題
        "BITQ", "BETZ", "ESPO", "CIBR",
    ]

    # 台股中文名稱映射
    TW_STOCK_NAMES = {
        "2330.TW": "台積電",
        "2454.TW": "聯發科",
        "2308.TW": "台達電",
        "2891.TW": "中信金",
        "2882.TW": "國泰金",
        "2881.TW": "富邦金",
        "3293.TWO": "鈊象電子",
    }

    # 台股 ETF 中文名稱映射
    TW_ETF_NAMES = {
        "00403A.TW": "主動統一升級50",
        "00981A.TW": "主動統一台股增長",
        "0050.TW": "元大台灣50",
        "00830.TW": "國泰費城半導體",
        "00919.TW": "群益台灣精選高息",
        "00878.TW": "國泰永續高股息",
        "0056.TW": "元大高股息",
    }

    def _strip_tw_suffix(self, ticker: str) -> str:
        """移除台股代碼的 .TW 或 .TWO 後綴"""
        if ticker.endswith('.TWO'):
            return ticker[:-4]
        if ticker.endswith('.TW'):
            return ticker[:-3]
        return ticker

    def get_tw_etf_name(self, ticker: str) -> str:
        """取得台股 ETF 中文名稱"""
        return self.TW_ETF_NAMES.get(ticker, self._strip_tw_suffix(ticker))

    def __init__(self, config_file: str = 'config.json'):
        """初始化監控器"""
        self.config = self.load_config(config_file)
        self.notification_sender = NotificationSender(self.config)
        self.trading_hours = self.config.get('trading_hours', {})

        # 監控的股票列表
        tickers_config = self.config.get('tickers', {})
        self.us_tickers = tickers_config.get('us_stocks', [])
        self.us_etfs = tickers_config.get('us_etfs', [])
        self.tw_tickers = tickers_config.get('tw_stocks', [])
        self.tw_etfs = tickers_config.get('tw_etfs', [])
        self.all_tickers = self.us_tickers + self.us_etfs + self.tw_tickers + self.tw_etfs

        logger.info(f"StockMonitor initialized with {len(self.us_tickers)} US stocks and {len(self.tw_tickers)} TW stocks")

        # 台股數據來源追蹤（Yahoo Finance / TWSE OpenAPI）
        self._tw_data_source = "Yahoo Finance"

    def load_config(self, config_file: str) -> Dict:
        """加載配置文件"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning(f"Config file {config_file} not found, using defaults")
            return {}
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            return {}

    def get_tw_stock_name(self, ticker: str) -> str:
        """獲取台股中文名稱"""
        return self.TW_STOCK_NAMES.get(ticker, self._strip_tw_suffix(ticker))

    def is_trading_time(self, market: str = 'all') -> Dict[str, bool]:
        """檢查是否為交易時間"""
        result = {}
        
        if market in ['all', 'us']:
            us_config = self.trading_hours.get('us_market', {})
            if us_config.get('enabled', True):
                us_timezone = pytz.timezone(us_config.get('timezone', 'US/Eastern'))
                now = datetime.now(us_timezone)
                hour = now.hour
                minute = now.minute
                weekday = now.weekday()  # 0=Monday, 6=Sunday
                
                start_time = us_config.get('start_hour', 9) * 60 + us_config.get('start_minute', 30)
                end_time = us_config.get('end_hour', 16) * 60 + us_config.get('end_minute', 0)
                current_time = hour * 60 + minute
                
                is_weekday = weekday < 5 if us_config.get('weekdays_only', True) else True
                is_trading_hours = start_time <= current_time < end_time
                
                result['us'] = is_weekday and is_trading_hours
                logger.debug(f"US Market - Weekday: {is_weekday}, Time in range: {is_trading_hours}, Trading: {result['us']}")
        
        if market in ['all', 'tw']:
            tw_config = self.trading_hours.get('tw_market', {})
            if tw_config.get('enabled', True):
                tw_timezone = pytz.timezone(tw_config.get('timezone', 'Asia/Taipei'))
                now = datetime.now(tw_timezone)
                hour = now.hour
                minute = now.minute
                weekday = now.weekday()
                
                start_time = tw_config.get('start_hour', 9) * 60 + tw_config.get('start_minute', 0)
                end_time = tw_config.get('end_hour', 13) * 60 + tw_config.get('end_minute', 30)
                current_time = hour * 60 + minute
                
                is_weekday = weekday < 5 if tw_config.get('weekdays_only', True) else True
                is_trading_hours = start_time <= current_time < end_time
                
                result['tw'] = is_weekday and is_trading_hours
                logger.debug(f"TW Market - Weekday: {is_weekday}, Time in range: {is_trading_hours}, Trading: {result['tw']}")
        
        return result
    
    def get_tw_realtime_data(self) -> List[Dict]:
        """從 Yahoo Finance 獲取台股數據"""
        try:
            logger.info("Fetching TW stocks from Yahoo Finance...")

            # 從 config.json 讀取監控的台股個股清單
            tw_tickers = self.tw_tickers
            if not tw_tickers:
                logger.info("No TW stocks configured")
                return []

            logger.info(f"Fetching data for {len(tw_tickers)} TW stocks from Yahoo Finance...")
            tickers_obj = yf.Tickers(tw_tickers)

            stock_data = []
            for ticker in tw_tickers:
                try:
                    info = tickers_obj.tickers.get(ticker)
                    if not info:
                        continue
                    info = info.info

                    volume = info.get('regularMarketVolume', 0) or 0
                    price = info.get('regularMarketPrice', 0) or 0
                    previous_close = info.get('previousClose', 0) or 0
                    market_cap = info.get('marketCap', 0) or 0

                    if volume > 0:
                        change = price - previous_close if previous_close > 0 else 0
                        change_percent = (change / previous_close * 100) if previous_close > 0 else 0

                        # 获取中文名称
                        name = self.get_tw_stock_name(ticker)

                        stock_data.append({
                            'ticker': ticker,
                            'name': name,
                            'volume': volume,
                            'price': price,
                            'change': change,
                            'change_percent': change_percent,
                            'market_cap': market_cap,
                            'market': 'TW',
                            'timestamp': datetime.now(pytz.timezone('Asia/Taipei')).isoformat()
                        })
                        logger.debug(f"{ticker} ({name}): Volume={volume:,}, Price=NT${price:.2f}")
                    time.sleep(1)  # 降低 Yahoo Finance 限流風險
                except Exception as e:
                    logger.warning(f"Skip {ticker}: {e}")
                    time.sleep(1)
                    continue

            logger.info(f"Fetched {len(stock_data)} TW stocks from Yahoo Finance")

            # yfinance 限流或失敗時，自動切換到 TWSE OpenAPI
            if not stock_data and tw_tickers:
                logger.warning("Yahoo Finance returned no TW stock data (likely rate limited), falling back to TWSE OpenAPI...")
                self._tw_data_source = "TWSE OpenAPI"
                return self._fetch_tw_from_twse(tw_tickers)

            self._tw_data_source = "Yahoo Finance"
            return stock_data

        except Exception as e:
            logger.error(f"Error fetching TW stocks from Yahoo Finance: {e}")
            logger.warning("Trying TWSE OpenAPI as fallback...")
            self._tw_data_source = "TWSE OpenAPI"
            return self._fetch_tw_from_twse(self.tw_tickers)

    def get_tw_etf_data(self) -> List[Dict]:
        """從 Yahoo Finance 獲取台股 ETF 即時數據"""
        try:
            logger.info("Fetching TW ETFs data...")

            tw_etfs = self.tw_etfs
            if not tw_etfs:
                logger.info("No TW ETFs configured")
                return []

            logger.info(f"Fetching data for {len(tw_etfs)} TW ETFs...")
            tickers_obj = yf.Tickers(tw_etfs)

            etf_data = []
            for ticker in tw_etfs:
                try:
                    info = tickers_obj.tickers.get(ticker)
                    if not info:
                        continue
                    info = info.info

                    volume = info.get('regularMarketVolume', 0) or 0
                    price = info.get('regularMarketPrice', 0) or 0
                    previous_close = info.get('previousClose', 0) or 0
                    market_cap = info.get('marketCap', 0) or 0

                    if previous_close > 0:
                        change_percent = ((price - previous_close) / previous_close) * 100
                    else:
                        change_percent = 0

                    if volume > 0:
                        etf_data.append({
                            'ticker': ticker,
                            'name': self.get_tw_etf_name(ticker),
                            'volume': volume,
                            'price': price,
                            'change_percent': change_percent,
                            'market_cap': market_cap,
                            'market': 'TW',
                            'type': 'etf',
                            'timestamp': datetime.now(pytz.timezone('Asia/Taipei')).isoformat()
                        })
                        logger.debug(f"{ticker} (ETF): Volume={volume:,}, Price=NT${price:.2f}")
                    time.sleep(1)  # 降低 Yahoo Finance 限流風險
                except Exception as e:
                    logger.warning(f"Skip ETF {ticker}: {e}")
                    time.sleep(1)
                    continue

            logger.info(f"Fetched {len(etf_data)} TW ETFs from Yahoo Finance")

            # yfinance 限流或失敗時，自動切換到 TWSE OpenAPI
            if not etf_data and tw_etfs:
                logger.warning("Yahoo Finance returned no TW ETF data (likely rate limited), falling back to TWSE OpenAPI...")
                self._tw_data_source = "TWSE OpenAPI"
                return self._fetch_tw_from_twse(tw_etfs)

            self._tw_data_source = "Yahoo Finance"
            return etf_data

        except Exception as e:
            logger.error(f"Error fetching TW ETFs: {e}")
            logger.warning("Trying TWSE OpenAPI as fallback for TW ETFs...")
            self._tw_data_source = "TWSE OpenAPI"
            return self._fetch_tw_from_twse(self.tw_etfs)

    def _fetch_tw_from_twse(self, tickers: List[str]) -> List[Dict]:
        """從 TWSE/TPEx API 取得台股數據（yfinance 限流時的 fallback）

        上市(.TW) → TWSE STOCK_DAY API（最近交易日收盤數據）
        上櫃(.TWO) → TPEx 日收盤行情 API
        回傳格式與 yfinance 相同，volume 單位為股
        """
        try:
            logger.info(f"TWSE Fallback: fetching {len(tickers)} TW tickers...")
            taipei_tz = pytz.timezone('Asia/Taipei')
            ts = datetime.now(taipei_tz).isoformat()
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            }

            now = datetime.now(taipei_tz)
            date_yyyymmdd = now.strftime('%Y%m%d')
            roc_date = f"{now.year - 1911}/{now.month:02d}/{now.day:02d}"

            # 先取得所有上櫃(OTC)股票資料（一次性，供 .TWO 股票查詢）
            tpex_rows = None
            otc_tickers = [t for t in tickers if t.endswith('.TWO')]
            if otc_tickers:
                tpex_rows = self._fetch_tpex_daily(roc_date, headers)

            stock_data = []
            for ticker in tickers:
                try:
                    code = self._strip_tw_suffix(ticker)

                    if ticker.endswith('.TWO'):
                        # 上櫃：從 TPEx 資料中查找
                        if not tpex_rows:
                            logger.warning(f"TWSE: no TPEx data for {ticker}")
                            continue
                        row = None
                        for r in tpex_rows:
                            if r[0].strip() == code:
                                row = r
                                break
                        if not row:
                            logger.warning(f"TWSE: {ticker} not found in TPEx data")
                            continue
                        # TPEx fields: 代號, 名稱, 收盤, 漲跌, 開盤, 最高, 最低, 均價, 成交股數, 成交金額
                        name = row[1].strip()
                        price = float(row[2].replace(',', '').strip())
                        change_str = row[3].replace(',', '').strip()
                        change = float(change_str) if change_str else 0.0
                        vol_str = row[8].replace(',', '').strip()
                        volume = int(vol_str) if vol_str else 0
                    else:
                        # 上市：使用 TWSE STOCK_DAY API
                        url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&stockNo={code}&date={date_yyyymmdd}"
                        resp = requests.get(url, headers=headers, timeout=10)
                        resp.raise_for_status()
                        data = resp.json()
                        if data.get("stat") != "OK" or not data.get("data"):
                            logger.warning(f"TWSE: no STOCK_DAY data for {ticker}")
                            time.sleep(0.3)
                            continue
                        # 取最後一筆（最近交易日）
                        record = data["data"][-1]
                        # Fields: 日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數, 註記
                        vol_str = record[1].replace(',', '').strip()
                        volume = int(vol_str) if vol_str else 0
                        price = float(record[6].replace(',', '').strip())
                        change_str = record[7].replace(',', '').strip()
                        change = float(change_str) if change_str else 0.0
                        name = self.get_tw_stock_name(ticker) if ticker in self.TW_STOCK_NAMES else self.get_tw_etf_name(ticker)

                    if volume > 0 and price > 0:
                        previous_close = price - change if change != 0 else price
                        change_percent = (change / previous_close * 100) if previous_close > 0 else 0

                        stock_data.append({
                            'ticker': ticker,
                            'name': name,
                            'volume': volume,
                            'price': price,
                            'change': change,
                            'change_percent': change_percent,
                            'market_cap': 0,
                            'market': 'TW',
                            'timestamp': ts,
                        })
                        logger.info(f"TWSE: {ticker} ({name}) vol={volume:,} price={price:.2f} change={change_percent:+.2f}%")

                    time.sleep(0.3)

                except Exception as e:
                    logger.warning(f"TWSE: skip {ticker}: {e}")
                    time.sleep(0.3)
                    continue

            logger.info(f"TWSE Fallback: fetched {len(stock_data)}/{len(tickers)} tickers")
            return stock_data

        except Exception as e:
            logger.error(f"TWSE API error: {e}")
            return []

    def _fetch_tpex_daily(self, roc_date: str, headers: dict) -> list:
        """取得 TPEx 上櫃股票日收盤行情（一次性取得全部，再由呼叫端篩選）"""
        try:
            url = f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&d={roc_date}"
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("stat") != "ok":
                logger.warning(f"TPEx: stat={data.get('stat')}")
                return []
            tables = data.get("tables", [])
            if not tables:
                return []
            rows = tables[0].get("data", [])
            logger.info(f"TPEx: fetched {len(rows)} OTC stocks for {roc_date}")
            return rows
        except Exception as e:
            logger.error(f"TPEx API error: {e}")
            return []

    # ── 台股即時排行（範圍固定為監控清單 7 檔個股＋7 檔 ETF）────────────
    # 2026-10-15 修正：範圍收回為 config 監控清單（原全市場股池已移除），
    # 資料來源維持證交所 MIS 即時行情 API 批次查詢。

    TW_ETF_CODE_RE = re.compile(r'^00\d{2,4}[A-Z]?$')

    @classmethod
    def _is_tw_etf_code(cls, code: str) -> bool:
        """台股 ETF/ETN 代碼：00 開頭 4~6 碼（0050、00878、00631L、00981A）"""
        return bool(cls.TW_ETF_CODE_RE.match(str(code or '').strip().upper()))

    def _load_tw_universe(self) -> List[Dict]:
        """即時查詢標的：固定為 config 監控清單（個股＋ETF）。"""
        items, seen = [], set()
        for t in (self.tw_tickers + self.tw_etfs):
            code = self._strip_tw_suffix(t)
            if not code or code in seen:
                continue
            seen.add(code)
            is_otc = t.endswith('.TWO')
            nm = (self.get_tw_stock_name(t) if t in self.TW_STOCK_NAMES
                  else self.get_tw_etf_name(t))
            items.append({'code': code, 'name': nm,
                          'ex': 'otc' if is_otc else 'tse'})
        logger.info(f"TW watchlist universe: {len(items)} securities")
        return items

    def _fetch_tw_mis_quotes(self, items: List[Dict]) -> List[Dict]:
        """以證交所 MIS 即時行情 API 批次查詢股池，回傳即時量價清單。"""
        if not items:
            return []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://mis.twse.com.tw/stock/index.jsp",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        taipei_tz = pytz.timezone('Asia/Taipei')
        ts = datetime.now(taipei_tz).isoformat()
        ex_map = {it['code']: it['ex'] for it in items}

        quotes, failed_batches = [], 0
        for i in range(0, len(items), TW_MIS_BATCH):
            batch = items[i:i + TW_MIS_BATCH]
            ex_ch = "|".join(f"{it['ex']}_{it['code']}.tw" for it in batch)
            try:
                resp = requests.get(TW_MIS_URL, params={
                    "ex_ch": ex_ch, "json": "1", "delay": "0",
                    "_": int(time.time() * 1000),
                }, headers=headers, timeout=20)
                resp.raise_for_status()
                arr = resp.json().get("msgArray", [])
            except Exception as e:
                failed_batches += 1
                logger.warning(f"MIS batch {i // TW_MIS_BATCH + 1} failed: {e}")
                continue

            for m in arr:
                code = str(m.get('c', '')).strip()
                if not code:
                    continue
                try:
                    vol = int(str(m.get('v') or 0).replace(',', ''))
                except Exception:
                    vol = 0
                price = 0.0
                for key in ('z', 'pz', 'o', 'y'):
                    raw = str(m.get(key) or '').strip()
                    if raw and raw != '-':
                        try:
                            price = float(raw)
                            break
                        except Exception:
                            continue
                try:
                    prev = float(str(m.get('y') or 0))
                except Exception:
                    prev = 0.0
                if vol <= 0 or price <= 0:
                    continue
                ex = ex_map.get(code, 'tse')
                quotes.append({
                    'ticker': f"{code}.{'TWO' if ex == 'otc' else 'TW'}",
                    'code': code,
                    'name': str(m.get('n', '')).strip() or code,
                    'volume': vol,                       # 單位：張
                    'price': price,
                    'change_percent': ((price - prev) / prev * 100) if prev > 0 else 0.0,
                    'market': 'TW',
                    'is_etf': self._is_tw_etf_code(code),
                    'timestamp': ts,
                })
            time.sleep(0.5)   # 降低 MIS 速率限制風險

        logger.info(f"TW MIS realtime: {len(quotes)}/{len(items)} quotes "
                    f"({failed_batches} failed batches)")
        return quotes

    def fetch_tw_market_realtime(self) -> List[Dict]:
        """取得台股監控清單（固定範圍）即時量價（MIS 批次查詢）。"""
        items = self._load_tw_universe()
        if not items:
            logger.warning("TW universe empty, cannot build realtime rankings")
            return []
        return self._fetch_tw_mis_quotes(items)

    def rank_tw_volume(self, quotes: List[Dict]) -> Dict[str, List[Dict]]:
        """台股成交量排行：個股與 ETF（範圍固定為監控清單，即時）"""
        stocks = sorted([q for q in quotes if not q.get('is_etf')],
                        key=lambda x: x['volume'], reverse=True)
        etfs = sorted([q for q in quotes if q.get('is_etf')],
                      key=lambda x: x['volume'], reverse=True)
        top_stocks, top_etfs = stocks[:TW_VOL_TOP_N], etfs[:TW_VOL_TOP_N]
        for i, s in enumerate(top_stocks, 1):
            s['rank'] = i
        for i, e in enumerate(top_etfs, 1):
            e['rank'] = i
        logger.info(f"TW volume ranking: {len(top_stocks)} stocks / {len(top_etfs)} ETFs")
        return {'stocks': top_stocks, 'etfs': top_etfs}

    def rank_tw_drop(self, quotes: List[Dict]) -> Dict[str, List[Dict]]:
        """台股跌幅排行：監控標的中跌幅超過門檻者（依跌幅排序）"""
        stocks = sorted([q for q in quotes if not q.get('is_etf')],
                        key=lambda x: x['volume'], reverse=True)[:TW_DROP_POOL]
        etfs = sorted([q for q in quotes if q.get('is_etf')],
                      key=lambda x: x['volume'], reverse=True)[:TW_VOL_TOP_N]
        drop_stocks = sorted([s for s in stocks if s['change_percent'] < TW_DROP_MIN],
                             key=lambda x: x['change_percent'])
        drop_etfs = sorted([e for e in etfs if e['change_percent'] < TW_DROP_MIN],
                           key=lambda x: x['change_percent'])
        logger.info(f"TW drop ranking: {len(drop_stocks)} stocks / {len(drop_etfs)} ETFs "
                    f"(pool {len(stocks)}/{len(etfs)}, threshold {TW_DROP_MIN}%)")
        return {'stocks': drop_stocks, 'etfs': drop_etfs}

    @staticmethod
    def format_tw_volume(volume: int) -> str:
        """台股成交量格式化（單位：張）"""
        try:
            vol = int(volume)
        except Exception:
            vol = 0
        if vol >= 10000:
            return f"{vol / 10000:.1f}萬張"
        return f"{vol:,}張"

    def _fetch_us_most_active_stocks(self, count: int = 50) -> List[Dict]:
        """透過 Yahoo Finance screener API 動態取得全市場最活躍個股"""
        try:
            url = "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved"
            params = {
                "formatted": "false",
                "lang": "en-US",
                "region": "US",
                "scrIds": "most_actives",
                "count": str(count),
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }

            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            results = data.get("finance", {}).get("result", [])
            if not results:
                logger.warning("Screener API returned no results")
                return []

            quotes = results[0].get("quotes", [])
            taipei_tz = pytz.timezone("Asia/Taipei")
            ts = datetime.now(taipei_tz).isoformat()

            stock_data = []
            for q in quotes:
                quote_type = q.get("quoteType", "")
                if quote_type != "EQUITY":
                    continue
                volume = q.get("regularMarketVolume", 0) or 0
                price = q.get("regularMarketPrice", 0) or 0
                if volume <= 0 or price <= 0:
                    continue
                stock_data.append({
                    "ticker": q.get("symbol", ""),
                    "name": q.get("shortName", q.get("symbol", "")),
                    "volume": volume,
                    "price": price,
                    "change_percent": q.get("regularMarketChangePercent", 0) or 0,
                    "market_cap": q.get("marketCap", 0) or 0,
                    "market": "US",
                    "timestamp": ts,
                })

            logger.info(f"Dynamic screener: fetched {len(stock_data)} most active US stocks")
            return stock_data

        except Exception as e:
            logger.error(f"Yahoo Finance screener API failed: {e}")
            return []

    def _fetch_us_stocks_fallback(self) -> List[Dict]:
        """Fallback: 使用 yfinance 從固定清單取得美股個股數據"""
        us_tickers = [
            "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA",
            "BRK.B", "LLY", "AVGO", "JPM", "V", "XOM", "PG", "JNJ",
            "COST", "MA", "HD", "MRK", "ABBV", "NFLX", "AMD", "ORCL",
            "PEP", "BAC", "KO", "CSCO", "TMO", "WMT", "CRM", "ABT",
            "MCD", "CVX", "DHR", "NKE", "ACN", "ADBE", "LIN", "WFC",
            "INTC", "VZ", "DIS", "IBM", "NEE", "HON", "CAT", "MS",
            "GS", "BA", "UNH", "LOW", "QCOM", "SCHW", "TXN"
        ]

        logger.info(f"Fallback: fetching {len(us_tickers)} US stocks via yfinance...")
        tickers_obj = yf.Tickers(us_tickers)

        stock_data = []
        for ticker in us_tickers:
            try:
                info = tickers_obj.tickers.get(ticker)
                if not info:
                    continue
                info = info.info
                volume = info.get('regularMarketVolume', 0) or 0
                price = info.get('regularMarketPrice', 0) or 0
                change = info.get('regularMarketChangePercent', 0) or 0
                market_cap = info.get('marketCap', 0) or 0

                if volume > 0:
                    stock_data.append({
                        'ticker': ticker,
                        'name': ticker,
                        'volume': volume,
                        'price': price,
                        'change_percent': change,
                        'market_cap': market_cap,
                        'market': 'US',
                        'timestamp': datetime.now(pytz.timezone('Asia/Taipei')).isoformat()
                    })
            except Exception as e:
                logger.warning(f"Skip {ticker}: {e}")
                continue

        logger.info(f"Fallback: fetched {len(stock_data)} US stocks")
        return stock_data

    def get_us_realtime_data(self) -> List[Dict]:
        """動態取得美股最活躍個股數據（screener API → yfinance fallback）"""
        logger.info("Fetching US stocks data...")

        # 優先使用 Yahoo Finance screener API 動態取得全市場最活躍個股
        stock_data = self._fetch_us_most_active_stocks(count=50)
        if stock_data:
            return stock_data

        # Fallback: 使用 yfinance 從固定清單取得
        logger.warning("Screener API unavailable, falling back to fixed list via yfinance")
        try:
            return self._fetch_us_stocks_fallback()
        except Exception as e:
            logger.error(f"Error fetching US stocks (fallback): {e}")
            return []

    def get_us_etf_data(self) -> List[Dict]:
        """動態取得美股 ETF 數據（廣泛監控池 → 依交易量排序取前 N 名）"""
        try:
            logger.info("Fetching US ETFs data from broad universe...")

            # 使用廣泛 ETF 監控池，依實際交易量動態排序
            us_etfs = self.US_ETF_UNIVERSE
            logger.info(f"Fetching data for {len(us_etfs)} US ETFs from universe...")

            tickers_obj = yf.Tickers(us_etfs)

            taipei_tz = pytz.timezone('Asia/Taipei')
            ts = datetime.now(taipei_tz).isoformat()

            etf_data = []
            for ticker in us_etfs:
                try:
                    info = tickers_obj.tickers.get(ticker)
                    if not info:
                        continue
                    info = info.info

                    volume = info.get('regularMarketVolume', 0) or 0
                    price = info.get('regularMarketPrice', 0) or 0
                    change = info.get('regularMarketChangePercent', 0) or 0
                    market_cap = info.get('marketCap', 0) or 0

                    if volume > 0 and price > 0:
                        etf_data.append({
                            'ticker': ticker,
                            'name': ticker,
                            'volume': volume,
                            'price': price,
                            'change_percent': change,
                            'market_cap': market_cap,
                            'market': 'US',
                            'type': 'etf',
                            'timestamp': ts
                        })
                except Exception as e:
                    logger.warning(f"Skip ETF {ticker}: {e}")
                    continue

            # 依交易量降序排序
            etf_data.sort(key=lambda x: x['volume'], reverse=True)

            logger.info(f"Fetched {len(etf_data)} US ETFs from universe (sorted by volume)")
            return etf_data

        except Exception as e:
            logger.error(f"Error fetching US ETFs from universe: {e}")
            # Fallback: 使用 config 中的小清單
            logger.warning("Falling back to config ETF list")
            return self._fetch_us_etfs_fallback()

    def _fetch_us_etfs_fallback(self) -> List[Dict]:
        """Fallback: 使用 config 中的 ETF 清單取得數據"""
        try:
            us_etfs = self.us_etfs
            if not us_etfs:
                logger.info("No US ETFs configured for fallback")
                return []

            logger.info(f"Fallback: fetching {len(us_etfs)} US ETFs from config...")
            tickers_obj = yf.Tickers(us_etfs)

            etf_data = []
            for ticker in us_etfs:
                try:
                    info = tickers_obj.tickers.get(ticker)
                    if not info:
                        continue
                    info = info.info

                    volume = info.get('regularMarketVolume', 0) or 0
                    price = info.get('regularMarketPrice', 0) or 0
                    change = info.get('regularMarketChangePercent', 0) or 0
                    market_cap = info.get('marketCap', 0) or 0

                    if volume > 0:
                        etf_data.append({
                            'ticker': ticker,
                            'name': ticker,
                            'volume': volume,
                            'price': price,
                            'change_percent': change,
                            'market_cap': market_cap,
                            'market': 'US',
                            'type': 'etf',
                            'timestamp': datetime.now(pytz.timezone('Asia/Taipei')).isoformat()
                        })
                except Exception as e:
                    logger.warning(f"Skip ETF {ticker}: {e}")
                    continue

            etf_data.sort(key=lambda x: x['volume'], reverse=True)
            logger.info(f"Fallback: fetched {len(etf_data)} US ETFs")
            return etf_data

        except Exception as e:
            logger.error(f"Error fetching US ETFs (fallback): {e}")
            return []

    def get_stock_data(self) -> List[Dict]:
        """獲取股票數據 - 獲取最活躍的股票並排名前10"""
        # 檢查交易時間
        trading_status = self.is_trading_time('all')
        logger.info(f"Trading status - US: {trading_status.get('us', False)}, TW: {trading_status.get('tw', False)}")

        if not trading_status.get('us', False) and not trading_status.get('tw', False):
            logger.info("No market is currently in trading hours, skipping stock data fetch")
            return []

        stock_data = []

        # 台股: 使用台灣證券交易所即時數據
        if trading_status.get('tw', False):
            tw_data = self.get_tw_realtime_data()
            stock_data.extend(tw_data)

        # 美股: 使用yfinance (延遲約15分鐘,對每半小時監控已足夠)
        if trading_status.get('us', False):
            us_data = self.get_us_realtime_data()
            stock_data.extend(us_data)

        logger.info(f"Fetched {len(stock_data)} stocks with volume data")
        return stock_data
    
    def calculate_volume_rankings(self, stock_data: List[Dict]) -> List[Dict]:
        """計算交易量排名 - 美股和台股各取前10"""
        # 分開美股和台股
        us_stocks = [s for s in stock_data if s['market'] == 'US']
        tw_stocks = [s for s in stock_data if s['market'] == 'TW']
        
        # 分別按交易量降序排序
        us_sorted = sorted(us_stocks, key=lambda x: x['volume'], reverse=True)
        tw_sorted = sorted(tw_stocks, key=lambda x: x['volume'], reverse=True)
        
        # 各取前10名
        top_us = us_sorted[:10]
        top_tw = tw_sorted[:10]
        
        # 合並結果,並添加排名
        result = []
        
        # 美股前10 (排名1-10)
        for i, stock in enumerate(top_us, 1):
            stock['rank'] = i
            stock['rank_in_market'] = i
            result.append(stock)
        
        # 台股前10 (排名1-10)
        for i, stock in enumerate(top_tw, 1):
            stock['rank'] = i  # 台股也從1開始排名
            stock['rank_in_market'] = i
            result.append(stock)
        
        logger.info(f"Top 10 US stocks by volume: {len(top_us)} stocks")
        logger.info(f"Top 10 TW stocks by volume: {len(top_tw)} stocks")
        
        return result
    
    def calculate_drop_rankings(self, us_stock_data: List[Dict], us_etf_data: List[Dict]) -> Dict[str, List[Dict]]:
        """計算美股個股和 ETF 的跌幅排行（交易量前N名且跌幅超過3%，按跌幅高到低排列）"""
        # 個股：按交易量降序排序，取前20名
        stocks_sorted = sorted(us_stock_data, key=lambda x: x['volume'], reverse=True)
        top20_stocks = stocks_sorted[:20]
        
        # 過濾出跌幅超過3%的（change_percent < -3）
        drop_stocks = [s for s in top20_stocks if s['change_percent'] < -3]
        
        # 按跌幅高到低排列（跌幅越大，change_percent 越小，所以升序排列）
        drop_stocks_sorted = sorted(drop_stocks, key=lambda x: x['change_percent'])
        
        # ETF：按交易量降序排序，取前10名
        etfs_sorted = sorted(us_etf_data, key=lambda x: x['volume'], reverse=True)
        top10_etfs = etfs_sorted[:10]
        
        # 過濾出跌幅超過3%的
        drop_etfs = [e for e in top10_etfs if e['change_percent'] < -3]
        
        # 按跌幅高到低排列
        drop_etfs_sorted = sorted(drop_etfs, key=lambda x: x['change_percent'])
        
        logger.info(f"US stocks drop rankings (top20 by volume, drop >3%): {len(drop_stocks_sorted)} stocks")
        logger.info(f"US ETFs drop rankings (top10 by volume, drop >3%): {len(drop_etfs_sorted)} ETFs")
        
        return {
            'stocks': drop_stocks_sorted,
            'etfs': drop_etfs_sorted
        }
    
    def format_volume(self, volume: int) -> str:
        """格式化交易量顯示"""
        if volume >= 1_000_000_000:
            return f"{volume / 1_000_000_000:.2f}B"
        elif volume >= 1_000_000:
            return f"{volume / 1_000_000:.2f}M"
        elif volume >= 1_000:
            return f"{volume / 1_000:.2f}K"
        else:
            return str(volume)
    
    def generate_telegram_message(self, top_stocks: List[Dict]) -> str:
        """生成 Telegram 訊息"""
        taipei_tz = pytz.timezone('Asia/Taipei')
        current_time = datetime.now(taipei_tz)
        
        # 分開美股和台股
        us_stocks = [s for s in top_stocks if s.get('market') == 'US']
        tw_stocks = [s for s in top_stocks if s.get('market') == 'TW']
        
        message = f"""
📊 <b>股票交易量排行榜</b>
📅 {current_time.strftime('%Y年%m月%d日 %H:%M')} (台灣時間)

"""
        
        # 美股前10
        if us_stocks:
            message += f"{'='*35}\n"
            message += f"🇺🇸 <b>美股交易量 Top10</b>\n"
            message += f"{'='*35}\n\n"
            
            for stock in us_stocks:
                rank_emoji = "🥇" if stock['rank'] == 1 else "🥈" if stock['rank'] == 2 else "🥉" if stock['rank'] == 3 else f"#{stock['rank']}"
                change_sign = "+" if stock['change_percent'] >= 0 else ""
                change_emoji = "📈" if stock['change_percent'] >= 0 else "📉"
                change_color = "🟢" if stock['change_percent'] >= 0 else "🔴"
                
                message += f"{rank_emoji} <b>{stock['ticker']}</b>\n"
                message += f"   交易量: {self.format_volume(stock['volume'])}\n"
                message += f"   股價: ${stock['price']:.2f}\n"
                message += f"   漲跌幅: {change_emoji} {change_color} {change_sign}{stock['change_percent']:.2f}%\n\n"
        
        # 台股前10
        if tw_stocks:
            message += f"{'='*35}\n"
            message += f"🇹🇼 <b>台股交易量 Top10</b>\n"
            message += f"{'='*35}\n\n"
            
            for stock in tw_stocks:
                rank_emoji = "🥇" if stock['rank'] == 1 else "🥈" if stock['rank'] == 2 else "🥉" if stock['rank'] == 3 else f"#{stock['rank']}"
                change_sign = "+" if stock['change_percent'] >= 0 else ""
                change_emoji = "📈" if stock['change_percent'] >= 0 else "📉"
                change_color = "🟢" if stock['change_percent'] >= 0 else "🔴"

                # 台股使用中文名稱,並在括號中顯示代號
                stock_name = stock.get('name', stock['ticker'])
                message += f"{rank_emoji} <b>{stock_name}</b> ({stock['ticker']})\n"
                message += f"   交易量: {self.format_volume(stock['volume'])}\n"
                message += f"   股價: NT${stock['price']:.2f}\n"
                message += f"   漲跌幅: {change_emoji} {change_color} {change_sign}{stock['change_percent']:.2f}%\n\n"
        
        message += f"{'='*35}\n"
        message += f"🤖 由 WorkBuddy 股票監控系統自動生成\n"
        message += f"📊 數據來源: Yahoo Finance"
        
        return message

    def generate_us_drop_message(self, drop_data: Dict[str, List[Dict]]) -> str:
        """生成美股個股和 ETF 跌幅排行 Telegram 訊息"""
        taipei_tz = pytz.timezone('Asia/Taipei')
        current_time = datetime.now(taipei_tz)
        
        drop_stocks = drop_data.get('stocks', [])
        drop_etfs = drop_data.get('etfs', [])
        
        message = f"""
📉 <b>美股跌幅排行警報</b>
📅 {current_time.strftime('%Y年%m月%d日 %H:%M')} (台灣時間)

"""
        
        # 個股跌幅排行
        if drop_stocks:
            message += f"{'='*35}\n"
            message += f"🇺🇸 <b>美股個股跌幅排行</b>\n"
            message += f"（動態篩選交易量前20名且跌幅超過3%）\n"
            message += f"{'='*35}\n\n"
            
            for i, stock in enumerate(drop_stocks, 1):
                change_sign = "+" if stock['change_percent'] >= 0 else ""
                change_emoji = "📈" if stock['change_percent'] >= 0 else "📉"
                change_color = "🟢" if stock['change_percent'] >= 0 else "🔴"
                
                message += f"{i}. <b>{stock['ticker']}</b>\n"
                message += f"   交易量: {self.format_volume(stock['volume'])}\n"
                message += f"   股價: ${stock['price']:.2f}\n"
                message += f"   跌幅: {change_emoji} {change_color} {change_sign}{stock['change_percent']:.2f}%\n\n"
        else:
            message += f"{'='*35}\n"
            message += f"🇺🇸 <b>美股個股跌幅排行</b>\n"
            message += f"（動態篩選交易量前20名且跌幅超過3%）\n"
            message += f"{'='*35}\n\n"
            message += f"✅ 今日無符合條件的個股（交易量前20名中無跌幅超過3%的個股）\n\n"
        
        # ETF 跌幅排行
        if drop_etfs:
            message += f"{'='*35}\n"
            message += f"📊 <b>美股 ETF 跌幅排行</b>\n"
            message += f"（動態篩選交易量前10名且跌幅超過3%）\n"
            message += f"{'='*35}\n\n"
            
            for i, etf in enumerate(drop_etfs, 1):
                change_sign = "+" if etf['change_percent'] >= 0 else ""
                change_emoji = "📈" if etf['change_percent'] >= 0 else "📉"
                change_color = "🟢" if etf['change_percent'] >= 0 else "🔴"
                
                message += f"{i}. <b>{etf['ticker']}</b>\n"
                message += f"   交易量: {self.format_volume(etf['volume'])}\n"
                message += f"   股價: ${etf['price']:.2f}\n"
                message += f"   跌幅: {change_emoji} {change_color} {change_sign}{etf['change_percent']:.2f}%\n\n"
        else:
            message += f"{'='*35}\n"
            message += f"📊 <b>美股 ETF 跌幅排行</b>\n"
            message += f"（動態篩選交易量前10名且跌幅超過3%）\n"
            message += f"{'='*35}\n\n"
            message += f"✅ 今日無符合條件的 ETF（交易量前10名中無跌幅超過3%的 ETF）\n\n"
        
        message += f"{'='*35}\n"
        message += f"🤖 由 WorkBuddy 股票監控系統自動生成\n"
        message += f"📊 數據來源: Yahoo Finance"
        
        return message
    
    # ── 台指期報價與損益試算 ──────────────────────────────────────
    def get_taiex_quotes(self) -> Dict[str, Dict]:
        """從期交所 MIS API 取得台指期報價（各商品取成交量最大的近月契約）

        回傳 {CID: {symbol,name,last,diff,diff_rate,ref,volume,date,time}}；
        失敗的商品直接略過（容錯）。
        """
        result: Dict[str, Dict] = {}
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "zh-TW,zh;q=0.9",
        })
        for cid in FUT_CIDS:
            payload = {
                "MarketType": "0", "SymbolType": "F", "KindID": "1",
                "CID": cid, "ExpireMonth": "", "RowSize": "全部",
                "PageNo": "", "SortColumn": "", "AscDesc": "A",
            }
            try:
                r = session.post(TAIFEX_QUOTE_URL, json=payload, timeout=15)
                if r.status_code != 200:
                    logger.warning("[TAIFEX] %s HTTP %d", cid, r.status_code)
                    continue
                ql = (r.json().get("RtData") or {}).get("QuoteList") or []
                fut = [x for x in ql if str(x.get("SymbolID", "")).endswith("-F")]
                if not fut:
                    logger.warning("[TAIFEX] %s 查無期貨契約", cid)
                    continue
                top = max(fut, key=lambda x: int(x.get("CTotalVolume") or 0))
                last = float(top.get("CLastPrice") or 0)
                if last <= 0:
                    logger.warning("[TAIFEX] %s 無有效報價", cid)
                    continue
                result[cid] = {
                    "symbol": top.get("SymbolID", ""),
                    "name": top.get("DispCName", ""),
                    "last": last,
                    "diff": float(top.get("CDiff") or 0),          # 漲跌點數(對昨結算)
                    "diff_rate": float(top.get("CDiffRate") or 0), # 漲跌%
                    "ref": float(top.get("CRefPrice") or 0),       # 昨結算價
                    "volume": int(top.get("CTotalVolume") or 0),
                    "date": top.get("CDate", ""),
                    "time": top.get("CTime", ""),
                }
                logger.info("[TAIFEX] %s %s last=%s diff=%s",
                            cid, top.get("DispCName"), last, top.get("CDiff"))
            except Exception as e:
                logger.warning("[TAIFEX] %s 查詢失敗：%s", cid, e)
            time.sleep(0.3)
        return result

    def _load_fut_state(self) -> Dict:
        try:
            with open(FUT_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_fut_state(self, state: Dict) -> None:
        try:
            with open(FUT_STATE_FILE, "w", encoding="utf-8", newline="") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[TAIFEX] 狀態檔寫入失敗：%s", e)

    def _update_fut_baseline(self, quotes: Dict[str, Dict],
                             now: datetime) -> Dict:
        """確保當日 09:00 後有進場基準價（假設 09:00 下單，多單與空單各五口）。

        當日第一次抓到報價時記錄各商品進場價（多空同價）；若監控 09:00 才
        啟動或 Render 重啟（狀態檔消失），則以當時報價為進場價並註明實際時間。
        """
        state = self._load_fut_state()
        today_key = now.strftime("%Y-%m-%d")
        if today_key in state:
            return state[today_key]
        if now.hour * 60 + now.minute < FUT_ENTRY_MINUTES:
            return None  # 尚未到 09:00
        entry = {}
        for cid in ("MXF", "TMF"):
            q = quotes.get(cid)
            if q and q.get("last"):
                entry[cid] = q["last"]
        if not entry:
            return None
        rec = {"entry_time": now.strftime("%H:%M"), "entry": entry}
        state[today_key] = rec
        self._save_fut_state(state)
        logger.info("[TAIFEX] 建立當日進場基準（%s）：%s",
                    rec["entry_time"], entry)
        return rec

    def _format_fut_section(self, quotes: Dict[str, Dict],
                            now: datetime) -> str:
        """組出台指期區塊：漲跌點數（一律顯示）＋ 09:30-11:30 損益試算"""
        if not quotes:
            return ""
        txf = quotes.get("TXF")
        lines = ["", "=" * 35, "📌 <b>台指期</b>"]
        if txf:
            d = txf["diff"]
            sign = "+" if d >= 0 else ""
            emoji = "📈" if d >= 0 else "📉"
            lines.append(
                f"{emoji} {txf['name']}：<b>{txf['last']:,.0f}</b>"
                f"（{sign}{d:,.0f} 點 / {sign}{txf['diff_rate']:.2f}%）"
            )
        # 損益試算：09:30 ~ 11:30，小台與微台分開計算
        # （假設 09:00 進場，多單五口／空單兩口，[2026-10-20] 空單改兩口）
        block = now.hour * 2 + now.minute // 30
        if block in FUT_PNL_BLOCKS:
            state = self._load_fut_state()
            rec = state.get(now.strftime("%Y-%m-%d"))
            if rec and rec.get("entry"):
                lines.append("")
                lines.append(
                    f"💰 <b>損益試算</b>（假設 {rec.get('entry_time', '09:00')} 進場，"
                    f"多單 小台×{FUT_PNL_QTY}、微台×{FUT_PNL_QTY}／"
                    f"空單 小台×{FUT_PNL_SHORT_QTY}、微台×{FUT_PNL_SHORT_QTY}，"
                    f"每點 50/10 元）"
                )
                # 多空雙向：09:00 同時模擬買進五口與賣出兩口，
                # 空單損益與多單相反（點數 × −1）
                for cid, label in (("MXF", "小台"), ("TMF", "微台")):
                    q = quotes.get(cid)
                    ep = rec["entry"].get(cid)
                    if not q or not ep:
                        continue
                    pts = q["last"] - float(ep)
                    sign = "+" if pts >= 0 else ""
                    for side, direction, qty in (
                        ("🟢 多單", 1, FUT_PNL_QTY),
                        ("🔴 空單", -1, FUT_PNL_SHORT_QTY),
                    ):
                        pnl = pts * FUT_CONTRACT_SIZE[cid] * qty * direction
                        lines.append(
                            f"　{side} {label} {qty}口：{float(ep):,.0f} → "
                            f"{q['last']:,.0f}（{sign}{pts:,.0f} 點）→ "
                            f"<b>{'+' if pnl >= 0 else '−'}{abs(pnl):,.0f} 元</b>"
                        )
            else:
                lines.append("（今日尚無進場基準價）")

        # [2026-10-16] 11:30 命中比對：08:00 自訂多空評分指標 vs 台指期走勢
        if block == FUT_HIT_BLOCK:
            try:
                import sentiment_index as senti_mod
                lines.append("")
                lines.append(senti_mod.format_hitrate_section(txf or {}, now).strip("\n"))
            except Exception as e:
                logger.warning("[TAIFEX] 自訂多空評分命中比對失敗：%s", e)
        return "\n".join(lines) + "\n\n"

    def generate_tw_drop_message(self, vol_rank: Dict[str, List[Dict]],
                                 drop_data: Dict[str, List[Dict]],
                                 fut_quotes: Dict[str, Dict] = None) -> str:
        """生成台股推播訊息：監控清單成交量排行（個股/ETF）＋跌幅排行＋台指期"""
        taipei_tz = pytz.timezone('Asia/Taipei')
        current_time = datetime.now(taipei_tz)

        top_stocks = vol_rank.get('stocks', [])
        top_etfs = vol_rank.get('etfs', [])
        drop_stocks = drop_data.get('stocks', [])
        drop_etfs = drop_data.get('etfs', [])

        message = f"""
🇹🇼 <b>台股監控（監控清單即時排行）</b>
📅 {current_time.strftime('%Y年%m月%d日 %H:%M')} (台灣時間)

"""

        def _tw_code(item: Dict) -> str:
            return item.get('code') or self._strip_tw_suffix(item.get('ticker', ''))

        def _tw_vol_str(item: Dict) -> str:
            # MIS 即時資料單位為張；備援（Yahoo/固定清單）資料沿用原格式
            return self.format_tw_volume(item['volume']) if 'code' in item else self.format_volume(item['volume'])

        def _tw_line(item: Dict, rank=None) -> str:
            r = rank if rank is not None else item.get('rank', 0)
            rank_emoji = "🥇" if r == 1 else "🥈" if r == 2 else "🥉" if r == 3 else f"{r}."
            change_sign = "+" if item['change_percent'] >= 0 else ""
            change_emoji = "🟢" if item['change_percent'] >= 0 else "🔴"
            return (
                f"{rank_emoji} <b>{_tw_code(item)} {item['name']}</b>\n"
                f"   量: {_tw_vol_str(item)}｜"
                f"價: NT${item['price']:.2f}｜"
                f"{change_emoji} {change_sign}{item['change_percent']:.2f}%\n"
            )

        # ── 個股成交量排行 ──
        message += f"{'='*35}\n"
        message += f"📈 <b>台股個股成交量排行</b>\n"
        message += f"（監控清單 {len(top_stocks)} 檔，MIS 即時，每半小時更新）\n"
        message += f"{'='*35}\n\n"
        if top_stocks:
            for s in top_stocks:
                message += _tw_line(s) + "\n"
        else:
            message += "（無即時資料）\n\n"

        # ── ETF 成交量排行 ──
        message += f"{'='*35}\n"
        message += f"📊 <b>台股 ETF 成交量排行</b>\n"
        message += f"（監控清單 {len(top_etfs)} 檔，MIS 即時，每半小時更新）\n"
        message += f"{'='*35}\n\n"
        if top_etfs:
            for e in top_etfs:
                message += _tw_line(e) + "\n"
        else:
            message += "（無即時資料）\n\n"

        # ── 跌幅排行（成交量前 50 名中跌幅 >3%）──
        message += f"{'='*35}\n"
        message += f"📉 <b>台股跌幅排行警報</b>\n"
        message += f"（監控標的中跌幅超過3%）\n"
        message += f"{'='*35}\n\n"

        if drop_stocks:
            message += "🔻 <b>個股</b>\n"
            for i, s in enumerate(drop_stocks, 1):
                change_sign = "+" if s['change_percent'] >= 0 else ""
                change_emoji = "🟢" if s['change_percent'] >= 0 else "🔴"
                message += (
                    f"{i}. <b>{_tw_code(s)} {s['name']}</b>\n"
                    f"   量: {_tw_vol_str(s)}｜"
                    f"價: NT${s['price']:.2f}｜"
                    f"{change_emoji} {change_sign}{s['change_percent']:.2f}%\n"
                )
        else:
            message += "✅ 監控標的中無個股跌幅超過3%\n"

        if drop_etfs:
            message += "\n🔻 <b>ETF</b>\n"
            for i, e in enumerate(drop_etfs, 1):
                change_sign = "+" if e['change_percent'] >= 0 else ""
                change_emoji = "🟢" if e['change_percent'] >= 0 else "🔴"
                message += (
                    f"{i}. <b>{_tw_code(e)} {e['name']}</b>\n"
                    f"   量: {_tw_vol_str(e)}｜"
                    f"價: NT${e['price']:.2f}｜"
                    f"{change_emoji} {change_sign}{e['change_percent']:.2f}%\n"
                )
        else:
            message += "✅ 監控標的 ETF 中無跌幅超過3%\n"

        # 台指期區塊（漲跌點數；09:30-11:30 加損益試算）
        if fut_quotes:
            try:
                message += self._format_fut_section(fut_quotes, datetime.now(taipei_tz))
            except Exception as e:
                logger.warning(f"Futures section skipped: {e}")

        message += f"{'='*35}\n"
        message += f"🤖 由 WorkBuddy 股票監控系統自動生成\n"
        message += f"📊 數據來源: {getattr(self, '_tw_data_source', 'Yahoo Finance')} / 期交所 MIS"

        return message

    def send_telegram_message(self, message: str) -> bool:
        """發送通知訊息到所有啟用的渠道"""
        results = self.notification_sender.send_to_all(message)
        
        # 檢查是否至少有一個渠道成功
        return any(results.values())
    
    def run_monitor(self):
        """執行一次監控檢查"""
        logger.info("="*50)
        logger.info("Starting stock monitor check...")
        logger.info("="*50)
        
        # 檢查交易時間
        trading_status = self.is_trading_time('all')
        us_trading = trading_status.get('us', False)
        tw_trading = trading_status.get('tw', False)
        
        if not us_trading and not tw_trading:
            logger.info("No market is currently in trading hours, skipping monitoring")
            logger.info("="*50 + "\n")
            return
        
        # 美股交易期間：使用新的跌幅排行邏輯
        if us_trading:
            logger.info("US market is trading, using drop rankings logic...")
            
            # 獲取美股個股數據
            us_stock_data = self.get_us_realtime_data()
            
            # 獲取美股 ETF 數據
            us_etf_data = self.get_us_etf_data()
            
            if not us_stock_data and not us_etf_data:
                logger.warning("No US stock/ETF data available")
                return
            
            # 計算跌幅排行
            drop_data = self.calculate_drop_rankings(us_stock_data, us_etf_data)
            
            # 顯示結果
            logger.info("\n" + "="*50)
            logger.info("US Stocks Drop Rankings (top20 by volume, drop >3%):")
            logger.info("="*50)
            for i, stock in enumerate(drop_data['stocks'], 1):
                logger.info(f"{i}. {stock['ticker']:>12} - Volume: {self.format_volume(stock['volume']):>10} - Drop: {stock['change_percent']:.2f}%")
            
            logger.info("\n" + "="*50)
            logger.info("US ETFs Drop Rankings (top10 by volume, drop >3%):")
            logger.info("="*50)
            for i, etf in enumerate(drop_data['etfs'], 1):
                logger.info(f"{i}. {etf['ticker']:>12} - Volume: {self.format_volume(etf['volume']):>10} - Drop: {etf['change_percent']:.2f}%")
            
            # 發送通知訊息
            logger.info("\nSending notification report...")
            notification_message = self.generate_us_drop_message(drop_data)
            self.send_telegram_message(notification_message)
            
            logger.info("\nUS drop rankings monitor check completed!")
            logger.info("="*50 + "\n")
        
        # 台股交易期間：監控清單即時成交量排行 + 跌幅排行（2026-10-15 修正範圍）
        if tw_trading:
            logger.info("TW market is trading, fetching watchlist realtime quotes...")

            tw_quotes = self.fetch_tw_market_realtime()

            if tw_quotes:
                vol_rank = self.rank_tw_volume(tw_quotes)
                drop_data = self.rank_tw_drop(tw_quotes)
                self._tw_data_source = "證交所 MIS 即時行情（監控清單）"
            else:
                # MIS 來源失效時退回 Yahoo/TWSE OpenAPI（同一份監控清單）
                logger.warning("TW market-wide quotes empty, falling back to fixed watchlist")
                tw_stock_data = self.get_tw_realtime_data()
                tw_etf_data = self.get_tw_etf_data()
                drop_data = self.calculate_drop_rankings(tw_stock_data, tw_etf_data)
                vol_rank = {'stocks': [], 'etfs': []}
                self._tw_data_source = getattr(self, '_tw_data_source', 'Yahoo Finance')

            # 台指期報價＋09:00 進場基準（獨立容錯）
            fut_quotes = {}
            try:
                taipei_tz = pytz.timezone('Asia/Taipei')
                fut_quotes = self.get_taiex_quotes()
                self._update_fut_baseline(fut_quotes, datetime.now(taipei_tz))
            except Exception as e:
                logger.warning(f"TAIFEX quotes failed, skip futures section: {e}")

            # 顯示結果
            logger.info("\n" + "="*50)
            logger.info("TW Stocks Volume Ranking (watchlist realtime):")
            logger.info("="*50)
            for s in vol_rank['stocks']:
                logger.info(f"{s['rank']}. {s['code']} {s['name']} - Volume: {self.format_tw_volume(s['volume'])} - Price: {s['price']:.2f} ({s['change_percent']:+.2f}%)")

            logger.info("\n" + "="*50)
            logger.info("TW ETFs Volume Ranking (watchlist realtime):")
            logger.info("="*50)
            for e in vol_rank['etfs']:
                logger.info(f"{e['rank']}. {e['code']} {e['name']} - Volume: {self.format_tw_volume(e['volume'])} - Price: {e['price']:.2f} ({e['change_percent']:+.2f}%)")

            logger.info("\n" + "="*50)
            logger.info("TW Drop Rankings (watchlist, drop >3%):")
            logger.info("="*50)
            for i, stock in enumerate(drop_data['stocks'], 1):
                logger.info(f"{i}. {stock['code'] if 'code' in stock else stock['ticker']} - Volume: {self.format_tw_volume(stock['volume'])} - Drop: {stock['change_percent']:.2f}%")

            # 發送通知訊息
            logger.info("\nSending notification report...")
            notification_message = self.generate_tw_drop_message(vol_rank, drop_data, fut_quotes=fut_quotes)
            self.send_telegram_message(notification_message)

            logger.info("\nTW monitor check completed!")
            logger.info("="*50 + "\n")
    
    def run_half_hourly(self):
        """在交易時間內每半小時執行監控"""
        logger.info("Starting half-hourly stock monitor (trading hours only)...")
        last_run_block = -1  # 每半小時一個 block: hour*2 + minute//30

        while True:
            try:
                # 獲取當前半小時區塊 (使用台灣時區作為基準)
                taipei_tz = pytz.timezone('Asia/Taipei')
                now = datetime.now(taipei_tz)
                current_block = now.hour * 2 + (now.minute // 30)

                # 檢查是否到達新的半小時區塊
                if current_block != last_run_block:
                    # 檢查交易時間
                    trading_status = self.is_trading_time('all')
                    us_trading = trading_status.get('us', False)
                    tw_trading = trading_status.get('tw', False)

                    if us_trading or tw_trading:
                        # 至少有一個市場在交易中,執行監控
                        self.run_monitor()
                        last_run_block = current_block
                    else:
                        logger.debug("No market is in trading hours, skipping stock monitor check")
                        last_run_block = current_block

                # 每分鐘檢查一次是否到新的半小時區塊
                time.sleep(60)

            except KeyboardInterrupt:
                logger.info("\nStopping monitor...")
                break
            except Exception as e:
                logger.error(f"Error in half-hourly loop: {e}")
                time.sleep(60)  # 出錯後等待1分鐘再重試


def main():
    """主函數"""
    import argparse
    parser = argparse.ArgumentParser(description='Stock Volume Monitor (US + TW) - Half-Hourly')
    parser.add_argument('--once', action='store_true', help='Run once immediately and exit (ignore trading hours check)')
    args = parser.parse_args()

    try:
        print("""
╔═══════════════════════════════════════════════════════╗
║                                                       ║
║        股票每半小時交易量監控系統                      ║
║        Stock Half-Hourly Volume Monitor                 ║
║        (美股 + 台股)                                  ║
║                                                       ║
╚═══════════════════════════════════════════════════════╝
        """)
    except:
        pass
    
    monitor = StockMonitor()
    
    if args.once:
        # 立即執行一次並退出，不受交易時間限制
        logger.info("Running stock monitor once (--once mode)...")
        monitor.run_monitor()
        logger.info("Done.")
    else:
        # 預設運行每半小時監控模式
        monitor.run_half_hourly()


if __name__ == "__main__":
    main()
