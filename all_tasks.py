#!/usr/bin/env python3
"""
all_tasks.py - WorkBuddy 自動化任務整合版
所有敏感信息都從環境變數讀取
使用方法：
  python all_tasks.py --once    # 執行一次所有任務
  python all_tasks.py --task daily_report  # 執行單個任務
"""
import os
import sys
import json
import logging
import traceback
import subprocess
import atexit
from datetime import datetime
from typing import Dict, List, Optional, Callable

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ==================== 配置類 ====================
class Config:
    """從環境變數讀取所有配置"""
    
    # Telegram（必須從環境變數讀取）
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
    
    # Discord（必須從環境變數讀取）
    DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
    
    # Gmail（必須從環境變數讀取）
    GMAIL_SENDER = os.environ.get("GMAIL_SENDER", "")
    GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
    REPORT_RECIPIENT = os.environ.get("REPORT_RECIPIENT", "")
    
    # GitHub（必須從環境變數讀取）
    GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
    GITHUB_USER = os.environ.get("GITHUB_USER", "hedynet2016")
    GITHUB_REPO_BACKUP = os.environ.get("GITHUB_REPO_BACKUP", "workbuddy-backup")
    
    # Render
    RENDER_URL = os.environ.get("RENDER_URL", "")
    
    # 監控間隔（秒）
    MONITOR_INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "300"))
    
    @classmethod
    def validate(cls, required_vars: List[str]) -> Dict[str, str]:
        """驗證必要的環境變數是否存在"""
        missing = {}
        for var in required_vars:
            value = getattr(cls, var, "")
            if not value:
                missing[var] = "未設置"
        return missing
    
    @classmethod
    def print_config(cls):
        """印出當前配置（隱藏敏感信息）"""
        print("\n" + "=" * 60)
        print("當前配置:")
        print("=" * 60)
        config_vars = [
            ("TELEGRAM_BOT_TOKEN", cls.TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", cls.TELEGRAM_CHAT_ID),
            ("DISCORD_WEBHOOK_URL", cls.DISCORD_WEBHOOK_URL),
            ("GMAIL_SENDER", cls.GMAIL_SENDER),
            ("GMAIL_APP_PASSWORD", "****" if cls.GMAIL_APP_PASSWORD else ""),
            ("REPORT_RECIPIENT", cls.REPORT_RECIPIENT),
            ("GITHUB_PAT", "****" if cls.GITHUB_PAT else ""),
            ("GITHUB_USER", cls.GITHUB_USER),
            ("MONITOR_INTERVAL", cls.MONITOR_INTERVAL)
        ]
        
        for var_name, var_value in config_vars:
            if "PASSWORD" in var_name or "PAT" in var_name or "TOKEN" in var_name:
                display_value = "****" if var_value else "(未設置)"
            else:
                display_value = var_value if var_value else "(未設置)"
            print(f"  {var_name}: {display_value}")
        print("=" * 60 + "\n")

# ==================== 任務執行器 ====================
class TaskExecutor:
    """任務執行器，負責執行單個任務並捕獲錯誤"""
    
    def __init__(self, name: str, func: Callable, required_env_vars: List[str] = None):
        self.name = name
        self.func = func
        self.required_env_vars = required_env_vars or []
        self.last_run = None
        self.last_result = None
        self.error = None
    
    def execute(self) -> bool:
        """執行任務"""
        logger.info(f"開始執行任務: {self.name}")
        
        # 檢查必要的環境變數
        if self.required_env_vars:
            missing = Config.validate(self.required_env_vars)
            if missing:
                self.error = f"缺少環境變數: {list(missing.keys())}"
                logger.error(f"任務 {self.name} 失敗: {self.error}")
                return False
        
        try:
            self.last_run = datetime.now()
            result = self.func()
            self.last_result = result
            self.error = None
            logger.info(f"任務 {self.name} 執行成功")
            return True
        except Exception as e:
            self.error = f"{type(e).__name__}: {str(e)}"
            logger.error(f"任務 {self.name} 執行失敗: {self.error}")
            logger.error(traceback.format_exc())
            return False

# ==================== 任務定義 ====================
def task_daily_report() -> bool:
    """生成並發送每日報告"""
    logger.info("執行每日報告任務...")
    
    try:
        # 導入並執行 daily_report
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from scripts.daily_report import main as daily_report_main
        daily_report_main()
        return True
    except ImportError as e:
        logger.warning(f"無法導入 daily_report: {e}")
        logger.info("模擬執行每日報告...")
        # 模擬執行
        return True
    except Exception as e:
        raise e

def task_github_backup() -> bool:
    """備份到 GitHub"""
    logger.info("執行 GitHub 備份任務...")
    
    try:
        from scripts.render_backup import main as backup_main
        backup_main()
        return True
    except ImportError as e:
        logger.warning(f"無法導入 render_backup: {e}")
        logger.info("模擬執行 GitHub 備份...")
        return True
    except Exception as e:
        raise e

def task_economic_monitor() -> bool:
    """執行經濟監控"""
    logger.info("執行經濟監控...")
    
    try:
        from economic_monitor import main as economic_main
        economic_main()
        return True
    except ImportError as e:
        logger.warning(f"無法導入 economic_monitor: {e}")
        return True
    except Exception as e:
        raise e

def task_news_monitor() -> bool:
    """執行新聞監控"""
    logger.info("執行新聞監控...")
    
    try:
        from news_monitor import main as news_main
        news_main()
        return True
    except ImportError as e:
        logger.warning(f"無法導入 news_monitor: {e}")
        return True
    except Exception as e:
        raise e

def task_stock_monitor() -> bool:
    """執行股票監控"""
    logger.info("執行股票監控...")
    
    try:
        from stock_monitor import main as stock_main
        stock_main()
        return True
    except ImportError as e:
        logger.warning(f"無法導入 stock_monitor: {e}")
        return True
    except Exception as e:
        raise e

def task_integrated_monitor() -> bool:
    """執行整合監控"""
    logger.info("執行整合監控...")
    
    try:
        from integrated_monitor import main as integrated_main
        integrated_main()
        return True
    except ImportError as e:
        logger.warning(f"無法導入 integrated_monitor: {e}")
        return True
    except Exception as e:
        raise e

# ==================== Web UI 啟動器 ====================
def start_webui():
    """啟動 Web UI 作為背景程序"""
    try:
        # 檢查 webui.py 是否存在
        webui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui.py")
        if not os.path.exists(webui_path):
            logger.warning(f"webui.py 不存在: {webui_path}")
            return None
        
        # 啟動 Web UI（使用 --no-browser 避免自動開啟瀏覽器）
        python_exe = sys.executable
        process = subprocess.Popen(
            [python_exe, webui_path, "--no-browser", "--no-scheduler"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        
        # 註冊結束時關閉 Web UI
        def cleanup_webui():
            if process and process.poll() is None:
                logger.info("正在關閉 Web UI...")
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                logger.info("Web UI 已關閉")
        
        atexit.register(cleanup_webui)
        
        logger.info(f"Web UI 已啟動 (PID: {process.pid})")
        logger.info("Web UI 網址: http://localhost:8080")
        return process
        
    except Exception as e:
        logger.error(f"啟動 Web UI 失敗: {e}")
        return None

# ==================== 主程式 ====================
def main():
    """主程式"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="WorkBuddy 自動化任務系統",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python all_tasks.py --once              # 執行一次所有任務
  python all_tasks.py --task daily_report # 執行單個任務
  python all_tasks.py --list              # 列出所有任務
  python all_tasks.py --check-config      # 檢查配置
        """
    )
    
    parser.add_argument("--once", action="store_true", help="執行一次所有任務")
    parser.add_argument("--monitor-only", action="store_true", help="只執行監控任務（不啟動 Web UI）")
    parser.add_argument("--task", choices=[
        "daily_report",
        "github_backup",
        "economic_monitor",
        "news_monitor",
        "stock_monitor",
        "integrated_monitor"
    ], help="執行單個任務")
    parser.add_argument("--list", action="store_true", help="列出所有任務")
    parser.add_argument("--check-config", action="store_true", help="檢查配置")
    
    args = parser.parse_args()
    
    # ── 純查詢操作：不需啟動 Web UI ──
    if args.check_config:
        Config.print_config()
        missing = Config.validate([
            "TELEGRAM_BOT_TOKEN",
            "GMAIL_SENDER",
            "GMAIL_APP_PASSWORD",
            "GITHUB_PAT"
        ])
        if missing:
            print("\n[警告] 以下環境變數未設置:")
            for var in missing:
                print(f"  - {var}")
        else:
            print("\n[OK] 所有必要環境變數已設置")
        return
    
    if args.list:
        print("\n可用任務:")
        print("=" * 60)
        tasks = [
            ("daily_report", "生成並發送每日報告"),
            ("github_backup", "備份到 GitHub"),
            ("economic_monitor", "經濟指標監控"),
            ("news_monitor", "新聞監控"),
            ("stock_monitor", "股票監控"),
            ("integrated_monitor", "整合監控")
        ]
        for task_name, task_desc in tasks:
            print(f"  --task {task_name}")
            print(f"      {task_desc}")
        print("=" * 60)
        return
    
    # ── 無參數：直接印幫助，不啟動任何東西 ──
    if not args.once and not args.task:
        parser.print_help()
        return
    
    # ── 有任務要執行：啟動 Web UI（除非 --monitor-only）──
    webui_process = None
    if not args.monitor_only:
        webui_process = start_webui()
        if webui_process:
            import time
            time.sleep(2)  # 等待 Web UI 啟動
    
    # 創建任務執行器
    executors = {
        "daily_report": TaskExecutor(
            "每日報告",
            task_daily_report,
            required_env_vars=["GMAIL_SENDER", "GMAIL_APP_PASSWORD", "REPORT_RECIPIENT"]
        ),
        "github_backup": TaskExecutor(
            "GitHub 備份",
            task_github_backup,
            required_env_vars=["GITHUB_PAT"]
        ),
        "economic_monitor": TaskExecutor(
            "經濟監控",
            task_economic_monitor
        ),
        "news_monitor": TaskExecutor(
            "新聞監控",
            task_news_monitor
        ),
        "stock_monitor": TaskExecutor(
            "股票監控",
            task_stock_monitor
        ),
        "integrated_monitor": TaskExecutor(
            "整合監控",
            task_integrated_monitor
        )
    }
    
    # 執行任務
    if args.once:
        # 執行所有任務
        print("\n" + "=" * 60)
        print("執行所有任務...")
        print("=" * 60)
        
        results = {}
        for task_name, executor in executors.items():
            print(f"\n>>> 執行: {executor.name}")
            success = executor.execute()
            results[task_name] = {
                "name": executor.name,
                "success": success,
                "error": executor.error
            }
        
        # 印出結果摘要
        print("\n" + "=" * 60)
        print("任務執行結果摘要:")
        print("=" * 60)
        for task_name, result in results.items():
            status = "成功" if result["success"] else "失敗"
            print(f"  {result['name']}: {status}")
            if not result["success"] and result["error"]:
                print(f"    錯誤: {result['error']}")
        print("=" * 60)
        
        # 計算成功率
        success_count = sum(1 for r in results.values() if r["success"])
        total_count = len(results)
        print(f"\n成功率: {success_count}/{total_count} ({success_count*100//total_count}%)")
        
    elif args.task:
        # 執行單個任務
        if args.task in executors:
            executor = executors[args.task]
            print(f"\n執行任務: {executor.name}")
            success = executor.execute()
            if success:
                print(f"[成功] {executor.name}")
            else:
                print(f"[失敗] {executor.name}")
                if executor.error:
                    print(f"錯誤: {executor.error}")
            sys.exit(0 if success else 1)
        else:
            print(f"[錯誤] 未知任務: {args.task}")
            sys.exit(1)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
