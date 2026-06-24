#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_backup.py - Render 容器內每日備份腳本
每天 09:00 由背景排程觸發，將當日 logs 和狀態推送到 GitHub workbuddy-backup repo

使用 GitHub Contents API 上傳（不需 git，適合容器環境）
"""
import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ── 路徑設定 ──
BASE_DIR = Path(__file__).resolve().parent.parent

LOG_FILES = [
    BASE_DIR / "integrated_monitor.log",
    BASE_DIR / "news_monitor.log",
    BASE_DIR / "stock_monitor.log",
    BASE_DIR / "economic_monitor.log",
    BASE_DIR / "webui_stdout.log",
]

# 狀態檔（daily_report 產生的報告副本等）
EXTRA_FILES = [
    BASE_DIR / "scripts" / f"report_{datetime.date.today().strftime('%Y-%m-%d')}.html",
]

# ── GitHub 設定 ──
TOKEN = os.environ.get("GITHUB_PAT", "")
OWNER = "hedynet2016"
REPO = "workbuddy-backup"
BRANCH = "main"
TODAY_STR = datetime.date.today().strftime("%Y-%m-%d")
TIMESTAMP = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def github_api(method, path, data=None):
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}"
    headers = {
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        body = None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"}
    except Exception as e:
        return {"error": str(e)}


def get_remote_sha(filepath):
    result = github_api("GET", f"{filepath}?ref={BRANCH}")
    if result and isinstance(result, dict) and "sha" in result:
        return result["sha"]
    return None


def upload_file(remote_path, content_bytes, commit_msg):
    content_b64 = base64.b64encode(content_bytes).decode("utf-8")
    sha = get_remote_sha(remote_path)
    body = {
        "message": commit_msg,
        "content": content_b64,
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha
    result = github_api("PUT", remote_path, body)
    if result and isinstance(result, dict) and "content" in result:
        return True
    return False


def extract_today_lines(log_path):
    today_lines = []
    if not log_path.exists():
        return today_lines
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if TODAY_STR in line:
                    today_lines.append(line)
    except Exception:
        pass
    return today_lines


def main():
    if not TOKEN:
        print("[ERROR] GITHUB_PAT 環境變數未設定")
        return 1

    print(f"[{TIMESTAMP}] 開始 Render 每日備份...")
    success = 0
    failed = 0

    # 1. 上傳當日 logs（只取今天的行，避免檔案過大）
    for log_path in LOG_FILES:
        if not log_path.exists():
            print(f"  [SKIP] {log_path.name} (不存在)")
            continue
        today_lines = extract_today_lines(log_path)
        if not today_lines:
            print(f"  [SKIP] {log_path.name} (今日無記錄)")
            continue
        content = "".join(today_lines).encode("utf-8")
        remote_path = f"render-logs/{TODAY_STR}/{log_path.name}"
        ok = upload_file(remote_path, content, f"Backup {TODAY_STR}: {log_path.name}")
        if ok:
            print(f"  [OK]   {remote_path} ({len(today_lines)} lines)")
            success += 1
        else:
            print(f"  [FAIL] {remote_path}")
            failed += 1

    # 2. 上傳報告副本（如果有）
    for extra in EXTRA_FILES:
        if not extra.exists():
            continue
        content = extra.read_bytes()
        remote_path = f"render-logs/{TODAY_STR}/{extra.name}"
        ok = upload_file(remote_path, content, f"Backup {TODAY_STR}: {extra.name}")
        if ok:
            print(f"  [OK]   {remote_path}")
            success += 1
        else:
            print(f"  [FAIL] {remote_path}")
            failed += 1

    # 3. 上傳容器狀態摘要
    status = {
        "date": TODAY_STR,
        "timestamp": TIMESTAMP,
        "platform": "render",
        "log_files": [f.name for f in LOG_FILES if f.exists()],
        "log_sizes": {f.name: f.stat().st_size for f in LOG_FILES if f.exists()},
    }
    status_json = json.dumps(status, indent=2, ensure_ascii=False).encode("utf-8")
    remote_path = f"render-logs/{TODAY_STR}/_status.json"
    ok = upload_file(remote_path, status_json, f"Backup {TODAY_STR}: container status")
    if ok:
        print(f"  [OK]   {remote_path}")
        success += 1
    else:
        print(f"  [FAIL] {remote_path}")
        failed += 1

    print(f"\n備份完成: 成功 {success}，失敗 {failed}")
    print(f"Repo: https://github.com/{OWNER}/{REPO}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
