#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_scheduler.py - Render 容器內背景排程器
在 webui + monitor 同一個容器裡運作，定時觸發：
  - 08:00  economic_news_push.py（經濟指標新聞推播到 Telegram/Discord）
  - 09:00  render_backup.py（推送當日 logs 到 GitHub）
  - 18:00  daily_report.py（生成每日報告）

因為 Render Cron Job 不共享 Web Service 的檔案系統，
所以用同一容器內的背景排程來讀取 logs 最可靠。
"""
import os
import sys
import time
import subprocess
import threading
import datetime
from pathlib import Path

import schedule

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def run_daily_report():
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [Scheduler] 啟動 daily_report.py...")
    try:
        result = subprocess.run(
            [PYTHON, str(BASE_DIR / "scripts" / "daily_report.py")],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(BASE_DIR),
        )
        print(result.stdout)
        if result.stderr:
            print(f"[STDERR] {result.stderr}", file=sys.stderr)
        print(f"[{ts}] [Scheduler] daily_report.py 完成 (exit={result.returncode})")
    except subprocess.TimeoutExpired:
        print(f"[{ts}] [Scheduler] daily_report.py 超時（300s）")
    except Exception as e:
        print(f"[{ts}] [Scheduler] daily_report.py 失敗: {e}")


def run_backup():
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [Scheduler] 啟動 render_backup.py...")
    try:
        result = subprocess.run(
            [PYTHON, str(BASE_DIR / "scripts" / "render_backup.py")],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(BASE_DIR),
            env={**os.environ},
        )
        print(result.stdout)
        if result.stderr:
            print(f"[STDERR] {result.stderr}", file=sys.stderr)
        print(f"[{ts}] [Scheduler] render_backup.py 完成 (exit={result.returncode})")
    except subprocess.TimeoutExpired:
        print(f"[{ts}] [Scheduler] render_backup.py 超時（300s）")
    except Exception as e:
        print(f"[{ts}] [Scheduler] render_backup.py 失敗: {e}")


def run_economic_news_push():
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [Scheduler] 啟動 economic_news_push.py...")
    try:
        result = subprocess.run(
            [PYTHON, str(BASE_DIR / "scripts" / "economic_news_push.py")],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(BASE_DIR),
            env={**os.environ},
        )
        print(result.stdout)
        if result.stderr:
            print(f"[STDERR] {result.stderr}", file=sys.stderr)
        print(f"[{ts}] [Scheduler] economic_news_push.py 完成 (exit={result.returncode})")
    except subprocess.TimeoutExpired:
        print(f"[{ts}] [Scheduler] economic_news_push.py 超時（120s）")
    except Exception as e:
        print(f"[{ts}] [Scheduler] economic_news_push.py 失敗: {e}")


def run_in_thread(func):
    def wrapper():
        t = threading.Thread(target=func, daemon=True)
        t.start()
    return wrapper


def main():
    # 設定排程（使用台北時間，容器已設定 TZ=Asia/Taipei）
    schedule.every().day.at("08:00").do(run_in_thread(run_economic_news_push))
    schedule.every().day.at("09:00").do(run_in_thread(run_backup))
    schedule.every().day.at("18:00").do(run_in_thread(run_daily_report))

    # 啟動時執行一次測試（可透過環境變數關閉）
    if os.environ.get("SCHEDULER_TEST_ON_START", "").lower() not in ("0", "false", "no"):
        print("[Scheduler] 啟動測試：檢查腳本是否可正常 import...")
        for script in ["scripts/economic_news_push.py", "scripts/daily_report.py", "scripts/render_backup.py"]:
            path = BASE_DIR / script
            if path.exists():
                print(f"  [OK] {script} 存在")
            else:
                print(f"  [MISS] {script} 不存在")

    print("[Scheduler] 排程已啟動：")
    print("  08:00 → economic_news_push.py（經濟指標新聞推播）")
    print("  09:00 → render_backup.py（推送 logs 到 GitHub）")
    print("  18:00 → daily_report.py（生成每日報告）")
    print(f"  目前時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
