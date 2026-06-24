#!/usr/bin/env python3

"""

新闻监控工具 - 每日热门文章监控

News Monitor - Daily Popular Articles Tracker

"""



import feedparser

import requests

from datetime import datetime, timedelta

import time

import json

import logging

import math

import xml.etree.ElementTree as ET

from typing import List, Dict, Tuple, Optional

import yfinance as yf

from deep_translator import GoogleTranslator

from notification_sender import NotificationSender

import html

from bs4 import BeautifulSoup



# 早上 8 點財經新聞取材來源(已實測可用):

#   媒體:CNBC、Wall Street Journal、Bloomberg、MarketWatch、Financial Times、Yahoo Finance

#   投資分析:Seeking Alpha

#   科技媒體:科技新報 TechNews(繁體中文 RSS)、Digitimes(中文科技產業 RSS)

#   總經數據:MacroMicro 財經M平方(繁體中文 RSS,含行情快報/總經解讀)

#   官方經濟數據:美國勞工部(BLS) - CPI、PPI、非農就業、失業率(公開 API)

#   注:Reuters/Morningstar/Schwab 公開 RSS 已失效,已替換為同等級可用來源

# 另附美股選擇權 Call/Put 交易量前十排行、名人交易揭露、SEC 官方 13F 持倉



# 配置日誌

logging.basicConfig(

    level=logging.INFO,

    format='%(asctime)s - %(levelname)s - %(message)s',

    handlers=[

        logging.FileHandler('news_monitor.log', encoding='utf-8'),

        logging.StreamHandler()

    ]

)

logger = logging.getLogger(__name__)





