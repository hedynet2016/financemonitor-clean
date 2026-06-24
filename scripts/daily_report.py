#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_report.py - WorkBuddy Daily Report
每日下午 6 點自動彙整推播問題、錯誤、功能變更、待辦事項，
並以 HTML 郵件（含圓餅圖、曲線圖）寄送至 admin@hedynet.com

問題陳述原則: 5W1H1N
  Who    - 發生對象 (哪個模組/服務)
  What   - 發生現象及影響
  When   - 發生時間
  Where  - 發生位置 (函式/API/端點)
  Why    - 發生原因
  How    - 問題流程檢討
  Number - 發生數量及比例
"""

import os
import re
import sys
import json
import smtplib
import datetime
import base64
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ──────────────────────────────────────────────
# 路徑設定（自動適配本機 Windows 和 Render Linux）
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent   # 20260310182331/ 或 /app/

# Render 上沒有本機的 .workbuddy 目錄，使用容器內路徑
_is_render = os.environ.get("RENDER", "") == "1" or Path("/.dockerenv").exists() or os.environ.get("PORT")

MEMORY_DIR = BASE_DIR / ".workbuddy" / "memory"
# 在 Render 上建立一個用於存放每日狀態的目錄
RENDER_STATUS_DIR = BASE_DIR / "logs_status"
RENDER_STATUS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILES = [
    BASE_DIR / "integrated_monitor.log",
    BASE_DIR / "news_monitor.log",
    BASE_DIR / "stock_monitor.log",
    BASE_DIR / "economic_monitor.log",
    BASE_DIR / "webui_stdout.log",
]

# 自動化排程備份目錄（本機路徑；Render 上不存在會自動跳過）
AUTOMATION_BACKUP_DIR = Path(os.environ.get(
    "AUTOMATION_BACKUP_DIR",
    "/c/Users/Ben/.workbuddy/automation-backups"
))
WB_MEMORY_DIR = Path(os.environ.get(
    "WB_MEMORY_DIR",
    "/c/Users/Ben/.workbuddy/memory"
))

# ──────────────────────────────────────────────
# 環境變數
# ──────────────────────────────────────────────
def load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()

GMAIL_SENDER   = os.environ.get("GMAIL_SENDER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT      = os.environ.get("REPORT_RECIPIENT", "admin@hedynet.com")

# ──────────────────────────────────────────────
# 日期
# ──────────────────────────────────────────────
TODAY = datetime.date.today()
YESTERDAY = TODAY - datetime.timedelta(days=1)
TODAY_STR = TODAY.strftime("%Y-%m-%d")
TODAY_DISPLAY = TODAY.strftime("%Y/%m/%d")
YESTERDAY_STR = YESTERDAY.strftime("%Y-%m-%d")
YESTERDAY_DISPLAY = YESTERDAY.strftime("%Y/%m/%d")

# ──────────────────────────────────────────────
# 1. 讀取當日日誌（取今天的行）
# ──────────────────────────────────────────────
def read_today_logs():
    all_lines = []
    for log_path in LOG_FILES:
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if TODAY_STR in line:
                        all_lines.append(line.rstrip())
        except Exception:
            pass
    return all_lines

def read_recent_logs():
    """
    讀取從昨天到今天的所有日誌（用於圓餅圖和曲線圖）
    有幾天就算幾天（目前是 6/24 ~ 6/25，共 2 天）
    """
    all_lines = []
    # 從昨天到今天
    for i in range((TODAY - YESTERDAY).days + 1):
        d = YESTERDAY + datetime.timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        for log_path in LOG_FILES:
            if not log_path.exists():
                continue
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if ds in line:
                            all_lines.append(line.rstrip())
            except Exception:
                pass
    return all_lines

# ──────────────────────────────────────────────
# 2. 解析錯誤與問題
# ──────────────────────────────────────────────
ERROR_CATEGORIES = {
    "BLS API":         re.compile(r"\[BLS\].*REQUEST_NOT_PROCESSED|BLS.*api.*fail", re.I),
    "Huodongxing 逾時": re.compile(r"\[Huodongxing\].*timed out|Huodongxing.*timeout", re.I),
    "ACCUPASS 逾時":   re.compile(r"\[ACCUPASS\].*timed out|ACCUPASS.*timeout", re.I),
    "HTTP/網路錯誤":    re.compile(r"ConnectionError|ConnectTimeout|ReadTimeout|HTTPError|status[\s:_]+5\d{2}", re.I),
    "解析/格式錯誤":    re.compile(r"ParseError|JSONDecodeError|KeyError|IndexError|AttributeError", re.I),
    "Telegram 推播":   re.compile(r"Telegram.*fail|telegram.*error|TelegramError", re.I),
    "Discord 推播":    re.compile(r"Discord.*fail|discord.*error|DiscordError", re.I),
    "其他 ERROR":      re.compile(r"\s-\s(ERROR|CRITICAL)\s", re.I),
}

def classify_issues(lines):
    """
    回傳：
      issues      = { category: [line, ...] }
      error_count = { category: count }
      resolved    = { category: count }   # 同一類有後續 INFO 說成功視為解決
    """
    issues = defaultdict(list)
    for line in lines:
        lvl = "ERROR" if " - ERROR - " in line else ("WARNING" if " - WARNING - " in line else None)
        if lvl is None:
            continue
        matched = False
        for cat, pattern in ERROR_CATEGORIES.items():
            if pattern.search(line):
                issues[cat].append(line)
                matched = True
                break
        if not matched and lvl == "ERROR":
            issues["其他 ERROR"].append(line)

    error_count = {cat: len(v) for cat, v in issues.items() if v}

    # 簡單估算：每個 ERROR/WARNING 後若出現對應 "successfully" 視為解決
    resolved = defaultdict(int)
    lines_text = "\n".join(lines)
    for cat in error_count:
        # 粗略：若同日誌含有對應來源 + "successfully"
        kw = cat.split()[0]
        if re.search(rf"\[{kw}\].*successfully|{kw}.*success", lines_text, re.I):
            resolved[cat] = min(error_count[cat], error_count[cat] // 2 + 1)

    return issues, error_count, dict(resolved)

# ──────────────────────────────────────────────
# 2-b. 5W1H1N 解析（從單條 log line 萃取七個維度）
# ──────────────────────────────────────────────
# 模組 -> 中文名稱對照
_MODULE_NAMES = {
    "bls":          "BLS 美國勞工局 API",
    "huodongxing":  "活動行平台爬蟲",
    "accupass":     "ACCUPASS 活動平台爬蟲",
    "telegram":     "Telegram 推播機器人",
    "discord":      "Discord 推播機器人",
    "news":         "新聞監控模組",
    "stock":        "股票監控模組",
    "economic":     "經濟數據監控模組",
    "webui":        "Web UI 介面",
    "integrated":   "整合監控主程式",
}

# 原因推斷規則
_WHY_RULES = [
    (re.compile(r"timed out|timeout|ReadTimeout|ConnectTimeout", re.I),
     "連線逾時，可能為對方伺服器回應慢或網路不穩"),
    (re.compile(r"ConnectionError|ConnectionRefused",             re.I),
     "無法建立連線，目標主機拒絕或無回應"),
    (re.compile(r"status[\s:_]+(5\d{2})",                        re.I),
     "對方伺服器回傳 5xx 錯誤，伺服器端發生異常"),
    (re.compile(r"status[\s:_]+(4\d{2})",                        re.I),
     "對方伺服器回傳 4xx 錯誤，請求參數或授權有問題"),
    (re.compile(r"JSONDecodeError|json.*decode",                  re.I),
     "回應內容非合法 JSON，可能 API 格式變更或回傳錯誤頁面"),
    (re.compile(r"KeyError|IndexError",                           re.I),
     "資料結構與預期不符，API 回應欄位可能異動"),
    (re.compile(r"AttributeError",                                re.I),
     "物件屬性不存在，程式邏輯或資料型別錯誤"),
    (re.compile(r"REQUEST_NOT_PROCESSED",                         re.I),
     "API 請求未被處理，可能超出配額或維護中"),
    (re.compile(r"TelegramError|discord.*error",                  re.I),
     "推播 API 呼叫失敗，可能 Token 失效或頻道不存在"),
]

# HOW 建議對照
_HOW_MAP = {
    "連線逾時":       "確認目標服務可用性，考慮增加重試（retry）機制與超時設定",
    "無法建立連線":   "檢查防火牆、DNS 設定；確認對方主機 IP/Port 是否變更",
    "5xx":           "稍後重試；若持續發生請聯繫對方 API 支援或切換備援端點",
    "4xx":           "檢查 API Key/Token 是否有效；確認請求參數格式是否符合文件",
    "JSON":          "比對 API 最新文件，更新解析邏輯；加入原始回應 logging",
    "資料結構":      "加入 schema 驗證；在解析前先印出原始資料輔助 debug",
    "物件屬性":      "加入 isinstance/hasattr 防呆；確認資料初始化流程",
    "配額":          "確認 API 訂閱方案；加入配額監控警報",
    "推播":          "重新確認 Bot Token 與 Chat ID；測試推播頻道是否存在",
}


def build_5w1h1n(cat, lines, error_count, resolved):
    """
    根據分類名稱與該類別所有 log lines，建立 5W1H1N 字典：
      who, what, when, where, why, how, number
    """
    total  = error_count.get(cat, len(lines))
    res    = resolved.get(cat, 0)
    pct    = round(res / total * 100) if total else 0
    unres  = total - res
    all_total = sum(error_count.values()) or 1
    cat_pct = round(total / all_total * 100)

    # WHO: 從分類名稱對應模組
    who = cat
    for key, name in _MODULE_NAMES.items():
        if key in cat.lower():
            who = name
            break

    # WHEN: 取第一筆與最後一筆時間戳
    ts_list = []
    for l in lines:
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", l)
        if m:
            ts_list.append(m.group(1))
    if ts_list:
        if len(ts_list) > 1 and ts_list[0] != ts_list[-1]:
            when = f"{ts_list[0]} ～ {ts_list[-1]}"
        else:
            when = ts_list[0]
    else:
        when = TODAY_STR

    # WHERE: 從 log 行萃取模組標籤或函式名稱
    where_hits = []
    for l in lines[:3]:
        m = re.search(r"\[([A-Za-z0-9_]+)\]", l)
        if m:
            where_hits.append(m.group(1))
        m2 = re.search(r"in (\w+)\(\)", l)
        if m2:
            where_hits.append(m2.group(1) + "()")
    where = "、".join(dict.fromkeys(where_hits)) if where_hits else cat

    # WHAT: 取最具代表性的 log 訊息（首筆去掉時間戳）
    sample = lines[0] if lines else ""
    # 去掉時間戳+logger
    what_msg = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,\d]* - \w+ - (ERROR|WARNING|INFO) - ", "", sample).strip()
    what_msg = what_msg[:160] if what_msg else cat + " 發生異常"

    # WHY: 根據規則比對
    why = "原因待查"
    how_key = None
    combined = " ".join(lines[:10])
    for pattern, reason in _WHY_RULES:
        if pattern.search(combined):
            why = reason
            # 取關鍵字對應 HOW
            for k in _HOW_MAP:
                if k in reason:
                    how_key = k
                    break
            break

    # HOW: 查 HOW_MAP
    how = _HOW_MAP.get(how_key, "蒐集完整 stack trace，逐步縮小問題範圍；加入自動重試與告警機制")

    # NUMBER
    number = (
        f"共發生 {total} 次（佔今日所有問題 {cat_pct}%）"
        f"，已解決 {res} 次（{pct}%），未解決 {unres} 次"
    )

    return {
        "who":    who,
        "what":   what_msg,
        "when":   when,
        "where":  where,
        "why":    why,
        "how":    how,
        "number": number,
    }


# ──────────────────────────────────────────────
# 3. 讀取記憶體日誌 (當天與近 7 天)
# ──────────────────────────────────────────────
def read_memory_logs():
    """回傳今天的記憶體日誌內容（project memory）"""
    md_path = MEMORY_DIR / f"{TODAY_STR}.md"
    content = ""
    if md_path.exists():
        content = md_path.read_text(encoding="utf-8", errors="replace")
    return content

def read_recent_memory(days=7):
    """讀取最近 N 天的記憶體 md，回傳列表 [(date_str, content)]"""
    result = []
    for i in range(days):
        d = TODAY - datetime.timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        md_path = MEMORY_DIR / f"{ds}.md"
        if md_path.exists():
            result.append((ds, md_path.read_text(encoding="utf-8", errors="replace")))
    return result

# ──────────────────────────────────────────────
# 4. 解析功能/排程新增刪除
# ──────────────────────────────────────────────
def parse_feature_changes(memory_content):
    """從當天記憶體 md 萃取新增/刪除功能/排程"""
    added   = []
    removed = []
    for line in memory_content.splitlines():
        low = line.lower()
        if any(k in low for k in ["新增", "add", "added", "新功能", "啟用", "enable"]):
            added.append(line.strip("- •*# "))
        elif any(k in low for k in ["刪除", "移除", "remove", "removed", "停用", "disable", "deprecated"]):
            removed.append(line.strip("- •*# "))
    return added, removed

# ──────────────────────────────────────────────
# 5. 讀取自動化排程
# ──────────────────────────────────────────────
def read_automations():
    automations = []
    if not AUTOMATION_BACKUP_DIR.exists():
        return automations
    for f in sorted(AUTOMATION_BACKUP_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            automations.append(data)
        except Exception:
            pass
    return automations

# ──────────────────────────────────────────────
# 6. 解析從昨天到報告當天的問題發生與解決數量（用於曲線圖）
# ──────────────────────────────────────────────
def get_daily_problem_counts():
    """
    讀取從昨天到報告當天的日誌，統計每天「問題發生數量」
    回傳 [(日期label, 數量)]
    """
    counts = []
    for i in range((TODAY - YESTERDAY).days + 1):
        d = YESTERDAY + datetime.timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        label = d.strftime("%m/%d")
        cnt = 0
        for log_path in LOG_FILES:
            if not log_path.exists():
                continue
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if ds in line and (" - ERROR - " in line or " - WARNING - " in line):
                            cnt += 1
            except Exception:
                pass
        counts.append((label, cnt))
    return counts

def get_daily_solved_counts():
    """
    讀取 MEMORY_DIR 從昨天開始到報告當天（今天）的 .md，
    估算每天「問題解決數量」（以"修復"、"解決"、"fix"等關鍵字出現次數）
    回傳 [(日期label, 數量)]
    """
    counts = []
    fix_pattern = re.compile(r"修復|解決|fix|resolved|fixed|repaired|corrected", re.I)
    for i in range((TODAY - YESTERDAY).days + 1):
        d = YESTERDAY + datetime.timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        label = d.strftime("%m/%d")
        md_path = MEMORY_DIR / f"{ds}.md"
        cnt = 0
        if md_path.exists():
            text = md_path.read_text(encoding="utf-8", errors="replace")
            cnt = len(fix_pattern.findall(text))
        counts.append((label, cnt))
    return counts

# ──────────────────────────────────────────────
# 7. 組合 TODO 與建議
# ──────────────────────────────────────────────
def extract_todos(memory_content):
    todos = []
    for line in memory_content.splitlines():
        low = line.lower()
        if any(k in low for k in ["待", "todo", "待處理", "待完成", "需要", "建議", "optimize", "improve"]):
            stripped = line.strip("- •*# ")
            if stripped:
                todos.append(stripped)
    # 從 MEMORY.md 也撈一些
    mem_path = MEMORY_DIR / "MEMORY.md"
    if mem_path.exists():
        for line in mem_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "待" in line or "TODO" in line.upper():
                stripped = line.strip("- •*# ")
                if stripped and stripped not in todos:
                    todos.append(stripped)
    return todos[:10]  # 最多10條

# ──────────────────────────────────────────────
# 8. 生成圖表（base64 PNG，使用 matplotlib）
# ──────────────────────────────────────────────
def generate_pie_chart(error_count):
    """生成問題分類圓餅圖，回傳 base64 PNG 字串"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm

        if not error_count:
            return None

        # 設定中文字型
        font_candidates = [
            "Microsoft JhengHei", "Microsoft YaHei", "SimHei",
            "Noto Sans CJK TC", "Noto Sans CJK SC",
            "Noto Sans TC", "WenQuanYi Micro Hei",
            "DejaVu Sans",
        ]
        font_name = "DejaVu Sans"
        available = {f.name for f in fm.fontManager.ttflist}
        for fc in font_candidates:
            if fc in available:
                font_name = fc
                break

        labels = list(error_count.keys())
        sizes  = list(error_count.values())
        colors = [
            "#e74c3c","#e67e22","#f1c40f","#2ecc71",
            "#3498db","#9b59b6","#1abc9c","#e91e63"
        ][:len(labels)]

        fig, ax = plt.subplots(figsize=(6, 4.5), facecolor="#1a1a2e")
        ax.set_facecolor("#1a1a2e")
        wedges, texts, autotexts = ax.pie(
            sizes, labels=None, colors=colors,
            autopct="%1.0f%%", startangle=140,
            textprops={"color": "white", "fontsize": 9, "fontfamily": font_name},
            pctdistance=0.75,
        )
        for t in autotexts:
            t.set_color("white")
            t.set_fontsize(8)

        legend = ax.legend(
            wedges, [f"{l} ({s})" for l, s in zip(labels, sizes)],
            loc="lower center", bbox_to_anchor=(0.5, -0.15),
            ncol=2, fontsize=7,
            facecolor="#2d2d44", edgecolor="#555",
            labelcolor="white",
            prop={"family": font_name, "size": 7},
        )
        ax.set_title(f"{YESTERDAY_DISPLAY} 問題分類統計（每日下午 6 點報告）",
                     color="white", fontsize=11, fontfamily=font_name, pad=8)

        import io
        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", dpi=130,
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        data = base64.b64encode(buf.read()).decode()
        return data
    except Exception as e:
        print(f"[WARN] 圓餅圖生成失敗: {e}")
        return None


