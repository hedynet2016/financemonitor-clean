#!/usr/bin/env python3

"""

综合监控系统 - 同时运行股票、新闻和经济指标监控

Integrated Monitor - Stock, News and Economic Monitoring System

"""



import threading

import time

import logging

from datetime import datetime

import pytz

import signal

import sys



# 配置日誌

logging.basicConfig(

    level=logging.INFO,

    format='%(asctime)s - %(levelname)s - %(message)s',

    handlers=[

        logging.FileHandler('integrated_monitor.log', encoding='utf-8'),

        logging.StreamHandler()

    ]

)

logger = logging.getLogger(__name__)



# ── Patch yfinance cache directory ──

# yfinance stores cookies in %LOCALAPPDATA%\py-yfinance\ which is blocked by sandbox.

import os as _os

import tempfile as _tempfile

_YF_SAFE_CACHE = _os.path.join(_tempfile.gettempdir(), 'yfinance_sandbox')

_os.makedirs(_YF_SAFE_CACHE, exist_ok=True)

_os.environ['YF_CACHE_DIR'] = _YF_SAFE_CACHE



# Monkey-patch appdirs BEFORE yfinance is first imported by sub-modules

try:

    import appdirs as _ad

    _ad.user_cache_dir = lambda: _YF_SAFE_CACHE

except ImportError:

    pass



from stock_monitor import StockMonitor

from news_monitor import NewsMonitor

from economic_monitor import EconomicMonitor

from product_monitor import ProductMonitor



# Directly override yfinance cache class attributes (belt-and-suspenders)

# _CookieDBManager and _TzDBManager lazily initialise SQLite on first access,

# so changing _cache_dir before first Ticker call is sufficient.

import yfinance.cache as _yf_cache

_yf_cache._CookieDBManager._cache_dir = _YF_SAFE_CACHE

_yf_cache._TzDBManager._cache_dir = _YF_SAFE_CACHE

_yf_cache._ISINDBManager._cache_dir = _YF_SAFE_CACHE



# Suppress yfinance 404/ERROR logs (delisted/renamed tickers return 404, expected)

import logging as _logging

_logging.getLogger('yfinance').setLevel(_logging.CRITICAL)





