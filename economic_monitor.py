#!/usr/bin/env python3

"""

经济指标监控工具 - 实时经济指标通知

Economic Indicator Monitor - Real-time Economic Data Alerts

"""



import requests

from datetime import datetime, timedelta

import time

import json

import logging

from typing import List, Dict

# ── Patch yfinance cache directory (must be before yfinance import) ──
import os as _os
import tempfile as _tempfile
_YF_SAFE_CACHE = _os.path.join(_tempfile.gettempdir(), 'yfinance_sandbox')
_os.makedirs(_YF_SAFE_CACHE, exist_ok=True)
_os.environ['YF_CACHE_DIR'] = _YF_SAFE_CACHE
try:
    import appdirs as _ad
    _ad.user_cache_dir = lambda: _YF_SAFE_CACHE
except ImportError:
    pass

import yfinance as yf

# Directly override yfinance cache class attributes (belt-and-suspenders)
import yfinance.cache as _yf_cache
_yf_cache._CookieDBManager._cache_dir = _YF_SAFE_CACHE
_yf_cache._TzDBManager._cache_dir = _YF_SAFE_CACHE
_yf_cache._ISINDBManager._cache_dir = _YF_SAFE_CACHE

from deep_translator import GoogleTranslator

from notification_sender import NotificationSender



# 配置日誌

logging.basicConfig(

    level=logging.INFO,

    format='%(asctime)s - %(levelname)s - %(message)s',

    handlers=[

        logging.FileHandler('economic_monitor.log', encoding='utf-8'),

        logging.StreamHandler()

    ]

)

logger = logging.getLogger(__name__)