def generate_line_chart(problem_counts, solved_counts):
    """
    生成從昨天到報告當天的問題發生與解決數量曲線圖（雙曲線）
    回傳 base64 PNG 字串
    """
    try:
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm

        font_candidates = [
            "Microsoft JhengHei", "Microsoft YaHei", "SimHei",
            "Noto Sans CJK TC", "Noto Sans CJK SC",
            "Noto Sans TC", "WenQuanYi Micro Hei",
            "DejaVu Sans",
        ]
        font_name = "DejaVu Sans"
        available = {f.name for f in fm.fontManager.ttflist}
        for fc in font_candidates:
            if fc in available:
                font_name = fc
                break

        labels = [x[0] for x in problem_counts]
        problem_values = [x[1] for x in problem_counts]
        solved_values = [x[1] for x in solved_counts]

        fig, ax = plt.subplots(figsize=(7, 4.2), facecolor="#1a1a2e")
        ax.set_facecolor("#16213e")

        # 曲線 1：問題發生數量（紅色）
        ax.plot(labels, problem_values, color="#ef5350", linewidth=2.5,
                marker="o", markersize=7, markerfacecolor="#ef9a9a",
                label="問題發生")
        ax.fill_between(labels, problem_values, alpha=0.15, color="#ef5350")

        # 曲線 2：問題解決數量（綠色）
        ax.plot(labels, solved_values, color="#66bb6a", linewidth=2.5,
                marker="s", markersize=7, markerfacecolor="#a5d6a7",
                label="問題解決")
        ax.fill_between(labels, solved_values, alpha=0.15, color="#66bb6a")

        # 標註數值
        for lbl, val in zip(labels, problem_values):
            ax.annotate(str(val), (lbl, val),
                        textcoords="offset points", xytext=(0, 8),
                        ha="center", color="#ffcdd2", fontsize=9)
        for lbl, val in zip(labels, solved_values):
            ax.annotate(str(val), (lbl, val),
                        textcoords="offset points", xytext=(0, -15),
                        ha="center", color="#c8e6c9", fontsize=9)

        ax.set_xlabel(f"日期（{YESTERDAY_DISPLAY} ~ {TODAY_DISPLAY}）",
                      color="#90caf9", fontsize=9, fontfamily=font_name)
        ax.set_ylabel("筆數", color="#90caf9", fontsize=9, fontfamily=font_name)
        ax.set_title(f"從 {YESTERDAY_DISPLAY} 至 {TODAY_DISPLAY} 問題趨勢",
                     color="white", fontsize=11, fontfamily=font_name, pad=8)
        ax.tick_params(colors="white", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#444")
        ax.set_ylim(bottom=0)
        ax.grid(axis="y", color="#334", linestyle="--", linewidth=0.6)
        ax.legend(loc="upper left", fontsize=8, facecolor="#2d2d44",
                  edgecolor="#555", labelcolor="white")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", dpi=130,
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        data = base64.b64encode(buf.read()).decode()
        return data
    except Exception as e:
        print(f"[WARN] 曲線圖生成失敗: {e}")
        return None

# ──────────────────────────────────────────────
# 9. 組合 HTML 郵件
# ──────────────────────────────────────────────
def build_html(issues, error_count, resolved, added, removed, todos,
               automations, pie_b64, line_b64, problem_counts, solved_counts, memory_content,
               yesterday_display=YESTERDAY_DISPLAY):

    def section(title, content_html, icon=""):
        return f"""
        <div class="section">
          <div class="section-title">{icon} {title}</div>
          {content_html}
        </div>"""

    total_all = sum(error_count.values()) or 0

    # ── 問題彙整總表（含 Number 比例欄）
    if error_count:
        rows = ""
        for cat, cnt in sorted(error_count.items(), key=lambda x: -x[1]):
            res   = resolved.get(cat, 0)
            pct   = int(cnt / total_all * 100) if total_all else 0
            badge = (f'<span class="badge-ok">已解決 {res}/{cnt}</span>'
                     if res > 0 else f'<span class="badge-err">未解決 {cnt}</span>')
            rows += (f"<tr><td>{cat}</td>"
                     f"<td class='num'>{cnt}</td>"
                     f"<td class='num'>{pct}%</td>"
                     f"<td>{badge}</td></tr>")
        issues_html = f"""
        <table>
          <thead><tr><th>問題類別</th><th>次數</th><th>佔比</th><th>狀態</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <p style="font-size:12px;color:#888;margin-top:6px;">今日共記錄 <b>{total_all}</b> 筆錯誤/警告</p>"""
    else:
        issues_html = '<p class="ok-text">今日無錯誤或警告記錄 🎉</p>'

    # ── 圖表
    charts_html = ""
    if pie_b64:
        charts_html += f'<img src="data:image/png;base64,{pie_b64}" alt="問題分類圓餅圖" class="chart-img">'
    if line_b64:
        charts_html += f'<img src="data:image/png;base64,{line_b64}" alt="問題解決曲線圖" class="chart-img">'
    if charts_html:
        charts_section = section("📊 統計圖表", f'<div class="charts-row">{charts_html}</div>', "")
    else:
        charts_section = ""

    # ── 5W1H1N 問題詳細卡片
    _LABEL_COLOR = {
        "who":    ("#1565c0", "#e3f2fd"),
        "what":   ("#b71c1c", "#ffebee"),
        "when":   ("#4527a0", "#ede7f6"),
        "where":  ("#1b5e20", "#e8f5e9"),
        "why":    ("#e65100", "#fff3e0"),
        "how":    ("#006064", "#e0f7fa"),
        "number": ("#37474f", "#eceff1"),
    }
    _LABEL_NAME = {
        "who":    "👤 Who｜發生對象",
        "what":   "⚡ What｜現象及影響",
        "when":   "🕐 When｜發生時間",
        "where":  "📍 Where｜發生位置",
        "why":    "❓ Why｜發生原因",
        "how":    "🔍 How｜流程檢討",
        "number": "🔢 Number｜數量及比例",
    }

    cards_html = ""
    if issues:
        for cat, lns in issues.items():
            w7 = build_5w1h1n(cat, lns, error_count, resolved)
            rows_inner = ""
            for key in ("who","what","when","where","why","how","number"):
                fc, bg = _LABEL_COLOR[key]
                rows_inner += (
                    f"<tr>"
                    f"<td style='width:170px;font-weight:700;color:{fc};"
                    f"background:{bg};white-space:nowrap;padding:7px 12px;'>"
                    f"{_LABEL_NAME[key]}</td>"
                    f"<td style='padding:7px 12px;font-size:13px;color:#333;'>{w7[key]}</td>"
                    f"</tr>"
                )
            lvl_badge = ('<span class="badge-err">ERROR</span>'
                         if any("ERROR" in l for l in lns)
                         else '<span class="badge-warn">WARNING</span>')
            cards_html += f"""
            <div class="card5w">
              <div class="card5w-hd">
                {lvl_badge}
                <span style="font-weight:700;margin-left:8px;">{cat}</span>
                <span style="float:right;font-size:12px;color:#999;">{len(lns)} 筆</span>
              </div>
              <table class="card5w-tb">{rows_inner}</table>
            </div>"""
        detail_html = cards_html
    else:
        detail_html = '<p class="ok-text">無詳細錯誤記錄。</p>'

    # ── 功能新增/刪除
    if added or removed:
        feat_html = ""
        if added:
            items = "".join(f"<li>{x}</li>" for x in added)
            feat_html += f'<p class="sub-title">✅ 新增</p><ul>{items}</ul>'
        if removed:
            items = "".join(f"<li>{x}</li>" for x in removed)
            feat_html += f'<p class="sub-title">🗑 刪除 / 停用</p><ul>{items}</ul>'
    else:
        feat_html = "<p>今日無功能新增或刪除記錄。</p>"

    # ── 排程
    if automations:
        sch_rows = ""
        for a in automations:
            status_badge = ('<span class="badge-ok">ACTIVE</span>'
                            if a.get("status") == "ACTIVE"
                            else '<span class="badge-warn">PAUSED</span>')
            sch_rows += (f"<tr><td>{a.get('name','—')}</td>"
                         f"<td>{a.get('rrule','—')}</td>"
                         f"<td>{status_badge}</td></tr>")
        sch_html = f"""<table>
          <thead><tr><th>排程名稱</th><th>週期</th><th>狀態</th></tr></thead>
          <tbody>{sch_rows}</tbody></table>"""
    else:
        sch_html = "<p>無排程資料。</p>"

    # ── 待辦/建議
    if todos:
        items = "".join(f"<li>{t}</li>" for t in todos)
        todo_html = f"<ul class='todo-list'>{items}</ul>"
    else:
        todo_html = "<p>今日無待完成事項記錄。</p>"

    # ── 日誌摘要
    if memory_content.strip():
        lines_snippet = memory_content.strip().splitlines()[:25]
        snippet_text  = "\n".join(lines_snippet)
        log_html = f"<pre class='log-pre'>{snippet_text}</pre>"
    else:
        log_html = "<p>今日無記憶體日誌。</p>"

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WorkBuddy Daily Report {TODAY_DISPLAY}</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f4f6fb; color: #222; margin: 0; padding: 0; }}
  .wrapper {{ max-width: 900px; margin: 24px auto; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 16px rgba(0,0,0,.10); }}
  .header {{ background: linear-gradient(135deg, #1a237e 0%, #283593 60%, #1976d2 100%); color: #fff; padding: 28px 36px; }}
  .header h1 {{ margin: 0 0 6px; font-size: 22px; letter-spacing: 1px; }}
  .header p {{ margin: 0; font-size: 13px; opacity: .85; }}
  .section {{ padding: 20px 36px; border-bottom: 1px solid #e8ecf1; }}
  .section:last-child {{ border-bottom: none; }}
  .section-title {{ font-size: 15px; font-weight: 700; color: #1a237e; margin-bottom: 12px; border-left: 4px solid #1976d2; padding-left: 10px; }}
  .sub-title {{ font-size: 13px; font-weight: 600; color: #333; margin: 10px 0 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 6px; }}
  th {{ background: #e8eaf6; color: #283593; text-align: left; padding: 7px 10px; font-weight: 600; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }}
  tr:hover td {{ background: #f5f5ff; }}
  .num {{ text-align: center; font-weight: 700; color: #c62828; }}
  .badge-ok  {{ background: #e8f5e9; color: #2e7d32; border-radius: 4px; padding: 2px 8px; font-size: 11px; font-weight: 700; }}
  .badge-err {{ background: #ffebee; color: #c62828; border-radius: 4px; padding: 2px 8px; font-size: 11px; font-weight: 700; }}
  .badge-warn{{ background: #fff8e1; color: #e65100; border-radius: 4px; padding: 2px 8px; font-size: 11px; font-weight: 700; }}
  .ok-text {{ color: #2e7d32; font-weight: 600; font-size: 14px; margin: 8px 0; }}
  .charts-row {{ display: flex; gap: 16px; flex-wrap: wrap; justify-content: center; margin-top: 8px; }}
  .chart-img {{ border-radius: 8px; max-width: 100%; height: auto; background: #1a1a2e; }}
  ul {{ margin: 4px 0 8px 18px; padding: 0; }}
  ul li {{ margin-bottom: 4px; font-size: 13px; }}
  .todo-list li {{ color: #37474f; }}
  .log-pre {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 6px; padding: 12px; font-size: 11.5px; overflow-x: auto; color: #333; white-space: pre-wrap; word-break: break-word; }}
  /* 5W1H1N 卡片 */
  .card5w {{ border: 1px solid #dde3f0; border-radius: 8px; margin-bottom: 16px; overflow: hidden; }}
  .card5w-hd {{ background: #f0f4ff; padding: 9px 14px; border-bottom: 1px solid #dde3f0; font-size: 13px; }}
  .card5w-tb {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 0; }}
  .card5w-tb td {{ border-bottom: 1px solid #f0f0f0; }}
  .card5w-tb tr:last-child td {{ border-bottom: none; }}
  .card5w-tb tr:hover td {{ background: #f9fbff !important; }}
  .footer {{ background: #e8eaf6; color: #666; text-align: center; padding: 14px; font-size: 12px; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>📊 WorkBuddy Daily Report（昨日報告）</h1>
    <p>報告日期：{TODAY_DISPLAY} &nbsp;|&nbsp; 數據日期：{yesterday_display} &nbsp;|&nbsp; 系統：FinanceMonitor &nbsp;|&nbsp; 自動生成</p>
  </div>

  {section("🚨 昨日推播問題彙整", issues_html, "")}
  {charts_section}
  {section("📋 問題 5W1H1N 詳細分析", detail_html, "")}
  {section("🔧 功能與排程變更", feat_html, "")}
  {section("⏰ 現行自動化排程", sch_html, "")}
  {section("📝 今日工作日誌摘要", log_html, "")}
  {section("📌 待完成事項與優化建議", todo_html, "")}

  <div class="footer">
    WorkBuddy Daily Report &middot; 自動發送 &middot; {TODAY_DISPLAY} 18:00
    &nbsp;|&nbsp; 問題陳述原則：5W1H1N
  </div>
</div>
</body>
</html>"""
    return html

# ──────────────────────────────────────────────
# 10. 發送郵件
# ──────────────────────────────────────────────
def send_email(html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Workbuddy Daily Report（昨日報告）{YESTERDAY_DISPLAY}"
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = RECIPIENT

    part_html = MIMEText(html_body, "html", "utf-8")
    msg.attach(part_html)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, [RECIPIENT], msg.as_bytes())
        print(f"[OK] 郵件已發送至 {RECIPIENT}")
        return True
    except Exception as e:
        print(f"[ERROR] 郵件發送失敗: {e}")
        return False

# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def main():
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 開始生成 Daily Report ({TODAY_DISPLAY})...")

    # 讀資料（修改：使用從昨天到今天的所有日誌）
    log_lines      = read_recent_logs()
    memory_content = read_memory_logs()
    problem_counts = get_daily_problem_counts()
    solved_counts  = get_daily_solved_counts()
    automations    = read_automations()

    # 分析
    issues, error_count, resolved = classify_issues(log_lines)
    added, removed = parse_feature_changes(memory_content)
    todos = extract_todos(memory_content)

    print(f"  錯誤/警告類別: {len(error_count)} 種，共 {sum(error_count.values())} 筆")
    print(f"  功能新增: {len(added)} | 刪除: {len(removed)}")
    print(f"  待辦事項: {len(todos)}")

    # 圖表
    pie_b64  = generate_pie_chart(error_count)
    line_b64 = generate_line_chart(problem_counts, solved_counts)

    # 組 HTML
    html = build_html(
        issues, error_count, resolved,
        added, removed, todos,
        automations, pie_b64, line_b64,
        problem_counts, solved_counts, memory_content,
        yesterday_display=YESTERDAY_DISPLAY
    )

    # 發送
    ok = send_email(html)

    # 儲存副本（使用今天的日期作為檔名）
    output_path = BASE_DIR / "scripts" / f"report_{TODAY_STR}.html"
    output_path.write_text(html, encoding="utf-8")
    print(f"  報告副本已儲存: {output_path}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
