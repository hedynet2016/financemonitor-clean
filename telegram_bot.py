#!/usr/bin/env python3
"""
Telegram Bot 命令介面 - FinanceMonitor
接收 Telegram 命令並觸發對應動作，結果回傳到 Telegram 和 Discord
授權機制：只有 config.json 中設定的 chat_id 才能下命令
"""
import asyncio
import atexit
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# 確保專案根目錄在 Python path
PROJECT_ROOT = Path(__file__).parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from telegram import Update
from telegram import error as tg_error
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ============================================================
# 日誌設定
# ============================================================
log_dir = PROJECT_ROOT / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(log_dir / "telegram_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ============================================================
# 讀取設定
# ============================================================
def load_config():
    config_path = PROJECT_ROOT / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_bot_token(config):
    return config.get("telegram", {}).get("bot_token", "")

def get_authorized_chat_ids(config):
    """取得授權的 chat_id 列表（支援單一或列表）"""
    chat_id = config.get("telegram", {}).get("chat_id", "")
    if isinstance(chat_id, list):
        return [str(c) for c in chat_id]
    return [str(chat_id)]

def get_discord_webhook(config):
    """取得預設 Discord webhook URL"""
    return config.get("discord", {}).get("webhook_url", "")

# ============================================================
# 狀態追蹤
# ============================================================
STATUS_FILE = PROJECT_ROOT / "logs" / "bot_status.json"

def load_status():
    if STATUS_FILE.exists():
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "last_run": None,
        "last_run_status": None,
        "last_run_duration": None,
        "schedule_enabled": True,
        "bot_start_time": datetime.now().isoformat(),
    }

def save_status(status):
    STATUS_FILE.parent.mkdir(exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)

# ============================================================
# 執行 integrated_monitor.py
# ============================================================
def run_monitor(mode: str, progress_callback=None) -> dict:
    """
    執行 integrated_monitor.py
    mode: 'once' | 'news' | 'events' | 'stocks'
    回傳: {'success': bool, 'output': str, 'duration': float}
    """
    cmd_map = {
        "once": ["integrated_monitor.py", "--once"],
        "news": ["integrated_monitor.py", "--news-only"],
        "events": ["integrated_monitor.py", "--events-only"],
        "stocks": ["integrated_monitor.py", "--stocks-only"],
        "product": ["product_monitor.py", "--once"],
    }
    cmd = cmd_map.get(mode, cmd_map["once"])
    script_path = PROJECT_ROOT / cmd[0]
    full_cmd = [sys.executable, str(script_path)] + cmd[1:]

    logger.info(f"執行命令: {' '.join(map(str, full_cmd))}")
    if progress_callback:
        progress_callback(f"正在執行 {mode}...")

    start = time.time()
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 分鐘 timeout
            cwd=PROJECT_ROOT,
        )
        duration = round(time.time() - start, 1)

        # 擷取 ERROR 行
        errors = []
        for line in (result.stdout + result.stderr).split("\n"):
            if "ERROR" in line or "Exception" in line:
                errors.append(line.strip())

        success = result.returncode == 0 and len(errors) == 0
        output = result.stdout[-3000:] if result.stdout else result.stderr[-3000:]

        return {
            "success": success,
            "returncode": result.returncode,
            "output": output,
            "errors": errors[:10],
            "duration": duration,
        }
    except subprocess.TimeoutExpired:
        duration = round(time.time() - start, 1)
        return {
            "success": False,
            "returncode": -1,
            "output": "執行逾時（超過 10 分鐘）",
            "errors": ["TimeoutExpired"],
            "duration": duration,
        }
    except Exception as e:
        duration = round(time.time() - start, 1)
        return {
            "success": False,
            "returncode": -1,
            "output": str(e),
            "errors": [str(e)],
            "duration": duration,
        }

# ============================================================
# 命令處理函式
# ============================================================
AUTHORIZED_CHAT_IDS = []