class NewsMonitor:

    """新闻监控类"""

    

    def __init__(self, config_file: str = 'config.json'):

        """初始化监控器"""

        self.config = self.load_config(config_file)

        self.notification_sender = NotificationSender(self.config)

        self.news_config = self.config.get('news', {})

        self.timezone = self.news_config.get('timezone', 'US/Eastern')

        

        # 新闻源配置 - 早上8點:主要財經媒體 + 投資研究機構

        # ✅ 以下 URL 均經過實際測試可正常存取

        self.news_sources = [

            {

                'name': 'CNBC',

                'rss_url': 'https://www.cnbc.com/id/10000664/device/rss/rss.html',

                'base_url': 'https://www.cnbc.com'

            },

            {

                'name': 'Wall Street Journal',

                'rss_url': 'https://feeds.a.dj.com/rss/RSSMarketsMain.xml',

                'base_url': 'https://www.wsj.com'

            },

            {

                # Reuters RSS 已停用公開 feed,改用 Bloomberg(同等級媒體)

                'name': 'Bloomberg',

                'rss_url': 'https://feeds.bloomberg.com/markets/news.rss',

                'base_url': 'https://www.bloomberg.com'

            },

            {

                # MarketWatch:道瓊旗下,與 WSJ 同集團,提供市場快訊

                'name': 'MarketWatch',

                'rss_url': 'https://feeds.marketwatch.com/marketwatch/topstories/',

                'base_url': 'https://www.marketwatch.com'

            },

            {

                # Yahoo Finance:涵蓋廣,常轉載 Reuters/AP 財經電訊

                'name': 'Yahoo Finance',

                'rss_url': 'https://finance.yahoo.com/rss/topstories',

                'base_url': 'https://finance.yahoo.com'

            },

            {

                # Financial Times:歐洲最重要財經媒體

                'name': 'Financial Times',

                'rss_url': 'https://www.ft.com/rss/home/uk',

                'base_url': 'https://www.ft.com'

            },

            {

                'name': 'Seeking Alpha',

                'rss_url': 'https://seekingalpha.com/feed.xml',

                'base_url': 'https://seekingalpha.com'

            },

            {

                # 科技新報:台灣繁體中文科技財經媒體,報導半導體/AI/供應鏈/財報

                'name': '科技新報 TechNews',

                'rss_url': 'https://technews.tw/feed/',

                'base_url': 'https://technews.tw'

            },

            {

                # MacroMicro 財經M平方:台灣最大總經數據平台,同時提供繁體中文新聞分析

                # RSS 涵蓋行情快報、總經解讀、圖表分析等深度內容

                'name': 'MacroMicro 財經M平方',

                'rss_url': 'https://www.macromicro.me/feed',

                'base_url': 'https://www.macromicro.me'

            },

            {

                # Digitimes:台灣專業科技產業媒體,涵蓋半導體/AI/供應鏈/IC設計/製造

                # 使用 gb-www 中文 RSS(標題為繁/簡體中文,無需翻譯)

                # 免費 RSS 可抓標題摘要,全文需訂閱

                'name': 'Digitimes 科技媒體',

                'rss_url': 'https://gb-www.digitimes.com.tw/tech/rss/xml/xmlrss_10_0_cn.xml',

                'base_url': 'https://www.digitimes.com'

            },

        ]

        

        # 初始化翻译器

        self.translator = GoogleTranslator(source='auto', target='zh-TW')

        

        logger.info(f"NewsMonitor initialized with {len(self.news_sources)} sources")



        # ── 名人金融交易監控 ──────────────────────────────────────────────

        # 結構:{'name': 顯示名稱, 'keywords': [搜尋關鍵字], 'category': 分類}

        self.vip_targets = [

            # 川普家族

            {'name': 'Donald Trump Jr',  'keywords': ['donald trump jr', 'trump jr'],      'category': '川普家族'},

            {'name': 'Eric Trump',        'keywords': ['eric trump'],                        'category': '川普家族'},

            {'name': 'Ivanka Trump',      'keywords': ['ivanka trump', 'ivanka'],            'category': '川普家族'},

            {'name': 'Jared Kushner',     'keywords': ['jared kushner', 'kushner'],          'category': '川普家族'},

            {'name': 'Barron Trump',      'keywords': ['barron trump'],                      'category': '川普家族'},

            {'name': 'Lara Trump',        'keywords': ['lara trump'],                        'category': '川普家族'},

            # 裴洛西夫婦

            {'name': 'Nancy Pelosi',      'keywords': ['nancy pelosi', 'pelosi'],            'category': '裴洛西夫婦'},

            {'name': 'Paul Pelosi',       'keywords': ['paul pelosi'],                       'category': '裴洛西夫婦'},

            # 科技名人

            {'name': 'Elon Musk',         'keywords': ['elon musk', 'musk'],                 'category': '科技名人'},

            {'name': 'Bill Gates',        'keywords': ['bill gates', 'gates foundation'],    'category': '科技名人'},

            {'name': 'Peter Thiel',       'keywords': ['peter thiel', 'thiel'],              'category': '科技名人'},

            {'name': 'Jeff Bezos',        'keywords': ['jeff bezos', 'bezos'],               'category': '科技名人'},

            {'name': 'Mark Zuckerberg',   'keywords': ['mark zuckerberg', 'zuckerberg'],     'category': '科技名人'},

            {'name': 'Tim Cook',          'keywords': ['tim cook'],                          'category': '科技名人'},

            {'name': 'Jensen Huang',      'keywords': ['jensen huang'],                      'category': '科技名人'},

            # 創投 / 投資名人

            {'name': 'Warren Buffett',    'keywords': ['warren buffett', 'buffett', 'berkshire hathaway'], 'category': '投資大師'},

            {'name': 'Charlie Munger',    'keywords': ['charlie munger', 'munger'],          'category': '投資大師'},

            {'name': 'Michael Burry',     'keywords': ['michael burry', 'burry'],            'category': '投資大師'},

            {'name': 'Cathie Wood',       'keywords': ['cathie wood', 'ark invest'],         'category': '投資大師'},

            {'name': 'Ray Dalio',         'keywords': ['ray dalio', 'bridgewater'],          'category': '投資大師'},

            {'name': 'George Soros',      'keywords': ['george soros', 'soros'],             'category': '投資大師'},

        ]



        # ── 13F 官方申報監控機構清單 ─────────────────────────────────────

        # 結構:{'name': 顯示名稱, 'cik': SEC CIK號, 'category': 分類,

        #        'display_name': 中文簡稱, 'keywords': [RSS新聞搜尋關鍵字]}

        # CIK 編號:SEC EDGAR 唯一識別碼

        self.institutions_13f = [

            # 投資大師 / 對沖基金

            {

                'name': 'Berkshire Hathaway (Buffett)',

                'cik': '0001067983', 'category': '投資大師', 'display_name': '巴菲特/波克夏',

                'keywords': ['berkshire hathaway', 'warren buffett', 'buffett', 'berkshire'],

            },

            {

                'name': 'Soros Fund Management',

                'cik': '0001029730', 'category': '投資大師', 'display_name': '索羅斯基金',

                'keywords': ['soros fund', 'george soros', 'soros'],

            },

            {

                'name': 'Bridgewater Associates',

                'cik': '0001350694', 'category': '投資大師', 'display_name': '橋水基金(達里奧)',

                'keywords': ['bridgewater', 'ray dalio', 'dalio'],

            },

            {

                'name': 'Scion Asset Management (Burry)',

                'cik': '0001649339', 'category': '投資大師', 'display_name': '巴瑞/Scion',

                'keywords': ['michael burry', 'burry', 'scion asset'],

            },

            {

                'name': 'ARK Investment Management',

                'cik': '0001697748', 'category': '投資大師', 'display_name': '凱西·伍德/ARK',

                'keywords': ['cathie wood', 'ark invest', 'ark innovation', 'cathie'],

            },

            # 主要科技公司大股東 / 家族辦公室

            {

                'name': 'Bill & Melinda Gates Foundation Trust',

                'cik': '0001166559', 'category': '科技名人', 'display_name': '蓋茲基金會',

                'keywords': ['bill gates', 'gates foundation', 'melinda gates'],

            },

            # 大型機構(流動性指標)

            {

                'name': 'Pershing Square Capital (Ackman)',

                'cik': '0001336528', 'category': '投資大師', 'display_name': '艾克曼/Pershing',

                'keywords': ['bill ackman', 'ackman', 'pershing square'],

            },

            {

                'name': 'Duquesne Family Office (Druckenmiller)',

                'cik': '0001536411', 'category': '投資大師', 'display_name': 'Druckenmiller',

                'keywords': ['druckenmiller', 'stanley druckenmiller', 'duquesne'],

            },

        ]

        # ── 13F 持倉公司英文名稱 → 中文對照表 ──────────────────────────

        # SEC 13F 的 nameOfIssuer 欄位為英文大寫縮寫,此處提供中文標準譯名

        # key 使用小寫以方便比對(比對時統一 .lower().strip())

        self.company_name_zh: Dict[str, str] = {

            # 科技股

            'apple inc':                    '蘋果',

            'apple':                        '蘋果',

            'microsoft corp':               '微軟',

            'microsoft corporation':        '微軟',

            'microsoft':                    '微軟',

            'amazon com inc':               '亞馬遜',

            'amazon.com inc':               '亞馬遜',

            'amazon':                       '亞馬遜',

            'alphabet inc':                 'Alphabet (Google)',

            'alphabet inc cl a':            'Alphabet A (Google)',

            'alphabet inc cl c':            'Alphabet C (Google)',

            'alphabet':                     'Alphabet (Google)',

            'meta platforms inc':           'Meta (臉書)',

            'meta platforms inc cl a':      'Meta (臉書)',

            'meta platforms':               'Meta (臉書)',

            'facebook inc':                 'Meta (臉書)',

            'nvidia corp':                  '輝達 NVIDIA',

            'nvidia':                       '輝達 NVIDIA',

            'tesla inc':                    '特斯拉',

            'tesla':                        '特斯拉',

            'netflix inc':                  'Netflix',

            'netflix':                      'Netflix',

            'salesforce inc':               'Salesforce',

            'salesforce.com inc':           'Salesforce',

            'oracle corp':                  'Oracle',

            'oracle':                       'Oracle',

            'intel corp':                   '英特爾',

            'intel':                        '英特爾',

            'advanced micro devices':       'AMD',

            'amd':                          'AMD',

            'broadcom inc':                 '博通',

            'broadcom':                     '博通',

            'qualcomm inc':                 '高通',

            'qualcomm':                     '高通',

            'taiwan semiconductor':         '台積電 TSMC',

            'taiwan semiconductor mfg':     '台積電 TSMC',

            'tsmc':                         '台積電 TSMC',

            'alibaba group':                '阿里巴巴',

            'alibaba':                      '阿里巴巴',

            'tencent':                      '騰訊',

            'baidu inc':                    '百度',

            'shopify inc':                  'Shopify',

            'snowflake inc':                'Snowflake',

            'palantir technologies':        'Palantir',

            'uber technologies':            'Uber',

            'airbnb inc':                   'Airbnb',

            'crowdstrike holdings':         'CrowdStrike',

            # 金融股

            'jpmorgan chase':               '摩根大通',

            'jpmorgan chase & co':          '摩根大通',

            'jp morgan':                    '摩根大通',

            'bank of america':              '美國銀行',

            'bank of america corp':         '美國銀行',

            'wells fargo':                  '富國銀行',

            'wells fargo & co':             '富國銀行',

            'goldman sachs':                '高盛',

            'goldman sachs group':          '高盛',

            'morgan stanley':               '摩根士丹利',

            'citigroup inc':                '花旗集團',

            'citigroup':                    '花旗集團',

            'american express':             '美國運通',

            'american express co':          '美國運通',

            'visa inc':                     'Visa',

            'mastercard inc':               'Mastercard',

            'mastercard':                   'Mastercard',

            'berkshire hathaway':           '波克夏·哈薩威',

            'berkshire hathaway inc':       '波克夏·哈薩威',

            'charles schwab':               '嘉信理財',

            # 消費 / 零售

            'walmart inc':                  '沃爾瑪',

            'walmart':                      '沃爾瑪',

            'costco wholesale':             'Costco',

            'costco wholesale corp':        'Costco',

            'home depot inc':               '家得寶',

            'home depot':                   '家得寶',

            'mcdonalds corp':               '麥當勞',

            'nike inc':                     '耐吉 Nike',

            'starbucks corp':               '星巴克',

            'coca cola co':                 '可口可樂',

            'coca-cola co':                 '可口可樂',

            'pepsi':                        '百事可樂',

            'pepsico inc':                  '百事可樂',

            # 醫療 / 生技

            'johnson & johnson':            '嬌生',

            'unitedhealth group':           '聯合健康',

            'eli lilly':                    '禮來製藥',

            'eli lilly & co':               '禮來製藥',

            'pfizer inc':                   '輝瑞',

            'pfizer':                       '輝瑞',

            'abbvie inc':                   'AbbVie',

            'merck & co':                   '默克',

            'novo nordisk':                 '諾和諾德',

            # 能源

            'exxon mobil':                  '埃克森美孚',

            'exxon mobil corp':             '埃克森美孚',

            'chevron corp':                 '雪佛龍',

            'chevron':                      '雪佛龍',

            # ETF / 基金

            'spdr s&p 500':                 'SPY ETF (S&P500)',

            'spdr':                         'SPDR ETF',

            'invesco qqq':                  'QQQ ETF (那斯達克)',

            'ishares':                      'iShares ETF',

            'vanguard':                     '先鋒基金 ETF',

        }



        # 13F 數據快取(避免每次都重新爬取,兩小時過期)

        self._13f_cache: Dict = {}

        self._13f_cache_time: Optional[datetime] = None

        self._13f_cache_ttl_hours: int = 6  # 13F 數據每季度更新一次,快取6小時



        # 監控選擇權的美股清單(市值大、選擇權流動性高)

        self.options_tickers = [

            "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",

            "AMD", "NFLX", "SPY", "QQQ", "AVGO", "JPM", "BAC", "XOM",

            "INTC", "CRM", "ORCL", "BABA", "MU"

        ]



        # ── SEC Form 4 高管持股異動監控清單 ────────────────────────────

        # 僅追蹤科技七巨頭 CEO / CFO 對自家股票的買賣申報

        # CIK 來源:SEC EDGAR 官方

        self.form4_companies = [

            {'name': 'Apple',     'zh': '蘋果',           'cik': '0000320193',  'ticker': 'AAPL'},

            {'name': 'Microsoft', 'zh': '微軟',           'cik': '0000789019',  'ticker': 'MSFT'},

            {'name': 'NVIDIA',    'zh': '輝達 NVIDIA',    'cik': '0001045810',  'ticker': 'NVDA'},

            {'name': 'Alphabet',  'zh': 'Alphabet(Google)','cik': '0001652044', 'ticker': 'GOOGL'},

            {'name': 'Amazon',    'zh': '亞馬遜',          'cik': '0001018724', 'ticker': 'AMZN'},

            {'name': 'Meta',      'zh': 'Meta(臉書)',      'cik': '0001326801', 'ticker': 'META'},

            {'name': 'Tesla',     'zh': '特斯拉',          'cik': '0001318605', 'ticker': 'TSLA'},

            # 僅追蹤七巨頭，不再追蹤延伸科技龍頭

        ]

        # 高管職銜白名單(只取這些職位的 Form 4)

        # 僅追蹤 CEO / CFO 兩個核心職位

        self.form4_titles_whitelist = [

            'ceo', 'chief executive officer', 'chief executive',

            'cfo', 'chief financial officer', 'chief financial',

        ]

        # Form 4 快取(24 小時,避免頻繁爬取)

        self._form4_cache: List[Dict] = []

        self._form4_cache_time: Optional[datetime] = None

        self._form4_cache_ttl_hours: int = 12

        # Form 4 已處理記錄(保留用,不再作過濾):key = accession_no

        self._form4_sent: Dict[str, datetime] = {}



        # ── IPO 訊息監控 ────────────────────────────────────────────

        # 來源:CNBC / WSJ / Bloomberg / MarketWatch / FT / Seeking Alpha

        # IPO 已發送記錄(7日去重):key = 標題 hash

        self._ipo_sent: Dict[str, datetime] = {}

        # IPO 快取(12 小時)

        self._ipo_cache: List[Dict] = []

        self._ipo_cache_time: Optional[datetime] = None

        self._ipo_cache_ttl_hours: int = 12



        # ── 美股財報公布監控(科技七巨頭)─────────────────────────────

        # 來源:CNBC / WSJ / Bloomberg / MarketWatch / FT / Seeking Alpha

        # 7日內同一標題不重複推播

        self._earnings_sent: Dict[str, datetime] = {}

        # 財報快取(12 小時)

        self._earnings_cache: List[Dict] = []

        self._earnings_cache_time: Optional[datetime] = None

        self._earnings_cache_ttl_hours: int = 12

        # 科技七巨頭公司名稱對照表(用於財報關鍵字過濾)

        self.magnificent7 = {

            'AAPL': {'name': 'Apple', 'zh': '蘋果'},

            'MSFT': {'name': 'Microsoft', 'zh': '微軟'},

            'GOOGL': {'name': 'Alphabet', 'zh': '谷歌'},

            'GOOG': {'name': 'Alphabet', 'zh': '谷歌'},

            'AMZN': {'name': 'Amazon', 'zh': '亞馬遜'},

            'NVDA': {'name': 'NVIDIA', 'zh': '輝達'},

            'META': {'name': 'Meta', 'zh': 'Meta'},

            'TSLA': {'name': 'Tesla', 'zh': '特斯拉'},

        }



        # ── 美國勞工部(BLS) 經濟指標快取 ──────────────────────────────

        # 使用 BLS 公開 API v1(無需 Key),每次呼叫取最近一期發布值

        # 快取 12 小時避免頻繁呼叫

        self._bls_cache: List[Dict] = []

        self._bls_cache_time: Optional[datetime] = None

        self._bls_cache_ttl_hours: int = 12

        # 已推播記錄(7日去重):key = series_id + period(如 "CUUR0000SA0_2025M04")

        self._bls_sent: Dict[str, datetime] = {}



        # BLS 監控系列清單(v1 公開 API,無需 Key)

        self.bls_series = [

            {

                'series_id': 'CUUR0000SA0',

                'name_zh':   '消費者物價指數 CPI(全部項目)',

                'name_en':   'CPI-U All Items',

                'unit':      '指數',

                'category':  '通膨',

            },

            {

                'series_id': 'CUUR0000SA0L1E',

                'name_zh':   '核心 CPI(扣除食品與能源)',

                'name_en':   'Core CPI (ex Food & Energy)',

                'unit':      '指數',

                'category':  '通膨',

            },

            {

                'series_id': 'WPSFD4',

                'name_zh':   '生產者物價指數 PPI(最終需求)',

                'name_en':   'PPI Final Demand',

                'unit':      '指數',

                'category':  '通膨',

            },

            {

                'series_id': 'LNS14000000',

                'name_zh':   '失業率',

                'name_en':   'Unemployment Rate',

                'unit':      '%',

                'category':  '就業',

            },

            {

                'series_id': 'CES0000000001',

                'name_zh':   '非農就業人數',

                'name_en':   'Nonfarm Payroll Employment',

                'unit':      '千人',

                'category':  '就業',

            },

        ]

    

    def _translate_company_name(self, english_name: str) -> str:

        """

        將 13F XML 中的英文公司名稱轉為中文顯示.

        優先查詢 company_name_zh 對照表(精確 + 模糊前綴比對),

        若無命中則嘗試以 GoogleTranslator 翻譯,失敗時保留原文.

        回傳格式:[中文名稱](若已知)或原文.

        """

        if not english_name:

            return english_name

        key = english_name.lower().strip()



        # 1. 精確比對

        if key in self.company_name_zh:

            return self.company_name_zh[key]



        # 2. 前綴模糊比對(處理如 "APPLE INC CL A" 這類含股份類別的名稱)

        for en_key, zh_val in self.company_name_zh.items():

            if key.startswith(en_key) or en_key.startswith(key[:min(len(key), 12)]):

                return zh_val



        # 3. Google Translate fallback(限短名稱以節省時間)

        if len(english_name) <= 60:

            try:

                translated = self.translator.translate(english_name)

                # 若翻譯結果與原文相同(無法翻譯),直接回傳原文

                if translated and translated.lower() != english_name.lower():

                    return translated

            except Exception:

                pass



        # 4. 無法翻譯,回傳原文

        return english_name



    def load_config(self, config_file: str) -> Dict:

        """加载配置文件"""

        try:

            with open(config_file, 'r', encoding='utf-8') as f:

                return json.load(f)

        except FileNotFoundError:

            logger.warning(f"Config file {config_file} not found, using defaults")

            return {}

        except Exception as e:

            logger.error(f"Error loading config: {e}")

            return {}

    

    @staticmethod

    def _clean_html(text: str) -> str:

        """移除 HTML 標籤與殘留碎片（處理 Digitimes 等來源的 <img src="//..."><br /> 問題）"""

        if not text:

            return ''

        import re

        # 先用 BeautifulSoup 剝離所有 HTML 標籤

        cleaned = BeautifulSoup(text, 'html.parser').get_text(separator=' ')

        # 補刀：移除任何殘留的 <...> 片段（BeautifulSoup 可能遺漏不完整標籤）

        cleaned = re.sub(r'<[^>]*>', '', cleaned)

        # 壓縮多餘空白

        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        return cleaned



    # 用於過濾伺服器錯誤標題的 pattern（feedparser 解析到 Error 頁面時 title 會帶有此類字串）

    # 使用 (keyword, max_len) 格式：標題長度 <= max_len 且包含 keyword 才判為錯誤頁

    _ERROR_TITLE_PATTERNS = [

        # ── HTTP 狀態碼錯誤（max_len=200 以防 CDN/WAF 回傳長描述性標題） ──

        ('error 404', 200),

        ('error 500', 200),

        ('error 502', 200),

        ('error 503', 200),

        ('error 504', 200),

        # Cloudflare 額外錯誤碼 (520-524)

        ('error 520', 200),

        ('error 521', 200),

        ('error 522', 200),

        ('error 523', 200),

        ('error 524', 200),

        # ── Web 伺服器錯誤 ──

        ('404 not found', 200),

        ('502 bad gateway', 200),

        ('503 service unavailable', 200),

        ('504 gateway timeout', 200),

        ('500 internal server error', 200),

        ('internal server error', 200),

        ('server error', 80),

        ('access denied', 80),

        # ── CDN / WAF / Proxy 錯誤 ──

        ('an error occurred while processing your request', 200),

        ('the request could not be satisfied', 200),

        ('request could not be satisfied', 200),

        ('this page isn', 200),

        ('service temporarily unavailable', 200),

        # ── WordPress / PHP 錯誤（科技新報 TechNews 等 WP 站點） ──

        ('critical error on this website', 200),

        ('critical error on your website', 200),

        ('there has been a critical error', 200),

        ('fatal error:', 200),

        ('briefly unavailable for scheduled maintenance', 200),

        ('error establishing a database connection', 200),

    ]



    @staticmethod

    def _is_error_title(title: str) -> bool:

        """判斷 entry title 是否為伺服器錯誤頁的標題（如 'Error 500 (Server Error)!!'）"""

        if not title:

            return True

        t = title.lower().strip()

        for pat, max_len in NewsMonitor._ERROR_TITLE_PATTERNS:

            if pat in t and len(t) <= max_len:

                return True

        return False



    @staticmethod

    def _safe_title_zh(item: dict, title_key: str = 'title_zh', orig_key: str = 'title') -> str:

        """

        取 title_zh，若為空或含錯誤頁文字則降級回原文 title。

        用於所有格式化函數，防止 Google Translate 500 回傳值滲入推播訊息。

        """

        zh = item.get(title_key, '') or ''

        if not zh or NewsMonitor._is_error_title(zh):

            return item.get(orig_key, '') or ''

        return zh



    @staticmethod

    def _safe_summary_zh(item: dict, sum_key: str = 'summary_zh', orig_key: str = 'summary') -> str:

        """

        取 summary_zh，若為空或含錯誤頁文字則降級回原文 summary。

        """

        zh = item.get(sum_key, '') or ''

        if not zh or NewsMonitor._is_error_title(zh):

            return item.get(orig_key, '') or ''

        return zh



    def fetch_rss_feed(self, source: Dict) -> List[Dict]:

        """获取RSS feed内容（含 HTTP 狀態碼預檢 + 重試 + 錯誤標題過濾）"""

        if not source.get('rss_url'):

            return []

        logger.info(f"Fetching RSS feed from {source['name']}...")

        articles = []

        rss_url = source['rss_url']



        # ── 預先用 requests 確認 HTTP 狀態，避免把 Error 頁 HTML 餵給 feedparser ──

        max_retries = 3

        feed = None

        for attempt in range(max_retries):

            try:

                resp = requests.get(rss_url, timeout=15,

                                    headers={'User-Agent': 'Mozilla/5.0 (compatible; NewsMonitor/1.0)'})

                if resp.status_code >= 500:

                    logger.warning(

                        f"Feed HTTP {resp.status_code} for {source['name']} "

                        f"(attempt {attempt+1}/{max_retries}), retrying..."

                    )

                    if attempt < max_retries - 1:

                        time.sleep(3)

                    continue

                # 狀態正常，讓 feedparser 解析已下載內容

                feed = feedparser.parse(resp.content)

                break

            except Exception as e:

                logger.warning(f"  [{source['name']}] HTTP pre-check error (attempt {attempt+1}): {e}")

                if attempt < max_retries - 1:

                    time.sleep(3)



        if feed is None:

            # 所有重試都失敗，fallback 直接讓 feedparser 嘗試

            logger.warning(f"All HTTP pre-checks failed for {source['name']}, falling back to feedparser direct parse")

            try:

                feed = feedparser.parse(rss_url)

            except Exception as e:

                logger.error(f"Error fetching RSS feed from {source['name']}: {e}")

                return articles



        try:

            if feed.bozo:

                logger.warning(f"Feed parse warning for {source['name']}: {feed.bozo_exception}")



            logger.info(f"Found {len(feed.entries)} articles in {source['name']}")



            for entry in feed.entries[:20]:  # 取前20篇文章

                try:

                    raw_title = entry.get('title', 'Untitled')



                    # ── 過濾錯誤頁標題（Digitimes 等來源偶爾伺服器 500，feedparser 解析到錯誤 HTML）──

                    if self._is_error_title(raw_title):

                        logger.warning(f"  [{source['name']}] Skipping error-page entry: {raw_title!r}")

                        continue



                    # 获取发布时间

                    published = entry.get('published_parsed')

                    if published:

                        pub_date = datetime(*published[:6])

                    else:

                        pub_date = datetime.now()



                    # 计算文章热度(简单基于发布时间和内容长度)

                    age_hours = (datetime.now() - pub_date).total_seconds() / 3600

                    if age_hours < 24:  # 只保留24小时内的文章

                        # 热度分数: 越新分数越高, 内容越长分数越高

                        raw_content = entry.get('description', entry.get('summary', ''))

                        # 去除 HTML 標籤（DIGITIMES 等來源的 description 含 <img src="...">）

                        content = self._clean_html(raw_content)

                        heat_score = max(0, 100 - age_hours * 2) + min(len(content) / 100, 50)



                        article = {

                            'source': source['name'],

                            'base_url': source['base_url'],

                            'title': raw_title,

                            'link': entry.get('link', ''),

                            'summary': content,

                            'published': pub_date,

                            'heat_score': heat_score

                        }

                        articles.append(article)



                except Exception as e:

                    logger.error(f"Error processing article: {e}")

                    continue



        except Exception as e:

            logger.error(f"Error fetching RSS feed from {source['name']}: {e}")



        logger.info(f"Successfully fetched {len(articles)} articles from {source['name']}")

        return articles



    def translate_text(self, text: str, max_retries: int = 3) -> str:

        if not text or not text.strip():

            return ""



        # 截断过长的文本

        if len(text) > 1000:

            text = text[:1000] + "..."



        for attempt in range(max_retries):

            try:

                result = self.translator.translate(text)

                # 防護：翻譯結果若為伺服器錯誤頁面文字（如 Google Translate 500 回傳），直接回傳原文

                if result and self._is_error_title(result):

                    logger.debug(f"Translation returned error-page text, using original: {result[:60]!r}")

                    return text

                return result

            except Exception as e:

                logger.warning(f"Translation attempt {attempt + 1} failed: {e}")

                if attempt < max_retries - 1:

                    time.sleep(2)

                else:

                    return text  # 翻译失败则返回原文

    

    # ════════════════════════════════════════════════════════════════

    # ■ 美國勞工部(BLS) 官方經濟指標

    # ════════════════════════════════════════════════════════════════



    def fetch_bls_indicators(self) -> List[Dict]:

        """

        從 BLS 公開 API v1(無需 API Key) 抓取最新一期官方經濟數據:

          - 消費者物價指數 CPI(含核心 CPI)

          - 生產者物價指數 PPI

          - 失業率

          - 非農就業人數(千人)



        過濾規則:

          - 只顯示一週內(7天)新發布的數據

          - 7日去重:同一 series+period 不重複推播

          - 若無新數據,回傳快取中最後一期已知值(避免空白)



        每筆回傳格式:

        {

          'series_id':   str,

          'name_zh':     str,   # 中文名稱

          'name_en':     str,   # 英文名稱

          'value':       str,   # 指數/百分比/千人數值

          'period':      str,   # 期間(如 '2025M04' 表示 2025年4月)

          'period_zh':   str,   # 中文期間(如 '2025年4月')

          'year':        str,

          'prev_value':  str,   # 前期值(供比較)

          'change':      float, # 與前期差值

          'is_new':      bool,  # 是否為一週內新數據

          'category':    str,   # '通膨' / '就業'

          'unit':        str,

          'source_url':  str,   # BLS 官方連結

        }

        """

        now = datetime.now()



        # 快取命中(12小時)

        if (self._bls_cache_time and

                (now - self._bls_cache_time).total_seconds() < self._bls_cache_ttl_hours * 3600 and

                self._bls_cache):

            logger.info("  [BLS] Using cached data")

            return self._bls_cache



        # 清理超過 7 天的推播記錄

        self._bls_sent = {

            k: v for k, v in self._bls_sent.items()

            if (now - v).days < 7

        }



        results = []



        # BLS period 格式轉中文

        def _period_zh(period: str, year: str) -> str:

            """將 BLS period 轉為可讀中文,如 'M04' -> '2025年4月'"""

            if not period:

                return year

            if period.startswith('M') and len(period) == 3:

                month = int(period[1:])

                return f"{year}年{month}月"

            if period.startswith('Q'):

                q = period[1:]

                return f"{year}年第{q}季"

            if period == 'A01':

                return f"{year}年(全年)"

            return f"{year} {period}"



        for series in self.bls_series:

            sid = series['series_id']

            url = f"https://api.bls.gov/publicAPI/v1/timeseries/data/{sid}"

            try:

                resp = requests.get(url, timeout=15,

                                    headers={'User-Agent': 'WorkBuddy Monitor admin@workbuddy.com'})

                if not resp.ok:

                    logger.warning(f"  [BLS] {sid} HTTP {resp.status_code}")

                    continue

                data = resp.json()

                if data.get('status') != 'REQUEST_SUCCEEDED':

                    logger.warning(f"  [BLS] {sid} API status: {data.get('status')}")

                    continue

                series_data = data.get('Results', {}).get('series', [])

                if not series_data:

                    continue

                points = series_data[0].get('data', [])

                if not points:

                    continue



                # BLS 回傳最新在前

                latest = points[0]

                prev   = points[1] if len(points) > 1 else None



                lat_value  = latest.get('value', '')

                lat_period = latest.get('period', '')

                lat_year   = latest.get('year', '')

                prev_value = prev.get('value', '') if prev else ''



                # 計算差值

                try:

                    change = round(float(lat_value) - float(prev_value), 3) if prev_value else 0.0

                except Exception:

                    change = 0.0



                # 估算發布日期:BLS 通常月末/次月初發布,以 period 推算

                # M04 -> 2025-04-01 (簡化:以期間第一天估算,不影響邏輯)

                est_release = now  # 預設為今天

                try:

                    if lat_period.startswith('M') and len(lat_period) == 3:

                        month = int(lat_period[1:])

                        year_i = int(lat_year)

                        # BLS 通常在次月中旬發布,以 period+1 month 估算

                        from calendar import monthrange

                        if month == 12:

                            est_release = datetime(year_i + 1, 1, 15)

                        else:

                            est_release = datetime(year_i, month + 1, 15)

                except Exception:

                    pass



                dedup_key = f"{sid}_{lat_year}{lat_period}"

                is_new = (now - est_release).days <= 7 and dedup_key not in self._bls_sent



                source_url = (

                    f"https://www.bls.gov/cgi-bin/surveymost.pl?"

                    f"series={sid}"

                )



                item = {

                    'series_id':  sid,

                    'name_zh':    series['name_zh'],

                    'name_en':    series['name_en'],

                    'value':      lat_value,

                    'period':     lat_period,

                    'period_zh':  _period_zh(lat_period, lat_year),

                    'year':       lat_year,

                    'prev_value': prev_value,

                    'change':     change,

                    'is_new':     is_new,

                    'category':   series['category'],

                    'unit':       series['unit'],

                    'source_url': f"https://www.bls.gov/data/",

                }

                results.append(item)



                if is_new:

                    self._bls_sent[dedup_key] = now



                time.sleep(0.3)  # 避免過快請求 BLS API



            except Exception as e:

                logger.warning(f"  [BLS] {sid} fetch error: {e}")

                continue



        # 更新快取

        self._bls_cache = results

        self._bls_cache_time = now

        logger.info(f"[BLS] Fetched {len(results)} economic indicators")

        return results



    def fetch_bls_news(self) -> List[Dict]:

        """

        從財經媒體 RSS 抓取與 BLS 經濟指標相關的新聞。

        來源: CNBC, WSJ, Bloomberg, MarketWatch, Yahoo Finance, FT, Seeking Alpha, MacroMicro

        關鍵字: CPI, PPI, 失業率, 非農就業, 通膨, 就業報告等

        時間過濾: 一週內(7天)

        去重: 7日內相同標題不重複

        """

        import hashlib

        from datetime import timedelta

        

        now = datetime.now()

        

        # 清理超過 7 天的推播記錄

        if hasattr(self, "_bls_news_sent"):

            self._bls_news_sent = {

                k: v for k, v in self._bls_news_sent.items()

                if (now - v).days < 7

            }

        else:

            self._bls_news_sent = {}

        

        # BLS 相關新聞 RSS 來源

        bls_rss_sources = [

            {

                "name": "CNBC Economy",

                "url": "https://www.cnbc.com/id/10001119/device/rss/rss.html",

                "lang": "en",

            },

            {

                "name": "Bloomberg Economy",

                "url": "https://feeds.bloomberg.com/economics/news.rss",

                "lang": "en",

            },

            {

                "name": "MarketWatch Economy",

                "url": "https://www.marketwatch.com/rss/topstories",

                "lang": "en",

            },

            {

                "name": "Financial Times Economy",

                "url": "https://www.ft.com/?format=rss",

                "lang": "en",

            },

            {

                "name": "Seeking Alpha Economy",

                "url": "https://seekingalpha.com/market-news.xml",

                "lang": "en",

            },

            {

                "name": "Yahoo Finance Economy",

                "url": "https://finance.yahoo.com/news/rssindex",

                "lang": "en",

            },

            {

                "name": "MacroMicro",

                "url": "https://www.macromicro.me/feed",

                "lang": "zh",

            },

        ]

        

        # BLS 經濟指標關鍵字（英文 + 中文）

        bls_keywords = [

            # 英文關鍵字

            "cpi", "consumer price index", "inflation",

            "ppi", "producer price index",

            "unemployment rate", "jobless claims", "non-farm payroll",

            "nf payroll", "employment report", "jobs report",

            "labor department", "bureau of labor statistics",

            "bls", "inflation rate", "core cpi",

            # 中文關鍵字

            "消費者物價指數", "通膨", "通貨膨脹",

            "生產者物價指數",

            "失業率", "非農就業", "就業報告",

            "勞工部", "經濟數據", "經濟指標",

        ]

        

        results = []

        

        for src in bls_rss_sources:

            try:

                articles = self.fetch_rss_feed(src)

                for article in articles:

                    title = article.get("title", "").lower()

                    summary = article.get("summary", "").lower()

                    combined = title + " " + summary

                    

                    # 檢查是否包含 BLS 相關關鍵字

                    if not any(kw.lower() in combined for kw in bls_keywords):

                        continue

                    

                    # 時間過濾: 一週內

                    pub = article.get("published_parsed")

                    if pub:

                        try:

                            pub_dt = datetime(*pub[:6])

                            if (now - pub_dt).days > 7:

                                continue

                        except Exception:

                            pass

                    

                    # 去重

                    title_key = article.get("title", "")

                    dedup_key = hashlib.md5(title_key.encode("utf-8")).hexdigest()

                    if dedup_key in self._bls_news_sent:

                        continue

                    

                    results.append(article)

                    self._bls_news_sent[dedup_key] = now

                    

            except Exception as e:

                src_name = src.get("name", "unknown")

                logger.warning(f"[BLS News] {src_name} fetch error: {e}")

                continue

        

        # 排序: 最新的在前

        results.sort(key=lambda x: x.get("published_parsed", (0,)), reverse=True)

        

        logger.info(f"[BLS News] Fetched {len(results)} related news articles")

        return results





    def _format_bls_section(self, indicators: List[Dict], bls_news: List[Dict] = None) -> str:

        """

        格式化美國勞工部(BLS) 官方經濟數據區塊.

        新數據(一週內)以 NEW 標記,舊數據顯示最後已知值.

        按通膨 / 就業分組顯示.

        附來源連結 https://www.bls.gov/data/

        """

        section  = f"\n{'='*40}\n"

        section += "🏛️ <b>美國勞工部(BLS) 官方經濟指標</b>\n"

        section += "(CPI / PPI / 失業率 / 非農就業 -- 官方最新發布值)\n"

        section += f"{'='*40}\n\n"



        if not indicators:

            section += "📭 目前無法取得 BLS 經濟數據(API 可能暫時無回應)\n"

            section += f"{'='*40}\n"

            return section



        # 按分類分組

        grouped: Dict[str, List[Dict]] = {}

        for item in indicators:

            grouped.setdefault(item['category'], []).append(item)



        cat_emoji = {'通膨': '📈', '就業': '👷'}

        for cat, items in grouped.items():

            emoji = cat_emoji.get(cat, '📊')

            section += f"{emoji} <b>【{cat}】</b>\n"

            for item in items:

                new_tag = " 🆕<b>最新發布</b>" if item['is_new'] else ""

                # 差值標記

                chg = item['change']

                if chg > 0:

                    chg_str = f"+{chg}"

                    chg_emoji = "⬆️"

                elif chg < 0:

                    chg_str = str(chg)

                    chg_emoji = "⬇️"

                else:

                    chg_str = "0"

                    chg_emoji = "➡️"

                unit = item['unit']

                val_display = (

                    f"{item['value']}%"   if unit == '%' else

                    f"{item['value']} 千人" if unit == '千人' else

                    item['value']

                )

                prev_display = (

                    f"{item['prev_value']}%" if unit == '%' and item['prev_value'] else

                    f"{item['prev_value']} 千人" if unit == '千人' and item['prev_value'] else

                    item['prev_value']

                ) if item['prev_value'] else 'N/A'



                section += (

                    f"  📌 <b>{item['name_zh']}</b>{new_tag}\n"

                    f"     期間: {item['period_zh']}  "

                    f"值: <b>{val_display}</b>  "

                    f"前期: {prev_display}  "

                    f"{chg_emoji} 變動: {chg_str}\n"

                    f"     英文: {item['name_en']}\n"

                )

            section += "\n"



        # 相關新聞 (來自財經媒體 RSS)

        if bls_news:

            section += f"{'='*40}\n"

            section += "📰 <b>BLS 經濟指標相關新聞</b>\n"

            section += f"{'='*40}\n\n"

            for article in bls_news[:10]:  # 最多顯示 10 篇

                title = article.get("title", "")

                link = article.get("link", "")

                published = article.get("published", "")

                summary = article.get("summary", "")[:200]

                

                section += f'  📌 <b><a href="{link}">{title}</a></b>\n'

                if published:

                    section += f"     📅 {published}\n"

                if summary:

                    section += f"     📝 {summary}...\n"

                section += "\n"

            section += "\n"



        section += f"{'='*40}\n"

        section += (

            "🏛️ 資料來源: "

            "<a href=\"https://www.bls.gov/data/\">美國勞工部統計局 (BLS) 官方數據</a>\n"

        )

        section += "⚠️ 一週內新發布數據以 🆕 標記,其餘顯示最後已知值\n"

        return section



    # ── 每日熱門新聞:主題關鍵字白名單 ─────────────────────────────────────

    # 只保留標題或摘要中包含以下關鍵字的文章

    # 僅關注: 美股七巨頭 (Magnificent Seven) + OpenAI / SpaceX / Anthropic

    TOPIC_KEYWORDS = [

        # 七巨頭 - Apple

        'apple', 'aapl',

        # 七巨頭 - Microsoft

        'microsoft', 'msft',

        # 七巨頭 - NVIDIA

        'nvidia', 'nvda',

        # 七巨頭 - Google / Alphabet

        'google', 'alphabet', 'googl', 'goog',

        # 七巨頭 - Amazon

        'amazon', 'amzn',

        # 七巨頭 - Meta

        'meta', 'facebook',

        # 七巨頭 - Tesla

        'tesla', 'tsla',

        # OpenAI / SpaceX / Anthropic

        'openai', 'spacex', 'anthropic',

    ]



    def _is_relevant_article(self, title: str, summary: str) -> bool:

        """判斷文章是否提及七巨頭或 OpenAI/SpaceX/Anthropic"""

        text = (title + ' ' + summary).lower()

        return any(kw in text for kw in self.TOPIC_KEYWORDS)



    def get_top_articles(self) -> List[Dict]:

        """獲取熱門文章 Top 10（僅限七巨頭 + OpenAI/SpaceX/Anthropic）"""

        all_articles = []



        # 从所有源获取文章

        for source in self.news_sources:

            articles = self.fetch_rss_feed(source)

            all_articles.extend(articles)



        # ── 主題過濾:只保留相關文章 ───────────────────────────────────────

        before = len(all_articles)

        all_articles = [

            a for a in all_articles

            if self._is_relevant_article(a['title'], a.get('summary', ''))

        ]

        after = len(all_articles)

        logger.info(f"Topic filter: {before} → {after} articles (removed {before - after} off-topic)")



        # 按热度分数排序

        all_articles.sort(key=lambda x: x['heat_score'], reverse=True)



        # 取前10名

        top_10 = all_articles[:10]



        # 添加排名并翻译

        for i, article in enumerate(top_10, 1):

            article['rank'] = i

            article['title_zh'] = self.translate_text(article['title'])

            article['summary_zh'] = self.translate_text(article['summary'])



        return top_10



    def _match_politician(self, text: str) -> Dict:

        """

        檢查文字是否包含追蹤名人,回傳匹配到的 {'name': ..., 'category': ...}

        未匹配則回傳空 dict

        """

        text_lower = text.lower()

        for target in self.vip_targets:

            for kw in target['keywords']:

                if kw in text_lower:

                    return {'name': target['name'], 'category': target['category']}

        return {}



    def fetch_politician_trades(self) -> List[Dict]:

        """

        從 CNBC 和 WSJ RSS 抓取名人交易相關新聞(使用關鍵字過濾).

        

        來源:

          1. CNBC RSS(關鍵字過濾)

          2. Wall Street Journal RSS(關鍵字過濾)

        

        回傳每筆格式:

        {

          'source': str,

          'politician': str,   # 顯示名稱

          'category': str,     # 分類

          'title': str,

          'title_zh': str,    # 中文翻譯

          'link': str,        # 來源網址

          'published': datetime,

        }

        """

        logger.info("Fetching VIP trade news from media sources...")

        results = []

        seen_links = set()



        trade_keywords = [

            'stock', 'trade', 'bought', 'sold', 'purchase', 'sell',

            'invest', 'crypto', 'bitcoin', 'option', 'futures',

            'portfolio', 'disclosure', 'filing', 'shares', 'etf',

            'warrant', 'stake', 'holding', 'position', 'buys', 'sells',

            'acquired', 'divested', 'bet', 'wager', 'trading',

        ]



        # ── 從所有新聞來源(CNBC、WSJ、Reuters、Morningstar、Schwab 等)抓取相關新聞 ──

        for src in self.news_sources:

            try:

                feed = feedparser.parse(src['rss_url'])

                logger.info(f"  [{src['name']}] entries: {len(feed.entries)}")

                

                for entry in feed.entries[:50]:

                    title = entry.get('title', '')

                    # ── 過濾錯誤頁標題 ──────────────────────────────────

                    if self._is_error_title(title):

                        logger.warning(f"  [{src['name']}] Skipping error-page entry: {title!r}")

                        continue

                    summary = self._clean_html(entry.get('summary', entry.get('description', '')))

                    link = entry.get('link', '')

                    combined = f"{title} {summary}"



                    # 檢查是否匹配追蹤的名人

                    matched = self._match_politician(combined)

                    if not matched:

                        continue



                    # 檢查是否包含交易關鍵字

                    if not any(kw in combined.lower() for kw in trade_keywords):

                        continue



                    # 去重

                    if link in seen_links:

                        continue

                    seen_links.add(link)



                    # 取得發布時間(不限天數,永遠顯示最新內容)

                    published_raw = entry.get('published_parsed')

                    pub_date = datetime(*published_raw[:6]) if published_raw else datetime.now()

                    results.append({

                        'source':    src['name'],

                        'politician': matched['name'],

                        'category':  matched['category'],

                        'title':     title,

                        'title_zh':  '',  # 稍後翻譯

                        'link':      link,

                        'published': pub_date,

                    })

            except Exception as e:

                logger.warning(f"  [{src['name']}] keyword filter error: {e}")



        # ── 補充 MacroMicro 財經M平方(科技名人/總經相關)──────────────────

        try:

            mm_feed = feedparser.parse('https://www.macromicro.me/feed')

            for entry in mm_feed.entries[:40]:

                title = entry.get('title', '')

                # ── 過濾錯誤頁標題 ──────────────────────────────────────

                if self._is_error_title(title):

                    continue

                combined = title.lower()

                matched = self._match_politician(combined)

                if not matched:

                    continue

                if not any(kw in combined for kw in trade_keywords):

                    continue

                link = entry.get('link', '')

                if link in seen_links:

                    continue

                seen_links.add(link)

                pub_date = now

                try:

                    if entry.get('published_parsed'):

                        pub_date = datetime(*entry.published_parsed[:6])

                except Exception:

                    pass

                results.append({

                    'source':    'MacroMicro 財經M平方',

                    'politician': matched['name'],

                    'category':  matched['category'],

                    'title':     title,

                    'title_zh':  '',

                    'link':      link,

                    'published': pub_date,

                })

        except Exception as e:

            logger.warning(f"  [MacroMicro VIP] error: {e}")



        # 按發布時間排序(最新優先),取前20筆

        results.sort(key=lambda x: x['published'], reverse=True)

        results = results[:20]



        # 翻譯標題

        for item in results:

            item['title_zh'] = self.translate_text(item['title'])



        logger.info(f"VIP trade news found: {len(results)}")

        return results



    def _format_politician_trades_section(self, trades: List[Dict]) -> str:

        """格式化名人金融交易揭露訊息區塊(按分類分組)"""

        section  = f"\n{'='*40}\n"

        section += "🌟 <b>名人金融交易動態揭露</b>\n"

        section += "(政治人物 ／ 科技名人 ／ 投資大師)\n"

        section += f"{'='*40}\n\n"



        if not trades:

            section += "📭 目前無名人金融交易相關資訊\n"

        else:

            # 按分類分組

            category_order = ['川普家族', '裴洛西夫婦', '科技名人', '投資大師']

            category_emoji = {

                '川普家族':  '🏛️',

                '裴洛西夫婦': '🏛️',

                '科技名人':  '💻',

                '投資大師':  '💰',

            }



            # 收集各分類資料

            grouped: Dict[str, List[Dict]] = {}

            for item in trades:

                cat = item.get('category', '其他')

                grouped.setdefault(cat, []).append(item)



            # 依照預設順序輸出

            output_categories = [c for c in category_order if c in grouped]

            # 若有未預期分類,附加於末尾

            for cat in grouped:

                if cat not in output_categories:

                    output_categories.append(cat)



            idx = 1

            for cat in output_categories:

                emoji = category_emoji.get(cat, '📌')

                section += f"{emoji} <b>【{cat}】</b>\n"

                for item in grouped[cat]:

                    pub_str   = item['published'].strftime('%m/%d %H:%M')

                    title_zh  = self._safe_title_zh(item)

                    section += (

                        f"  #{idx} 👤 <b>{html.escape(item['politician'])}</b>  "

                        f"[{html.escape(item['source'])}]  {pub_str}\n"

                        f"     📌 <a href=\"{html.escape(item['link'])}\">{html.escape(title_zh)}</a>\n"

                        f"     🔤 {html.escape(item['title'])}\n\n"

                    )

                    idx += 1



        section += f"{'='*40}\n"

        section += (

            "📊 資料來源: CNBC ／ 華爾街日報 ／ Bloomberg ／ "

            "MarketWatch ／ Financial Times ／ Yahoo Finance ／ Seeking Alpha(新聞報導)\n"

        )

        section += "📋 官方持倉數據: 詳見下方 SEC 13F 官方申報區塊\n"

        section += "⚠️ 註: 本區塊數據來自財經媒體報導,非官方交易揭露\n"

        return section



    def _match_13f_institution(self, text: str) -> Optional[Dict]:

        """

        檢查文字是否提及任何 13F 監控機構,回傳匹配到的機構資訊 dict,

        未匹配則回傳 None.

        """

        text_lower = text.lower()

        for inst in self.institutions_13f:

            for kw in inst.get('keywords', []):

                if kw in text_lower:

                    return inst

        return None



    def fetch_13f_media_news(self) -> List[Dict]:

        """

        從 CNBC 和 WSJ RSS 抓取與 13F 監控機構相關的媒體報導.



        篩選邏輯:

          1. 文章標題或摘要包含任一機構關鍵字(berkshire、buffett、cathie wood…)

          2. 同時包含投資 / 持倉 / 申報相關關鍵字

          3. 只保留 30 天內的文章(13F 報告季度性,新聞週期較長)

          4. 按發布時間降序,最多回傳 20 筆



        回傳每筆格式:

        {

          'source':        str,       # 'CNBC' / 'Wall Street Journal'

          'institution':   str,       # 機構顯示名稱(中文)

          'category':      str,       # 分類

          'title':         str,       # 英文標題

          'title_zh':      str,       # 中文翻譯標題

          'summary_zh':    str,       # 中文摘要(前150字)

          'link':          str,       # 文章 URL

          'published':     datetime,

        }

        """

        logger.info("Fetching 13F-related media news from CNBC, WSJ, Bloomberg, MarketWatch, FT, Seeking Alpha...")

        results = []

        seen_links: set = set()



        # 投資 / 持倉 / 13F 申報相關關鍵字

        invest_keywords = [

            '13f', 'filing', 'holdings', 'portfolio', 'position',

            'stake', 'buys', 'sells', 'bought', 'sold', 'invested',

            'invest', 'acquisition', 'divest', 'increased', 'decreased',

            'added', 'reduced', 'shares', 'quarter', 'fund', 'hedge fund',

            'sec filing', 'disclosure', 'owns', 'holding',

        ]



        for src in self.news_sources:

            try:

                feed = feedparser.parse(src['rss_url'])

                logger.info(f"  [13F Media] [{src['name']}] entries: {len(feed.entries)}")



                for entry in feed.entries[:50]:

                    title   = entry.get('title', '')

                    # ── 過濾錯誤頁標題 ──────────────────────────────────

                    if self._is_error_title(title):

                        logger.warning(f"  [13F Media] [{src['name']}] Skipping error-page entry: {title!r}")

                        continue

                    summary = self._clean_html(entry.get('summary', entry.get('description', '')))

                    link    = entry.get('link', '')

                    combined = f"{title} {summary}"



                    # ① 必須提及監控機構

                    matched_inst = self._match_13f_institution(combined)

                    if not matched_inst:

                        continue



                    # ② 必須含投資相關關鍵字

                    if not any(kw in combined.lower() for kw in invest_keywords):

                        continue



                    # ③ 去重

                    if link in seen_links:

                        continue

                    seen_links.add(link)





                    # ④ 取得發布時間(不限天數,永遠顯示最新內容)

                    published_raw = entry.get('published_parsed')

                    pub_date = datetime(*published_raw[:6]) if published_raw else datetime.now()



                    results.append({

                        'source':      src['name'],

                        'institution': matched_inst['display_name'],

                        'full_name':   matched_inst['name'],

                        'category':    matched_inst['category'],

                        'title':       title,

                        'title_zh':    '',    # 稍後翻譯

                        'summary':     summary[:300] if summary else '',

                        'summary_zh':  '',    # 稍後翻譯

                        'link':        link,

                        'published':   pub_date,

                    })



            except Exception as e:

                logger.warning(f"  [13F Media] [{src['name']}] error: {e}")



        # ── 補充 MacroMicro 財經M平方(13F 機構相關)───────────────────────

        try:

            mm_feed = feedparser.parse('https://www.macromicro.me/feed')

            for entry in mm_feed.entries[:40]:

                title    = entry.get('title', '')

                # ── 過濾錯誤頁標題 ──────────────────────────────────────

                if self._is_error_title(title):

                    continue

                combined = title.lower()

                matched_inst = self._match_13f_institution(combined)

                if not matched_inst:

                    continue

                if not any(kw in combined for kw in invest_keywords):

                    continue

                link = entry.get('link', '')

                if link in seen_links:

                    continue

                seen_links.add(link)

                pub_date = now

                try:

                    if entry.get('published_parsed'):

                        pub_date = datetime(*entry.published_parsed[:6])

                except Exception:

                    pass

                results.append({

                    'source':      'MacroMicro 財經M平方',

                    'institution': matched_inst['display_name'],

                    'full_name':   matched_inst['name'],

                    'category':    matched_inst['category'],

                    'title':       title,

                    'title_zh':    '',

                    'summary':     '',

                    'summary_zh':  '',

                    'link':        link,

                    'published':   pub_date,

                })

        except Exception as e:

            logger.warning(f"  [MacroMicro 13F] error: {e}")



        # 按發布時間降序,取前 20 筆

        results.sort(key=lambda x: x['published'], reverse=True)

        results = results[:20]



        # 翻譯標題與摘要

        for item in results:

            item['title_zh']   = self.translate_text(item['title'])

            item['summary_zh'] = self.translate_text(item['summary']) if item['summary'] else ''



        logger.info(f"13F media news found: {len(results)}")

        return results



    def _format_13f_media_section(self, media_news: List[Dict]) -> str:

        """

        格式化 CNBC / WSJ 對 13F 機構的媒體報導區塊(按機構分組).

        附中文標題、中文摘要、英文原標、發布時間與來源連結.

        """

        section  = f"\n{'─'*40}\n"

        section += "📰 <b>媒體追蹤報導</b>(CNBC ／ 華爾街日報 ／ Bloomberg ／ MarketWatch ／ FT ／ Seeking Alpha ／ MacroMicro)\n"

        section += f"{'─'*40}\n\n"



        if not media_news:

            section += "📭 目前無 13F 機構相關媒體報導\n"

            return section



        # 按機構分組

        grouped: Dict[str, List[Dict]] = {}

        for item in media_news:

            key = item['institution']

            grouped.setdefault(key, []).append(item)



        # 依照 institutions_13f 順序輸出

        inst_order = [i['display_name'] for i in self.institutions_13f]

        output_keys = [k for k in inst_order if k in grouped]

        for k in grouped:

            if k not in output_keys:

                output_keys.append(k)



        for inst_name in output_keys:

            articles = grouped[inst_name]

            section += f"  🏦 <b>{inst_name}</b>({len(articles)} 篇)\n"

            for idx, art in enumerate(articles, 1):

                pub_str   = art['published'].strftime('%m/%d %H:%M')

                title_zh  = self._safe_title_zh(art)

                summary_zh = self._safe_summary_zh(art)

                src_tag   = '📊' if art['source'] == 'CNBC' else '📰'



                section += (

                    f"  {src_tag} <b>#{idx}</b> [{html.escape(art['source'])}]  {pub_str}\n"

                    f"     <b><a href=\"{html.escape(art['link'])}\">{html.escape(title_zh)}</a></b>\n"

                )

                if summary_zh:

                    # 截取前100字避免訊息過長

                    excerpt = summary_zh[:100] + ('…' if len(summary_zh) > 100 else '')

                    section += f"     {html.escape(excerpt)}\n"

                section += (

                    f"     🔤 {html.escape(art['title'])}\n\n"

                )



        return section



    def _fetch_latest_13f_accession(self, cik: str) -> Optional[str]:

        """

        透過 SEC EDGAR submissions API 取得指定 CIK 最新一筆 13F-HR 的 accession number.

        回傳格式:'0001234567-25-000001';若找不到則回傳 None.

        """

        cik_padded = cik.lstrip('0').zfill(10)

        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"

        headers = {'User-Agent': 'WorkBuddy Monitor admin@workbuddy.com'}

        try:

            resp = requests.get(url, headers=headers, timeout=15)

            resp.raise_for_status()

            data = resp.json()

            filings = data.get('filings', {}).get('recent', {})

            forms = filings.get('form', [])

            accessions = filings.get('accessionNumber', [])

            dates = filings.get('filingDate', [])

            # 依索引找最近一筆 13F-HR(非修正版 13F-HR/A 優先找原始版本)

            for i, form in enumerate(forms):

                if form == '13F-HR':

                    return accessions[i].replace('-', ''), dates[i]

            # 若無原始版本,嘗試找修正版

            for i, form in enumerate(forms):

                if form == '13F-HR/A':

                    return accessions[i].replace('-', ''), dates[i]

        except Exception as e:

            logger.warning(f"  [13F] Failed to get accession for CIK {cik}: {e}")

        return None, None



    def _parse_13f_xml(self, cik: str, accession_no: str) -> List[Dict]:

        """

        下載並解析 13F-HR 的 XML 持倉明細(infoTable),

        回傳持倉列表,每筆格式:

        {

          'ticker_name': str,   # 公司名稱

          'shares': int,        # 持股數量

          'value': int,         # 市值(千美元)

          'option_type': str,   # 'Put'/'Call'/'None'(選擇權)

          'transaction_type': str,  # 從比較前後期推算(若無則 'unknown')

        }

        """

        # 先取得文件索引,找到 XML 主文件 URL

        cik_num = cik.lstrip('0')

        idx_url = (

            f"https://www.sec.gov/Archives/edgar/data/{cik_num}/"

            f"{accession_no[:10]}-{accession_no[10:12]}-{accession_no[12:]}/"

            f"{accession_no[:10]}-{accession_no[10:12]}-{accession_no[12:]}-index.htm"

        )

        # 標準化 accession_no 格式

        an = accession_no  # 已去除 '-',18 位

        an_fmt = f"{an[:10]}-{an[10:12]}-{an[12:]}"  # 格式:0001067983-25-000001

        base_url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{an}/"

        headers = {'User-Agent': 'WorkBuddy Monitor admin@workbuddy.com'}



        try:

            # ── 步驟1:從 EDGAR 文件索引取得所有 XML 連結 ────────────────────

            # SEC 13F-HR 包含兩個 XML:

            #   primary_doc.xml   → 封面頁(不含持倉)

            #   infotable / *.xml → 真正的持倉明細(infoTable)

            index_url = f"{base_url}{an_fmt}-index.htm"

            resp = requests.get(index_url, headers=headers, timeout=15)

            xml_filename = None



            if resp.ok:

                import re as _re2

                # 收集所有 .xml href(排除 xslt/stylesheet/xslForm 樣式表)

                all_xml_hrefs = _re2.findall(

                    r'href=["\']([^"\']+\.xml)["\']', resp.text, _re2.IGNORECASE

                )

                candidates = []

                for href in all_xml_hrefs:

                    fname = href.split('/')[-1].lower()

                    # 排除樣式表路徑(含 xslForm 目錄)

                    if 'xslt' in fname or 'stylesheet' in fname or 'xslform' in href.lower():

                        continue

                    # 排除封面頁

                    if fname == 'primary_doc.xml':

                        continue

                    candidates.append(href.split('/')[-1])



                if candidates:

                    # 優先選含 infotable / 13f 字樣的,否則取第一個

                    for c in candidates:

                        if 'infotable' in c.lower() or '13f' in c.lower():

                            xml_filename = c

                            break

                    if not xml_filename:

                        xml_filename = candidates[0]



            if not xml_filename:

                # Fallback:常見命名

                xml_filename = 'infotable.xml'



            xml_url = f"{base_url}{xml_filename}"

            xml_resp = requests.get(xml_url, headers=headers, timeout=20)

            if not xml_resp.ok:

                # 再試其他常見名稱

                for fallback in ['infotable.xml', 'form13fInfoTable.xml']:

                    xml_url = f"{base_url}{fallback}"

                    xml_resp = requests.get(xml_url, headers=headers, timeout=20)

                    if xml_resp.ok:

                        break



            if not xml_resp.ok:

                logger.warning(f"  [13F] Cannot download XML for {cik} {an_fmt}")

                return []



            holdings = []

            # ── namespace-aware XML 解析 ───────────────────────────────────

            # SEC 不同機構提交的 13F XML 命名空間格式不一致,

            # 用 namespace-aware 方式先嘗試,失敗再用移除命名空間的 fallback

            _NS13F = 'http://www.sec.gov/edgar/document/thirteenf/informationtable'



            try:

                root = ET.fromstring(xml_resp.content)

            except ET.ParseError:

                # fallback:移除命名空間後再解析

                import re as _re_fb

                xml_text = _re_fb.sub(r'\sxmlns(?::\w+)?="[^"]*"', '', xml_resp.text)

                xml_text = _re_fb.sub(r'<(\w+):', '<', xml_text)

                xml_text = _re_fb.sub(r'</(\w+):', '</', xml_text)

                root = ET.fromstring(xml_text)



            # 找 infoTable(先用命名空間,再用無前綴)

            entries = root.findall(f'.//{{{_NS13F}}}infoTable')

            if not entries:

                entries = list(root.iter('infoTable'))



            def _find_text(entry, tag):

                el = entry.find(f'{{{_NS13F}}}{tag}')

                if el is None:

                    el = entry.find(tag)

                return el.text.strip() if el is not None and el.text else ''



            for entry in entries:

                name = _find_text(entry, 'nameOfIssuer')

                try:

                    value = int(_find_text(entry, 'value').replace(',', ''))

                except Exception:

                    value = 0

                try:

                    shares = int(_find_text(entry, 'sshPrnamt').replace(',', ''))

                except Exception:

                    shares = 0

                option_type = _find_text(entry, 'putCall')

                if name and (value > 0 or shares > 0):

                    holdings.append({

                        'ticker_name': name,

                        'shares': shares,

                        'value': value,       # 千美元

                        'option_type': option_type,

                    })

            return holdings



        except Exception as e:

            logger.warning(f"  [13F] XML parse error for {cik}: {e}")

            return []



    def fetch_all_13f_trades(self) -> List[Dict]:

        """

        抓取所有監控機構的最新 13F 申報,

        取得前5大持倉(按市值)並標記增減倉(本期 vs 上期對比).



        回傳每筆格式:

        {

          'institution': str,       # 機構顯示名稱

          'category': str,          # 分類

          'filing_date': str,       # 申報日期

          'report_period': str,     # 報告截止日(季度末)

          'top_holdings': [         # 前5大持倉

            {

              'rank': int,

              'ticker_name': str,

              'value_usd_k': int,   # 市值(千美元)

              'shares': int,

              'option_type': str,

            }

          ],

          'total_holdings_count': int,

          'source_url': str,

        }

        """

        # 快取機制:避免同一天多次重複爬取

        now = datetime.now()

        if (self._13f_cache_time and

                (now - self._13f_cache_time).total_seconds() < self._13f_cache_ttl_hours * 3600 and

                self._13f_cache):

            logger.info("  [13F] Using cached data")

            return list(self._13f_cache.values())



        logger.info("Fetching 13F filings from SEC EDGAR...")

        results = []



        for inst in self.institutions_13f:

            cik = inst['cik']

            name = inst['display_name']

            logger.info(f"  [13F] Fetching {name} (CIK: {cik})...")

            try:

                accession, filing_date = self._fetch_latest_13f_accession(cik)

                if not accession:

                    logger.warning(f"  [13F] No 13F found for {name}")

                    continue



                cik_num = cik.lstrip('0')

                an_fmt = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"

                source_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_num}&type=13F&dateb=&owner=include&count=5"



                holdings = self._parse_13f_xml(cik, accession)

                if not holdings:

                    logger.warning(f"  [13F] No holdings parsed for {name}")

                    continue



                # 按市值降序排序,取前5

                holdings.sort(key=lambda x: x['value'], reverse=True)

                top5 = holdings[:5]

                for i, h in enumerate(top5, 1):

                    h['rank'] = i

                    # 翻譯公司名稱為中文(優先查對照表,fallback GoogleTranslate)

                    h['ticker_name_zh'] = self._translate_company_name(h['ticker_name'])



                # 計算申報距今天數(供顯示用,不再作為跳過依據)

                filing_age_days = None

                is_recent_filing = True

                if filing_date:

                    try:

                        fd = datetime.strptime(filing_date, '%Y-%m-%d')

                        filing_age_days = (datetime.now() - fd).days

                    except Exception:

                        pass



                # 組成 accession 格式連結(直接指向 SEC 申報文件索引頁)

                an_url = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"

                filing_url = (

                    f"https://www.sec.gov/cgi-bin/browse-edgar?"

                    f"action=getcompany&CIK={cik_num}&type=13F&dateb=&owner=include&count=5"

                )

                direct_filing_url = (

                    f"https://www.sec.gov/Archives/edgar/data/{cik_num}/"

                    f"{accession}/{an_url}-index.htm"

                )



                results.append({

                    'institution': inst['display_name'],

                    'full_name': inst['name'],

                    'category': inst['category'],

                    'filing_date': filing_date or '未知',

                    'filing_age_days': filing_age_days,

                    'is_recent_filing': is_recent_filing,

                    'top_holdings': top5,

                    'total_holdings_count': len(holdings),

                    'source_url': source_url,

                    'direct_filing_url': direct_filing_url,

                    'cik': cik_num,

                })



                # 避免請求太頻繁

                time.sleep(0.5)



            except Exception as e:

                logger.error(f"  [13F] Error processing {name}: {e}")

                continue



        # 更新快取

        self._13f_cache = {r['cik']: r for r in results}

        self._13f_cache_time = now



        logger.info(f"13F fetch done -- {len(results)} institutions")

        return results



    def _format_13f_section(self, filings_13f: List[Dict],

                             media_news: List[Dict] = None) -> str:

        """格式化 SEC 官方 13F 持倉揭露 + 媒體報導整合區塊"""

        section  = f"\n{'='*40}\n"

        section += "📋 <b>SEC 官方 13F 機構持倉揭露</b>\n"

        section += "(每季申報,數據來自美國證管會 EDGAR 官方)\n"

        section += f"{'='*40}\n\n"



        if not filings_13f:

            section += "📭 目前無法取得 13F 持倉資料(SEC 伺服器可能暫時無回應)\n"

        else:

            category_order = ['投資大師', '科技名人']

            category_emoji = {'投資大師': '💰', '科技名人': '💻'}



            # 超過 180 天（半年）的機構先分離出來，不顯示持倉

            STALE_DAYS = 180

            stale_institutions = []

            for item in filings_13f:

                age = item.get('filing_age_days')

                if age is not None and age > STALE_DAYS:

                    stale_institutions.append(item)

            active_filings = [x for x in filings_13f if x not in stale_institutions]



            # 重新分組（只用 active_filings）

            grouped = {}

            for item in active_filings:

                cat = item.get('category', '其他')

                grouped.setdefault(cat, []).append(item)



            output_cats = [c for c in category_order if c in grouped]

            for cat in grouped:

                if cat not in output_cats:

                    output_cats.append(cat)



            for cat in output_cats:

                emoji = category_emoji.get(cat, '📌')

                section += f"{emoji} <b>【{cat}】</b>\n"

                for inst in grouped[cat]:

                    # 機構標題行:名稱 + 申報日(附距今天數)+ 持倉總數 + 直達申報文件連結

                    direct_url = inst.get('direct_filing_url') or inst.get('source_url', '#')

                    age_days = inst.get('filing_age_days')

                    age_note = f"(距今 {age_days} 天)" if age_days is not None else ""

                    section += (

                        f"  🏦 <b><a href=\"{html.escape(direct_url)}\">{html.escape(inst['institution'])}</a></b>  "

                        f"申報日: <b>{inst['filing_date']}</b> {age_note}  "

                        f"共 {inst['total_holdings_count']} 檔\n"

                    )

                    # 前5大持倉

                    for h in inst['top_holdings']:

                        rank_emoji = (

                            "🥇" if h['rank'] == 1 else

                            "🥈" if h['rank'] == 2 else

                            "🥉" if h['rank'] == 3 else

                            f"  #{h['rank']}"

                        )

                        # 市值格式化

                        value_b = h['value'] / 1_000_000  # 千美元 → 十億美元

                        value_str = (

                            f"${value_b:.2f}B" if value_b >= 1

                            else f"${h['value'] / 1000:.0f}M" if h['value'] >= 1000

                            else f"${h['value']}K"

                        )

                        # 選擇權標籤

                        opt_tag = f" 【{h['option_type']}】" if h.get('option_type') else ''

                        # 中文名稱(命中對照表)與英文原文

                        zh_name = h.get('ticker_name_zh', '')

                        en_name = h['ticker_name']

                        if zh_name and zh_name != en_name:

                            name_display = f"<b>{zh_name}</b>({en_name})"

                        else:

                            # 未能翻譯,僅顯示英文原文

                            name_display = f"<b>{en_name}</b>"



                        section += (

                            f"  {rank_emoji} {name_display}{opt_tag}\n"

                            f"       市值: {value_str}  股數: {h['shares']:,}\n"

                        )

                    section += "\n"



            # 若有機構申報超過半年，列出清單提示

            if stale_institutions:

                section += "⏳ <b>【申報超過 180 天，暫時隱藏】</b>\n"

                for inst in stale_institutions:

                    age = inst.get('filing_age_days', '?')

                    section += (

                        f"  ⚠️ {html.escape(inst['institution'])}  "

                        f"最後申報: {inst['filing_date']}（距今 {age} 天）\n"

                    )

                section += "   ↳ 等待新一期 13F 申報後將自動恢復顯示\n\n"



            # 若所有機構都超過半年

            if not active_filings and not stale_institutions:

                pass  # 已由 not filings_13f 分支處理

            elif not active_filings and stale_institutions:

                # active 為空時補一行說明

                section += "📭 目前所有追蹤機構的 13F 申報均已超過半年，等待新申報\n"



        # ── 媒體報導子區塊 ──────────────────────────────────────────────

        if media_news is not None:

            section += self._format_13f_media_section(media_news)



        # ── 來源聲明 ────────────────────────────────────────────────────

        section += f"{'='*40}\n"

        section += (

            "📊 官方數據: <a href=\"https://www.sec.gov/cgi-bin/browse-edgar"

            "?action=getcompany&type=13F&dateb=&owner=include&count=40\">"

            "SEC EDGAR 官方 13F 申報</a>(美國證券交易委員會)\n"

        )

        section += (

            "📰 媒體報導: "

            "<a href=\"https://www.cnbc.com\">CNBC</a> ／ "

            "<a href=\"https://www.wsj.com\">華爾街日報</a> ／ "

            "<a href=\"https://www.bloomberg.com\">Bloomberg</a> ／ "

            "<a href=\"https://www.marketwatch.com\">MarketWatch</a> ／ "

            "<a href=\"https://www.ft.com\">Financial Times</a> ／ "

            "<a href=\"https://seekingalpha.com\">Seeking Alpha</a>\n"

        )

        section += "⚠️ 注意: 13F 每季申報一次,數據有最多 45 天延遲,僅揭露多頭持倉\n"

        section += "ℹ️ 顯示各機構最近一期持倉揭露,申報日後括號標註距今天數\n"

        return section





    # ════════════════════════════════════════════════════════════════

    # ■ 區塊 ⑦  SEC Form 4 -- 高管持股異動監控

    # ════════════════════════════════════════════════════════════════



    def fetch_form4_insiders(self) -> List[Dict]:

        """

        從 SEC EDGAR RSS 抓取科技七巨頭 CEO / CFO 持股異動申報(Form 4).

        - 僅追蹤七巨頭(AAPL/MSFT/NVDA/GOOGL/AMZN/META/TSLA)的 CEO / CFO

        - 保留 30 天內的申報,超過則跳過

        - 快取 12 小時避免頻繁爬取



        每筆回傳格式:

        {

          'company_zh':  str,   # 公司中文名

          'company_en':  str,   # 公司英文名

          'ticker':      str,

          'name':        str,   # 申報人姓名

          'title':       str,   # 職稱(英文原文)

          'title_zh':    str,   # 職稱中文

          'action':      str,   # 'buy' / 'sell' / 'other'

          'shares':      int,   # 異動股數

          'price':       float, # 成交價格

          'amount_usd':  float, # 總金額 USD

          'date':        datetime,

          'accession_no':str,

          'link':        str,

        }

        """

        import hashlib as _hashlib



        now = datetime.now()



        # 快取命中

        if (self._form4_cache_time and

                (now - self._form4_cache_time).total_seconds() < self._form4_cache_ttl_hours * 3600 and

                self._form4_cache):

            logger.info("  [Form4] Using cached data")

            return self._form4_cache



        headers = {'User-Agent': 'WorkBuddy Monitor admin@workbuddy.com'}

        results = []



        # 職稱中文對照（僅白名單內職位需要翻譯）

        title_zh_map = {

            'ceo': '執行長 CEO', 'chief executive officer': '執行長 CEO',

            'chief executive': '執行長 CEO',

            'cfo': '財務長 CFO', 'chief financial officer': '財務長 CFO',

            'chief financial': '財務長 CFO',

        }



        def _title_zh(title_raw: str) -> str:

            tl = title_raw.lower().strip()

            for k, v in title_zh_map.items():

                if k in tl:

                    return v

            return title_raw



        def _is_whitelist_title(title_raw: str) -> bool:

            tl = title_raw.lower()

            return any(kw in tl for kw in self.form4_titles_whitelist)



        for company in self.form4_companies:

            cik_num = company['cik'].lstrip('0')

            # SEC EDGAR RSS:最近 40 筆 Form 4 申報

            rss_url = (

                f"https://www.sec.gov/cgi-bin/browse-edgar"

                f"?action=getcompany&CIK={cik_num}&type=4&dateb=&owner=include"

                f"&count=20&search_text=&output=atom"

            )

            try:

                resp = requests.get(rss_url, headers=headers, timeout=15)

                if not resp.ok:

                    logger.warning(f"  [Form4] {company['name']} RSS failed: {resp.status_code}")

                    continue



                feed = feedparser.parse(resp.text)

                for entry in feed.entries[:20]:

                    try:

                        # 取 accession number(entry id 末段)

                        entry_id = entry.get('id', '')

                        accession_no = entry_id.split('accession-number=')[-1] if 'accession-number=' in entry_id else entry_id



                        # 申報日期(保留30天內的申報,超過則跳過)

                        # SEC EDGAR Atom feed 只有 updated 欄位,無 published

                        pub_dt = now

                        try:

                            if entry.get('updated_parsed'):

                                pub_dt = datetime(*entry.updated_parsed[:6])

                            elif entry.get('filing-date'):

                                pub_dt = datetime.strptime(entry['filing-date'], '%Y-%m-%d')

                        except Exception:

                            pub_dt = now

                        if (now - pub_dt).days > 30:

                            continue



                        # 取申報連結

                        link = entry.get('link', '')



                        # 從 EDGAR XML 取詳細欄位(申報人姓名、職稱、交易明細)

                        # 先從索引頁取 xml 連結

                        an_clean = accession_no.replace('-', '')

                        index_url = (

                            f"https://www.sec.gov/Archives/edgar/data/{cik_num}/"

                            f"{an_clean}/{accession_no}-index.htm"

                        )

                        idx_resp = requests.get(index_url, headers=headers, timeout=10)

                        if not idx_resp.ok:

                            continue



                        import re as _re4

                        # 找 .xml 檔:優先選不含 xsl 路徑的(即原始資料 XML,非 XSLT 渲染版)

                        xml_hrefs = _re4.findall(

                            r'href="([^"]+\.xml)"', idx_resp.text, _re4.IGNORECASE

                        )

                        xml_url = None

                        # 第一輪:選不含 xsl 的路徑

                        for href in xml_hrefs:

                            if '/xsl' in href.lower():

                                continue

                            fname = href.split('/')[-1].lower()

                            if 'xslt' in fname or 'stylesheet' in fname:

                                continue

                            xml_url = href if href.startswith('http') else f"https://www.sec.gov{href}"

                            break

                        # 第二輪 fallback:若沒找到,取最後一個 xml(通常是原始資料)

                        if not xml_url and xml_hrefs:

                            href = xml_hrefs[-1]

                            xml_url = href if href.startswith('http') else f"https://www.sec.gov{href}"



                        if not xml_url:

                            continue



                        xml_resp = requests.get(xml_url, headers=headers, timeout=10)

                        if not xml_resp.ok:

                            continue



                        # 解析 Form 4 XML

                        try:

                            root4 = ET.fromstring(xml_resp.content)

                        except ET.ParseError:

                            continue



                        def _f4text(tag):

                            el = root4.find('.//' + tag)

                            return el.text.strip() if el is not None and el.text else ''



                        # 申報人資訊

                        reporter_name = _f4text('rptOwnerName')

                        reporter_title = _f4text('officerTitle')

                        # 若無 officerTitle,嘗試從 reportingOwnerRelationship 讀取

                        if not reporter_title:

                            is_director = _f4text('isDirector')

                            is_officer  = _f4text('isOfficer')

                            is_ten_pct  = _f4text('isTenPercentOwner')

                            if is_director == '1':

                                reporter_title = 'director'

                            elif is_officer == '1':

                                reporter_title = _f4text('officerTitle') or 'officer'

                            elif is_ten_pct == '1':

                                reporter_title = 'ten_percent_owner'

                            # 不 fallback 到 <value>(容易誤抓交易欄位)



                        if not _is_whitelist_title(reporter_title):

                            continue



                        # 交易明細(取第一筆非空)

                        action = 'other'

                        shares = 0

                        price = 0.0

                        for trans in root4.findall('.//nonDerivativeTransaction'):

                            try:

                                tc = trans.find('.//transactionCode')

                                tshares_el = trans.find('.//transactionShares/value')

                                tprice_el  = trans.find('.//transactionPricePerShare/value')

                                taq_el     = trans.find('.//transactionAcquiredDisposedCode/value')

                                if tc is None or tshares_el is None:

                                    continue

                                code = tc.text.strip() if tc.text else ''

                                aq   = taq_el.text.strip().upper() if taq_el is not None and taq_el.text else ''

                                s    = float(tshares_el.text.replace(',', '')) if tshares_el.text else 0

                                p    = float(tprice_el.text.replace(',', '')) if tprice_el is not None and tprice_el.text else 0.0

                                if code == 'P' or aq == 'A':

                                    action = 'buy'

                                elif code == 'S' or aq == 'D':

                                    action = 'sell'

                                shares += int(s)

                                if p > 0:

                                    price = p

                            except Exception:

                                continue



                        if shares == 0:

                            continue  # 無實際異動股數略過



                        amount_usd = shares * price



                        results.append({

                            'company_zh':   company['zh'],

                            'company_en':   company['name'],

                            'ticker':       company['ticker'],

                            'name':         reporter_name,

                            'title':        reporter_title,

                            'title_zh':     _title_zh(reporter_title),

                            'action':       action,

                            'shares':       shares,

                            'price':        price,

                            'amount_usd':   amount_usd,

                            'date':         pub_dt,

                            'accession_no': accession_no,

                            'link':         link or index_url,

                        })



                        # 標記已處理(不管是否推播,只要解析過就記錄)

                        self._form4_sent[accession_no] = now



                    except Exception as e:

                        logger.debug(f"  [Form4] {company['name']} entry parse error: {e}")

                        continue



            except Exception as e:

                logger.warning(f"  [Form4] {company['name']} fetch error: {e}")

                continue



        # 按日期降序

        results.sort(key=lambda x: x['date'], reverse=True)



        self._form4_cache = results

        self._form4_cache_time = now

        logger.info(f"[Form4] Fetched {len(results)} insider transactions")

        return results



    def fetch_form4_media_news(self) -> List[Dict]:

        """

        從六大財媒 RSS 抓取 Form 4 / 高管持股異動相關的媒體報導.

        關鍵字:form 4、insider buying、insider selling、executive trade、CEO sell、 CFO buy 等,

        搭配科技七巨頭名稱過濾.



        回傳格式:

        {

          'title':      str,

          'title_zh':   str,

          'summary':    str,

          'summary_zh': str,

          'source':     str,

          'link':       str,

          'published':  datetime,

          'key':        str,

          'company':    str,   # matched company ticker or None

        }

        """

        import hashlib as _hs



        now = datetime.now()



        headers = {

            'User-Agent': 'Mozilla/5.0 (compatible; WorkBuddyMonitor/1.0; +https://workbuddy.com)'

        }



        results = []

        seen_keys: set = set()



        # 六大財媒 RSS

        media_sources = [

            {'name': 'CNBC',             'url': 'https://www.cnbc.com/id/100003114/device/rss/rss.html'},

            {'name': 'WSJ',              'url': 'https://feeds.a.dj.com/rss/RSSMarketsMain.xml'},

            {'name': 'Bloomberg',        'url': 'https://feeds.bloomberg.com/markets/news.rss'},

            {'name': 'MarketWatch',      'url': 'https://feeds.marketwatch.com/marketwatch/topstories/'},

            {'name': 'Financial Times',  'url': 'https://www.ft.com/rss/home/us'},

            {'name': 'Seeking Alpha',    'url': 'https://seekingalpha.com/feed.xml'},

        ]



        # Form 4 / 七巨頭高管持股異動關鍵字（僅限 CEO / CFO 職銜）

        form4_keywords = [

            'form 4', 'form4',

            'ceo sells', 'ceo buys', 'ceo sold', 'ceo bought',

            'cfo sells', 'cfo buys', 'cfo sold', 'cfo bought',

            'chief executive sells', 'chief executive buys',

            'chief executive sold', 'chief executive bought',

            'chief financial sells', 'chief financial buys',

            'chief financial sold', 'chief financial bought',

        ]



        # 科技七巨頭名稱/代碼(用於匹配)

        company_patterns = {}

        for ticker, info in self.magnificent7.items():

            patterns = [ticker.lower(), info['name'].lower()]

            if info['zh']:

                patterns.append(info['zh'].lower())

            company_patterns[ticker] = patterns



        # 使用 CEO/CFO 關鍵字匹配媒體報導

        all_keywords = form4_keywords



        for src in media_sources:

            try:

                resp = requests.get(src['url'], headers=headers, timeout=15)

                feed = feedparser.parse(resp.text if resp.ok else src['url'])

                entries = feed.entries[:50]

                logger.debug(f"  [Form4Media] {src['name']}: fetched {len(entries)} entries")

                for entry in entries:

                    try:

                        title_raw = entry.get('title', '')

                        # ── 過濾錯誤頁標題 ──────────────────────────────

                        if self._is_error_title(title_raw):

                            logger.warning(f"  [Form4Media] [{src['name']}] Skipping error-page entry: {title_raw!r}")

                            continue

                        summary   = self._clean_html(entry.get('summary', entry.get('description', '')))

                        link      = entry.get('link', '')

                        combined  = (title_raw + ' ' + summary).lower()



                        # 必須包含 Form 4/高管持股關鍵字 或 VIP 關鍵字

                        if not any(kw in combined for kw in all_keywords):

                            continue



                        try:

                            pub_dt = datetime(*entry.published_parsed[:6])

                        except Exception:

                            pub_dt = now



                        key = _hs.sha256(title_raw.encode()).hexdigest()[:16]

                        if key in seen_keys:

                            continue

                        seen_keys.add(key)



                        # 匹配科技七巨頭

                        matched_company = None

                        for ticker, patterns in company_patterns.items():

                            if any(p in combined for p in patterns):

                                matched_company = ticker

                                break



                        try:

                            title_zh   = self.translator.translate(title_raw)    if len(title_raw)   < 200 else title_raw

                            summary_zh = self.translator.translate(summary[:300]) if summary else ''

                        except Exception:

                            title_zh   = title_raw

                            summary_zh = summary[:150]



                        results.append({

                            'title':      title_raw,

                            'title_zh':   title_zh,

                            'summary':    summary[:300],

                            'summary_zh': summary_zh,

                            'source':     src['name'],

                            'link':       link,

                            'published':  pub_dt,

                            'key':        key,

                            'company':    matched_company,

                        })

                    except Exception as e:

                        logger.debug(f"  [Form4Media] [{src['name']}] entry error: {e}")

            except Exception as e:

                logger.warning(f"  [Form4Media] {src['name']} RSS error: {e}")



        # 按時間降序,取前 20 筆

        results.sort(key=lambda x: x['published'], reverse=True)

        results = results[:20]



        logger.info(f"[Form4Media] Fetched {len(results)} media articles")

        return results



    def _format_form4_media_section(self, media_news: List[Dict]) -> str:

        """

        格式化 Form 4 / 七巨頭 CEO/CFO 持股異動媒體報導區塊.

        """

        section  = f"\n{'─'*40}\n"

        section += "📰 <b>媒體追蹤報導</b>(CNBC / WSJ / Bloomberg / MarketWatch / FT / Seeking Alpha)\n"

        section += "(僅追蹤七巨頭 CEO / CFO 持股異動相關)\n"

        section += f"{'─'*40}\n\n"



        if not media_news:

            section += "📭 目前無七巨頭 CEO / CFO 持股異動相關媒體報導\n"

            return section



        # 科技七巨頭優先

        mag7_news = [n for n in media_news if n['company']]



        if mag7_news:

            section += "🌟 <b>【科技七巨頭 CEO / CFO 持股異動】</b>\n"

            for idx, item in enumerate(mag7_news, 1):

                pub_str    = item['published'].strftime('%m/%d %H:%M')

                title_zh   = self._safe_title_zh(item)

                summary_zh = self._safe_summary_zh(item)

                company_zh = self.magnificent7.get(item['company'], {}).get('zh', item['company'])

                src_tag    = '📊' if item['source'] == 'CNBC' else '📰'

                section += (

                    f"  {src_tag} <b>#{idx}</b> [{html.escape(item['source'])}] {pub_str}\n"

                    f"     🏦 <b>{company_zh}({item['company']})</b>\n"

                    f"     <b><a href=\"{html.escape(item['link'])}\">{html.escape(title_zh)}</a></b>\n"

                )

                if summary_zh:

                    section += f"     📝 {html.escape(summary_zh[:150])}\n"

                section += "\n"



        if not mag7_news:

            section += "📭 目前無七巨頭 CEO / CFO 持股異動相關媒體報導\n"



        return section



    def _format_form4_section(self, trades: List[Dict], media_news: List[Dict] = None) -> str:

        """格式化 SEC Form 4 高管持股異動區塊"""

        section  = f"\n{'='*40}\n"

        section += "👔 <b>SEC Form 4 高管持股異動</b>(科技七巨頭 CEO / CFO)\n"

        section += "(CEO / CFO 買賣持股 -- 來源:SEC EDGAR 官方申報)\n"

        section += f"{'='*40}\n\n"



        if not trades:

            section += "📭 目前無重大高管持股異動申報\n"

            section += f"{'='*40}\n"

            return section



        # 只追蹤科技七巨頭 CEO / CFO 持股異動

        def _render_company_group(grouped_dict, label_emoji, label_text):

            s = ''

            if not grouped_dict:

                return s

            s += f"{label_emoji} <b>【{label_text}】</b>\n"

            for company_zh, items in grouped_dict.items():

                ticker = items[0]['ticker']

                s += f"🏢 <b>{company_zh}</b>({ticker})\n"

                for t in items:

                    action_emoji = '🟢 買入' if t['action'] == 'buy' else '🔴 賣出' if t['action'] == 'sell' else '⚪ 異動'

                    date_str = t['date'].strftime('%m/%d')

                    shares_str = f"{t['shares']:,}"

                    price_str  = f"${t['price']:,.2f}" if t['price'] > 0 else 'N/A'

                    amount_str = f"${t['amount_usd']:,.0f}" if t['amount_usd'] > 0 else 'N/A'

                    s += (

                        f"  {action_emoji}  <a href=\"{html.escape(t['link'])}\">{html.escape(t['name'])}</a>  [{html.escape(t['title_zh'])}]  {date_str}\n"

                        f"       股數: {shares_str} 股  均價: {price_str}  總額: {amount_str}\n"

                    )

                s += "\n"

            return s



        # 七巨頭分組並渲染

        grouped_mag7: Dict[str, List[Dict]] = {}

        for t in trades:

            grouped_mag7.setdefault(t['company_zh'], []).append(t)

        section += _render_company_group(grouped_mag7, '🌟', '科技七巨頭 CEO / CFO 持股異動')



        # ── 媒體報導子區塊 ──────────────────────────────────────────────

        if media_news is not None:

            section += self._format_form4_media_section(media_news)



        # ── 來源聲明 ───────────────────────────────────────────────────

        section += f"{'='*40}\n"

        section += (

            "👔 官方數據: <a href=\"https://www.sec.gov/cgi-bin/browse-edgar"

            "?action=getcompany&type=4&dateb=&owner=include&count=40\">"

            "SEC EDGAR Form 4 官方申報</a>\n"

        )

        section += (

            "📰 媒體報導: "

            "<a href=\"https://www.cnbc.com\">CNBC</a> ／ "

            "<a href=\"https://www.wsj.com\">WSJ</a> ／ "

            "<a href=\"https://www.bloomberg.com\">Bloomberg</a> ／ "

            "<a href=\"https://www.marketwatch.com\">MarketWatch</a> ／ "

            "<a href=\"https://www.ft.com\">Financial Times</a> ／ "

            "<a href=\"https://seekingalpha.com\">Seeking Alpha</a>\n"

        )

        section += "⚠️ 僅顯示 CEO / CFO 申報,同一筆申報不重複推播\n"

        return section



    # ════════════════════════════════════════════════════════════════

    # ■ 區塊 ⑧  IPO 重要訊息

    # ════════════════════════════════════════════════════════════════



    def fetch_ipo_news(self) -> List[Dict]:

        """

        從主流財媒 RSS 抓取最新 IPO 訊息:

          1. CNBC Markets RSS

          2. WSJ Markets RSS

          3. Bloomberg Markets RSS

          4. MarketWatch Top Stories RSS

          5. Financial Times Markets RSS

          6. Seeking Alpha RSS



        以 IPO 關鍵字過濾,7 日內同一標題(SHA256 前16位)不重複推播.



        每筆回傳格式:

        {

          'title':      str,

          'title_zh':   str,   # 中文翻譯

          'summary':    str,

          'summary_zh': str,

          'source':     str,   # 來源名稱

          'link':       str,

          'published':  datetime,

          'key':        str,   # 去重 key

        }

        """

        import hashlib as _hs



        now = datetime.now()



        # 快取命中

        if (self._ipo_cache_time and

                (now - self._ipo_cache_time).total_seconds() < self._ipo_cache_ttl_hours * 3600 and

                self._ipo_cache):

            logger.info("  [IPO] Using cached data")

            return self._ipo_cache



        # 清理超過 7 天已發送記錄

        self._ipo_sent = {

            k: v for k, v in self._ipo_sent.items()

            if (now - v).days < 7

        }



        headers = {'User-Agent': 'Mozilla/5.0 (compatible; WorkBuddyMonitor/1.0; +https://workbuddy.com)'}

        results = []

        seen_keys: set = set()



        # ── 六大財媒 RSS 來源(關鍵字 IPO 過濾)──────────────────────

        ipo_rss_sources = [

            {

                'name': 'CNBC',

                'url':  'https://www.cnbc.com/id/100003114/device/rss/rss.html',  # CNBC Markets

            },

            {

                'name': 'WSJ',

                'url':  'https://feeds.a.dj.com/rss/RSSMarketsMain.xml',          # WSJ Markets

            },

            {

                'name': 'Bloomberg',

                'url':  'https://feeds.bloomberg.com/markets/news.rss',            # Bloomberg Markets

            },

            {

                'name': 'MarketWatch',

                'url':  'https://feeds.marketwatch.com/marketwatch/topstories/',   # MarketWatch Top

            },

            {

                'name': 'Financial Times',

                'url':  'https://www.ft.com/rss/home/us',                          # FT US Edition

            },

            {

                'name': 'Seeking Alpha',

                'url':  'https://seekingalpha.com/feed.xml',                       # Seeking Alpha

            },

            {

                'name': '科技新報 TechNews',

                'url':  'https://technews.tw/feed/',                               # 繁體中文科技財經

            },

            {

                'name': 'MacroMicro 財經M平方',

                'url':  'https://www.macromicro.me/feed',                        # 台灣總經數據平台新聞

            },

            {

                'name': 'Digitimes 科技媒體',

                'url':  'https://gb-www.digitimes.com.tw/tech/rss/xml/xmlrss_10_0_cn.xml',

            },

        ]



        ipo_keywords = [

            'ipo', 'initial public offering', 'goes public', 'debut on',

            'listed on nasdaq', 'listed on nyse', 'priced its ipo',

            'filed for ipo', 's-1 filing', 'stock market debut',

            'trading debut', 'public listing', 'ipo pricing',

            # 科技新報中文關鍵字(繁體)

            'ipo', '上市', '首次公開募股', '掛牌', '公開發行',

        ]



        for src in ipo_rss_sources:

            try:

                resp = requests.get(src['url'], headers=headers, timeout=15)

                feed = feedparser.parse(resp.text if resp.ok else src['url'])

                entries = feed.entries[:40]

                logger.debug(f"  [IPO] {src['name']}: fetched {len(entries)} entries")

                for entry in entries:

                    try:

                        title_raw = entry.get('title', '')

                        # ── 過濾錯誤頁標題 ──────────────────────────────

                        if self._is_error_title(title_raw):

                            logger.warning(f"  [IPO] [{src['name']}] Skipping error-page entry: {title_raw!r}")

                            continue

                        summary   = self._clean_html(entry.get('summary', entry.get('description', '')))

                        link      = entry.get('link', '')

                        combined  = (title_raw + ' ' + summary).lower()

                        # 只保留 IPO 相關

                        if not any(kw in combined for kw in ipo_keywords):

                            continue

                        try:

                            pub_dt = datetime(*entry.published_parsed[:6])

                        except Exception:

                            pub_dt = now

                        key = _hs.sha256(title_raw.encode()).hexdigest()[:16]

                        if key in seen_keys or key in self._ipo_sent:

                            continue

                        seen_keys.add(key)

                        try:

                            title_zh   = self.translator.translate(title_raw)    if len(title_raw)   < 200 else title_raw

                            summary_zh = self.translator.translate(summary[:300]) if summary else ''

                        except Exception:

                            title_zh   = title_raw

                            summary_zh = summary[:150]

                        results.append({

                            'title':      title_raw,

                            'title_zh':   title_zh,

                            'summary':    summary[:300],

                            'summary_zh': summary_zh,

                            'source':     src['name'],

                            'link':       link,

                            'published':  pub_dt,

                            'key':        key,

                        })

                        self._ipo_sent[key] = now

                    except Exception as e:

                        logger.debug(f"  [IPO {src['name']}] entry error: {e}")

            except Exception as e:

                logger.warning(f"  [IPO] {src['name']} RSS error: {e}")



        # ── 補充 MacroMicro 財經M平方 IPO 相關文章 ───────────────────────

        try:

            mm_feed = feedparser.parse('https://www.macromicro.me/feed')

            for entry in mm_feed.entries[:40]:

                title_raw = entry.get('title', '')

                # ── 過濾錯誤頁標題 ──────────────────────────────────────

                if self._is_error_title(title_raw):

                    continue

                combined  = title_raw.lower()

                if not any(kw in combined for kw in ipo_keywords):

                    continue

                key = _hs.sha256(title_raw.encode()).hexdigest()[:16]

                if key in seen_keys or key in self._ipo_sent:

                    continue

                seen_keys.add(key)

                try:

                    title_zh = self.translator.translate(title_raw) if len(title_raw) < 200 else title_raw

                except Exception:

                    title_zh = title_raw

                pub_dt = now

                try:

                    if entry.get('published_parsed'):

                        pub_dt = datetime(*entry.published_parsed[:6])

                except Exception:

                    pass

                results.append({

                    'title':      title_raw,

                    'title_zh':   title_zh,

                    'summary':    '',

                    'summary_zh': '',

                    'source':     'MacroMicro 財經M平方',

                    'link':       entry.get('link', ''),

                    'published':  pub_dt,

                    'key':        key,

                })

                self._ipo_sent[key] = now

        except Exception as e:

            logger.warning(f"  [MacroMicro IPO] error: {e}")



        # 按時間降序

        results.sort(key=lambda x: x['published'], reverse=True)



        self._ipo_cache = results

        self._ipo_cache_time = now

        logger.info(f"[IPO] Fetched {len(results)} IPO news items")

        return results



    def _format_ipo_section(self, ipo_news: List[Dict]) -> str:

        """格式化 IPO 重要訊息區塊"""

        section  = f"\n{'='*40}\n"

        section += "🚀 <b>IPO 重要訊息</b>(新上市 ／ 上市動態)\n"

        section += f"{'='*40}\n\n"



        if not ipo_news:

            section += "📭 目前無重大 IPO 新聞\n"

            section += f"{'='*40}\n"

            return section



        for idx, item in enumerate(ipo_news, 1):

            pub_str    = item['published'].strftime('%m/%d %H:%M')

            title_show = self._safe_title_zh(item)

            sum_show   = self._safe_summary_zh(item)

            section += (

                f"#{idx} 📌 <b><a href=\"{html.escape(item['link'])}\">{html.escape(title_show)}</a></b>\n"

                f"   🔤 {html.escape(item['title'])}\n"

            )

            if sum_show:

                section += f"   📝 {html.escape(sum_show[:180])}\n"

            section += (

                f"   🕐 {pub_str}  ▪  來源: {html.escape(item['source'])}\n\n"

            )



        section += f"{'='*40}\n"

        section += (

            "📋 資料來源: "

            "<a href=\"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-1\">SEC EDGAR S-1 申報</a> ／ "

            "<a href=\"https://finance.yahoo.com\">Yahoo Finance</a> ／ "

            "<a href=\"https://seekingalpha.com\">Seeking Alpha</a> ／ "

            "<a href=\"https://www.marketwatch.com\">MarketWatch</a>\n"

        )

        section += "⚠️ 同一 IPO 訊息不重複推播\n"

        return section



    # ■ 區塊 ⑨  美股財報公布(科技七巨頭)

    # ════════════════════════════════════════════════════════════════



    def fetch_earnings_news(self) -> List[Dict]:

        """

        從主流財媒 RSS 抓取美股財報公布訊息,特別關注科技七巨頭:

          1. CNBC Markets RSS

          2. WSJ Markets RSS

          3. Bloomberg Markets RSS

          4. MarketWatch Top Stories RSS

          5. Financial Times Markets RSS

          6. Seeking Alpha RSS



        以財報關鍵字 + 科技七巨頭名稱過濾,7 日內同一標題不重複推播.



        每筆回傳格式:

        {

          'title':      str,

          'title_zh':   str,   # 中文翻譯

          'summary':    str,

          'summary_zh': str,

          'source':     str,   # 來源名稱

          'link':       str,

          'published':  datetime,

          'key':        str,   # 去重 key

          'company':    str,   # 匹配到的公司代碼(如 AAPL)

        }

        """

        import hashlib as _hs



        now = datetime.now()



        # 快取命中

        if (self._earnings_cache_time and

                (now - self._earnings_cache_time).total_seconds() < self._earnings_cache_ttl_hours * 3600 and

                self._earnings_cache):

            logger.info("  [Earnings] Using cached data")

            return self._earnings_cache



        # 清理超過 7 天已發送記錄

        self._earnings_sent = {

            k: v for k, v in self._earnings_sent.items()

            if (now - v).days < 7

        }



        headers = {'User-Agent': 'Mozilla/5.0 (compatible; WorkBuddyMonitor/1.0; +https://workbuddy.com)'}

        results = []

        seen_keys: set = set()



        # ── 六大財媒 RSS 來源(關鍵字財報過濾)──────────────────────

        earnings_rss_sources = [

            {

                'name': 'CNBC',

                'url':  'https://www.cnbc.com/id/100003114/device/rss/rss.html',

            },

            {

                'name': 'WSJ',

                'url':  'https://feeds.a.dj.com/rss/RSSMarketsMain.xml',

            },

            {

                'name': 'Bloomberg',

                'url':  'https://feeds.bloomberg.com/markets/news.rss',

            },

            {

                'name': 'MarketWatch',

                'url':  'https://feeds.marketwatch.com/marketwatch/topstories/',

            },

            {

                'name': 'Financial Times',

                'url':  'https://www.ft.com/rss/home/us',

            },

            {

                'name': 'Seeking Alpha',

                'url':  'https://seekingalpha.com/feed.xml',

            },

            {

                'name': '科技新報 TechNews',

                'url':  'https://technews.tw/feed/',                    # 繁體中文科技財報

            },

            {

                'name': 'MacroMicro 財經M平方',

                'url':  'https://www.macromicro.me/feed',               # 台灣總經數據平台新聞

            },

            {

                'name': 'Digitimes 科技媒體',

                'url':  'https://gb-www.digitimes.com.tw/tech/rss/xml/xmlrss_10_0_cn.xml',

            },

        ]



        # 財報相關關鍵字

        earnings_keywords = [

            'earnings', 'earnings report', 'quarterly results', 'quarterly earnings',

            'beats estimates', 'misses estimates', 'profit rises', 'profit falls',

            'revenue rises', 'revenue falls', 'eps', 'revenue growth', 'profit margin',

            'guidance', 'outlook', 'forecast', 'earnings call', 'q1 earnings',

            'q2 earnings', 'q3 earnings', 'q4 earnings', 'fiscal year',

            # 科技新報中文關鍵字(繁體)

            '財報', '季報', '營收', '獲利', '每股盈餘', '營業利益', '業績',

            '超越預期', '優於預期', '低於預期', '年增', '季增',

        ]



        # 科技七巨頭名稱/代碼(用於匹配)

        company_patterns = {}

        for ticker, info in self.magnificent7.items():

            patterns = [ticker.lower(), info['name'].lower()]

            if info['zh']:

                patterns.append(info['zh'].lower())

            company_patterns[ticker] = patterns



        for src in earnings_rss_sources:

            try:

                resp = requests.get(src['url'], headers=headers, timeout=15)

                feed = feedparser.parse(resp.text if resp.ok else src['url'])

                entries = feed.entries[:40]

                logger.debug(f"  [Earnings] {src['name']}: fetched {len(entries)} entries")

                for entry in entries:

                    try:

                        title_raw = entry.get('title', '')

                        # ── 過濾錯誤頁標題 ──────────────────────────────

                        if self._is_error_title(title_raw):

                            logger.warning(f"  [Earnings] [{src['name']}] Skipping error-page entry: {title_raw!r}")

                            continue

                        summary   = self._clean_html(entry.get('summary', entry.get('description', '')))

                        link      = entry.get('link', '')

                        combined  = (title_raw + ' ' + summary).lower()



                        # 必須包含財報關鍵字

                        if not any(kw in combined for kw in earnings_keywords):

                            continue



                        # 匹配科技七巨頭(優先顯示)

                        matched_company = None

                        for ticker, patterns in company_patterns.items():

                            if any(p in combined for p in patterns):

                                matched_company = ticker

                                break



                        try:

                            pub_dt = datetime(*entry.published_parsed[:6])

                        except Exception:

                            pub_dt = now



                        # 發佈超過 7 天的財報新聞不再顯示

                        if (now - pub_dt).days > 7:

                            continue



                        key = _hs.sha256(title_raw.encode()).hexdigest()[:16]

                        if key in seen_keys or key in self._earnings_sent:

                            continue

                        seen_keys.add(key)



                        try:

                            title_zh   = self.translator.translate(title_raw)    if len(title_raw)   < 200 else title_raw

                            summary_zh = self.translator.translate(summary[:300]) if summary else ''

                        except Exception:

                            title_zh   = title_raw

                            summary_zh = summary[:150]



                        results.append({

                            'title':      title_raw,

                            'title_zh':   title_zh,

                            'summary':    summary[:300],

                            'summary_zh': summary_zh,

                            'source':     src['name'],

                            'link':       link,

                            'published':  pub_dt,

                            'key':        key,

                            'company':    matched_company,

                        })

                        self._earnings_sent[key] = now

                    except Exception as e:

                        logger.debug(f"  [Earnings {src['name']}] entry error: {e}")

            except Exception as e:

                logger.warning(f"  [Earnings] {src['name']} RSS error: {e}")



        # ── 補充 MacroMicro 財經M平方財報相關文章 ────────────────────────

        try:

            mm_feed = feedparser.parse('https://www.macromicro.me/feed')

            for entry in mm_feed.entries[:40]:

                title_raw = entry.get('title', '')

                # ── 過濾錯誤頁標題 ──────────────────────────────────────

                if self._is_error_title(title_raw):

                    continue

                combined  = title_raw.lower()

                if not any(kw in combined for kw in earnings_keywords):

                    continue

                # 檢查是否匹配科技七巨頭

                matched_company = None

                for ticker, info in self.magnificent7.items():

                    patterns = [ticker.lower(), info['name'].lower()]

                    if any(p in combined for p in patterns):

                        matched_company = ticker

                        break

                key = _hs.sha256(title_raw.encode()).hexdigest()[:16]

                if key in seen_keys or key in self._earnings_sent:

                    continue

                seen_keys.add(key)

                try:

                    title_zh = self.translator.translate(title_raw) if len(title_raw) < 200 else title_raw

                except Exception:

                    title_zh = title_raw

                pub_dt = now

                try:

                    if entry.get('published_parsed'):

                        pub_dt = datetime(*entry.published_parsed[:6])

                except Exception:

                    pass

                results.append({

                    'title':      title_raw,

                    'title_zh':   title_zh,

                    'summary':    '',

                    'summary_zh': '',

                    'source':     'MacroMicro 財經M平方',

                    'link':       entry.get('link', ''),

                    'published':  pub_dt,

                    'key':        key,

                    'company':    matched_company,

                })

                self._earnings_sent[key] = now

        except Exception as e:

            logger.warning(f"  [MacroMicro Earnings] error: {e}")



        # 按時間降序,優先顯示科技七巨頭相關

        results.sort(key=lambda x: (x['company'] is None, x['published']), reverse=True)



        self._earnings_cache = results

        self._earnings_cache_time = now

        logger.info(f"[Earnings] Fetched {len(results)} earnings news items")

        return results



    def _format_earnings_section(self, earnings_news: List[Dict]) -> str:

        """格式化美股財報公布區塊"""

        section  = f"\n{'='*40}\n"

        section += "📊 <b>美股財報公布</b>(重點財報)\n"

        section += f"{'='*40}\n\n"



        if not earnings_news:

            section += "📭 目前無重大財報新聞\n"

            section += f"{'='*40}\n"

            return section



        # 分類顯示:科技七巨頭優先

        mag7_news = [n for n in earnings_news if n['company']]

        other_news = [n for n in earnings_news if not n['company']]



        if mag7_news:

            section += "🌟 <b>【科技七巨頭】</b>\n"

            for idx, item in enumerate(mag7_news, 1):

                pub_str    = item['published'].strftime('%m/%d %H:%M')

                title_show = self._safe_title_zh(item)

                sum_show   = self._safe_summary_zh(item)

                company_zh = self.magnificent7.get(item['company'], {}).get('zh', item['company'])

                section += (

                    f"#{idx} 📌 <b>{company_zh} ({item['company']})</b>\n"

                    f"   <b><a href=\"{html.escape(item['link'])}\">{html.escape(title_show)}</a></b>\n"

                    f"   🔤 {html.escape(item['title'])}\n"

                )

                if sum_show:

                    section += f"   📝 {html.escape(sum_show[:180])}\n"

                section += (

                    f"   🕐 {pub_str}  ▪  來源: {html.escape(item['source'])}\n\n"

                )



        if other_news:

            section += "📈 <b>【其他重要財報】</b>\n"

            for idx, item in enumerate(other_news, 1):

                pub_str    = item['published'].strftime('%m/%d %H:%M')

                title_show = self._safe_title_zh(item)

                sum_show   = self._safe_summary_zh(item)

                section += (

                    f"#{idx} 📌 <b><a href=\"{html.escape(item['link'])}\">{html.escape(title_show)}</a></b>\n"

                    f"   🔤 {html.escape(item['title'])}\n"

                )

                if sum_show:

                    section += f"   📝 {html.escape(sum_show[:180])}\n"

                section += (

                    f"   🕐 {pub_str}  ▪  來源: {html.escape(item['source'])}\n\n"

                )



        section += f"{'='*40}\n"

        section += (

            "📋 資料來源: "

            "<a href=\"https://www.cnbc.com/markets/\">CNBC</a> ／ "

            "<a href=\"https://www.wsj.com/markets\">WSJ</a> ／ "

            "<a href=\"https://www.bloomberg.com/markets\">Bloomberg</a> ／ "

            "<a href=\"https://www.marketwatch.com/\">MarketWatch</a> ／ "

            "<a href=\"https://www.ft.com/\">Financial Times</a> ／ "

            "<a href=\"https://seekingalpha.com/\">Seeking Alpha</a>\n"

        )

        section += "⚠️ 同一財報訊息不重複推播\n"

        return section



    def get_options_ranking(self) -> Tuple[List[Dict], List[Dict]]:

        """

        抓取美股選擇權交易量,回傳 Call 前10 與 Put 前10

        """

        def _safe_int(val, default=0) -> int:

            """安全轉 int,處理 NaN/None"""

            try:

                if val is None:

                    return default

                f = float(val)

                if math.isnan(f) or math.isinf(f):

                    return default

                return int(f)

            except (TypeError, ValueError):

                return default



        def _safe_float(val, default=0.0) -> float:

            """安全轉 float,處理 NaN/None"""

            try:

                if val is None:

                    return default

                f = float(val)

                if math.isnan(f) or math.isinf(f):

                    return default

                return f

            except (TypeError, ValueError):

                return default



        logger.info("Fetching US options data from Yahoo Finance...")

        call_records = []

        put_records  = []



        for ticker_sym in self.options_tickers:

            try:

                tk = yf.Ticker(ticker_sym)

                expirations = tk.options

                if not expirations:

                    continue



                # 只取最近兩個到期日以加快速度

                for expiry in expirations[:2]:

                    try:

                        chain = tk.option_chain(expiry)



                        # --- Call ---

                        for _, row in chain.calls.iterrows():

                            vol = _safe_int(row.get('volume'))

                            if vol > 0:

                                call_records.append({

                                    'ticker':        ticker_sym,

                                    'contract':      row.get('contractSymbol', ''),

                                    'expiry':        expiry,

                                    'strike':        _safe_float(row.get('strike')),

                                    'volume':        vol,

                                    'open_interest': _safe_int(row.get('openInterest')),

                                    'last_price':    _safe_float(row.get('lastPrice')),

                                    'iv':            _safe_float(row.get('impliedVolatility')),

                                    'type':          'CALL',

                                })



                        # --- Put ---

                        for _, row in chain.puts.iterrows():

                            vol = _safe_int(row.get('volume'))

                            if vol > 0:

                                put_records.append({

                                    'ticker':        ticker_sym,

                                    'contract':      row.get('contractSymbol', ''),

                                    'expiry':        expiry,

                                    'strike':        _safe_float(row.get('strike')),

                                    'volume':        vol,

                                    'open_interest': _safe_int(row.get('openInterest')),

                                    'last_price':    _safe_float(row.get('lastPrice')),

                                    'iv':            _safe_float(row.get('impliedVolatility')),

                                    'type':          'PUT',

                                })

                    except Exception as e:

                        logger.warning(f"Options chain error {ticker_sym} {expiry}: {e}")

                        continue



            except Exception as e:

                logger.error(f"Options fetch error {ticker_sym}: {e}")

                continue



        # 依個股聚合交易量（同一個股不同履約價合計），再取前10

        def _aggregate_by_ticker(records):

            agg = {}

            for r in records:

                t = r['ticker']

                if t not in agg:

                    agg[t] = {

                        'ticker':        t,

                        'volume':        0,

                        'open_interest': 0,

                        # 保留最大交易量的那筆合約資訊（履約價、到期日、IV）

                        'top_strike':    0.0,

                        'top_expiry':    '',

                        'top_iv':        0.0,

                        'top_vol':       0,

                        'type':          r['type'],

                    }

                agg[t]['volume']        += r['volume']

                agg[t]['open_interest'] += r['open_interest']

                # 記錄該個股中交易量最大的單一合約

                if r['volume'] > agg[t]['top_vol']:

                    agg[t]['top_vol']    = r['volume']

                    agg[t]['top_strike'] = r['strike']

                    agg[t]['top_expiry'] = r['expiry']

                    agg[t]['top_iv']     = r['iv']

            sorted_list = sorted(agg.values(), key=lambda x: x['volume'], reverse=True)

            for i, r in enumerate(sorted_list, 1):

                r['rank'] = i

            return sorted_list[:10]



        top_calls = _aggregate_by_ticker(call_records)

        top_puts  = _aggregate_by_ticker(put_records)



        logger.info(f"Options ranking done -- top calls: {len(top_calls)}, top puts: {len(top_puts)}")

        return top_calls, top_puts

    

    def _format_options_section(self, top_calls: List[Dict], top_puts: List[Dict]) -> str:

        """產生 Call/Put 排行的訊息區塊"""

        def fmt_vol(v: int) -> str:

            if v >= 1_000_000:

                return f"{v/1_000_000:.1f}M"

            if v >= 1_000:

                return f"{v/1_000:.1f}K"

            return str(v)



        section = ""



        if top_calls:

            section += f"\n{'='*35}\n"

            section += f"📈 <b>美股看多 Call 交易量 Top10</b>\n"

            section += f"{'='*35}\n\n"

            for r in top_calls:

                rank_emoji = "🥇" if r['rank'] == 1 else "🥈" if r['rank'] == 2 else "🥉" if r['rank'] == 3 else f"#{r['rank']}"

                iv_pct = f"{r['top_iv']*100:.1f}%" if r['top_iv'] > 0 else "N/A"

                section += (

                    f"{rank_emoji} <b>{html.escape(r['ticker'])}</b>  "

                    f"總交易量: {fmt_vol(r['volume'])}\n"

                    f"   最熱合約: 履約價 ${r['top_strike']:.1f}  到期 {html.escape(r['top_expiry'])}  IV: {iv_pct}\n"

                    f"   未平倉: {fmt_vol(r['open_interest'])}\n\n"

                )



        if top_puts:

            section += f"{'='*35}\n"

            section += f"📉 <b>美股看空 Put 交易量 Top10</b>\n"

            section += f"{'='*35}\n\n"

            for r in top_puts:

                rank_emoji = "🥇" if r['rank'] == 1 else "🥈" if r['rank'] == 2 else "🥉" if r['rank'] == 3 else f"#{r['rank']}"

                iv_pct = f"{r['top_iv']*100:.1f}%" if r['top_iv'] > 0 else "N/A"

                section += (

                    f"{rank_emoji} <b>{html.escape(r['ticker'])}</b>  "

                    f"總交易量: {fmt_vol(r['volume'])}\n"

                    f"   最熱合約: 履約價 ${r['top_strike']:.1f}  到期 {html.escape(r['top_expiry'])}  IV: {iv_pct}\n"

                    f"   未平倉: {fmt_vol(r['open_interest'])}\n\n"

                )



        return section



    # ■ 區塊 ⑪  ICT/AI 活動資訊(美/中/台,未來三個月)

    # ════════════════════════════════════════════════════════════════

    def _scrape_accupass_events(self, keywords: List[str] = None) -> List[Dict]:

        """

        從 ACCUPASS 活動通平台搜尋 ICT/AI 相關活動。

        針對指定關鍵字搜尋並解析事件標題、日期、地點、連結。



        Args:

            keywords: 搜尋關鍵字列表,預設 ['AI', 'Openclaw', 'Hermes', 'Gemini', 'Claude']



        Returns:

            與 fetch_ict_ai_events() 相同格式的活動列表

        """

        import hashlib as _hs

        import re

        from datetime import timedelta



        if keywords is None:

            keywords = ['AI', 'Openclaw', 'Hermes', 'Gemini', 'Claude']



        headers = {'User-Agent': 'Mozilla/5.0 (compatible; WorkBuddyMonitor/1.0)'}

        results = []

        seen_ids: set = set()



        taiwan_cities = [

            '台北市', '台中市', '高雄市', '桃園市', '新北市', '屏東縣',

            '台南市', '新竹市', '彰化縣', '雲林縣', '嘉義市', '基隆市',

            '宜蘭縣', '花蓮縣', '台東縣', '苗栗縣', '南投縣', '澎湖縣', '金門縣',

            '線上活動',

        ]

        city_pattern = '(' + '|'.join(taiwan_cities) + ')'



        for kw in keywords:

            try:

                url = 'https://www.accupass.com/search?q=' + kw

                resp = requests.get(url, headers=headers, timeout=15)

                if not resp.ok:

                    logger.debug('  [ACCUPASS] ' + kw + ': HTTP ' + str(resp.status_code))

                    continue

                resp.encoding = 'utf-8'



                # 解析 event-banner alt 屬性中的活動標題,以及附近的日期和 event ID

                for m in re.finditer(r'alt="event-banner-([^"]+?)"', resp.text):

                    title = m.group(1)

                    start = max(0, m.start() - 300)

                    end = min(len(resp.text), m.end() + 1200)

                    ctx = resp.text[start:end]



                    id_m = re.search(r'/event/(\d{15,30})', ctx)

                    if not id_m:

                        continue

                    event_id = id_m.group(1)



                    if event_id in seen_ids:

                        continue



                    date_m = re.search(r'(\d{4}\.\d{2}\.\d{2})', ctx)

                    loc_m = re.search(city_pattern, ctx)



                    event_date_raw = date_m.group(1) if date_m else ''

                    # 從完整 context 或 type tag 判斷線上/線下

                    location_label = loc_m.group(1) if loc_m else '台灣地區'

                    if location_label == '線上活動':

                        location_label = '線上活動'



                    # 解析活動日期

                    event_date = None

                    if event_date_raw:

                        try:

                            event_date = datetime.strptime(event_date_raw, '%Y.%m.%d')

                        except Exception:

                            pass



                    # 時間過濾: 僅保留未來 90 天內的活動,過期不列入

                    if event_date:

                        if event_date < datetime.now() or event_date > datetime.now() + timedelta(days=90):

                            continue

                    else:

                        # 無日期的事件跳過

                        continue



                    # 關鍵字過濾: 標題必須包含目標關鍵字

                    title_lower = title.lower()

                    if not any(kw.lower() in title_lower for kw in keywords):

                        continue



                    # 去重 key

                    key_raw = title + '|' + event_id

                    key = _hs.sha256(key_raw.encode('utf-8', errors='replace')).hexdigest()[:16]



                    seen_ids.add(event_id)



                    summary_text = '[ACCUPASS 活動通] ' + kw + ' 相關活動'

                    loc_display = location_label

                    if loc_display == '線上活動':

                        loc_display = '線上'



                    results.append({

                        'title':         title,

                        'title_zh':      title,  # ACCUPASS 已是中文,無需翻譯

                        'summary':       summary_text,

                        'summary_zh':    summary_text,

                        'source':        'ACCUPASS 活動通',

                        'link':          'https://www.accupass.com/event/' + event_id,

                        'published':     datetime.now(),

                        'event_date_raw': event_date_raw,

                        'event_date':    event_date,

                        'location':      '台灣地區' if loc_display != '線上' else '線上',

                        'is_online':    location_label == '線上活動',

                        'key':           key,

                    })



                logger.debug('  [ACCUPASS] ' + kw + ': ' + str(len([r for r in results if r['source'] == 'ACCUPASS 活動通'])) + ' events (cumulative)')



            except Exception as e:

                logger.warning('  [ACCUPASS] ' + kw + ': ' + str(e))

                continue



        # 依活動日期排序

        results.sort(key=lambda x: x['event_date'] or datetime.max)

        logger.info('  [ACCUPASS] Total unique events: ' + str(len(results)))

        return results



    def _scrape_digitimes_eventplus(self) -> List[Dict]:

        """

        從 DIGITIMES EventPlus (https://www.digitimes.com.tw/eventplus/) 抓取所有活動。

        頁面內嵌 JSON (recentActivities) 包含完整的 title/place/start_date/activity_url。



        Returns:

            與 fetch_ict_ai_events() 相同格式的活動列表

        """

        import hashlib as _hs

        import re

        import html as _html

        from datetime import timedelta



        headers = {'User-Agent': 'Mozilla/5.0 (compatible; WorkBuddyMonitor/1.0)'}

        results = []

        seen_keys: set = set()



        try:

            resp = requests.get('https://www.digitimes.com.tw/eventplus/', headers=headers, timeout=15)

            if not resp.ok:

                logger.warning('  [EventPlus] HTTP ' + str(resp.status_code))

                return results

            resp.encoding = 'utf-8'

            text = resp.text



            # 從內嵌 JSON (recentActivities) 提取每個活動物件

            # 格式: {&quot;unique_id&quot;:&quot;...&quot;,&quot;title&quot;:&quot;...&quot;,...,&quot;activity_url&quot;:&quot;...&quot;}

            # JSON 中的 / 被轉義為 \/

            event_blocks = re.findall(

                r'\{(?:[^{}]|\{[^{}]*\})*?&quot;unique_id&quot;(?:[^{}]|\{[^{}]*\})*?&quot;activity_url&quot;(?:[^{}]|\{[^{}]*\})*?\}',

                text

            )



            for block in event_blocks:

                # 提取各欄位（注意 JSON 中 / 被轉義為 \/）

                title_m = re.search(r'&quot;title&quot;:&quot;([^&]+?)&quot;', block)

                if not title_m:

                    continue

                # 解碼 HTML entities 和 Unicode (\uXXXX)

                title_raw = _html.unescape(title_m.group(1)).strip()

                # Python \uXXXX 轉義需要 encode/decode

                title = title_raw.encode('utf-8').decode('unicode_escape') if '\\u' in title_raw else title_raw



                uid_m = re.search(r'&quot;unique_id&quot;:&quot;([^&]+?)&quot;', block)

                _event_uid = uid_m.group(1) if uid_m else ''



                date_m = re.search(r'&quot;start_date&quot;:&quot;(\d{4}\\\/\d{2}\\\/\d{2})&quot;', block)

                date_raw = date_m.group(1).replace('\\/', '/') if date_m else ''



                url_m = re.search(r'&quot;activity_url&quot;:&quot;\s*(https?:\\\/\\\/[^&]+?)&quot;', block)

                url = url_m.group(1).replace('\\/', '/').strip() if url_m else ''



                place_m = re.search(r'&quot;place&quot;:&quot;([^&]+?)&quot;', block)

                place_raw = _html.unescape(place_m.group(1)).strip() if place_m else ''

                place = place_raw.encode('utf-8').decode('unicode_escape') if '\\u' in place_raw else place_raw



                loc_m = re.search(r'&quot;location&quot;:&quot;([^&]+?)&quot;', block)

                loc_raw = _html.unescape(loc_m.group(1)).strip() if loc_m else ''

                loc_region = loc_raw.encode('utf-8').decode('unicode_escape') if '\\u' in loc_raw else loc_raw



                status_m = re.search(r'&quot;attend_status&quot;:&quot;([^&]+?)&quot;', block)

                status_raw = _html.unescape(status_m.group(1)).strip() if status_m else ''

                status = status_raw.encode('utf-8').decode('unicode_escape') if '\\u' in status_raw else status_raw



                if not title or not url or not date_raw:

                    continue

                # 用 title+url 去重（同活動可能出現在多個區塊）

                dedup_key = title + '|' + url

                if dedup_key in seen_keys:

                    continue

                seen_keys.add(dedup_key)



                # 解析日期

                event_date = None

                try:

                    event_date = datetime.strptime(date_raw, '%Y/%m/%d')

                except Exception:

                    pass



                # 時間過濾：僅保留未來 90 天內的活動,過期不列入

                if event_date:

                    if event_date < datetime.now() or event_date > datetime.now() + timedelta(days=90):

                        continue

                else:

                    continue



                # 判斷線上/線下

                location_label = place if place else ('線上活動' if '線上' in title else '台灣地區')

                is_online = '線上' in location_label or 'online' in location_label.lower()



                # 去重 key

                key_raw = title + '|' + url

                key = _hs.sha256(key_raw.encode('utf-8', errors='replace')).hexdigest()[:16]



                summary_text = '[DIGITIMES EventPlus] ' + location_label



                results.append({

                    'title':          title,

                    'title_zh':       title,  # 已是中文

                    'summary':        summary_text,

                    'summary_zh':     summary_text,

                    'source':         'DIGITIMES EventPlus',

                    'link':           url,

                    'published':      datetime.now(),

                    'event_date_raw': date_raw,

                    'event_date':     event_date,

                    'location':       '台灣地區' if not is_online else '線上',

                    'is_online':      is_online,

                    'key':            key,

                })



            # 依活動日期排序

            results.sort(key=lambda x: x['event_date'] or datetime.max)

            logger.info('  [EventPlus] Total unique events: ' + str(len(results)))



        except Exception as e:

            logger.warning('  [EventPlus] ' + str(e))

            return results



        return results



    def _scrape_huodongxing_events(self, keywords: List[str] = None) -> List[Dict]:

        """

        從活動行 (huodongxing.com) 搜尋並解析活動。

        使用搜尋端點 /search?qs={kw}&list=list&st=1,4 進行關鍵字過濾,

        直接取得伺服器端篩選後的結果（非 SSR 全量列表）。

        僅保留未來 90 天內即將到來的活動（14 天連續推播去重在 fetch_ict_ai_events 中統一處理）。



        /search 頁面 HTML 結構 (search-tab-content-item-mesh):

          <a href="/event/{ID}?...">

            <img class="item-logo" src="..." alt="TITLE">

          </a>

          <div class="item-mesh-conter">

            <p class="date-pp">05月13日<span>08:00</span></p>

            <a class="item-title" href="/event/{ID}?..." title="TITLE">TITLE</a>

            <div class="item-dress flex">

              <p class="item-dress-pp">線上活動 / 城市</p>

            </div>

          </div>



        Args:

            keywords: 搜尋關鍵字列表,預設 ['AI', 'Openclaw', 'Hermes', 'Gemini', 'Claude']



        Returns:

            與 fetch_ict_ai_events() 相同格式的活動列表

        """

        import hashlib as _hs

        import re as _re

        import html as _html

        import time as _time

        import random as _random

        from datetime import timedelta



        if keywords is None:

            keywords = ['AI', 'Openclaw', 'Hermes', 'Gemini', 'Claude']



        # 使用手機 UA 繞過 WAF；若需綁定特定網路接口(BSSID/IP)可在 session.proxies 或

        # 自定義 HTTPAdapter(source_address=...) 中指定。

        # 手機熱點連線時請停用 Ethernet 確保流量從 Wi-Fi 接口出去。

        headers = {

            'User-Agent': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '

                          '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',

            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',

            'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',

            'Accept-Encoding': 'gzip, deflate, br',

        }

        results = []

        seen_ids: set = set()

        now = datetime.now()

        cutoff = now + timedelta(days=90)



        def _parse_hdx_date(time_str: str) -> Optional[datetime]:

            """解析活動行日期格式,失敗回傳 None.



            格式（/search 頁面）:

              - '05月13日<span>08:00</span>'  → 5/13

              - '05月13日 ~ 06月13日'        → 5/13 ~ 6/13（取開始日期）

            格式（/events 頁面 fallback）:

              - '明天 HH:MM'     → today + 1d

              - '后天 HH:MM'     → today + 2d

              - '今天 HH:MM'     → today

              - 'MM/DD 周X HH:MM' → MM/DD

            """

            if not time_str:

                return None

            # 先去除 HTML 標籤

            ts = _re.sub(r'<[^>]+>', '', time_str).strip()



            # 相對日期

            if ts.startswith('明天'):

                return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

            if ts.startswith('后天'):

                return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2)

            if ts.startswith('今天'):

                return now.replace(hour=0, minute=0, second=0, microsecond=0)



            # 中文日期: MM月DD日 (取第一個)

            m_cn = _re.search(r'(\d{1,2})月(\d{1,2})日', ts)

            if m_cn:

                month = int(m_cn.group(1))

                day = int(m_cn.group(2))

                try:

                    d = datetime(now.year, month, day)

                except ValueError:

                    return None

                # 跨年判斷: 若解析出來的日期「比今天早超過 60 天」,

                # 且月分小於當前月, 才視為跨年(明年)。

                # 這樣可避免把「今年已過期的活動」誤判為明年。

                delta_days = (now - d).days

                if delta_days > 60 and month < now.month:

                    try:

                        d = datetime(now.year + 1, month, day)

                    except ValueError:

                        pass

                return d



            # 斜線日期 MM/DD

            m = _re.search(r'(\d{1,2})/(\d{1,2})', ts)

            if m:

                month = int(m.group(1))

                day = int(m.group(2))

                try:

                    d = datetime(now.year, month, day)

                except ValueError:

                    return None

                if d < now - timedelta(days=1) and month < now.month:

                    try:

                        d = datetime(now.year + 1, month, day)

                    except ValueError:

                        pass

                return d



            return None



        waf_blocked = False



        for kw in keywords:

            if waf_blocked:

                logger.debug('  [Huodongxing] Skipping ' + kw + ' (WAF block active)')

                continue



            try:

                # 使用 /search 端點（伺服器端關鍵字過濾）

                url = ('https://www.huodongxing.com/search'

                       + '?ps=100&pi=0&list=list'

                       + '&qs=' + kw

                       + '&st=1,4')

                resp = requests.get(url, headers=headers, timeout=15)

                if not resp.ok:

                    logger.debug('  [Huodongxing] ' + kw + ': HTTP ' + str(resp.status_code))

                    continue

                resp.encoding = 'utf-8'

                text = resp.text



                # 偵測 WAF / GeeTest CAPTCHA 封鎖

                # 注意: 'gt3' 可能出現在合法頁面的 GeeTest SDK 載入腳本中,

                # 必須檢查是否為真正的封鎖頁面（透過多重特徵確認）

                is_blocked = (

                    '操作过于频繁' in text or '操作過於頻繁' in text

                    or 'verify.huodongxing.com' in text

                    or ('gt3' in text[:500] and 'search-tab-content' not in text[:2000])

                )

                if is_blocked:

                    logger.warning(

                        '  [Huodongxing] WAF/CAPTCHA blocked (IP-level). '

                        'All Huodongxing keywords will be skipped this run.'

                    )

                    waf_blocked = True

                    continue



                if len(text) < 10000 and 'search-tab-content' not in text:

                    logger.warning(

                        '  [Huodongxing] Response too small (' + str(len(text))

                        + ' bytes), likely blocked. Skipping all keywords.'

                    )

                    waf_blocked = True

                    continue



                # 找出所有活動 mesh 區塊（/search 頁面用 mesh 區塊為單位解析）

                mesh_blocks = list(_re.finditer(

                    r'class="search-tab-content-item-mesh',

                    text

                ))

                logger.debug('  [Huodongxing] ' + kw + ': found ' + str(len(mesh_blocks)) + ' mesh blocks')



                for mesh_m in mesh_blocks:

                    start = mesh_m.start()

                    end = min(len(text), start + 3000)

                    block = text[start:end]



                    # 活動 ID + 標題: <a class="item-title" href="/event/{ID}?..." title="TITLE">TITLE</a>

                    title_link = _re.search(

                        r'class="item-title"\s+href="/event/(\d+)\?[^"]*"\s*(?:target="[^"]*"\s*)?title="([^"]*)"',

                        block

                    )

                    if not title_link:

                        # 嘗試寬鬆匹配

                        title_link = _re.search(

                            r'item-title.*?href="/event/(\d+)\?',

                            block

                        )

                        if title_link:

                            event_id = title_link.group(1)

                            # 取 a 標籤內的純文字

                            title_a = _re.search(

                                r'item-title[^>]*>(.*?)</a>',

                                block, _re.DOTALL

                            )

                            title = _html.unescape(title_a.group(1)).strip() if title_a else ''

                            title = _re.sub(r'<[^>]+>', '', title).strip()

                        else:

                            continue

                    else:

                        event_id = title_link.group(1)

                        title = _html.unescape(title_link.group(2)).strip()



                    if not title or not event_id:

                        continue



                    # 去重 by event ID

                    if event_id in seen_ids:

                        continue



                    # 日期: <p class="date-pp">05月13日<span>08:00</span></p>

                    time_str = ''

                    date_p = _re.search(

                        r'class="date-pp"\s*>\s*(.*?)\s*</p>',

                        block, _re.DOTALL

                    )

                    if date_p:

                        time_str = date_p.group(1).strip()

                    if not time_str:

                        # Fallback: 一般 <p> 標籤

                        date_p2 = _re.search(r'<p[^>]*>\s*(\d{1,2}[月/].*?)\s*</p>', block, _re.DOTALL)

                        if date_p2:

                            time_str = date_p2.group(1).strip()



                    # 地點: <p class="item-dress-pp"> 或 <span class="item-dress-pp">

                    location_label = '線上活動'

                    loc_m = _re.search(

                        r'class="item-dress-pp"\s*>\s*(.*?)\s*</(?:p|span)>',

                        block, _re.DOTALL

                    )

                    if loc_m:

                        raw_loc = loc_m.group(1).strip()

                        raw_loc = _re.sub(r'<[^>]+>', '', raw_loc).strip()

                        if raw_loc:

                            location_label = raw_loc

                    is_online = '線上' in location_label



                    event_date = _parse_hdx_date(time_str)

                    if not event_date:

                        continue



                    # 90 天過濾: 用 .date() 比較,避免把「今天活動」濾掉

                    if event_date.date() < now.date() or event_date > cutoff:

                        continue



                    # /search 端點已由伺服器過濾關鍵字,無需本地過濾;

                    # 仍保留關鍵字過濾作為安全網（避免全站搜尋漏網之魚）

                    title_lower = title.lower()

                    if not any(k.lower() in title_lower for k in keywords):

                        continue



                    seen_ids.add(event_id)



                    key_raw = title + '|' + event_id

                    key = _hs.sha256(key_raw.encode('utf-8', errors='replace')).hexdigest()[:16]



                    summary_text = '[活動行] ' + kw + ' 相關活動'



                    results.append({

                        'title':         title,

                        'title_zh':      title,

                        'summary':       summary_text,

                        'summary_zh':    summary_text,

                        'source':        '活動行 Huodongxing',

                        'link':          'https://www.huodongxing.com/event/' + event_id,

                        'published':     now,

                        'event_date_raw': time_str[:60],

                        'event_date':    event_date,

                        'location':      '中國內地' if not is_online else '線上',

                        'is_online':     is_online,

                        'key':           key,

                    })



                logger.debug('  [Huodongxing] ' + kw + ': '

                             + str(len([r for r in results

                                        if r['source'] == '活動行 Huodongxing']))

                             + ' events (cumulative)')



            except Exception as e:

                logger.warning('  [Huodongxing] ' + kw + ': ' + str(e))

                continue



            # 避免頻繁請求被限流

            _time.sleep(2 + _random.random() * 3)



        # 依活動日期排序

        results.sort(key=lambda x: x['event_date'] or datetime.max)

        if waf_blocked and not results:

            logger.info(

                '  [Huodongxing] BLOCKED by WAF/CAPTCHA - 0 events. '

                '活動行使用 GeeTest 極驗驗證，目前伺服器 IP 被全站封鎖，無法爬取。'

            )

        else:

            if waf_blocked:

                logger.info('  [Huodongxing] Some keywords blocked by WAF, collected {} events'.format(len(results)))

            else:

                logger.info('  [Huodongxing] Total unique events: ' + str(len(results)))

        return results



    # ── allevents.in 搜尋（關鍵字: 拍, 棚）────────────────────────────

    def _scrape_allevents_events(self) -> List[Dict]:

        """

        從 https://allevents.in/taipei/photography 抓取台北攝影活動。

        過濾: 僅週一至週五

        排序: 活動日期由近到遠（ascending）

        來源標記: Allevents.in

        """

        import hashlib as _hs



        now = datetime.now()

        headers = {

            'User-Agent': (

                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '

                'AppleWebKit/537.36 (KHTML, like Gecko) '

                'Chrome/126.0.0.0 Safari/537.36'

            ),

            'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',

        }

        results = []

        seen_keys: set = set()



        try:

            url = 'https://allevents.in/taipei/photography'

            resp = requests.get(url, headers=headers, timeout=15)

            if resp.status_code != 200:

                logger.debug(f'  [Allevents] Taipei: HTTP {resp.status_code}')

                return results



            soup = BeautifulSoup(resp.text, 'html.parser')

            cards = soup.find_all('li', class_='event-card')

            logger.debug(f'  [Allevents] Taipei: found {len(cards)} cards')



            for card in cards:

                try:

                    # ── 基本欄位 ──────────────────────────────

                    e_id    = card.get('data-e-id', '')

                    link    = card.get('data-link', '')

                    title   = card.get('data-name', '').strip()

                    if not title or not link:

                        continue



                    # ── 活動日期（div.date）────────────────────

                    date_div  = card.find('div', class_='date')

                    date_raw = date_div.get_text(strip=True) if date_div else ''

                    event_date = None

                    if date_raw:

                        # 格式範例: "Fri, 05 Jun 2026 06:00 PM"

                        # 或跨日:  "Fri, 05 Jun - Sun, 07 Jun 2026"

                        first_part = date_raw.split(' - ')[0]

                        for fmt in [

                            '%a, %d %b, %Y - %I:%M %p',   # Sat, 06 Jun, 2026 - 09:00 AM

                            '%a, %d %b, %Y',               # Sat, 06 Jun, 2026

                            '%a, %d %b %Y %I:%M %p',

                            '%a, %d %b %Y',

                            '%a, %d %b %Y %I%p',

                        ]:

                            try:

                                event_date = datetime.strptime(

                                    first_part, fmt

                                )

                                break

                            except Exception:

                                continue



                    # ── 週六日過濾（僅週一至週五）──────────

                    # weekday(): Mon=0 … Sat=5, Sun=6

                    if event_date and event_date.weekday() >= 5:

                        continue



                    # ── 地點（div.location）──────────────────

                    loc_div  = card.find('div', class_='location')

                    location = loc_div.get_text(strip=True) if loc_div else ''



                    # ── 去重 key ────────────────────────────

                    key = _hs.sha256(

                        f'{e_id}_{title}'.encode()

                    ).hexdigest()[:16]

                    if key in seen_keys:

                        continue

                    seen_keys.add(key)



                    # ── 地點分類 ────────────────────────────

                    loc_lower = location.lower()

                    is_online = any(

                        w in loc_lower

                        for w in ['online', 'virtual', 'webinar', 'live stream',

                                  '線上', '網路', '遠端']

                    )

                    if is_online:

                        loc_tag = '線上'

                    else:

                        # URL 已限定台北攝影活動，預設標記為台灣地區

                        loc_tag = '台灣地區'



                    results.append({

                        'title':        title,

                        'title_zh':     title,

                        'summary':      location,

                        'summary_zh':   location,

                        'source':       'Allevents.in',

                        'link':         link,

                        'published':    event_date or now,

                        'event_date_raw': date_raw,

                        'event_date':   event_date,

                        'location':     loc_tag,

                        'is_online':   is_online,

                        'key':          key,

                    })



                except Exception as e:

                    logger.debug(f'  [Allevents] Parse error: {e}')

                    continue



        except Exception as e:

            logger.warning(f'  [Allevents] Taipei: {e}')



        # ── 排序：活動日期由近到遠 ─────────────────────────────

        results.sort(

            key=lambda x: x['event_date'] or datetime.max

        )

        logger.debug(f'  [Allevents] Total: {len(results)} events')

        return results



    def fetch_ict_ai_events(self) -> List[Dict]:

        """

        從四個活動平台爬蟲抓取 ICT/AI 活動資訊(會議/展覽/論壇/線上研討會),

        涵蓋: 美國矽谷、中國內地、台灣地區,未來三個月內。

        來源: ACCUPASS 活動通(關鍵字: AI/Openclaw/Hermes/Gemini/Claude),

               DIGITIMES EventPlus(https://www.digitimes.com.tw/eventplus/),

               活動行 Huodongxing(關鍵字: AI/Openclaw/Hermes/Gemini/Claude),

               Allevents.in(搜尋 Taipei 地點 → 標題過濾「拍」「棚」任一；僅週一至週五)

        ```

        {

          'title':         str,

          'title_zh':      str,    # 中文翻譯

          'summary':       str,

          'summary_zh':    str,

          'source':        str,    # 來源名稱

          'link':          str,    # 活動/報名網址

          'published':     datetime,# 文章發布時間

          'event_date_raw':str,    # 原始活動日期字串

          'event_date':    datetime,# 解析後活動日期(無法解析則 None)

          'location':      str,    # 地點標記: '矽谷'/'中國'/'台灣'/'線上'/'其他'

          'is_online':    bool,   # 是否線上活動

          'key':           str,    # 去重 key(SHA256 前16位)

        }

        """

        import hashlib as _hs

        from datetime import timedelta



        now = datetime.now()

        three_months_later = now + timedelta(days=90)



        # 確保 _events_sent 和 _event_push_days 始終存在

        if not hasattr(self, '_events_sent'):

            self._events_sent: Dict[str, datetime] = {}

        if not hasattr(self, '_event_push_days'):

            self._event_push_days: Dict[str, set] = {}



        # 快取命中

        if (self._ipo_cache_time and   # 重用 IPO 快取變數(若無則另建)

                (now - self._ipo_cache_time).total_seconds() < self._ipo_cache_ttl_hours * 3600 and

                self._ipo_cache):

            # 注意: 此處故意用 IPO 快取變數名稱,實際上應獨立;見下方 __init__ 補充

            pass



        # 確保有獨立的 events 快取變數(若 __init__ 尚未定義)

        if not hasattr(self, '_events_cache'):

            self._events_cache = []

            self._events_cache_time = None

            self._events_cache_ttl_hours = 12



        if (self._events_cache_time and

                (now - self._events_cache_time).total_seconds() < self._events_cache_ttl_hours * 3600 and

                self._events_cache):

            logger.info("  [Events] Using cached data")

            return self._events_cache



        # 清理超過 7 天已發送記錄

        self._events_sent = {

            k: v for k, v in self._events_sent.items()

            if (now - v).days < 7

        }



        results = []

        # ■ ACCUPASS 活動通 ────────────────────────────────────────────

        try:

            accupass_events = self._scrape_accupass_events()

            results.extend(accupass_events)

            logger.info(f"  [Events] ACCUPASS: fetched {len(accupass_events)} events")

        except Exception as e:

            logger.warning(f"  [Events] ACCUPASS: {e}")



        # ■ DIGITIMES EventPlus ─────────────────────────────────────────

        try:

            eventplus_events = self._scrape_digitimes_eventplus()

            results.extend(eventplus_events)

            logger.info(f"  [Events] EventPlus: fetched {len(eventplus_events)} events")

        except Exception as e:

            logger.warning(f"  [Events] EventPlus: {e}")



        # ■ 活動行 Huodongxing ─────────────────────────────────────────

        try:

            huodongxing_events = self._scrape_huodongxing_events()

            results.extend(huodongxing_events)

            logger.info(f"  [Events] Huodongxing: fetched {len(huodongxing_events)} events")

        except Exception as e:

            logger.warning(f"  [Events] Huodongxing: {e}")



        # ■ Allevents.in 搜尋（關鍵字: 拍, 棚）─────────────────────────────

        try:

            allevents_items = self._scrape_allevents_events()

            results.extend(allevents_items)

            logger.info(f"  [Events] Allevents.in: fetched {len(allevents_items)} events")

        except Exception as e:

            logger.warning(f"  [Events] Allevents.in: {e}")



        # 翻譯標題與摘要(中文來源跳過)

        zh_translate_sources = {

            'Digitimes 科技媒體', '科技新報 TechNews', 'MacroMicro 財經M平方',

            'ACCUPASS 活動通', 'DIGITIMES EventPlus', '活動行 Huodongxing',

            'Allevents.in',

        }

        for item in results:

            try:

                if item['source'] not in zh_translate_sources:

                    if item['title']:

                        item['title_zh'] = self.translate_text(item['title'][:200])

                else:

                    item['title_zh'] = item['title']

                if item['source'] not in zh_translate_sources:

                    if item['summary']:

                        item['summary_zh'] = self.translate_text(item['summary'][:300])

                else:

                    item['summary_zh'] = item['summary']

            except Exception:

                pass



        # ── 14 天連續推播去重 ──────────────────────────────────────────

        # 同一活動若已連續推播 14 天,第 15 天起不再列入顯示內容

        today_str = now.strftime('%Y-%m-%d')

        cutoff_14d_ago = (now - timedelta(days=14)).strftime('%Y-%m-%d')

        # 清理超過 14 天的舊記錄

        for k in list(self._event_push_days.keys()):

            self._event_push_days[k] = {

                d for d in self._event_push_days[k] if d >= cutoff_14d_ago

            }

            if not self._event_push_days[k]:

                del self._event_push_days[k]

        # 過濾：已推播 >= 14 天的活動跳過

        filtered = []

        skipped_push = 0

        for item in results:

            key = item.get('key', '')

            if key and key in self._event_push_days and len(self._event_push_days[key]) >= 14:

                skipped_push += 1

                continue

            filtered.append(item)

        if skipped_push > 0:

            logger.info(f"  [Events] Skipped {skipped_push} events (pushed >= 14 days)")

        results = filtered

        # 標記今天已推播的活動

        for item in results:

            key = item.get('key', '')

            if key:

                self._event_push_days.setdefault(key, set()).add(today_str)



        # 排序: 優先用活動日期,其次用文章發布日期(由近到遠 = 最早發生的活動排最前面)

        results.sort(key=lambda x: x['event_date'] or x['published'])



        # 寫入快取

        self._events_cache = results[:30]

        self._events_cache_time = now



        logger.info(f"  [Events] Total collected: {len(results)} ICT/AI events")

        return results[:30]



    def _format_ict_ai_events_section(self, events: List[Dict]) -> str:

        """格式化 ICT/AI 活動資訊區塊"""

        now = datetime.now()

        section  = f"\n{'='*40}\n"

        section += "🗓️ <b>ICT/AI 活動資訊</b>(未來三個月)\n"

        section += "(美國矽谷 ／ 中國內地 ／ 台灣地區 ｜ 線上+線下)\n"

        section += f"{'='*40}\n\n"



        if not events:

            section += "📭 目前無即將到來的 ICT/AI 活動資訊\n"

            section += f"{'='*40}\n"

            section += "📋 資料來源: ACCUPASS 活動通 / DIGITIMES EventPlus / 活動行 Huodongxing / Allevents.in\n"

            return section



        # 依月份分組

        month_groups: Dict[str, List[Dict]] = {}

        for item in events:

            dt = item['event_date'] or item['published']

            month_key = dt.strftime('%Y-%m')

            month_groups.setdefault(month_key, []).append(item)



        # 地點 emoji 映射

        loc_emoji = {

            '矽谷/美國':   '🇺🇸',

            '中國內地':    '🇨🇳',

            '台灣地區':    '🇹🇼',

            '線上':        '🌐',

            '其他':        '📍',

        }



        for month_key in sorted(month_groups.keys()):

            items = month_groups[month_key]

            month_dt = datetime.strptime(month_key + '-01', '%Y-%m-%d')

            month_label = month_dt.strftime('%Y年%m月')

            section += '📅 <b>【' + month_label + '】</b>\n'

            for item in items:

                pub_str    = (item['event_date'] or item['published']).strftime('%m/%d')

                loc_mark  = loc_emoji.get(item['location'], '\U0001f4cd')

                title_show = self._safe_title_zh(item)

                raw_sum    = self._safe_summary_zh(item)

                sum_show   = raw_sum[:150] if raw_sum else ''

                section += (

                    f"  {loc_mark} <b><a href=\"{html.escape(item['link'])}\">{html.escape(title_show)}</a></b>\n"

                )

                if sum_show:

                    section += f"   \U0001f4dd {html.escape(sum_show)}...\n"

                section += (

                    f"   \U0001f4c5 {pub_str}  \u25aa  \u5730\u9ede: {html.escape(item['location'])}\n\n"

                )



        section += f"{'='*40}\n"

        section += (

            "\U0001f4cb \u8cc7\u6599\u4f86\u6e90: "

            "ACCUPASS 活動通 / DIGITIMES EventPlus / 活動行 Huodongxing / Allevents.in\n"

        )

        section += "\u26a0\ufe0f \u540c\u4e00\u6d3b\u52d5\u8a0a\u606f\u4e0d\u91cd\u8907\u63a8\u64ad\uff1b\u540c\u4e00\u6d3b\u52d5\u6700\u591a\u9023\u7e8c\u63a8\u64ad14\u5929\uff0c\u7b2c15\u5929\u8d77\u4e0d\u518d\u5217\u5165\n"

        return section





    def generate_telegram_message(self, top_articles: List[Dict],

                                  top_calls: List[Dict] = None,

                                  top_puts: List[Dict] = None,

                                  politician_trades: List[Dict] = None,

                                  filings_13f: List[Dict] = None,

                                  media_13f: List[Dict] = None,

                                  form4_trades: List[Dict] = None,

                                  form4_media: List[Dict] = None,

                                  ipo_news: List[Dict] = None,

                                  earnings_news: List[Dict] = None,

                                  bls_indicators: List[Dict] = None,

                                  bls_news: List[Dict] = None,

                                  ict_ai_events: List[Dict] = None,

                                  mag7_events: List[Dict] = None) -> str:

        """生成Telegram消息"""

        current_time = datetime.now()

        date_str = current_time.strftime('%Y年%m月%d日')

        time_str = current_time.strftime('%H:%M')

        

        message = '\n\U0001f4f0 <b>七巨頭 + OpenAI/SpaceX/Anthropic 熱門新聞 Top10</b>\n'

        message += '\U0001f4c5 ' + date_str + ' EST\n'

        message += '\U0001f550 生成时间: ' + time_str + '\n'

        message += '========================================\n\n'

        

        for article in top_articles:

            # ── 最終防線: 只有 title（原文）是錯誤頁才跳過整篇；

            #    title_zh 翻譯結果是錯誤頁時，改回傳原文 title，不跳過 ──

            if self._is_error_title(article.get('title', '')):

                logger.warning(f"  [generate_msg] Skipping error-page article (title): {article.get('title','')!r}")

                continue

            if self._is_error_title(article.get('title_zh', '')):

                logger.warning(f"  [generate_msg] title_zh is error-page, fallback to original title: {article.get('title_zh','')!r}")

                article['title_zh'] = article.get('title', '')

            if self._is_error_title(article.get('summary_zh', '')):

                article['summary_zh'] = article.get('summary', '')

            r = article['rank']

            if r == 1:

                rank_emoji = "\U0001f947"

            elif r == 2:

                rank_emoji = "\U0001f948"

            elif r == 3:

                rank_emoji = "\U0001f949"

            else:

                rank_emoji = '#' + str(r)

            source_emoji = "\U0001f4ca" if article['source'] == 'CNBC' else "\U0001f4f0"

            

            message += rank_emoji + ' <b><a href="' + html.escape(article['link']) + '">' + html.escape(article['title_zh']) + '</a></b>\n'

            message += '   來源: ' + html.escape(article['source']) + '\n'

            message += '   摘要: ' + html.escape(article['summary_zh'][:150]) + '...\n\n'

        

        message += '========================================\n'

        message += '\U0001f916 由 WorkBuddy 新聞監控系統自動生成\n'

        message += '\U0001f4f0 新聞來源: CNBC & Wall Street Journal'



        # 若有 Call/Put 資料,附加在訊息末尾

        if top_calls or top_puts:

            message += self._format_options_section(top_calls or [], top_puts or [])

            message += '===================================\n'

            message += '\U0001f4ca 選擇權資料來源: Yahoo Finance'



        # 附加政治人物交易揭露(媒體報導來源)

        if politician_trades is not None:

            message += self._format_politician_trades_section(politician_trades)



        # 附加 SEC 官方 13F 持倉揭露 + CNBC/WSJ 媒體報導(合併在同一區塊)

        if filings_13f is not None:

            message += self._format_13f_section(filings_13f, media_news=media_13f)

        

        # ―― 區塊 ⑦:SEC Form 4 高管持股異動 ―――――――――――――――――――――――――

        if form4_trades is not None:

            message += self._format_form4_section(form4_trades, media_news=form4_media)

        

        # ―― 區塊 ⑧:IPO 重要訊息 ―――――――――――――――――――――――――――――――――

        if ipo_news is not None:

            message += self._format_ipo_section(ipo_news)

        

        # ―― 區塊 ⑨:美股財報公布(科技七巨頭)――――――――――――――――――――――

        if earnings_news is not None:

            message += self._format_earnings_section(earnings_news)

        

        # ―― 區塊 ⑩:美國勞工部(BLS) 官方經濟指標 ―――――――――――――――

        if bls_indicators is not None or bls_news:

            message += self._format_bls_section(bls_indicators, bls_news=bls_news)



        # ―― 區塊 ⑪:ICT/AI 活動資訊(美/中/台,未來三個月) ――――――

        if ict_ai_events is not None:

            message += self._format_ict_ai_events_section(ict_ai_events)

        

        return message



    def send_telegram_message(self, message: str, discord_webhook: str = None) -> bool:

        """發送通知訊息(超過4000字元自動分段)"""

        logger.info(f"Total message length: {len(message)} chars")

        # 超過 4000 字元時使用分段發送,否則走 send_to_all 一次搞定

        if len(message) > 4000:

            logger.info("Message too long, using chunked sending...")

            return self.notification_sender.send_long_message(message, discord_webhook=discord_webhook)

        results = self.notification_sender.send_to_all(message, discord_webhook=discord_webhook)

        return any(results.values())

    

    def run_news_only(self):

        """執行一次新聞監控檢查(區塊 ①~⑩)"""

        logger.info("="*50)

        logger.info("Starting news-only monitor check (blocks 1-10)...")

        logger.info("="*50)



        # 獲取熱門文章

        top_articles = self.get_top_articles()



        if not top_articles:

            logger.warning("No articles available")

            return



        # 顯示文章結果

        logger.info("\n" + "="*50)

        logger.info("Top 10 Articles:")

        logger.info("="*50)

        for article in top_articles:

            logger.info(f"#{article['rank']} [{article['source']}] {article['title']}")



        # 抓取選擇權 Call/Put 交易量排行(獨立容錯)

        top_calls, top_puts = [], []

        try:

            logger.info("Fetching options ranking...")

            top_calls, top_puts = self.get_options_ranking()

        except Exception as e:

            logger.error(f"Options ranking failed, will skip: {e}")



        # 抓取名人交易揭露(媒體報導來源,獨立容錯)

        politician_trades = []

        try:

            logger.info("Fetching VIP trade disclosures (media sources)...")

            politician_trades = self.fetch_politician_trades()

        except Exception as e:

            logger.error(f"VIP trade fetch failed, will skip: {e}")



        # 抓取 SEC 官方 13F 持倉申報(獨立容錯)

        filings_13f = []

        try:

            logger.info("Fetching SEC official 13F filings from EDGAR...")

            filings_13f = self.fetch_all_13f_trades()

        except Exception as e:

            logger.error(f"SEC 13F fetch failed, will skip: {e}")



        # 抓取 CNBC / WSJ 對 13F 機構的媒體報導(獨立容錯)

        media_13f = []

        try:

            logger.info("Fetching 13F-related media news from CNBC & WSJ...")

            media_13f = self.fetch_13f_media_news()

        except Exception as e:

            logger.error(f"13F media news fetch failed, will skip: {e}")



        # ── 區塊 ⑦:SEC Form 4 高管持股異動(科技七巨頭)──────────

        form4_trades = []

        try:

            logger.info("Fetching SEC Form 4 insider transactions...")

            form4_trades = self.fetch_form4_insiders()

        except Exception as e:

            logger.error(f"Form 4 fetch failed, will skip: {e}")



        # ── 區塊 ⑦(附):Form 4 媒體報導 ────────────────────────

        form4_media = []

        try:

            logger.info("Fetching Form 4 media news (CNBC/WSJ/Bloomberg/MarketWatch/FT/Seeking Alpha)...")

            form4_media = self.fetch_form4_media_news()

        except Exception as e:

            logger.error(f"Form 4 media fetch failed, will skip: {e}")



        # ── 區塊 ⑧:IPO 重要訊息 ─────────────────────────────────

        ipo_news = []

        try:

            logger.info("Fetching IPO news...")

            ipo_news = self.fetch_ipo_news()

        except Exception as e:

            logger.error(f"IPO news fetch failed, will skip: {e}")



        # ── 區塊 ⑨:美股財報公布(科技七巨頭)────────────────────

        earnings_news = []

        try:

            logger.info("Fetching US earnings news (Magnificent 7)...")

            earnings_news = self.fetch_earnings_news()

        except Exception as e:

            logger.error(f"Earnings news fetch failed, will skip: {e}")



        # ── 區塊 ⑩:美國勞工部(BLS) 官方經濟指標 ─────────────

        bls_indicators = []

        bls_news = []

        try:

            logger.info("Fetching BLS economic indicators (CPI/PPI/unemployment/nonfarm payroll)...")

            bls_indicators = self.fetch_bls_indicators()

        except Exception as e:

            logger.error(f"BLS indicators fetch failed, will skip: {e}")

        try:

            logger.info("Fetching BLS-related news from financial media...")

            bls_news = self.fetch_bls_news()

        except Exception as e:

            logger.error(f"BLS news fetch failed, will skip: {e}")



        # 發送整合通知(不含區塊 ⑪)

        logger.info("Sending notification report (news blocks 1-10)...")

        notification_message = self.generate_telegram_message(

            top_articles, top_calls, top_puts, politician_trades,

            filings_13f, media_13f,

            form4_trades=form4_trades,

            form4_media=form4_media,

            ipo_news=ipo_news,

            earnings_news=earnings_news,

            bls_indicators=bls_indicators,

            bls_news=bls_news,

            ict_ai_events=None,  # 區塊 ⑪ 獨立發送

        )

        self.send_telegram_message(notification_message)



        logger.info("News-only monitor check completed!")

        logger.info("="*50 + "\n")



    def run_events_only(self):

        """執行一次活動監控檢查(區塊 ⑪ 獨立)"""

        logger.info("="*50)

        logger.info("Starting events-only monitor check (block 11)...")

        logger.info("="*50)



        # ── 區塊 ⑪:ICT/AI 活動資訊(美/中/台,未來三個月) ─────

        ict_ai_events = []

        try:

            logger.info("Fetching ICT/AI events (US/China/Taiwan, next 3 months)...")

            ict_ai_events = self.fetch_ict_ai_events()

        except Exception as e:

            logger.error(f"ICT/AI events fetch failed, will skip: {e}")



        if not ict_ai_events:

            logger.info("No ICT/AI events to send.")

            logger.info("Events-only monitor check completed (no data).")

            logger.info("="*50 + "\n")

            return



        # 發送活動通知(僅區塊 ⑪)

        logger.info("Sending events notification (block 11 only)...")

        events_message = self._format_ict_ai_events_section(ict_ai_events)

        events_discord_webhook = self.config.get('discord', {}).get('events_webhook_url')

        self.send_telegram_message(events_message, discord_webhook=events_discord_webhook)



        logger.info("Events-only monitor check completed!")

        logger.info("="*50 + "\n")



    def run_monitor(self):

        """執行一次完整監控檢查(新聞 + 活動, 為相容性保留)"""

        logger.info("="*50)

        logger.info("Starting full monitor check (news + events)...")

        logger.info("="*50)



        # 執行新聞區塊 ①~⑩

        self.run_news_only()



        # 執行活動區塊 ⑪

        self.run_events_only()



    def run_daily(self, target_hour: int = 6):

        """每天定时执行监控(默认早上6点)"""

        logger.info(f"Starting daily news monitor at {target_hour}:00 AM...")

        

        while True:

            try:

                now = datetime.now()

                

                # 检查是否到了目标时间

                if now.hour == target_hour and now.minute < 5:

                    self.run_monitor()

                    

                    # 等待1小时避免重复执行

                    time.sleep(3600)

                else:

                    # 计算到下一个目标时间的等待时间

                    if now.hour < target_hour:

                        next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)

                    else:

                        next_run = (now.replace(hour=target_hour, minute=0, second=0, microsecond=0) + timedelta(days=1))

                    

                    wait_seconds = (next_run - now).total_seconds()

                    logger.info(f"Next run at {next_run.strftime('%Y-%m-%d %H:%M')} EST, waiting {wait_seconds/3600:.1f} hours")

                    time.sleep(min(wait_seconds, 3600))

                

            except KeyboardInterrupt:

                logger.info("\nStopping monitor...")

                break

            except Exception as e:

                logger.error(f"Error in daily loop: {e}")

                time.sleep(300)  # 出错后等待5分钟再重试





def main():

    """主函数"""

    try:

        print("""

╔═══════════════════════════════════════════════════════╗

║                                                       ║

║        每日热门财经新闻监控系统                       ║

║        Daily Popular News Monitor                     ║

║                                                       ║

╚═══════════════════════════════════════════════════════╝

        """)

    except:

        pass

    

    monitor = NewsMonitor()

    

    # 运行一次测试

    monitor.run_monitor()

    

    # 如果需要每天定时运行,取消下面的注释

    # monitor.run_daily(target_hour=6)





if __name__ == "__main__":

    main()