class IntegratedMonitor:

    """综合监控类"""

    

    def __init__(self, config_file: str = 'config.json'):

        """初始化监控系统"""

        self.config_file = config_file

        self.config = self._load_config(config_file)

        self.stock_monitor = StockMonitor(config_file)

        self.news_monitor = NewsMonitor(config_file)

        self.economic_monitor = EconomicMonitor(config_file)

        self.running = False

        self.stock_thread = None

        self.news_thread = None

        self.economic_thread = None

        self.events_thread = None

        

        # 初始化商品追蹤監控（傳入 config 字典而非檔案路徑）

        self.product_monitor = ProductMonitor(self.config)

        self.product_thread = None

        

        # 设置信号处理

        signal.signal(signal.SIGINT, self.signal_handler)

        signal.signal(signal.SIGTERM, self.signal_handler)

        

        logger.info("Integrated Monitor initialized")

    

    def _load_config(self, config_file: str) -> dict:

        """讀取設定檔"""

        import json

        with open(config_file, 'r', encoding='utf-8') as f:

            return json.load(f)

    

    def signal_handler(self, signum, frame):

        """信号处理器"""

        logger.info(f"Received signal {signum}, shutting down...")

        self.stop()

        sys.exit(0)

    

    def stock_monitor_worker(self):

        """股票监控工作线程 - 每半小時執行一次"""

        logger.info("Starting stock monitor worker (half-hourly)...")

        last_run_block = -1  # 每半小時一個 block: hour*2 + minute//30



        while self.running:

            try:

                import pytz

                # 獲取當前半小時區塊 (使用台灣時區作為基準)

                taipei_tz = pytz.timezone('Asia/Taipei')

                now = datetime.now(TAIPEI_TZ)

                current_block = now.hour * 2 + (now.minute // 30)



                # 檢查是否到達新的半小時區塊

                if current_block != last_run_block:

                    # 檢查交易時間

                    trading_status = self.stock_monitor.is_trading_time('all')

                    us_trading = trading_status.get('us', False)

                    tw_trading = trading_status.get('tw', False)



                    if us_trading or tw_trading:

                        # 至少有一個市場在交易中,執行監控

                        self.stock_monitor.run_monitor()

                    else:

                        logger.debug("No market is in trading hours, skipping stock monitor check")



                    last_run_block = current_block



                # 每分鐘檢查一次是否到新的半小時區塊

                time.sleep(60)



            except Exception as e:

                logger.error(f"Error in stock monitor worker: {e}")

                time.sleep(60)

    

    def news_monitor_worker(self):

        """新闻监控工作线程 - 每天目标时间执行一次（含重启补发）"""

        logger.info("Starting news monitor worker...")

        target_hour = self.news_monitor.news_config.get('daily_hour', 8)

        last_run_date = None
        
        logger.info(f"News monitor: target={target_hour}:00 Taipei, last_run_date=None")
        heartbeat_count = 0
        
        
        # 使用明確時區（台北時間）
        taipei_tz = pytz.timezone('Asia/Taipei')
        



        while self.running:

            try:

                now = datetime.now(TAIPEI_TZ)

                today = now.date()



                should_run = False



                # 正常触发：target_hour 点前 5 分钟内

                if now.hour == target_hour and now.minute < 5 and last_run_date != today:

                    should_run = True



                # 补发触发：程序重启后，当天未发、且时间仍在 target_hour ~ target_hour+2 之间

                elif (

                    last_run_date != today

                    and target_hour <= now.hour < target_hour + 2

                ):

                    logger.info(f"News monitor catch-up: missed {target_hour}:00, running now at {now.strftime('%H:%M')}")

                    should_run = True



                if should_run:

                    logger.info(f"Triggering news monitor (blocks 1-10) at {now.strftime('%H:%M')}...")

                    try:
                        self.news_monitor.run_news_only()
                        logger.info(f"News monitor run COMPLETED at {datetime.now(TAIPEI_TZ).strftime('%H:%M:%S')}")
                    except Exception as e:
                        logger.error(f"News monitor run FAILED: {e}", exc_info=True)

                    last_run_date = today



                    # 等待1小时，避免窗口内重复执行

                    for _ in range(60):

                        if not self.running:

                            break

                        time.sleep(60)

                else:

                    heartbeat_count += 1
                    if heartbeat_count % 30 == 0:
                        logger.info(f"[HEARTBEAT] news_monitor_worker alive, "
                                    f"now={now.strftime('%Y-%m-%d %H:%M:%S %Z')}, "
                                    f"target_hour={target_hour}, last_run={last_run_date}")
                    time.sleep(60)



            except Exception as e:

                logger.error(f"Error in news monitor worker: {e}")

                time.sleep(60)

    

    def events_monitor_worker(self):

        """ICT/AI 活動監控工作線程 - 每天 14:00 ET 執行一次(含重啟補發)"""

        logger.info("Starting events monitor worker...")

        # 從 config.json 讀取活動發布時間, 預設 14 (下午 2 點)

        target_hour = self.news_monitor.news_config.get('events_hour', 14)

        last_run_date = None
        
        # 使用明確時區（台北時間）
        taipei_tz = pytz.timezone('Asia/Taipei')
        



        while self.running:

            try:

                now = datetime.now(TAIPEI_TZ)

                today = now.date()



                should_run = False



                # 正常觸發: target_hour 點前 5 分鐘

                if now.hour == target_hour and now.minute < 5 and last_run_date != today:

                    should_run = True



                # 補發觸發: 程式重啟後, 當天未發, 且時間仍在 target_hour ~ target_hour+2 之間

                elif (

                    last_run_date != today

                    and target_hour <= now.hour < target_hour + 2

                ):

                    logger.info(f"Events monitor catch-up: missed {target_hour}:00, running now at {now.strftime('%H:%M')}")

                    should_run = True



                if should_run:

                    logger.info(f"Triggering events monitor (block 11) at {now.strftime('%H:%M')}...")

                    self.news_monitor.run_events_only()

                    last_run_date = today



                    # 等待 1 小時, 避免視窗內重複執行

                    for _ in range(60):

                        if not self.running:

                            break

                        time.sleep(60)

                else:

                    time.sleep(60)



            except Exception as e:

                logger.error(f"Error in events monitor worker: {e}")

                time.sleep(60)



#     def economic_monitor_worker(self):

#         """经济指标监控工作线程 - 每天早上8点执行一次（含重启补发）"""

#         logger.info("Starting economic monitor worker...")

#         target_hour = self.economic_monitor.economic_config.get('daily_hour', 8)

#         last_run_date = None



#         while self.running:

#             try:

#                 now = datetime.now(TAIPEI_TZ)

#                 today = now.date()



#                 should_run = False



#                 # 正常触发

#                 if now.hour == target_hour and now.minute < 5 and last_run_date != today:

#                     should_run = True



#                 # 补发触发：重启后当天未发、时间仍在 target_hour ~ target_hour+2

#                 elif (

#                     last_run_date != today

#                     and target_hour <= now.hour < target_hour + 2

#                 ):

#                     logger.info(f"Economic monitor catch-up: missed {target_hour}:00, running now at {now.strftime('%H:%M')}")

#                     should_run = True



#                 if should_run:

#                     logger.info(f"Triggering economic monitor at {now.strftime('%H:%M')}...")

#                     self.economic_monitor.run_monitor()

#                     last_run_date = today



#                     # 等待1小时，避免窗口内重复执行

#                     for _ in range(60):

#                         if not self.running:

#                             break

#                         time.sleep(60)

#                 else:

#                     time.sleep(60)



#             except Exception as e:

#                 logger.error(f"Error in economic monitor worker: {e}")

#                 time.sleep(60)

    

    def product_monitor_worker(self):

        """商品追蹤工作线程 - 每天 16:00 執行一次（含重啟補發）"""

        logger.info("Starting Product monitor worker...")

        target_hour = 16  # 16:00 台北時間

        last_run_date = None
        
        # 使用明確時區（台北時間）
        taipei_tz = pytz.timezone('Asia/Taipei')
        

        

        while self.running:

            try:

                now = datetime.now(TAIPEI_TZ)

                today = now.date()

                

                should_run = False

                

                # 正常觸發：16:00 点前 5 分鐘内

                if now.hour == target_hour and now.minute < 5 and last_run_date != today:

                    should_run = True

                

                # 补发觸發：程序重啟后，当天未发、且時間仍在 16:00 ~ 18:00 之間

                elif (

                    last_run_date != today

                    and target_hour <= now.hour < target_hour + 2

                ):

                    logger.info(f"Product monitor catch-up: missed {target_hour}:00, running now at {now.strftime('%H:%M')}")

                    should_run = True

                

                if should_run:

                    logger.info(f"Triggering Product monitor at {now.strftime('%H:%M')}...")

                    try:

                        self.product_monitor.run()

                    except Exception as e:

                        logger.error(f"Error in Product monitor: {e}")

                    last_run_date = today

                    

                    # 等待1小时，避免窗口内重複執行

                    for _ in range(60):

                        if not self.running:

                            break

                        time.sleep(60)

                else:

                    time.sleep(60)

                

            except Exception as e:

                logger.error(f"Error in Product monitor worker: {e}")

                time.sleep(60)

    

    def start(self):

        """启动监控系统"""

        if self.running:

            logger.warning("Monitor is already running")

            return

        

        self.running = True

        logger.info("="*60)

        logger.info("Starting Integrated Monitoring System...")

        logger.info("="*60)

        

        # 启动股票监控线程

        self.stock_thread = threading.Thread(target=self.stock_monitor_worker, name="StockMonitor")

        self.stock_thread.daemon = True

        self.stock_thread.start()

        logger.info("Stock monitor thread started")

        

        # 启动新闻监控线程 (區塊 ①~⑩, 每日 08:00 ET)

        self.news_thread = threading.Thread(target=self.news_monitor_worker, name="NewsMonitor")

        self.news_thread.daemon = True

        self.news_thread.start()

        logger.info("News monitor thread started (blocks 1-10, 08:00 ET)")



        # 启动活動监控线程 (區塊 ⑪ ICT/AI 活動, 每日 14:00 ET)

        self.events_thread = threading.Thread(target=self.events_monitor_worker, name="EventsMonitor")

        self.events_thread.daemon = True

        self.events_thread.start()

        logger.info("Events monitor thread started (block 11, 14:00 ET)")

        

        # 啟動商品追蹤線程 (每日 16:00 台北時間)

        self.product_thread = threading.Thread(target=self.product_monitor_worker, name="ProductMonitor")

        self.product_thread.daemon = True

        self.product_thread.start()

        logger.info("Product monitor thread started (16:00 daily)")

        

        # 经济指标监控已停用 (config.json: economic.enabled = false)

        # self.economic_thread = threading.Thread(target=self.economic_monitor_worker, name="EconomicMonitor")

        # self.economic_thread.daemon = True

        # self.economic_thread.start()

        logger.info("Economic monitor thread DISABLED")

        

        logger.info("="*60)

        logger.info("All monitors started successfully!")

        logger.info("Press Ctrl+C to stop")

        logger.info("="*60)

    

    def stop(self):

        """停止监控系统"""

        if not self.running:

            logger.warning("Monitor is not running")

            return

        

        logger.info("Stopping Integrated Monitoring System...")

        self.running = False

        

        # 等待线程结束

        if self.stock_thread and self.stock_thread.is_alive():

            self.stock_thread.join(timeout=5)

        

        if self.news_thread and self.news_thread.is_alive():

            self.news_thread.join(timeout=5)

        

        if self.events_thread and self.events_thread.is_alive():

            self.events_thread.join(timeout=5)

        

        if self.product_thread and self.product_thread.is_alive():

            self.product_thread.join(timeout=5)

        

        logger.info("Integrated Monitoring System stopped")

    

    def run_once(self):

        """執行一次所有監控檢查(新聞區塊 ①~⑩ + 活動區塊 ⑪)"""

        logger.info("="*60)

        logger.info("Running one-time check...")

        logger.info("="*60)



        logger.info("\n" + "="*60)

        logger.info("Running News Monitor Check (blocks 1-10)...")

        logger.info("="*60)

        self.news_monitor.run_news_only()



        logger.info("\n" + "="*60)

        logger.info("Running Events Monitor Check (block 11)...")

        logger.info("="*60)

        self.news_monitor.run_events_only()



        logger.info("\n" + "="*60)

        logger.info("One-time check completed!")

        logger.info("="*60)



    def run_news_once(self):

        """只執行新聞區塊 (blocks 1-10)"""

        logger.info("="*60)

        logger.info("Running News Only (blocks 1-10)...")

        logger.info("="*60)

        self.news_monitor.run_news_only()

        logger.info("News only check completed!")



    def run_events_once(self):

        """只執行活動區塊 (block 11)"""

        logger.info("="*60)

        logger.info("Running Events Only (block 11)...")

        logger.info("="*60)

        self.news_monitor.run_events_only()

        logger.info("Events only check completed!")



    def run_stocks_once(self):

        """只執行股市監控"""

        logger.info("="*60)

        logger.info("Running Stock Monitor Only...")

        logger.info("="*60)

        self.stock_monitor.run_monitor()

        logger.info("Stock monitor check completed!")

def main():

    """主函数"""

    import argparse

    parser = argparse.ArgumentParser(description='Integrated Monitor')

    parser.add_argument('--once', action='store_true', help='Run full check once and exit')

    parser.add_argument('--news-only', action='store_true', help='Run news blocks (1-10) only')

    parser.add_argument('--events-only', action='store_true', help='Run events block (11) only')

    parser.add_argument('--stocks-only', action='store_true', help='Run stock monitor only')

    args = parser.parse_args()



    monitor = IntegratedMonitor()



    if args.news_only:

        monitor.run_news_once()

    elif args.events_only:

        monitor.run_events_once()

    elif args.stocks_only:

        monitor.run_stocks_once()

    elif args.once:

        monitor.run_once()

    else:

        monitor.start()

        try:

            while True:

                time.sleep(1)

        except KeyboardInterrupt:

            monitor.stop()



if __name__ == "__main__":

    main()