async def auth_check(update: Update) -> bool:
    """檢查使用者是否授權"""
    chat_id = str(update.effective_chat.id)
    if chat_id not in AUTHORIZED_CHAT_IDS:
        await update.message.reply_text("抱歉，您沒有權限使用此 Bot。")
        logger.warning(f"未授權存取來自 chat_id={chat_id}")
        return False
    return True

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    help_text = (
        "FinanceMonitor Bot 指令列表\n"
        "============================\n"
        "/run      - 立即執行完整推播（新聞 + 活動）\n"
        "/news     - 只執行新聞區塊（區塊 1-10）\n"
        "/events   - 只執行活動區塊（區塊 11）\n"
        "/stocks   - 只執行股市監控\n"
        "/product  - 只執行商品追蹤（雅虎拍賣三賣場）\n"
        "/status   - 顯示系統狀態\n"
        "/help     - 顯示此說明\n"
    )
    await update.message.reply_text(help_text)

async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    await update.message.reply_text("收到 /run 命令，正在執行完整推播...")
    # 在背景執行，避免 blocking
    threading.Thread(
        target=_run_and_reply,
        args=(update, context, "once", "完整推播"),
        daemon=True,
    ).start()

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    await update.message.reply_text("收到 /news 命令，正在執行新聞區塊...")
    threading.Thread(
        target=_run_and_reply,
        args=(update, context, "news", "新聞區塊"),
        daemon=True,
    ).start()

async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    await update.message.reply_text("收到 /events 命令，正在執行活動區塊...")
    threading.Thread(
        target=_run_and_reply,
        args=(update, context, "events", "活動區塊"),
        daemon=True,
    ).start()

async def cmd_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    await update.message.reply_text("收到 /stocks 命令，正在執行股市監控...")
    threading.Thread(
        target=_run_and_reply,
        args=(update, context, "stocks", "股市監控"),
        daemon=True,
    ).start()

async def cmd_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    await update.message.reply_text("收到 /product 命令，正在執行商品追蹤（雅虎拍賣三賣場，約需 5~8 分鐘）...")
    threading.Thread(
        target=_run_and_reply,
        args=(update, context, "product", "商品追蹤"),
        daemon=True,
    ).start()

