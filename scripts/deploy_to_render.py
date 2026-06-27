#!/usr/bin/env python3
"""
deploy_to_render.py - 一鍵部署本機程式碼到 Render

功能：
  1. 將本機的部署相關檔案推送到 GitHub financemonitor repo
  2. Render 偵測到 push 後自動重新建置部署
  3. 約 5 分鐘後新版上線

使用方式：
  python scripts/deploy_to_render.py              # 正常部署
  python scripts/deploy_to_render.py --check       # 只檢查有哪些檔案變更，不實際推送
  python scripts/deploy_to_render.py --dry-run     # 模擬部署，不推送

設計理念：
  - workbuddy-backup repo：每日 09:00 完整備份（全部檔案）
  - financemonitor repo：只放 Render 部署需要的檔案（本腳本管理）
  - 兩者獨立運作，互不干擾
"""
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

# ── 設定 ──
# Token 從環境變數讀取（避免硬編碼被 GitHub Secret Scanning 攔截）
TOKEN = os.environ.get("GITHUB_PAT", "")
if not TOKEN:
    # 嘗試從 .env 檔讀取
    env_path = os.path.join(PROJECT_ROOT, "scripts", ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.strip().startswith("GITHUB_PAT="):
                    TOKEN = line.strip().split("=", 1)[1].strip("\"'")
                    break
if not TOKEN:
    print("[ERROR] 找不到 GITHUB_PAT 環境變數")
    print("        請設定: export GITHUB_PAT=ghp_xxxxx")
    print("        或在 scripts/.env 加入: GITHUB_PAT=ghp_xxxxx")
    sys.exit(1)

OWNER = "hedynet2016"
REPO = "financemonitor"
BRANCH = "main"

# 專案根目錄（scripts/ 的上兩層）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 部署到 Render 的檔案清單（只放程式碼和設定，不放日誌/記憶體/報告）
DEPLOY_FILES = [
    # 核心程式
    "integrated_monitor.py",
    "stock_monitor.py",
    "news_monitor.py",
    "product_monitor.py",
    "economic_monitor.py",
    "webui.py",
    "telegram_bot.py",
    # 依賴與設定
    "requirements.txt",
    "Dockerfile",
    "render.yaml",
    ".dockerignore",
    ".gitignore",
    # 部署腳本
    "scripts/generate_config.py",
    "scripts/render_start.sh",
    "scripts/render_scheduler.py",
    "scripts/render_backup.py",
    "scripts/daily_report.py",
    "scripts/deploy_to_render.py",
    # 文件
    "README.md",
    "RENDER_DEPLOY_GUIDE.md",
]


def github_api(method, path, data=None):
    """呼叫 GitHub REST API"""
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
        err_body = e.read().decode("utf-8")
        return {"error": f"HTTP {e.code}: {err_body}"}
    except Exception as e:
        return {"error": str(e)}


def get_remote_sha(filepath):
    """取得遠端檔案的 sha（更新時需要）；不存在回傳 None"""
    result = github_api("GET", f"{filepath}?ref={BRANCH}")
    if result and isinstance(result, dict) and "sha" in result:
        return result["sha"]
    return None


def get_remote_content(filepath):
    """取得遠端檔案內容的 base64"""
    result = github_api("GET", f"{filepath}?ref={BRANCH}")
    if result and isinstance(result, dict) and "content" in result:
        return result["content"]
    return None


def file_changed(filepath):
    """比對本機檔案和遠端是否不同"""
    local_path = os.path.join(PROJECT_ROOT, filepath)
    if not os.path.exists(local_path):
        return False, None, None

    with open(local_path, "rb") as f:
        local_raw = f.read()
    local_b64 = base64.b64encode(local_raw).decode("utf-8")

    remote_b64 = get_remote_content(filepath)
    if remote_b64 is None:
        return True, local_b64, None  # 遠端不存在 = 新檔案

    # 比對 base64 內容（GitHub 回傳的 base64 每 76 字元有換行，需先去除所有換行）
    local_clean = local_b64.replace("\n", "").strip()
    remote_clean = (remote_b64 or "").replace("\n", "").strip()
    changed = local_clean != remote_clean
    return changed, local_b64, get_remote_sha(filepath)


def deploy(dry_run=False, check_only=False):
    """執行部署"""
    print("=" * 60)
    print("  deploy_to_render.py - 部署到 Render")
    print("=" * 60)
    print(f"  本機目錄: {PROJECT_ROOT}")
    print(f"  目標 repo: {OWNER}/{REPO}")
    print(f"  分支: {BRANCH}")
    print(f"  模式: {'檢查 only' if check_only else '模擬' if dry_run else '正式部署'}")
    print("=" * 60)
    print()

    # ── 第一階段：掃描變更 ──
    changed_files = []
    new_files = []
    unchanged_files = []
    missing_files = []

    print("[1/2] 掃描檔案變更...")
    for filepath in DEPLOY_FILES:
        local_path = os.path.join(PROJECT_ROOT, filepath)
        if not os.path.exists(local_path):
            missing_files.append(filepath)
            print(f"  [MISS] {filepath}")
            continue

        changed, local_b64, remote_sha = file_changed(filepath)

        if remote_sha is None:
            new_files.append((filepath, local_b64, None))
            print(f"  [NEW]  {filepath}")
        elif changed:
            changed_files.append((filepath, local_b64, remote_sha))
            print(f"  [UPD]  {filepath}")
        else:
            unchanged_files.append(filepath)
            print(f"  [---]  {filepath} (無變更)")

    print()
    print(f"  變更: {len(changed_files)} | 新增: {len(new_files)} | "
          f"無變更: {len(unchanged_files)} | 缺失: {len(missing_files)}")

    if missing_files:
        print(f"\n  [WARN] 以下檔案在本機找不到: {', '.join(missing_files)}")

    if not changed_files and not new_files:
        print("\n  所有檔案均無變更，無需部署。")
        return

    if check_only:
        print("\n  [CHECK] 僅檢查模式，不執行部署。")
        print(f"\n  若要部署，執行: python scripts/deploy_to_render.py")
        return

    # ── 第二階段：推送變更 ──
    print(f"\n[2/2] 推送變更到 GitHub...")
    success = 0
    failed = 0

    for filepath, local_b64, sha in changed_files + new_files:
        body = {
            "message": f"Deploy update: {filepath}",
            "content": local_b64,
            "branch": BRANCH,
        }
        if sha:
            body["sha"] = sha

        if dry_run:
            print(f"  [DRY]  {filepath} (模擬推送)")
            success += 1
            continue

        result = github_api("PUT", filepath, body)

        if result and isinstance(result, dict) and "content" in result:
            html_url = result["content"].get("html_url", "")
            print(f"  [OK]   {filepath}")
            success += 1
        else:
            print(f"  [FAIL] {filepath}: {result}")
            failed += 1

    print()
    print("=" * 60)
    if failed == 0:
        if dry_run:
            print(f"  模擬部署完成！{success} 個檔案待推送。")
            print(f"  若要正式部署，執行: python scripts/deploy_to_render.py")
        else:
            print(f"  部署成功！{success} 個檔案已推送。")
            print(f"  Render 將在 5 分鐘內自動重新建置。")
            print(f"  監看部署狀態: https://dashboard.render.com")
            print(f"  Repo: https://github.com/{OWNER}/{REPO}")
    else:
        print(f"  部分失敗：成功 {success}，失敗 {failed}。")
        print(f"  請檢查失敗的檔案後重試。")
    print("=" * 60)


def main():
    dry_run = "--dry-run" in sys.argv
    check_only = "--check" in sys.argv
    deploy(dry_run=dry_run, check_only=check_only)


if __name__ == "__main__":
    main()