class EconomicMonitor:

    """经济指标监控类"""

    

    def __init__(self, config_file: str = 'config.json'):

        """初始化监控器"""

        self.config = self.load_config(config_file)

        self.notification_sender = NotificationSender(self.config)

        self.economic_config = self.config.get('economic', {})

        self.timezone = self.economic_config.get('timezone', 'US/Eastern')

        self.fred_api_key = self.config.get('fred_api_key', '')

        self.translator = GoogleTranslator(source='en', target='zh-TW')

        

        # 美国主要经济指标配置

        self.economic_indicators = [

            {

                'name': 'GDP增长率',

                'name_en': 'GDP Growth Rate',

                'importance': 'high',

                'category': 'growth'

            },

            {

                'name': '联邦基金利率',

                'name_en': 'Federal Funds Rate',

                'importance': 'high',

                'category': 'rate'

            },

            {

                'name': '失业率',

                'name_en': 'Unemployment Rate',

                'importance': 'high',

                'category': 'employment'

            },

            {

                'name': 'CPI通胀指数',

                'name_en': 'CPI Inflation',

                'importance': 'high',

                'category': 'inflation'

            },

            {

                'name': 'PCE物价指数',

                'name_en': 'PCE Price Index',

                'importance': 'medium',

                'category': 'inflation'

            },

            {

                'name': '非农就业数据',

                'name_en': 'Non-Farm Payrolls',

                'importance': 'high',

                'category': 'employment'

            },

            {

                'name': '零售销售',

                'name_en': 'Retail Sales',

                'importance': 'medium',

                'category': 'consumer'

            },

            {

                'name': 'PMI制造业指数',

                'name_en': 'PMI Manufacturing',

                'importance': 'medium',

                'category': 'production'

            },

            {

                'name': '新屋开工',

                'name_en': 'Housing Starts',

                'importance': 'medium',

                'category': 'housing'

            },

            {

                'name': '消费者信心指数',

                'name_en': 'Consumer Confidence',

                'importance': 'medium',

                'category': 'consumer'

            }

        ]

        

        # Yahoo Finance 经济相关ETF和指数

        self.yahoo_finance_indicators = [

            {

                'name': '10年期美国国债收益率',

                'name_en': 'US 10-Year Treasury Yield',

                'ticker': '^TNX',

                'importance': 'high',

                'category': 'bond'

            },

            {

                'name': '美元指数',

                'name_en': 'US Dollar Index',

                'ticker': 'DX-Y.NYB',

                'importance': 'high',

                'category': 'currency'

            },

            {

                'name': 'VIX波动率指数',

                'name_en': 'VIX Volatility Index',

                'ticker': '^VIX',

                'importance': 'high',

                'category': 'volatility'

            },

            {

                'name': '标普500指数',

                'name_en': 'S&P 500 Index',

                'ticker': '^GSPC',

                'importance': 'medium',

                'category': 'equity'

            },

            {

                'name': '纳斯达克指数',

                'name_en': 'NASDAQ Composite',

                'ticker': '^IXIC',

                'importance': 'medium',

                'category': 'equity'

            },

            {

                'name': '黄金期货',

                'name_en': 'Gold Futures',

                'ticker': 'GC=F',

                'importance': 'medium',

                'category': 'commodity'

            },

            {

                'name': '原油期货',

                'name_en': 'Crude Oil Futures',

                'ticker': 'CL=F',

                'importance': 'medium',

                'category': 'commodity'

            }

        ]



        # 记录已发送的事件

        self.sent_events = set()

        self.last_values = {}

        

        logger.info(f"EconomicMonitor initialized with {len(self.economic_indicators)} indicators")

    

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

    

    def fetch_economic_calendar(self) -> List[Dict]:

        """从 Yahoo Finance 获取经济指标数据"""

        logger.info("Fetching economic indicators from Yahoo Finance...")

        events = []



        try:

            # 获取所有指标代码

            tickers = [ind['ticker'] for ind in self.yahoo_finance_indicators]

            tickers_obj = yf.Tickers(tickers)



            for indicator in self.yahoo_finance_indicators:

                try:

                    ticker = indicator['ticker']

                    info = tickers_obj.tickers.get(ticker)

                    if not info:

                        continue

                    info = info.info



                    current_price = info.get('regularMarketPrice', 0) or 0

                    previous_close = info.get('previousClose', 0) or 0

                    change = info.get('regularMarketChange', 0) or 0

                    change_percent = info.get('regularMarketChangePercent', 0) or 0



                    if current_price > 0:

                        # 检查是否有显著变化

                        last_value = self.last_values.get(ticker)

                        change_sign = "+" if change_percent >= 0 else ""



                        # 只在有显著变化或首次获取时添加事件

                        if last_value is None or abs(change_percent) > 0.5:

                            events.append({

                                'source': 'Yahoo Finance',

                                'title': f"{indicator['name_en']}: {current_price:.2f}",

                                'ticker': ticker,

                                'name_zh': indicator['name'],

                                'current_value': current_price,

                                'change': change,

                                'change_percent': change_percent,

                                'importance': indicator['importance'],

                                'category': indicator['category'],

                                'published': datetime.now(),

                                'link': f"https://finance.yahoo.com/quote/{ticker}"

                            })



                            # 更新上次值

                            self.last_values[ticker] = current_price



                            logger.info(f"{indicator['name']}: {current_price:.2f} ({change_sign}{change_percent:.2f}%)")



                except Exception as e:

                    logger.error(f"Error fetching {indicator['ticker']}: {e}")

                    continue



            logger.info(f"Fetched {len(events)} economic indicators from Yahoo Finance")



        except Exception as e:

            logger.error(f"Error fetching from Yahoo Finance: {e}")



        return events

    

    def determine_importance(self, title: str) -> str:

        """根据标题判断事件重要性"""

        high_keywords = [

            'Federal Funds Rate', 'FOMC', 'Interest Rate Decision',

            'GDP', 'Non-Farm Payrolls', 'Unemployment Rate', 'CPI',

            'PCE', 'Consumer Price Index', 'Core CPI', 'PPI'

        ]

        

        medium_keywords = [

            'Retail Sales', 'PMI', 'Manufacturing', 'Services',

            'Housing', 'Construction', 'Trade Balance', 'Consumer Confidence'

        ]

        

        title_upper = title.upper()

        

        for keyword in high_keywords:

            if keyword.upper() in title_upper:

                return 'high'

        

        for keyword in medium_keywords:

            if keyword.upper() in title_upper:

                return 'medium'

        

        return 'low'

    

    def fetch_latest_indicators(self) -> List[Dict]:

        """从 Yahoo Finance 获取最新经济指标数据"""

        logger.info("Fetching latest economic indicators from Yahoo Finance...")

        indicators = []



        try:

            tickers = [ind['ticker'] for ind in self.yahoo_finance_indicators]

            tickers_obj = yf.Tickers(tickers)



            for indicator in self.yahoo_finance_indicators:

                try:

                    ticker = indicator['ticker']

                    info = tickers_obj.tickers.get(ticker)

                    if not info:

                        continue

                    info = info.info



                    current_price = info.get('regularMarketPrice', 0) or 0

                    change = info.get('regularMarketChange', 0) or 0

                    change_percent = info.get('regularMarketChangePercent', 0) or 0



                    if current_price > 0:

                        change_sign = "+" if change_percent >= 0 else ""

                        change_emoji = "📈" if change_percent >= 0 else "📉" if change_percent < 0 else "➡️"



                        # 格式化显示值

                        if indicator['category'] == 'bond':

                            value_str = f"{current_price:.2f}%"

                        elif indicator['category'] == 'volatility':

                            value_str = f"{current_price:.2f}"

                        elif indicator['category'] == 'currency':

                            value_str = f"{current_price:.2f}"

                        else:

                            value_str = f"{current_price:.2f}"



                        indicators.append({

                            'name': indicator['name'],

                            'name_en': indicator['name_en'],

                            'importance': indicator['importance'],

                            'category': indicator['category'],

                            'ticker': ticker,

                            'timestamp': datetime.now(),

                            'value': value_str,

                            'change': f"{change_sign}{change_percent:.2f}%",

                            'change_percent': change_percent,

                            'source': 'Yahoo Finance',

                            'link': f"https://finance.yahoo.com/quote/{ticker}",

                        })



                        logger.info(f"{indicator['name']}: {value_str} ({change_sign}{change_percent:.2f}%)")



                except Exception as e:

                    logger.error(f"Error fetching {indicator['ticker']}: {e}")

                    continue



            logger.info(f"Fetched {len(indicators)} indicators from Yahoo Finance")



        except Exception as e:

            logger.error(f"Error fetching indicators from Yahoo Finance: {e}")



        return indicators



    def fetch_fred_indicators(self) -> List[Dict]:

        """從 FRED API 獲取 CPI、失業率、聯準會利率最新數據"""

        if not self.fred_api_key:

            logger.warning("FRED API key not configured, skipping FRED indicators")

            return []



        fred_series = [

            {

                'series_id': 'CPIAUCSL',

                'name': '美國消費者物價指數(CPI)',

                'name_en': 'US CPI (Urban Consumers)',

                'importance': 'high',

                'category': 'inflation',

                'unit': '指數',

                'mom': True,   # 計算月增率

            },

            {

                'series_id': 'UNRATE',

                'name': '美國失業率',

                'name_en': 'US Unemployment Rate',

                'importance': 'high',

                'category': 'employment',

                'unit': '%',

                'yoy': False,

            },

            {

                'series_id': 'FEDFUNDS',

                'name': '聯準會基準利率',

                'name_en': 'Federal Funds Effective Rate',

                'importance': 'high',

                'category': 'rate',

                'unit': '%',

                'yoy': False,

            },

        ]



        indicators = []

        base_url = 'https://api.stlouisfed.org/fred/series/observations'



        for series in fred_series:

            try:

                params = {

                    'series_id': series['series_id'],

                    'api_key': self.fred_api_key,

                    'file_type': 'json',

                    'sort_order': 'desc',

                    'limit': 2,   # 只需最近2筆計算月增率

                }

                resp = requests.get(base_url, params=params, timeout=10)

                resp.raise_for_status()

                data = resp.json()

                observations = [o for o in data.get('observations', []) if o.get('value') != '.']



                if not observations:

                    logger.warning(f"No data for FRED series {series['series_id']}")

                    continue



                latest = observations[0]

                current_value = float(latest['value'])

                release_date = latest['date']



                # 計算月變化（失業率/利率為百分點；CPI 計算月增率 %）

                change_str = 'N/A'

                change_percent = 0.0

                extra_str = ''

                if len(observations) >= 2:

                    prev_value = float(observations[1]['value'])

                    change = current_value - prev_value

                    sign = '+' if change >= 0 else ''



                    if series.get('mom') and prev_value != 0:

                        # CPI 月增率 = (本月 - 上月) / 上月 * 100

                        mom_pct = change / prev_value * 100

                        change_percent = mom_pct

                        change_str = f"{sign}{change:.3f}"

                        extra_str = f"  月增率: {sign}{mom_pct:.2f}%"

                    else:

                        change_percent = change

                        change_str = f"{sign}{change:.2f}"



                unit = series['unit']

                value_str = f"{current_value:.2f}{unit}"



                indicators.append({

                    'name': series['name'],

                    'name_en': series['name_en'],

                    'importance': series['importance'],

                    'category': series['category'],

                    'ticker': series['series_id'],

                    'timestamp': datetime.now(),

                    'release_date': release_date,

                    'value': value_str,

                    'change': change_str,

                    'change_percent': change_percent,

                    'extra': extra_str,

                    'source': 'FRED',

                    'link': f"https://fred.stlouisfed.org/series/{series['series_id']}",

                })



                logger.info(f"{series['name']}: {value_str} (月變化: {change_str}){extra_str}")



            except Exception as e:

                logger.error(f"Error fetching FRED series {series['series_id']}: {e}")

                continue



        logger.info(f"Fetched {len(indicators)} indicators from FRED")

        return indicators



    def get_mock_value(self, category: str) -> str:

        """获取模拟数值（实际使用时替换）"""

        mock_values = {

            'growth': '2.1%',

            'rate': '5.25-5.50%',

            'employment': '3.8%',

            'inflation': '3.2%',

            'consumer': '0.4%',

            'production': '50.3',

            'housing': '1.45M'

        }

        return mock_values.get(category, 'N/A')

    

    def get_mock_change(self, category: str) -> str:

        """获取模拟变化（实际使用时替换）"""

        mock_changes = {

            'growth': '+0.3%',

            'rate': '不变',

            'employment': '-0.1%',

            'inflation': '-0.2%',

            'consumer': '+0.1%',

            'production': '+1.2',

            'housing': '+5.2%'

        }

        return mock_changes.get(category, 'N/A')

    

    def generate_telegram_message(self, events: List[Dict]) -> str:

        """生成Telegram消息"""

        current_time = datetime.now()

        

        if not events:

            return ""

        

        message = f"""

🚨 <b>美国重大经济指标发布</b>

📅 {current_time.strftime('%Y年%m月%d日')} EST

🕐 {current_time.strftime('%H:%M')}

{'='*40}



"""

        

        for event in events:

            importance_emoji = "🔴" if event['importance'] == 'high' else "🟡" if event['importance'] == 'medium' else "⚪"

            change_percent = event.get('change_percent', 0)

            change_sign = "+" if change_percent >= 0 else ""

            change_emoji = "📈" if change_percent > 0 else "📉" if change_percent < 0 else "➡️"

            name_zh = event.get('name_zh', '')

            link = event.get('link', '')

            

            if link:

                message += f"{importance_emoji} <b><a href=\"{link}\">{event['title']}</a></b>\n"

            else:

                message += f"{importance_emoji} <b>{event['title']}</b>\n"

            if name_zh:

                message += f"   指標: {name_zh}\n"

            message += f"   變化: {change_emoji} {change_sign}{change_percent:.2f}%\n"

            message += f"   來源: {event.get('source', 'Yahoo Finance')}\n\n"

        

        message += f"{'='*40}\n"

        message += f"🤖 由 WorkBuddy 经济指标监控系统自动生成"

        

        return message

    

    def generate_indicator_summary_message(self, indicators: List[Dict]) -> str:

        """生成经济指标汇总消息"""

        if not indicators:

            return ""



        current_time = datetime.now()



        message = f"""

📊 <b>美國經濟指標最新數據</b>

📅 {current_time.strftime('%Y年%m月%d日')} EST

🕐 {current_time.strftime('%H:%M')}

{'='*40}



"""



        # 分組：FRED 優先顯示（高重要性），再接 Yahoo Finance

        fred_indicators = [i for i in indicators if i.get('source') == 'FRED']

        yf_indicators   = [i for i in indicators if i.get('source') != 'FRED']



        if fred_indicators:

            message += "🏛 <b>官方統計數據（FRED）</b>\n\n"

            for indicator in fred_indicators:

                importance_emoji = "🔴" if indicator['importance'] == 'high' else "🟡"

                change_percent = indicator.get('change_percent', 0)

                change_emoji = "📈" if change_percent > 0 else "📉" if change_percent < 0 else "➡️"

                extra = indicator.get('extra', '')

                release_date = indicator.get('release_date', '')

                link = indicator.get('link', '')



                # 英文名稱翻成繁體中文

                try:

                    name_zh = self.translator.translate(indicator['name_en'])

                except Exception:

                    name_zh = indicator['name_en']



                if link:

                    message += f"{importance_emoji} <b><a href=\"{link}\">{indicator['name']}</a></b>\n"

                else:

                    message += f"{importance_emoji} <b>{indicator['name']}</b>\n"

                message += f"   英文名稱: {name_zh}\n"

                message += f"   當前值: {indicator['value']}\n"

                message += f"   月變化: {change_emoji} {indicator['change']}"

                if extra:

                    message += f"{extra}"

                message += "\n"

                if release_date:

                    message += f"   發佈日期: {release_date}\n"

                message += "\n"

            message += f"{'='*40}\n\n"



        if yf_indicators:

            message += "📈 <b>市場即時指標（Yahoo Finance）</b>\n\n"

            yf_sorted = sorted(yf_indicators, key=lambda x: x['importance'], reverse=True)

            for indicator in yf_sorted:

                importance_emoji = "🔴" if indicator['importance'] == 'high' else "🟡"

                change_str = indicator.get('change', '')

                change_emoji = "📈" if change_str.startswith('+') else "📉" if change_str.startswith('-') else "➡️"

                link = indicator.get('link', '')



                # 英文名稱翻成繁體中文

                try:

                    name_zh = self.translator.translate(indicator['name_en'])

                except Exception:

                    name_zh = indicator['name_en']



                if link:

                    message += f"{importance_emoji} <b><a href=\"{link}\">{indicator['name']}</a></b>\n"

                else:

                    message += f"{importance_emoji} <b>{indicator['name']}</b>\n"

                message += f"   英文名稱: {name_zh}\n"

                message += f"   當前值: {indicator['value']}\n"

                message += f"   變化: {change_emoji} {change_str}\n"

                message += "\n"



        message += f"{'='*40}\n"

        message += f"🤖 由 WorkBuddy 經濟指標監控系統自動生成\n"

        message += f"數據來源: FRED (St. Louis Fed) + Yahoo Finance"



        return message

    

    def send_telegram_message(self, message: str) -> bool:

        """發送通知訊息到所有啟用的渠道"""

        results = self.notification_sender.send_to_all(message)

        

        # 檢查是否至少有一個渠道成功

        return any(results.values())

    

    def check_new_events(self):

        """检查新的重要经济事件"""

        events = self.fetch_economic_calendar()

        

        # 过滤重要事件

        important_events = [e for e in events if e['importance'] in ['high', 'medium']]

        

        # 只发送新事件

        new_events = []

        for event in important_events:

            event_key = f"{event['title']}_{event['published'].strftime('%Y%m%d%H%M')}"

            if event_key not in self.sent_events:

                new_events.append(event)

                self.sent_events.add(event_key)

        

        if new_events:

            logger.info(f"Found {len(new_events)} new important economic events")

            message = self.generate_telegram_message(new_events)

            if message:

                self.send_telegram_message(message)

        

        return new_events

    

    def run_monitor(self):

        """执行一次监控检查"""

        logger.info("="*50)

        logger.info("Starting economic monitor check...")

        logger.info("="*50)



        # 1. 检查 Yahoo Finance 异动事件

        new_events = self.check_new_events()

        if new_events:

            logger.info(f"Sent alerts for {len(new_events)} new events")

        else:

            logger.info("No new important economic events")



        # 2. 抓取 Yahoo Finance 即時指標

        yf_indicators = self.fetch_latest_indicators()



        # 3. 抓取 FRED 官方統計數據（CPI、失業率、聯準會利率）

        fred_indicators = self.fetch_fred_indicators()



        # 4. 合併後發送每日彙總

        all_indicators = fred_indicators + yf_indicators

        if all_indicators:

            summary_message = self.generate_indicator_summary_message(all_indicators)

            if summary_message:

                self.send_telegram_message(summary_message)

                logger.info("Sent economic indicator summary")



        logger.info("\nMonitor check completed!")

        logger.info("="*50 + "\n")

    

    def run_realtime(self, check_interval: int = 1800):

        """实时监控（默认每30分钟检查一次）"""

        logger.info(f"Starting real-time economic monitor (checking every {check_interval/60:.0f} minutes)...")

        

        while True:

            try:

                self.run_monitor()

                time.sleep(check_interval)

            

            except KeyboardInterrupt:

                logger.info("\nStopping monitor...")

                break

            except Exception as e:

                logger.error(f"Error in real-time loop: {e}")

                time.sleep(300)  # 出错后等待5分钟再重试





def main():

    """主函数"""

    try:

        print("""

╔═══════════════════════════════════════════════════════╗

║                                                       ║

║        美国经济指标实时监控系统                       ║

║        Real-time Economic Indicator Monitor               ║

║                                                       ║

╚═══════════════════════════════════════════════════════╝

        """)

    except:

        pass

    

    monitor = EconomicMonitor()

    

    # 运行一次测试

    monitor.run_monitor()

    

    # 如果需要实时监控，取消下面的注释

    # monitor.run_realtime(check_interval=1800)  # 每30分钟检查一次





if __name__ == "__main__":

    main()

