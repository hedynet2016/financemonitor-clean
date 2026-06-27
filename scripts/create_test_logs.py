#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
建立測試用日誌資料（6/24 和 6/25）
"""

from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
LOG_FILE = BASE_DIR / "integrated_monitor.log"

# 測試資料：6/24 的錯誤/警告
test_logs_0624 = [
    "2026-06-24 10:15:23,123 - ERROR - [BLS] REQUEST_NOT_PROCESSED: API quota exceeded",
    "2026-06-24 10:16:45,456 - WARNING - [Huodongxing] Connection timed out after 30s",
    "2026-06-24 11:20:12,789 - ERROR - Telegram push failed: ConnectionError",
    "2026-06-24 12:30:55,012 - WARNING - [ACCUPASS] Timeout waiting for response",
    "2026-06-24 14:45:33,345 - ERROR - [BLS] REQUEST_NOT_PROCESSED: Server error 503",
    "2026-06-24 15:10:21,678 - WARNING - Discord webhook failed: HTTPError 429",
    "2026-06-24 16:20:44,901 - ERROR - JSONDecodeError: Invalid response from API",
    "2026-06-24 17:30:15,234 - WARNING - [Huodongxing] Retry attempt 1/3 failed",
]

# 測試資料：6/25 的錯誤/警告
test_logs_0625 = [
    "2026-06-25 09:10:33,111 - ERROR - [BLS] REQUEST_NOT_PROCESSED: Rate limit reached",
    "2026-06-25 10:25:47,444 - WARNING - Telegram notification timeout",
    "2026-06-25 11:40:59,777 - ERROR - Discord push error: Unauthorized",
    "2026-06-25 13:55:11,000 - WARNING - [ACCUPASS] Connection reset by peer",
]

# 寫入日誌檔
all_logs = test_logs_0624 + test_logs_0625
with open(LOG_FILE, "w", encoding="utf-8") as f:
    for log in all_logs:
        f.write(log + "\n")

print(f"[OK] 測試日誌已建立: {LOG_FILE}")
print(f"  共 {len(test_logs_0624)} 筆（6/24）")
print(f"  共 {len(test_logs_0625)} 筆（6/25）")
print(f"  總計 {len(all_logs)} 筆測試資料")
