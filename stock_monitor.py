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
                except Exception as e:
                    logger.warning(f"Skip {ticker}: {e}")
                    continue

            logger.info(f"Fetched {len(stock_data)} TW stocks from Yahoo Finance")
            return stock_data

        except Exception as e:
            logger.error(f"Error fetching TW stocks from Yahoo Finance: {e}")
            return []

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
                except Exception as e:
                    logger.warning(f"Skip ETF {ticker}: {e}")
                    continue

            logger.info(f"Fetched {len(etf_data)} TW ETFs")
            return etf_data

        except Exception as e:
            logger.error(f"Error fetching TW ETFs: {e}")
            return []

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
    
    def generate_tw_drop_message(self, drop_data: Dict[str, List[Dict]]) -> str:
        """生成台股個股和 ETF 跌幅排行 Telegram 訊息"""
        taipei_tz = pytz.timezone('Asia/Taipei')
        current_time = datetime.now(taipei_tz)
        
        drop_stocks = drop_data.get('stocks', [])
        drop_etfs = drop_data.get('etfs', [])
        
        message = f"""
📉 <b>台股跌幅排行警報</b>
📅 {current_time.strftime('%Y年%m月%d日 %H:%M')} (台灣時間)

"""
        
        # 個股跌幅排行
        if drop_stocks:
            message += f"{'='*35}\n"
            message += f"🇹🇼 <b>台股個股跌幅排行</b>\n"
            message += f"（交易量前20名且跌幅超過3%）\n"
            message += f"{'='*35}\n\n"
            
            for i, stock in enumerate(drop_stocks, 1):
                change_sign = "+" if stock['change_percent'] >= 0 else ""
                change_emoji = "📈" if stock['change_percent'] >= 0 else "📉"
                change_color = "🟢" if stock['change_percent'] >= 0 else "🔴"
                stock_code = self._strip_tw_suffix(stock['ticker'])
                stock_name = stock['name']
                
                message += f"{i}. <b>{stock_code} {stock_name}</b>\n"
                message += f"   交易量: {self.format_volume(stock['volume'])}\n"
                message += f"   股價: NT${stock['price']:.2f}\n"
                message += f"   跌幅: {change_emoji} {change_color} {change_sign}{stock['change_percent']:.2f}%\n\n"
        else:
            message += f"{'='*35}\n"
            message += f"🇹🇼 <b>台股個股跌幅排行</b>\n"
            message += f"（交易量前20名且跌幅超過3%）\n"
            message += f"{'='*35}\n\n"
            message += f"✅ 今日無符合條件的個股（交易量前20名中無跌幅超過3%的個股）\n\n"
        
        # ETF 跌幅排行
        if drop_etfs:
            message += f"{'='*35}\n"
            message += f"📊 <b>台股 ETF 跌幅排行</b>\n"
            message += f"（交易量前10名且跌幅超過3%）\n"
            message += f"{'='*35}\n\n"
            
            for i, etf in enumerate(drop_etfs, 1):
                change_sign = "+" if etf['change_percent'] >= 0 else ""
                change_emoji = "📈" if etf['change_percent'] >= 0 else "📉"
                change_color = "🟢" if etf['change_percent'] >= 0 else "🔴"
                etf_code = self._strip_tw_suffix(etf['ticker'])
                etf_name = etf['name']
                
                message += f"{i}. <b>{etf_code} {etf_name}</b>\n"
                message += f"   交易量: {self.format_volume(etf['volume'])}\n"
                message += f"   股價: NT${etf['price']:.2f}\n"
                message += f"   跌幅: {change_emoji} {change_color} {change_sign}{etf['change_percent']:.2f}%\n\n"
        else:
            message += f"{'='*35}\n"
            message += f"📊 <b>台股 ETF 跌幅排行</b>\n"
            message += f"（交易量前10名且跌幅超過3%）\n"
            message += f"{'='*35}\n\n"
            message += f"✅ 今日無符合條件的 ETF（交易量前10名中無跌幅超過3%的 ETF）\n\n"
        
        message += f"{'='*35}\n"
        message += f"🤖 由 WorkBuddy 股票監控系統自動生成\n"
        message += f"📊 數據來源: Yahoo Finance"
        
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
        
        # 台股交易期間：使用跌幅排行邏輯
        if tw_trading:
            logger.info("TW market is trading, using drop rankings logic...")
            
            # 獲取台股個股數據
            tw_stock_data = self.get_tw_realtime_data()
            
            # 獲取台股 ETF 數據
            tw_etf_data = self.get_tw_etf_data()
            
            if not tw_stock_data and not tw_etf_data:
                logger.warning("No TW stock/ETF data available")
                return
            
            # 計算跌幅排行（復用同一個函數）
            drop_data = self.calculate_drop_rankings(tw_stock_data, tw_etf_data)
            
            # 顯示結果
            logger.info("\n" + "="*50)
            logger.info("TW Stocks Drop Rankings (top20 by volume, drop >3%):")
            logger.info("="*50)
            for i, stock in enumerate(drop_data['stocks'], 1):
                logger.info(f"{i}. {stock['ticker']:>12} - Volume: {self.format_volume(stock['volume']):>10} - Drop: {stock['change_percent']:.2f}%")
            
            logger.info("\n" + "="*50)
            logger.info("TW ETFs Drop Rankings (top10 by volume, drop >3%):")
            logger.info("="*50)
            for i, etf in enumerate(drop_data['etfs'], 1):
                logger.info(f"{i}. {etf['ticker']:>12} - Volume: {self.format_volume(etf['volume']):>10} - Drop: {etf['change_percent']:.2f}%")
            
            # 發送通知訊息
            logger.info("\nSending notification report...")
            notification_message = self.generate_tw_drop_message(drop_data)
            self.send_telegram_message(notification_message)
            
            logger.info("\nTW drop rankings monitor check completed!")
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