def _run_and_reply(update, context, mode: str, label: str):
    """背景執行並回覆結果"""
    loop = context.application.update_queue._loop
    import asyncio

    def progress(msg):
        asyncio.run_coroutine_threadsafe(
            update.message.reply_text(msg), loop
        )

    result = run_monitor(mode, progress_callback=progress)
    status = load_status()
    status["last_run"] = datetime.now().isoformat()
    status["last_run_status"] = "success" if result["success"] else "error"
    status["last_run_duration"] = result["duration"]
    save_status(status)

    if result["success"]:
        msg = (
            f"✅ {label} 執行成功！\n"
            f"耗時：{result['duration']} 秒\n"
            f"推播已發送到 Telegram 和 Discord。"
        )
    else:
        error_summary = "\n".join(result["errors"][:5])
        msg = (
            f"❌ {label} 執行失敗！\n"
            f"耗時：{result['duration']} 秒\n"
            f"錯誤摘要：\n{error_summary}\n"
            f"\n詳細輸出：\n{result['output'][-500:]}"
        )
    asyncio.run_coroutine_threadsafe(
        update.message.reply_text(msg), loop
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    status = load_status()
    last_run = status.get("last_run", "尚未執行")
    last_status = status.get("last_run_status", "N/A")
    last_duration = status.get("last_run_duration", "N/A")
    schedule_enabled = status.get("schedule_enabled", True)
    bot_start = status.get("bot_start_time", "N/A")

    msg = (
        "系統狀態\n"
        "========\n"
        f"Bot 啟動時間：{bot_start}\n"
        f"排程啟用：{'✅ 是' if schedule_enabled else '❌ 否'}\n"
        f"上次執行時間：{last_run}\n"
        f"上次執行狀態：{'✅ 成功' if last_status == 'success' else '❌ 失敗'}\n"
        f"上次執行耗時：{last_duration} 秒\n"
    )
    await update.message.reply_text(msg)

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("無效命令，請輸入 /help 查看可用指令。")

# ============================================================
# 排程推播（背景執行緒）
# ============================================================
_scheduler_monitor = None
_webui_process = None

def start_scheduler():
    """在背景啟動 IntegratedMonitor 排程（每日/每半小時/每30分鐘推播）"""
    global _scheduler_monitor
    from integrated_monitor import IntegratedMonitor
    _scheduler_monitor = IntegratedMonitor()
    _scheduler_monitor.start()
    logger.info("排程推播已啟動（背景執行緒）")

def start_webui():
    """啟動 webui.py 作為背景子程序（如果尚未執行）"""
    global _webui_process
    # 先檢查 8080 是否已有人在監聽，若有必要則終止
    kill_webui_port_8080()
    # 等待埠口真正釋放（Windows 上 taskkill 是異步的）
    for _ in range(20):  # 最多等 2 秒
        time.sleep(0.1)
        try:
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True,
                timeout=3, encoding="utf-8", errors="replace"
            )
            if not any(":8080" in line and "LISTENING" in line for line in result.stdout.splitlines()):
                break
        except Exception:
            time.sleep(0.2)
    else:
        logger.warning("8080 埠口仍在佔用中，強制啟動 webui.py")

    try:
        webui_path = PROJECT_ROOT / "webui.py"
        python_exe = sys.executable
        _webui_process = subprocess.Popen(
            [python_exe, str(webui_path), "--no-browser"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(PROJECT_ROOT),
        )
        logger.info(f"webui.py 已啟動（PID {_webui_process.pid}，埠口 8080）")
    except Exception as e:
        logger.warning(f"無法啟動 webui.py: {e}")
        _webui_process = None


def kill_webui_port_8080():
    """終止佔用 8080 埠口的 webui.py 程序（由 Ctrl+C 或其他退出觸發）"""
    global _webui_process
    # 優先嘗試終止我們自己啟動的子程序（優雅關閉）
    if _webui_process is not None:
        try:
            if _webui_process.poll() is None:  # 仍在執行
                _webui_process.terminate()
                try:
                    _webui_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    _webui_process.kill()
                    _webui_process.wait(timeout=2)
                logger.info(f"webui.py 子程序（PID {_webui_process.pid}）已終止")
        except Exception:
            pass
        _webui_process = None

    # 備用：透過埠口強制終止（可能由其他程序佔用）
    try:
        import platform
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True,
                timeout=8, encoding="utf-8", errors="replace"
            )
            for line in result.stdout.splitlines():
                if ":8080" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    pid = parts[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=8,
                                   encoding="utf-8", errors="replace")
                    logger.info(f"已終止 webui.py 程序（PID {pid}，埠口 8080）")
                    break
        else:
            result = subprocess.run(
                ["lsof", "-ti", ":8080"], capture_output=True, text=True,
                timeout=8, encoding="utf-8", errors="replace"
            )
            pids = result.stdout.strip().split()
            for pid in pids:
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=8)
            if pids:
                logger.info(f"已終止 webui.py 程序（PID {', '.join(pids)}，埠口 8080）")
    except Exception as e:
        logger.debug(f"終止 8080 埠口程序時出錯（可忽略）: {e}")


def stop_scheduler():
    """停止排程推播"""
    global _scheduler_monitor
    if _scheduler_monitor:
        _scheduler_monitor.stop()
        _scheduler_monitor = None
        logger.info("排程推播已停止")


# ============================================================
# 主程式
# ============================================================
def main():
    global AUTHORIZED_CHAT_IDS

    # 註冊退出時自動清理 8080 埠口的 webui.py
    atexit.register(kill_webui_port_8080)

    # 註冊 Ctrl+C / SIGTERM 信號處理（確保跨平台清理）
    def _signal_handler(signum, frame):
        logger.info(f"收到信號 {signum}，正在關閉所有服務...")
        stop_scheduler()
        kill_webui_port_8080()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    # Windows 上可能沒有 SIGBREAK，忽略 AttributeError
    try:
        signal.signal(signal.SIGBREAK, _signal_handler)
    except AttributeError:
        pass

    import argparse
    parser = argparse.ArgumentParser(description="FinanceMonitor 統一起動器（Bot + 排程推播）")
    parser.add_argument("--once", action="store_true",
                        help="執行一次完整推播後退出（測試用，等同 integrated_monitor.py --once）")
    parser.add_argument("--no-scheduler", action="store_true",
                        help="只啟動 Telegram Bot，不啟動自動排程推播")
    parser.add_argument("--no-bot", action="store_true",
                        help="只啟動排程推播，不啟動 Telegram Bot（等同舊 integrated_monitor.py）")
    parser.add_argument("--no-webui", action="store_true",
                        help="不自動啟動 Web UI（http://localhost:8080）")
    args = parser.parse_args()

    # ── --once 模式：執行一次後退出 ──
    if args.once:
        print("執行一次性完整推播...")
        result = run_monitor("once")
        if result["success"]:
            print(f"推播完成！（耗時 {result['duration']}s）")
        else:
            print(f"推播失敗！（耗時 {result['duration']}s）")
            for err in result["errors"]:
                print(f"  錯誤: {err}")
        return

    config = load_config()

    # ── 啟動 Web UI（預設啟用） ──
    if not args.no_webui:
        start_webui()
    else:
        logger.info("Web UI 已停用（--no-webui）")

    # ── 啟動排程推播（預設啟用） ──
    if not args.no_scheduler:
        start_scheduler()
    else:
        logger.info("排程推播已停用（--no-scheduler）")

    # ── 啟動 Telegram Bot（預設啟用） ──
    if args.no_bot:
        logger.info("Telegram Bot 已停用（--no-bot），排程推播繼續運行...")
        print("排程推播已啟動，按 Ctrl+C 停止")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_scheduler()
            kill_webui_port_8080()
            print("已停止")
        return

    bot_token = get_bot_token(config)
    if not bot_token or bot_token.strip() in ('', 'x', 'X', 'your_token_here', 'YOUR_BOT_TOKEN'):
        logger.debug("config.json 中的 telegram.bot_token 未設定或為佔位符，跳過 Bot 啟動")
        # 僅執行排程模式（不啟動 Bot）
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_scheduler()
            kill_webui_port_8080()
            print("已停止")
        return

    AUTHORIZED_CHAT_IDS = get_authorized_chat_ids(config)
    logger.info(f"授權 chat_ids: {AUTHORIZED_CHAT_IDS}")

    # 建立 Application
    try:
        app = Application.builder().token(bot_token).build()
    except Exception as e:
        logger.error(f"Telegram Bot 初始化失敗: {e}")
        print(f"錯誤：Telegram Bot 初始化失敗：{e}")
        print("  排程推播仍在背景運行，按 Ctrl+C 停止")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_scheduler()
            kill_webui_port_8080()
            print("已停止")
        return

    # 註冊命令處理器
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("stocks", cmd_stocks))
    app.add_handler(CommandHandler("product", cmd_product))
    app.add_handler(CommandHandler("status", cmd_status))

    # 未知命令
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # 全域錯誤處理器（避免 Conflict/Network 等例外導致 Bot 崩潰）
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
        error = context.error
        if isinstance(error, tg_error.Conflict):
            logger.warning("Telegram Conflict: 另一個 Bot 實例仍在釋放中，5 秒後自動重試...")
            await asyncio.sleep(5)
        elif isinstance(error, tg_error.NetworkError):
            logger.warning(f"Telegram 網路錯誤（將自動重試）: {error}")
        elif isinstance(error, tg_error.TimedOut):
            logger.debug(f"Telegram 連線超時（長輪詢正常行為）: {error}")
        else:
            logger.error(f"Telegram Bot 未處理的例外: {error}", exc_info=True)
    app.add_error_handler(error_handler)

    scheduler_status = "已啟用" if not args.no_scheduler else "已停用"
    webui_status = "已啟動 (http://localhost:8080)" if not args.no_webui else "已停用"
    logger.info(f"統一起動器啟動中（Bot + Web UI {webui_status} + 排程推播 {scheduler_status}）...")
    print(f"FinanceMonitor 統一起動器")
    print(f"  Web UI: {webui_status}")
    print(f"  Telegram Bot: 已啟動（授權 chat_id: {AUTHORIZED_CHAT_IDS[0]}）")
    print(f"  排程推播: {scheduler_status}")
    print(f"  在 Telegram 發送 /help 查看命令")
    print(f"  按 Ctrl+C 停止所有服務")

    # 啟動 polling
    try:
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            close_loop=False,
        )
    except Exception as e:
        logger.error(f"Telegram Bot polling 失敗: {e}")
        print(f"錯誤：Telegram Bot polling 中斷：{e}")
        print("  排程推播仍在背景運行，按 Ctrl+C 停止")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        stop_scheduler()
        kill_webui_port_8080()
        print("所有服務已停止")

if __name__ == "__main__":
    main()
