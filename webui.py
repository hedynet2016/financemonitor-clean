#!/usr/bin/env python3
"""
WorkBuddy FinanceMonitor Web UI
================================
Web-based management console for FinanceMonitor.
Access at http://localhost:8080

Usage:
    python webui.py              # Web UI + background scheduler
    python webui.py --port 9090  # Custom port
    python webui.py --no-browser # Don't auto-open browser
    python webui.py --no-scheduler  # Web UI only (no scheduler)
"""

import json
import os
import sys
import subprocess
import threading
import time
import logging
import shutil
import webbrowser
import atexit
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, redirect, url_for

# ── Setup ──────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
LOG_FILE = SCRIPT_DIR / "monitor.log"
HISTORY_FILE = SCRIPT_DIR / "push_history.json"
SCHEDULER_PROCS = []   # child processes started by start_scheduler()
BOT_PROC = None        # telegram_bot.py 子程序（供 /api/bot-status 診斷）
STATUS_FILE = SCRIPT_DIR / ".webui_status.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [WEBUI] %(message)s")
logger = logging.getLogger("webui")

app = Flask(__name__)


def _ensure_port_free(port):
    """Kill any stale processes still listening on the target port (Windows only).
    Prevents zombie Flask servers from intercepting requests after unclean shutdown."""
    if sys.platform != "win32":
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace"
        )
        stdout = result.stdout or ""
        killed = 0
        for line in stdout.split("\n"):
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                if not parts:
                    continue
                pid = parts[-1]
                if not pid.isdigit():
                    continue
                # Only kill Python processes to be safe
                try:
                    proc_info = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                        capture_output=True, text=True, timeout=5,
                        encoding="utf-8", errors="replace"
                    )
                    if proc_info.stdout and "python" in proc_info.stdout.lower():
                        subprocess.run(
                            ["taskkill", "/F", "/PID", pid],
                            capture_output=True, timeout=5
                        )
                        killed += 1
                        logger.info(f"Killed stale Python process PID {pid} on port {port}")
                except Exception:
                    pass
        if killed > 0:
            logger.info(f"Cleaned up {killed} stale process(es) on port {port}")
            time.sleep(1)
    except Exception as e:
        logger.warning(f"Port cleanup skipped: {e}")


# ── Background task state ──────────────────────────────────────────
_task_lock = threading.Lock()
_task_running = False
_task_result = None
_task_start_time = None


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        # Render 環境：config.json 可能尚未生成，嘗試從環境變數自動生成
        logger.warning(f"config.json not found at {CONFIG_FILE} — trying to generate from env vars...")
        try:
            import subprocess
            gen_script = SCRIPT_DIR / "scripts" / "generate_config.py"
            if gen_script.exists():
                result = subprocess.run(
                    [sys.executable, str(gen_script)],
                    capture_output=True, text=True, timeout=10
                )
                logger.info(f"generate_config.py stdout: {result.stdout.strip()}")
                if result.returncode == 0 and CONFIG_FILE.exists():
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        return json.load(f)
        except Exception as e:
            logger.error(f"Failed to auto-generate config.json: {e}")
        logger.error("config.json not found and auto-generation failed — returning empty config")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"config.json parse error: {e}")
        return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_status():
    if STATUS_FILE.exists():
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_status(data):
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_push_history(limit=200):
    """讀取推播歷史（最新 limit 筆，倒序）"""
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return list(reversed(data[-limit:]))
    except Exception:
        return []


# 推播類型顯示對照
PUSH_TYPE_LABELS = {
    "stock":    ("股市",   "bi-graph-up",    "text-success"),
    "news":     ("新聞",   "bi-newspaper",   "text-info"),
    "event":    ("活動",   "bi-calendar-event", "text-warning"),
    "product":  ("商品",   "bi-bag",         "text-primary"),
    "economic": ("經濟",   "bi-bank",        "text-secondary"),
    "test":     ("測試",   "bi-send",        "text-muted"),
    "other":    ("其他",   "bi-info-circle", "text-muted"),
}


def run_monitor_task(mode="once"):
    """Run monitor in background thread."""
    global _task_running, _task_result, _task_start_time
    with _task_lock:
        if _task_running:
            return False
        _task_running = True
        _task_result = None
        _task_start_time = datetime.now()

    mode_map = {
        "once":   ("integrated_monitor.py", ["--once"]),
        "news":   ("integrated_monitor.py", ["--news-only"]),
        "events": ("integrated_monitor.py", ["--events-only"]),
        "stocks": ("integrated_monitor.py", ["--stocks-only"]),
        "product":("product_monitor.py",   ["--once"]),
    }
    script, args = mode_map.get(mode, ("integrated_monitor.py", ["--once"]))

    try:
        python = shutil.which("python3") or shutil.which("python") or sys.executable
        cmd = [python, str(SCRIPT_DIR / script)] + args
        logger.info(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True,
                                cwd=str(SCRIPT_DIR), timeout=600)
        output = result.stdout[-5000:] if result.stdout else ""
        error_out = result.stderr[-2000:] if result.stderr else ""

        with _task_lock:
            _task_result = {
                "success": result.returncode == 0,
                "mode": mode,
                "returncode": result.returncode,
                "duration": round((datetime.now() - _task_start_time).total_seconds(), 1),
                "output": output,
                "errors": error_out,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            _task_running = False

        status = load_status()
        status["last_run"] = {
            "mode": mode,
            "success": result.returncode == 0,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_status(status)

    except subprocess.TimeoutExpired:
        with _task_lock:
            _task_result = {"success": False, "mode": mode, "error": "Timeout (10 min)"}
            _task_running = False
    except Exception as e:
        with _task_lock:
            _task_result = {"success": False, "mode": mode, "error": str(e)}
            _task_running = False


# ── Template helper ────────────────────────────────────────────────
def page(page_id, body_html, **ctx):
    """Wrap body_html in the base layout using CONTENT marker."""
    combined = BASE_LAYOUT.replace("{{CONTENT}}", body_html)
    ctx["page"] = page_id
    ctx["py_version"] = sys.version.split()[0]
    ctx["work_dir"] = str(SCRIPT_DIR)
    try:
        return render_template_string(combined, **ctx)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Template error in page {page_id}: {e}\n{tb}")
        return f"<html><body><h1>500 Template Error</h1><pre>{e}\n\n{tb}</pre></body></html>", 500


# ── Base layout (CONTENT marker gets replaced per page) ────────────
BASE_LAYOUT = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WorkBuddy - FinanceMonitor</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css" rel="stylesheet">
<style>
  :root { --bg: #0f172a; --card: #1e293b; --border: #334155; --accent: #3b82f6; --green: #22c55e; --red: #ef4444; --text: #ffffff; --muted: #cbd5e1; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; }
  .card-header { background: transparent; border-bottom: 1px solid var(--border); font-weight: 600; }
  .btn { border-radius: 8px; font-weight: 500; }
  .btn-primary { background: var(--accent); border-color: var(--accent); }
  .btn-success { background: #16a34a; border-color: #16a34a; }
  .btn-outline-light { border-color: var(--border); color: var(--text); }
  .btn-outline-light:hover { background: var(--border); }
  .nav-pills .nav-link { color: var(--muted); border-radius: 8px; }
  .nav-pills .nav-link.active { background: var(--accent); color: white; }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  .status-dot.online { background: var(--green); box-shadow: 0 0 8px var(--green); }
  .status-dot.running { background: #f59e0b; box-shadow: 0 0 8px #f59e0b; animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
  .form-control, .form-select { background: #0f172a; border: 1px solid var(--border); color: var(--text); }
  .form-control:focus, .form-select:focus { background: #0f172a; border-color: var(--accent); color: var(--text); box-shadow: 0 0 0 0.2rem rgba(59,130,246,0.25); }
  .form-label { color: var(--muted); font-size: 0.875rem; }
  .log-output { background: #000; color: #10b981; font-family: 'Fira Code', monospace; font-size: 0.8rem; padding: 1rem; border-radius: 8px; max-height: 400px; overflow-y: auto; white-space: pre-wrap; }
  .toast-container { position: fixed; top: 1rem; right: 1rem; z-index: 9999; }
  .spinner-border-sm { width: 1rem; height: 1rem; }
  .hero-icon { font-size: 3rem; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }
  .tag-success { background: rgba(34,197,94,0.15); color: var(--green); }
  .tag-danger { background: rgba(239,68,68,0.15); color: var(--red); }
  .block-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 1rem; margin-bottom: 0.75rem; }
  .block-card:hover { border-color: var(--accent); }
  .text-muted { color: var(--muted) !important; }
  small { color: var(--muted); }
  td, th, label, .form-label, .card-body, .card-header, .navbar-brand, .nav-link, p, span, div { color: var(--text); }
  .block-num { font-size: 1.5rem; font-weight: 700; color: var(--accent); }
  .block-date { font-size: 0.85rem; font-weight: 700; color: var(--accent); white-space: nowrap; }
  .source-tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.7rem; background: rgba(59,130,246,0.15); color: var(--accent); margin: 1px; }
  .filter-tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.7rem; background: rgba(245,158,11,0.15); color: #f59e0b; margin: 1px; }
</style>
</head>
<body>

<nav class="navbar navbar-dark border-bottom" style="background: var(--card); border-color: var(--border) !important;">
  <div class="container-fluid px-4">
    <a class="navbar-brand fw-bold" href="/">
      <i class="bi bi-graph-up-arrow text-primary me-2"></i>WorkBuddy
    </a>
    <div class="d-flex align-items-center gap-3">
      <span class="badge bg-success">Online</span>
    </div>
  </div>
</nav>

<div class="container-fluid px-4 py-3">
  <div class="row">
    <div class="col-md-2 mb-3">
      <ul class="nav nav-pills flex-column gap-1">
        <li class="nav-item"><a class="nav-link {{'active' if page=='dashboard'}}" href="/"><i class="bi bi-speedometer2 me-2"></i>儀表板</a></li>
        <li class="nav-item"><a class="nav-link {{'active' if page=='settings'}}" href="/settings"><i class="bi bi-gear me-2"></i>設定</a></li>
        <li class="nav-item"><a class="nav-link {{'active' if page=='tasks'}}" href="/tasks"><i class="bi bi-list-check me-2"></i>任務</a></li>
        <li class="nav-item"><a class="nav-link {{'active' if page=='history'}}" href="/history"><i class="bi bi-broadcast me-2"></i>推播歷史</a></li>
      </ul>
    </div>
    <div class="col-md-10">
      {{CONTENT}}
    </div>
  </div>
</div>

<div id="toastContainer" class="toast-container"></div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
function showToast(msg, type='info') {
  const colors = {info:'bg-primary', success:'bg-success', error:'bg-danger'};
  const icons = {info:'info-circle', success:'check-circle', error:'exclamation-triangle'};
  const container = document.getElementById('toastContainer');
  const id = 't' + Date.now();
  container.innerHTML +=
    '<div id="'+id+'" class="toast align-items-center text-white '+colors[type]+' border-0 mb-2" role="alert">' +
    '<div class="d-flex"><div class="toast-body"><i class="bi bi-'+icons[type]+' me-2"></i>'+msg+'</div>' +
    '<button class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div></div>';
  new bootstrap.Toast(document.getElementById(id), {delay: 3000}).show();
}
function triggerRun(mode) {
  const btn = event.target; btn.disabled = true;
  const orig = btn.innerHTML;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Running...';
  fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:mode})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){ showToast('Started '+mode,'success'); pollTaskStatus(); }
    else showToast(d.error||'Failed','error');
  }).catch(e=>showToast('Error: '+e,'error'))
  .finally(()=>{ btn.disabled=false; btn.innerHTML=orig; });
}
function pollTaskStatus() {
  fetch('/api/status').then(r=>r.json()).then(d=>{
    if(d.task&&d.task.running){
      document.getElementById('taskStatus').innerHTML =
        '<div class="alert alert-info d-flex align-items-center mb-0">' +
        '<span class="spinner-border spinner-border-sm me-2"></span>' +
        'Running: '+d.task.mode+' ('+d.task.elapsed+'s)</div>';
      setTimeout(pollTaskStatus,3000);
    }else if(d.task&&d.task.result){
      var r=d.task.result,cls=r.success?'success':'danger';
      document.getElementById('taskStatus').innerHTML =
        '<div class="alert alert-'+cls+' mb-0">'+(r.success?'OK':'FAILED')+': '+r.mode+' ('+r.duration+'s)</div>';
      setTimeout(function(){document.getElementById('taskStatus').innerHTML='';},5000);
    }
  });
}
if(document.getElementById('taskStatus')) pollTaskStatus();
</script>
</body>
</html>
"""



# ── Routes ─────────────────────────────────────────────────────────
@app.route("/favicon.ico")
def favicon():
    """Return empty favicon to prevent 404 errors."""
    return "", 204


@app.route("/health")
def health_check():
    """Render health check endpoint — always return 200 if the server is running.
    Returning 503 causes Render to restart the service, which kills all monitor threads."""
    config_exists = CONFIG_FILE.exists()
    return jsonify({
        "status": "ok",
        "config_loaded": config_exists,
        "timestamp": datetime.now().isoformat()
    }), 200


@app.route("/")
def dashboard():
    status = load_status()
    last_run = status.get("last_run", {})
    config = load_config()

    # 如果 config.json 尚未生成，顯示等待頁面
    if not config:
        return render_template_string("""
        <!DOCTYPE html><html><head><meta charset="UTF-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>FinanceMonitor - Starting</title>
        <style>body{font-family:sans-serif;background:#1a1a2e;color:#e0e0e0;
        display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
        .box{text-align:center;padding:40px;border-radius:12px;background:#16213e;
        box-shadow:0 4px 20px rgba(0,0,0,.3)}h1{color:#4fc3f7}p{color:#90caf9}
        .dot{display:inline-block;width:10px;height:10px;border-radius:50%;
        background:#4fc3f7;margin-right:6px;animation:pulse 1.5s infinite}
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}</style></head>
        <body><div class="box"><h1>⚙️ FinanceMonitor</h1>
        <p><span class="dot"></span>系統正在初始化中，請稍候...</p>
        <p style="font-size:13px;color:#666">config.json 即將由環境變數生成</p>
        <p style="font-size:12px;color:#555">Render free instance cold start may take 30-50s</p>
        </div></body></html>
        """), 503

    news_hour = config["news"].get("daily_hour", 8)
    events_hour = config["news"].get("events_hour", 14)
    now = datetime.now()
    next_news = now.replace(hour=news_hour, minute=0, second=0)
    if next_news <= now:
        next_news += timedelta(days=1)
    next_events = now.replace(hour=events_hour, minute=0, second=0)
    if next_events <= now:
        next_events += timedelta(days=1)
    # 計算下次商品追蹤推送時間（16:00 台北時間 = UTC+8）
    next_product_str = "16:00 TAIPEI"
    try:
        from zoneinfo import ZoneInfo
        taipei_tz = ZoneInfo("Asia/Taipei")
        now_tp = datetime.now(taipei_tz)
        next_product = now_tp.replace(hour=16, minute=0, second=0)
        if next_product <= now_tp:
            next_product += timedelta(days=1)
        next_product_str = next_product.strftime("%m/%d %H:%M")
    except Exception:
        pass

    tg_enabled = config["telegram"]["enabled"]
    dc_enabled = config["discord"]["enabled"]

    body = """\
<h4 class="mb-3"><i class="bi bi-speedometer2 me-2"></i>儀表板</h4>

<div class="row g-3 mb-4">
  <div class="col-sm-6 col-md-3">
    <div class="card p-3 text-center">
      <div class="hero-icon mb-2"><i class="bi bi-telegram text-info"></i></div>
      <div class="fw-bold">Telegram</div>
      <div><span class="tag {{'tag-success' if tg_enabled else 'tag-danger'}}">{{'ON' if tg_enabled else 'OFF'}}</span></div>
    </div>
  </div>
  <div class="col-sm-6 col-md-3">
    <div class="card p-3 text-center">
      <div class="hero-icon mb-2"><i class="bi bi-discord text-primary"></i></div>
      <div class="fw-bold">Discord</div>
      <div><span class="tag {{'tag-success' if dc_enabled else 'tag-danger'}}">{{'ON' if dc_enabled else 'OFF'}}</span></div>
    </div>
  </div>
  <div class="col-sm-6 col-md-3">
    <div class="card p-3 text-center">
      <div class="hero-icon mb-2"><i class="bi bi-clock text-warning"></i></div>
      <div class="fw-bold">Next News Push</div>
      <div class="fs-5">{{next_news}}</div>
    </div>
  </div>
  <div class="col-sm-6 col-md-3">
    <div class="card p-3 text-center">
      <div class="hero-icon mb-2"><i class="bi bi-calendar-event text-info"></i></div>
      <div class="fw-bold">Next Events Push</div>
      <div class="fs-5">{{next_events}}</div>
    </div>
  </div>

<div class="row g-3 mb-4">
  <div class="col-sm-6 col-md-3">
    <div class="card p-3 text-center">
      <div class="hero-icon mb-2"><i class="bi bi-tag text-success"></i></div>
      <div class="fw-bold">Next Product Push</div>
      <div class="fs-5">{{next_product}}</div>
    </div>
  </div>
</div>
</div>

<div class="card mb-4">
  <div class="card-header"><i class="bi bi-clock-history me-2"></i>上次執行</div>
  <div class="card-body">
    {% if last_run %}
      <div class="d-flex align-items-center gap-3">
        <span class="badge {% if last_run.success %}bg-success{% else %}bg-danger{% endif %} fs-6">
          {{last_run.mode or 'once'}}
        </span>
        <span class="text-muted">{{last_run.time}}</span>
        <span class="tag {{'tag-success' if last_run.success else 'tag-danger'}}">{{'OK' if last_run.success else 'FAIL'}}</span>
      </div>
    {% else %}
      <span class="text-muted">尚未執行過</span>
    {% endif %}
  </div>
</div>

<div class="card mb-4">
  <div class="card-header"><i class="bi bi-play-circle me-2"></i>手動觸發</div>
  <div class="card-body">
    <div class="d-flex flex-wrap gap-2 mb-3">
      <button class="btn btn-primary" onclick="triggerRun('once')"><i class="bi bi-play-fill me-1"></i>完整推播</button>
      <button class="btn btn-outline-light" onclick="triggerRun('news')"><i class="bi bi-newspaper me-1"></i>僅新聞</button>
      <button class="btn btn-outline-light" onclick="triggerRun('events')"><i class="bi bi-calendar me-1"></i>僅活動</button>
      <button class="btn btn-outline-light" onclick="triggerRun('stocks')"><i class="bi bi-bar-chart me-1"></i>僅股市</button>
      <button class="btn btn-outline-success" onclick="triggerRun('product')"><i class="bi bi-tag me-1"></i>商品追蹤</button>
    </div>
    <div id="taskStatus"></div>
  </div>
</div>

<div class="card">
  <div class="card-header"><i class="bi bi-info-circle me-2"></i>系統資訊</div>
  <div class="card-body">
    <table class="table table-dark table-borderless mb-0">
      <tr><td class="text-muted" style="width:200px">排程模式</td><td>每半小時 股市監控（台股時段含台指期）+ 07:00 早鳥新聞（熱門財經／13F 官方＋媒體／美股財報／投資熱門話題）+ 08:00 財經主推播（VIP／IPO／AI 動能＋公司債＋CDS／經濟指標／財經行事曆／台指期多空指標／自訂多空評分指標）+ 14:00 活動 + 16:00 商品追蹤（雅虎拍賣 固定賣場＋雙北同類店家）+ 09:00 自動備份</td></tr>
      <tr><td class="text-muted">推送管道</td><td>Telegram Bot + Discord Webhook</td></tr>
      <tr><td class="text-muted">Python</td><td>{{py_version}}</td></tr>
      <tr><td class="text-muted">工作目錄</td><td>{{work_dir}}</td></tr>
    </table>
  </div>
</div>
"""
    return page("dashboard", body,
                next_news=next_news.strftime("%H:%M"),
                next_events=next_events.strftime("%H:%M"),
                next_product=next_product_str,
                tg_enabled=tg_enabled, dc_enabled=dc_enabled,
                last_run=last_run)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        try:
            cfg = load_config()
            cfg["telegram"]["enabled"] = request.form.get("tg_enabled") == "1"
            cfg["telegram"]["bot_token"] = request.form.get("tg_token", "").strip()
            cfg["telegram"]["chat_id"] = request.form.get("tg_chat_id", "").strip()
            cfg["discord"]["enabled"] = request.form.get("dc_enabled") == "1"
            cfg["discord"]["webhook_url"] = request.form.get("dc_main_webhook", "").strip()
            cfg["discord"]["events_webhook_url"] = request.form.get("dc_events_webhook", "").strip()
            cfg["news"]["daily_hour"] = int(request.form.get("news_hour", 8))
            cfg["news"]["events_hour"] = int(request.form.get("events_hour", 14))
            cfg["trading_hours"]["us_market"]["enabled"] = request.form.get("us_market") == "1"
            cfg["trading_hours"]["tw_market"]["enabled"] = request.form.get("tw_market") == "1"
            save_config(cfg)
            return redirect(url_for("settings", saved=1))
        except Exception as e:
            return f"<div class='alert alert-danger'>Save failed: {e}</div>", 400

    cfg = load_config()
    saved = request.args.get("saved")

    # Build cleaned config for display
    PLACEHOLDERS = {"x", "your_token_here", "your_bot_token_here", ""}
    def clean_val(v, sensitive=False):
        """Return display value; mask sensitive placeholders."""
        if v in PLACEHOLDERS:
            return "(未設定)"
        if sensitive and v:
            return v[:8] + "..." + v[-4:] if len(v) > 14 else v
        return v

    dc = {
        "tg_enabled": cfg["telegram"]["enabled"],
        "tg_token": clean_val(cfg["telegram"].get("bot_token", ""), sensitive=True),
        "tg_chat_id": clean_val(cfg["telegram"].get("chat_id", "")),
        "dc_enabled": cfg["discord"]["enabled"],
        "dc_webhook": clean_val(cfg["discord"].get("webhook_url", ""), sensitive=True),
        "dc_events_webhook": clean_val(cfg["discord"].get("events_webhook_url", ""), sensitive=True),
        "news_hour": cfg["news"].get("daily_hour", 8),
        "events_hour": cfg["news"].get("events_hour", 14),
        "us_market": cfg["trading_hours"]["us_market"]["enabled"],
        "tw_market": cfg["trading_hours"]["tw_market"]["enabled"],
    }

    body = """\
<h4 class="mb-3"><i class="bi bi-gear me-2"></i>設定</h4>
{% if saved %}
<div class="alert alert-success alert-dismissible fade show" role="alert">
  <i class="bi bi-check-circle me-2"></i>設定已儲存！
  <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
</div>
{% endif %}

<form method="POST" action="/settings">
  <div class="card mb-4">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span><i class="bi bi-telegram me-2"></i>Telegram</span>
      <div class="form-check form-switch mb-0">
        <input class="form-check-input" type="checkbox" name="tg_enabled" value="1" id="tgEnabled" {{'checked' if dc.tg_enabled}}>
        <label class="form-check-label" for="tgEnabled">啟用</label>
      </div>
    </div>
    <div class="card-body">
      <div class="mb-3">
        <label class="form-label">Bot Token</label>
        <input class="form-control" name="tg_token" value="{{dc.tg_token}}" placeholder="123456:ABC...">
        <small class="text-muted">{{dc.tg_token}}</small>
      </div>
      <div class="mb-3">
        <label class="form-label">Chat ID</label>
        <input class="form-control" name="tg_chat_id" value="{{dc.tg_chat_id}}" placeholder="906851085">
        <small class="text-muted">{{dc.tg_chat_id}}</small>
      </div>
    </div>
  </div>

  <div class="card mb-4">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span><i class="bi bi-discord me-2"></i>Discord Webhook</span>
      <div class="form-check form-switch mb-0">
        <input class="form-check-input" type="checkbox" name="dc_enabled" value="1" id="dcEnabled" {{'checked' if dc.dc_enabled}}>
        <label class="form-check-label" for="dcEnabled">啟用</label>
      </div>
    </div>
    <div class="card-body">
      <div class="mb-3">
        <label class="form-label">主要 Webhook URL（新聞/股市）</label>
        <input class="form-control" name="dc_main_webhook" value="{{dc.dc_webhook}}">
        <small class="text-muted">{{dc.dc_webhook}}</small>
      </div>
      <div class="mb-3">
        <label class="form-label">活動 Webhook URL（獨立推送）</label>
        <input class="form-control" name="dc_events_webhook" value="{{dc.dc_events_webhook}}">
        <small class="text-muted">{{dc.dc_events_webhook}}</small>
      </div>
    </div>
  </div>

  <div class="card mb-4">
    <div class="card-header"><i class="bi bi-clock me-2"></i>排程（台北時間）</div>
    <div class="card-body">
      <table class="table table-dark table-borderless mb-3">
        <thead>
          <tr><th style="width:120px">時間</th><th>任務</th><th style="width:120px">來源</th></tr>
        </thead>
        <tbody>
          <tr><td><b>每半小時</b></td><td>股市監控（美股跌幅 &gt;3% 個股 + ETF；台股監控清單 7+7 檔個股/ETF 即時成交量排行＋跌幅排行＋台指期）</td><td><span class="badge bg-secondary">監控</span></td></tr>
          <tr><td><b>{{'%02d:00' % dc.news_hour}}</b></td><td>新聞 + 經濟指標 + AI 動能觀察 + 財經行事曆 + 台指期多空指標 + 投資熱門話題 推播</td><td><span class="badge bg-secondary">監控</span></td></tr>
          <tr><td><b>{{'%02d:00' % dc.events_hour}}</b></td><td>活動推播（ICT/AI 活動，2026-06-25 啟用）</td><td><span class="badge bg-secondary">監控</span></td></tr>
          <tr><td><b>16:00</b></td><td>商品追蹤（雅虎拍賣 9 關鍵字：固定 3 賣場 + 雙北地區同類 3C 店家）</td><td><span class="badge bg-secondary">監控</span></td></tr>
          <tr><td><b>09:00</b></td><td>自動備份 logs → GitHub</td><td><span class="badge bg-info">排程器</span></td></tr>
        </tbody>
      </table>
      <div class="row g-3">
        <div class="col-sm-4">
          <label class="form-label">每日新聞推送時間</label>
          <select class="form-select" name="news_hour">
            {% for h in range(24) %}
            <option value="{{h}}" {{'selected' if h==dc.news_hour}}>{{'%02d:00' % h}} 台北時間</option>
            {% endfor %}
          </select>
          <small class="text-muted">目前：{{'%02d:00' % dc.news_hour}} 台北時間</small>
        </div>
        <div class="col-sm-4">
          <label class="form-label">每日活動推送時間</label>
          <select class="form-select" name="events_hour">
            {% for h in range(24) %}
            <option value="{{h}}" {{'selected' if h==dc.events_hour}}>{{'%02d:00' % h}} 台北時間</option>
            {% endfor %}
          </select>
          <small class="text-muted">目前：{{'%02d:00' % dc.events_hour}} 台北時間</small>
        </div>
        <div class="col-sm-4 d-flex align-items-end">
          <small class="text-muted">16:00 商品追蹤 / 09:00 備份 為固定排程</small>
        </div>
      </div>
    </div>
  </div>

  <div class="card mb-4">
    <div class="card-header"><i class="bi bi-globe2 me-2"></i>市場監控</div>
    <div class="card-body">
      <div class="row g-3">
        <div class="col-sm-6">
          <div class="form-check form-switch">
            <input class="form-check-input" type="checkbox" name="us_market" value="1" id="usMarket" {{'checked' if dc.us_market}}>
            <label class="form-check-label" for="usMarket">美股市場</label>
            <small class="text-muted d-block">目前：{{'ON' if dc.us_market else 'OFF'}}</small>
          </div>
        </div>
        <div class="col-sm-6">
          <div class="form-check form-switch">
            <input class="form-check-input" type="checkbox" name="tw_market" value="1" id="twMarket" {{'checked' if dc.tw_market}}>
            <label class="form-check-label" for="twMarket">台股市場</label>
            <small class="text-muted d-block">目前：{{'ON' if dc.tw_market else 'OFF'}}</small>
          </div>
        </div>
      </div>
    </div>
  </div>

  <button type="submit" class="btn btn-primary btn-lg px-5"><i class="bi bi-check-lg me-2"></i>儲存設定</button>
</form>
"""
    return page("settings", body, dc=dc, saved=saved)


@app.route("/history")
@app.route("/logs")  # 向後相容
def history_view():
    """推播歷史頁面"""
    records = load_push_history(limit=200)

    # 統計
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_count = sum(1 for r in records if r.get("ts", "").startswith(today_str))
    total_tg = sum(1 for r in records if r.get("tg") is True)
    total_dc = sum(1 for r in records if r.get("dc") is True)
    total_fail = sum(1 for r in records if r.get("tg") is False and r.get("dc") is False)

    # 建立表格行
    rows_html = ""
    for r in records:
        ptype = r.get("type", "other")
        label, icon, color = PUSH_TYPE_LABELS.get(ptype, PUSH_TYPE_LABELS["other"])
        ts = r.get("ts", "")
        tg = r.get("tg")
        dc = r.get("dc")
        preview = r.get("preview", "")
        msg_len = r.get("len", 0)

        # 管道狀態圖示
        tg_icon = '<i class="bi bi-check-circle-fill text-success"></i>' if tg is True else \
                  '<i class="bi bi-x-circle-fill text-danger"></i>' if tg is False else \
                  '<i class="bi bi-dash-circle text-muted"></i>'
        dc_icon = '<i class="bi bi-check-circle-fill text-success"></i>' if dc is True else \
                  '<i class="bi bi-x-circle-fill text-danger"></i>' if dc is False else \
                  '<i class="bi bi-dash-circle text-muted"></i>'

        # 時間只顯示時分秒
        ts_short = ts[11:] if len(ts) >= 19 else ts

        rows_html += f"""\
        <tr>
          <td class="text-muted" style="white-space:nowrap;font-size:0.82rem">{ts_short}</td>
          <td><span class="tag" style="background:rgba(59,130,246,0.15);color:var(--accent)"><i class="bi {icon} me-1"></i>{label}</span></td>
          <td class="text-center">{tg_icon}</td>
          <td class="text-center">{dc_icon}</td>
          <td style="max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:0.82rem" title="{preview}">{preview}</td>
          <td class="text-muted text-end" style="font-size:0.82rem">{msg_len:,}</td>
        </tr>
"""

    if not records:
        rows_html = '<tr><td colspan="6" class="text-center text-muted py-4">尚無推播記錄</td></tr>'

    body = f"""\
<h4 class="mb-3 d-flex justify-content-between align-items-center">
  <span><i class="bi bi-broadcast me-2"></i>推播歷史</span>
  <button class="btn btn-sm btn-outline-light" onclick="location.reload()"><i class="bi bi-arrow-clockwise me-1"></i>重新整理</button>
</h4>

<div class="row g-3 mb-4">
  <div class="col-sm-6 col-md-3">
    <div class="card p-3 text-center">
      <div class="hero-icon mb-2"><i class="bi bi-send-check text-info"></i></div>
      <div class="fw-bold">今日推播</div>
      <div class="fs-4">{today_count}</div>
    </div>
  </div>
  <div class="col-sm-6 col-md-3">
    <div class="card p-3 text-center">
      <div class="hero-icon mb-2"><i class="bi bi-telegram text-info"></i></div>
      <div class="fw-bold">Telegram 成功</div>
      <div class="fs-4">{total_tg}</div>
    </div>
  </div>
  <div class="col-sm-6 col-md-3">
    <div class="card p-3 text-center">
      <div class="hero-icon mb-2"><i class="bi bi-discord text-primary"></i></div>
      <div class="fw-bold">Discord 成功</div>
      <div class="fs-4">{total_dc}</div>
    </div>
  </div>
  <div class="col-sm-6 col-md-3">
    <div class="card p-3 text-center">
      <div class="hero-icon mb-2"><i class="bi bi-exclamation-triangle text-danger"></i></div>
      <div class="fw-bold">雙管道失敗</div>
      <div class="fs-4">{total_fail}</div>
    </div>
  </div>
</div>

<div class="card">
  <div class="card-header d-flex justify-content-between align-items-center">
    <span><i class="bi bi-list-ul me-2"></i>最近 {len(records)} 筆推播記錄</span>
    <select class="form-select form-select-sm" style="width:auto" id="typeFilter" onchange="filterTable()">
      <option value="">全部類型</option>
      <option value="stock">股市</option>
      <option value="news">新聞</option>
      <option value="event">活動</option>
      <option value="product">商品</option>
      <option value="economic">經濟</option>
      <option value="test">測試</option>
      <option value="other">其他</option>
    </select>
  </div>
  <div class="card-body p-0">
    <table class="table table-dark table-hover mb-0" id="historyTable">
      <thead>
        <tr>
          <th style="width:80px">時間</th>
          <th style="width:80px">類型</th>
          <th class="text-center" style="width:50px">TG</th>
          <th class="text-center" style="width:50px">DC</th>
          <th>訊息摘要</th>
          <th class="text-end" style="width:70px">字數</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>
</div>

<script>
function filterTable() {{
  const filter = document.getElementById('typeFilter').value;
  const rows = document.querySelectorAll('#historyTable tbody tr');
  rows.forEach(row => {{
    if (!filter) {{
      row.style.display = '';
    }} else {{
      const typeCell = row.cells[1]?.textContent || '';
      row.style.display = typeCell.includes(
        {{stock:'股市',news:'新聞',event:'活動',product:'商品',economic:'經濟',test:'測試',other:'其他'}}[filter] || ''
      ) ? '' : 'none';
    }}
  }});
}}
</script>
"""
    return page("history", body)


@app.route("/tasks")
def tasks_view():
    """Display current push blocks, filter conditions, and data sources."""
    cfg = load_config()
    ts = cfg.get("tickers", {})
    us_stocks = ts.get("us_stocks", [])
    us_etfs = ts.get("us_etfs", [])
    tw_stocks = ts.get("tw_stocks", [])
    tw_etfs = ts.get("tw_etfs", [])
    th = cfg.get("trading_hours", {})
    us_mkt = th.get("us_market", {})
    tw_mkt = th.get("tw_market", {})
    nw = cfg.get("news", {})
    news_hour = nw.get("daily_hour", 8)
    morning_hour = nw.get("morning_hour", 7)
    events_hour = nw.get("events_hour", 14)

    # Build block info from code constants
    news_sources = ["CNBC", "WSJ", "Bloomberg", "MarketWatch", "Yahoo Finance", "FT", "Seeking Alpha", "TechNews", "MacroMicro", "Digitimes"]
    sec_api_13f = ["Berkshire Hathaway", "ARK Invest", "Baupost Group", "Appaloosa", "Tiger Global", "Bridgewater", "Soros Fund", "Dalio", "Norges Bank (挪威主權基金)", "Temasek (新加坡淡馬錫)"]
    mag7 = ["AAPL (Apple)", "MSFT (Microsoft)", "NVDA (NVIDIA)", "GOOGL (Alphabet)", "AMZN (Amazon)", "META (Meta)", "TSLA (Tesla)"]
    vip_categories = ["川普家族（含川普本人）", "裴洛西夫婦", "國會交易（MTG/Khanna/Gottheimer/Tuberville/Green/Hagerty）", "科技名人（Musk/Gates/Thiel/Bezos/Zuckerberg/Cook/Huang）", "政商名人（Dimon/Fink/Ackman/Druckenmiller/Bloomberg）", "投資大師（Buffett/Munger/Burry/Wood/Dalio/Soros）"]
    bls_indicators = ["CPI (CUUR0000SA0)", "Core CPI (CUUR0000SA0L1E)", "PPI (WPSFD4)", "Unemployment (LNS14000000)", "Non-Farm Payrolls (CES0000000001)"]
    ict_sources = ["TechCrunch", "VentureBeat", "The Verge", "Wired", "MIT Tech Review", "Ars Technica", "AnandTech", "CNET", "Tom\u2019s Hardware", "Hacker News", "ACCUPASS (5 keywords)", "EventPlus (all)", "Huodongxing (5 keywords)", "Allevents.in (weekdays)"]

    body = f"""\
<h4 class="mb-3"><i class="bi bi-list-check me-2"></i>推播任務總覽</h4>

<div class="card mb-4">
  <div class="card-header"><i class="bi bi-clock me-2"></i>排程</div>
  <div class="card-body">
    <table class="table table-dark table-borderless mb-0">
      <tr><td class="text-muted" style="width:200px">每日早鳥推播</td><td><b>{morning_hour:02d}:00 台北時間</b> — 熱門財經新聞／13F 官方＋媒體報導／美股財報公布／投資熱門話題（2026-10-20 起自 08:00 提前）</td></tr>
      <tr><td class="text-muted">每日主推播</td><td><b>{news_hour:02d}:00 台北時間</b> — VIP交易/IPO/AI動能（含公司債＋CDS）/經濟指標/財經行事曆/台指期多空指標/自訂多空評分指標（卡片左側為各區塊啟用日期）</td></tr>
      <tr><td class="text-muted">每日活動推播</td><td><b>{events_hour:02d}:00 台北時間</b> — ICT/AI 活動（2026-06-25 啟用，未來90天）</td></tr>
      <tr><td class="text-muted">每日商品追蹤</td><td><b>16:00 台北時間</b> — 雅虎拍賣商品監控（9 關鍵字，價格 $2,000~$15,000，排除NG，刊登 7 天內，上限 30 筆）<br>　① 固定賣場 3 家：樺仔二手電腦 / 點子3C 板橋店 / US3C<br>　② 雙北地區同類店家：台北市・新北市「店鋪型 3C 賣家」（與點子3C同類），條件與固定賣場完全相同，自動納入新店家</td></tr>
      <tr><td class="text-muted">每半小時股市監控</td><td>美股跌幅&gt;3%個股+ETF；台股監控清單（7 檔個股+7 檔 ETF）MIS 即時成交量排行＋跌幅排行，附台指期漲跌與 09:30-11:30 小台損益試算（09:00 進場：多單 5 口停損 20 點／空單 2 口停損 50 點，單向最大虧損 5,000 元）</td></tr>
      <tr><td class="text-muted">每日自動備份</td><td><b>09:00 台北時間</b> — 推送 logs 到 GitHub（render_scheduler.py）</td></tr>
    </table>
  </div>
</div>

<h5 class="mb-3"><i class="bi bi-newspaper me-2"></i>新聞推播區塊</h5>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-06-25</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">熱門財經新聞（七巨頭 + OpenAI/SpaceX/Anthropic）</div>
      <div class="mb-2"><span class="source-tag">10 來源</span> <span class="filter-tag">僅限七巨頭 + OpenAI/SpaceX/Anthropic</span> <span class="filter-tag">1 週內</span> <span class="filter-tag">每來源最多 5 篇</span></div>
      <div><small class="text-muted">來源：</small> {" &bull; ".join(news_sources)}</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-06-25</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">名人金融交易動態揭露</div>
      <div class="mb-2"><span class="source-tag">Google News 查詢式來源</span> <span class="filter-tag">6 組分類查詢</span> <span class="filter-tag">近 7 天</span> <span class="filter-tag">每人上限 3 筆</span> <span class="filter-tag">排除關稅政策類</span></div>
      <div><small class="text-muted">監控對象（33 位）：</small> {" &bull; ".join(vip_categories)}</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-06-25</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">13F 官方（SEC EDGAR API）</div>
      <div class="mb-2"><span class="source-tag">SEC API</span> <span class="filter-tag">每季申報</span></div>
      <div><small class="text-muted">追蹤機構：</small> {" &bull; ".join(sec_api_13f)}</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-06-25</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">13F 媒體報導</div>
      <div class="mb-2"><span class="source-tag">CNBC/WSJ/Bloomberg/MarketWatch/FT/SeekingAlpha/MacroMicro</span> <span class="filter-tag">關鍵詞過濾</span> <span class="filter-tag">1 週內</span></div>
      <div><small class="text-muted">關鍵詞：</small> 13F, quarterly filing, fund holdings, portfolio disclosure</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-06-25</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">IPO 重要訊息</div>
      <div class="mb-2"><span class="source-tag">10 來源</span> <span class="filter-tag">關鍵字過濾</span> <span class="filter-tag">7 天去重</span> <span class="filter-tag">12h 快取</span></div>
      <div><small class="text-muted">關鍵詞：</small> IPO, initial public offering, debut, listing, direct listing, SPAC merger</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-06-25</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">美股財報公布（科技七巨頭）</div>
      <div class="mb-2"><span class="source-tag">10 來源</span> <span class="filter-tag">Mag 7 公司名</span> <span class="filter-tag">1 週內</span> <span class="filter-tag">12h 快取</span></div>
      <div><small class="text-muted">監控公司：</small> {" &bull; ".join(mag7)}</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-08-17</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">AI 動能觀察（資本支出 &amp; Token 需求）</div>
      <div class="mb-2"><span class="source-tag">10 來源</span> <span class="filter-tag">AI capex / token demand 關鍵字</span> <span class="filter-tag">1 週內</span> <span class="filter-tag">7 日去重</span></div>
      <div><small class="text-muted">追蹤企業：</small> 台積電(TSMC) &bull; AVGO &bull; CRWV &bull; AMZN &bull; MSFT &bull; GOOGL &bull; NVDA &bull; LITE</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-06-27</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">經濟指標相關新聞</div>
      <div class="mb-2"><span class="source-tag">10 媒體</span> <span class="filter-tag">CPI / PPI / 失業率 / 非農就業 / Fed 利率</span> <span class="filter-tag">一週內</span></div>
      <div><small class="text-muted">來源：</small> CNBC / WSJ / Bloomberg / MarketWatch 等財經媒體 RSS</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-06-25</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">ICT/AI 活動資訊（美/中/台，未來 90 天）</div>
      <div class="mb-2"><span class="source-tag">14 來源</span> <span class="filter-tag">未來 90 天</span> <span class="filter-tag">14 天連續推送後去重</span></div>
      <div><small class="text-muted">來源：</small> {" &bull; ".join(ict_sources)}</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-09-01</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">財經行事曆（未來 30 天，由近到遠）</div>
      <div class="mb-2"><span class="source-tag">Fed / BLS 官方日程</span> <span class="filter-tag">財報為預估日</span> <span class="filter-tag">未來 30 天</span></div>
      <div><small class="text-muted">追蹤：</small> FOMC 利率決議 &bull; CPI 物價指數 &bull; 非農就業報告</div>
      <div><small class="text-muted">財報：</small> TSMC &bull; TESLA &bull; NVIDIA &bull; MSFT &bull; AMZN &bull; GOOGL &bull; AVGO &bull; SPACEX(SPCX)（預估日，以公司公告為準）</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-10-16</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">台指期多空指標（三大法人未平倉）</div>
      <div class="mb-2"><span class="source-tag">期交所 OpenAPI</span> <span class="filter-tag">臺股期貨（TX）</span> <span class="filter-tag">盤後公布前一交易日</span></div>
      <div><small class="text-muted">內容：</small> 外資及陸資 &bull; 投信 &bull; 自營商 未平倉淨部位（口）＋ 當日進出 ＋ 三大法人合計多空判定</div>
      <div><small class="text-muted">診斷：</small> <code>/api/taifex-test</code></div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-10-22</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">台指期結算日｜本月模擬總盈虧</div>
      <div class="mb-2"><span class="source-tag">11:30 台股推播</span> <span class="filter-tag">每月第三個星期三</span> <span class="filter-tag">依 08:00 建議方向累計</span></div>
      <div><small class="text-muted">內容：</small> 當月累計模擬損益 &bull; 出單次數 &bull; 獲利／虧損次數與勝率 &bull; 做多與做空分項合計 &bull; 最佳／最差單日 &bull; 停損次數 &bull; 觀望次數</div>
      <div><small class="text-muted">診斷：</small> <code>/api/fut-pnl-test</code></div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-10-16</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">自訂多空評分指標（每日 08:00）</div>
      <div class="mb-2"><span class="source-tag">Yahoo Finance</span> <span class="source-tag">TradingView 經濟行事曆</span> <span class="source-tag">證交所 FMTQIK</span> <span class="filter-tag">每項 5/10/15 分</span> <span class="filter-tag">合計 25~75 分</span></div>
      <div><small class="text-muted">五項指標：</small> ① 最新美股表現（市值前 20 大平均漲跌；來源失效時自動改用科技七巨頭）&bull; ② 美國 CPI（實際 vs 預期）&bull; ③ 美國非農就業（實際 vs 預期）&bull; ④ 國際油價（Brent 布蘭特單日漲跌 ±3%）&bull; ⑤ 前一日台股（加權指數漲跌點數）</div>
      <div><small class="text-muted">判定：</small> 合計 &gt; 60 分 → 建議做多 🔴；&lt; 50 分 → 建議做空 🟢；50 ~ 60 分 → 觀望 ⚪</div>
      <div><small class="text-muted">比對：</small> 11:30 台股推播以台指期（TXF 對昨結算）比對方向命中率並累計近 10 次勝率</div>
      <div><small class="text-muted">診斷：</small> <code>/api/sentiment-test</code> &bull; <code>/api/state-log</code></div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-10-22</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">評分狀態跨部署持久化（免 PAT）</div>
      <div class="mb-2"><span class="source-tag">GitHub Actions</span> <span class="source-tag">raw.githubusercontent.com</span> <span class="filter-tag">每日 08:05 / 11:35 / 12:31</span></div>
      <div><small class="text-muted">問題：</small> Render 免費版容器為臨時檔案系統，<code>sentiment_index_state.json</code> 每次重新部署即清空，導致月結算總盈虧歸零</div>
      <div><small class="text-muted">機制：</small> GitHub Actions（<code>state-sync.yml</code>）每日抓 <code>/api/state-export</code> 並 commit 到 <code>state-store</code> 分支（不觸發 Render 部署）；開機時 <code>state_persist.restore()</code> 從 raw 網址讀回並<b>依日期合併（本機優先）</b>，全程不需任何 token</div>
      <div><small class="text-muted">每日日誌：</small> <code>/api/state-log?days=30</code> 逐日列出建議方向、評分、命中、模擬損益、是否停損</div>
      <div><small class="text-muted">診斷：</small> <code>/api/state-persist</code>（可加 <code>?restore=1</code> 立即還原）</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-date">📅 2026-09-30</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">投資熱門話題（Reddit 美股×AI 當日熱門前 10）</div>
      <div class="mb-2"><span class="source-tag">匿名抓取、免 API key</span> <span class="filter-tag">Reddit 當日熱門前 10 名</span> <span class="filter-tag">美股 × AI 板</span> <span class="filter-tag">標題中文化</span></div>
      <div><small class="text-muted">來源板：</small> r/wallstreetbets × r/stocks × r/investing × r/StockMarket × r/artificial × r/singularity — 各板 sort=top RSS（排序即當日熱度排名，無票數欄位），輪流合併取前 10</div>
      <div><small class="text-muted">未納入：</small> PTT（Render 資料中心 IP 遭全面 403 封鎖）• Threads（JS 登入殼、無匿名熱門端點）• X（登入牆、Nitter 已停止、API 付費）</div>
    </div>
  </div>
</div>

<h5 class="mb-3 mt-4"><i class="bi bi-bar-chart me-2"></i>股市監控</h5>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num" style="font-size:1.5rem">$</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-2">美股跌幅排行（交易時段每半小時）</div>
      <div class="row g-3">
        <div class="col-md-6">
          <div class="fw-bold mb-1"><span class="filter-tag">個股（Top 20 交易量）</span> <span class="filter-tag">跌幅 >3%</span></div>
          <div><small class="text-muted">全市場動態篩選（screener API）</small></div>
        </div>
        <div class="col-md-6">
          <div class="fw-bold mb-1"><span class="filter-tag">ETF（Top 10 交易量）</span> <span class="filter-tag">跌幅 >3%</span></div>
          <div><small class="text-muted">96 檔 ETF 監控池，依交易量動態排序</small></div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num" style="font-size:1.5rem">NT</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">台股監控（監控清單即時排行）</div>
      <div class="mb-2">
        <span class="tag {'tag-success' if tw_mkt.get('enabled') else 'tag-danger'}">{'ON' if tw_mkt.get('enabled') else 'OFF'}</span>
        <small class="text-muted ms-2">{tw_mkt.get('start_hour',9):02d}:{tw_mkt.get('start_minute',0):02d} - {tw_mkt.get('end_hour',13):02d}:{tw_mkt.get('end_minute',30):02d} (Asia/Taipei)</small>
      </div>
      <div><small class="text-muted">來源：證交所 MIS 即時行情（範圍固定為下方監控清單，量價即時、每半小時重排）</small></div>
      <div class="mt-1"><small class="text-muted">推播內容：個股/ETF 成交量排行 ＋ 跌幅排行（監控標的、跌幅 &gt;3%）＋ 台指期（09:30-11:30 附小台損益試算：多單 5 口停損 20 點、空單 2 口停損 50 點，單向最大虧損 5,000 元）；MIS 失效時退回 Yahoo/證交所 OpenAPI</small></div>
      <div class="mt-2"><small class="text-muted">監控標的（排行範圍固定於此）：</small></div>
      <div><small class="text-muted">{len(tw_stocks)} 個股：</small><br><code>{" ".join([s.replace('.TWO','').replace('.TW','') for s in tw_stocks])}</code></div>
      <div class="mt-1"><small class="text-muted">{len(tw_etfs)} ETF：</small><br><code>{" ".join([s.replace('.TWO','').replace('.TW','') for s in tw_etfs])}</code></div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num" style="font-size:1.5rem; color: var(--muted);">$</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">美股交易時段</div>
      <div class="mb-2">
        <span class="tag {'tag-success' if us_mkt.get('enabled') else 'tag-danger'}">{'ON' if us_mkt.get('enabled') else 'OFF'}</span>
        <small class="text-muted ms-2">{us_mkt.get('start_hour',9):02d}:{us_mkt.get('start_minute',30):02d} - {us_mkt.get('end_hour',16):02d}:{us_mkt.get('end_minute',0):02d} (US/Eastern)</small>
        <small class="text-muted ms-2">週一至週五</small>
      </div>
    </div>
  </div>
</div>
"""
    return page("tasks", body)


# ── API ────────────────────────────────────────────────────────────
@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json() or {}
    mode = data.get("mode", "once")
    if mode not in ("once", "news", "events", "stocks", "product"):
        return jsonify({"ok": False, "error": f"Invalid mode: {mode}"}), 400

    with _task_lock:
        if _task_running:
            return jsonify({"ok": False, "error": "A task is already running."}), 409

    t = threading.Thread(target=run_monitor_task, args=(mode,), daemon=True)
    t.start()
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/status")
def api_status():
    with _task_lock:
        elapsed = (datetime.now() - _task_start_time).total_seconds() if _task_start_time and _task_running else 0
        task_info = {
            "running": _task_running,
            "mode": _task_result["mode"] if _task_result else None,
            "result": _task_result,
            "elapsed": round(elapsed, 1),
        }
    return jsonify({"task": task_info, "last_run": load_status().get("last_run")})


@app.route("/api/logs")
@app.route("/api/push-history")
def api_push_history():
    """推播歷史 API（向後相容 /api/logs）"""
    limit = request.args.get("lines", 100, type=int)
    records = load_push_history(limit=limit)
    return jsonify({"history": records, "count": len(records)})


@app.route("/api/news-status")
def api_news_status():
    """[2026-10-21] 新聞推播 worker 診斷端點：
    回傳雙時段（07:00 早鳥／08:00 主推播）最後執行日、重試次數與結果"""
    from pathlib import Path as _P
    status_file = _P(__file__).resolve().parent / "news_worker_status.json"
    state_file = _P(__file__).resolve().parent / "monitor_state.json"
    result = {"worker_status": None, "monitor_state": None}
    try:
        if status_file.exists():
            result["worker_status"] = json.loads(status_file.read_text(encoding="utf-8"))
    except Exception as e:
        result["worker_status_error"] = str(e)
    try:
        if state_file.exists():
            result["monitor_state"] = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception as e:
        result["monitor_state_error"] = str(e)
    return jsonify(result)


@app.route("/api/state-export")
def api_state_export():
    """[2026-10-22] 匯出多空評分狀態（給 GitHub Actions 每日同步用，唯讀）

    內容為 sentiment_index_state.json 原樣（訊號 + 每日命中/損益歷史），
    不含任何憑證或個人資料。
    """
    try:
        import json as _json
        import sentiment_index as _senti
        st = _senti.load_state()
        return _json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                            "signals": st.get("signals") or {},
                            "history": st.get("history") or []},
                           ensure_ascii=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/state-log")
def api_state_log():
    """[2026-10-22] 每日建議方向／命中／模擬損益日誌（唯讀，?days=30）"""
    from flask import request as _rq
    try:
        import sentiment_index as _senti
        days = int(_rq.args.get("days", "30"))
        hist = (_senti.load_state().get("history") or [])[-days:]
        rows = []
        for h in hist:
            pnl = h.get("pnl")
            rows.append({
                "date": h.get("date"),
                "direction": h.get("direction"),
                "score": h.get("total"),
                "hit": h.get("hit"),
                "pnl": pnl,
                "pnl_text": (None if pnl is None else
                             f"{'+' if pnl >= 0 else '-'}{abs(pnl):,.0f}"),
                "stopped": bool(h.get("stopped")),
                "sim_mode": h.get("sim_mode"),
                "recorded_at": h.get("recorded_at"),
            })
        total = sum(r["pnl"] for r in rows if r["pnl"] is not None)
        return jsonify({"days": days, "count": len(rows), "total_pnl": total,
                        "records": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/state-persist")
def api_state_persist():
    """[2026-10-22] 狀態檔跨部署持久化診斷／手動同步

    參數：
      ?push=1     立即把本機狀態上傳到備份 repo
      ?restore=1  立即從備份 repo 還原（依日期合併，本機優先）
    """
    from flask import request as _rq
    try:
        import state_persist as sp
        result = {"status": sp.status()}
        if _rq.args.get("push"):
            ok, msg = sp.upload("sentiment_index_state.json",
                                reason="manual push from /api/state-persist")
            result["push"] = {"ok": ok, "msg": msg}
        if _rq.args.get("restore"):
            result["restore"] = sp.restore()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fut-pnl-test")
def api_fut_pnl_test():
    """[2026-10-22] 台指期模擬損益診斷（唯讀，不寫狀態檔）

    回傳 09:00 進場基準、當日取樣高低點，以及依使用者規格計算的多空模擬損益：
    多單 5 口停損 20 點／空單 2 口停損 50 點（小台每點 50 元，單向上限 5,000 元）。
    """
    try:
        import stock_monitor as sm_mod
        from datetime import datetime as _dt
        mon = sm_mod.StockMonitor.__new__(sm_mod.StockMonitor)
        quotes = sm_mod.StockMonitor.get_taiex_quotes(mon)
        today = _dt.now().strftime('%Y-%m-%d')
        rec = (mon._load_fut_state() or {}).get(today)
        sim = mon._simulate_fut_pnl(rec, quotes) if rec else None
        import sentiment_index as _senti
        _now = _dt.now()
        _ms = _senti.month_summary(_now.year, _now.month)
        _ms.pop('records', None)
        return jsonify({
            "date": today,
            "is_settlement_day": _senti.is_settlement_day(_now),
            "month_summary": _ms,
            "spec": {
                "cid": sm_mod.FUT_PNL_CID,
                "point_value": sm_mod.FUT_CONTRACT_SIZE[sm_mod.FUT_PNL_CID],
                "long_qty": sm_mod.FUT_PNL_QTY,
                "long_stop_pts": sm_mod.FUT_STOP_PTS_LONG,
                "short_qty": sm_mod.FUT_PNL_SHORT_QTY,
                "short_stop_pts": sm_mod.FUT_STOP_PTS_SHORT,
                "max_loss_per_side": sm_mod.FUT_MAX_LOSS,
            },
            "quotes": {k: {kk: v[kk] for kk in ("name", "last", "diff", "ref") if kk in v}
                       for k, v in (quotes or {}).items()},
            "state": rec,
            "sim": sim,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/diagnostic")
def api_diagnostic():
    """診斷端點：檢查 GraphQL API、推播設定等"""
    result = {
        "graphql_api": False,
        "graphql_error": None,
        "product_state_file": False,
        "product_state_entries": 0,
    }

    # Check GraphQL API connectivity
    try:
        import requests as _req
        test_payload = {
            "variables": {
                "spaceId": "2092115390",
                "storeId": "Y2311650079",
                "q": "test",
                "sort": "recommended",
                "hits": 1,
                "offset": 0,
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "0de637297197474cdd15b2f7635ab08d6b689d0e6e3974336d44e5d11b669782",
                }
            },
        }
        test_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
            "Origin": "https://tw.bid.yahoo.com",
            "Referer": "https://tw.bid.yahoo.com/",
        }
        r = _req.post(
            "https://graphql.ec.yahoo.com/graphql",
            json=test_payload, headers=test_headers, timeout=10,
        )
        if r.status_code == 200:
            result["graphql_api"] = True
        else:
            result["graphql_error"] = "HTTP %d" % r.status_code
    except Exception as e:
        result["graphql_error"] = str(e)[:300]

    # Check product state file
    state_path = SCRIPT_DIR / "yahoo_state.json"
    if state_path.exists():
        try:
            import json as _json
            state = _json.loads(state_path.read_text(encoding="utf-8"))
            result["product_state_file"] = True
            result["product_state_entries"] = len(state.get("sent", {}))
        except Exception:
            pass

    return jsonify(result)


@app.route("/api/translate-test")
def api_translate_test():
    """翻譯引擎診斷：確認伺服器環境各翻譯引擎的可用性（無需等待排程推播）。

    用法: /api/translate-test?q=自訂測試字串
    """
    sample = request.args.get(
        "q", "Fed approves interest rate hike, signals one more to come this year"
    )
    out = {
        "sample": sample,
        "engines": {},
        "raw": {},
        "final": None,
        "api_keys": {
            "DEEPL_API_KEY": bool(os.environ.get("DEEPL_API_KEY")),
            "AZURE_TRANSLATOR_KEY": bool(os.environ.get("AZURE_TRANSLATOR_KEY")),
            "MYMEMORY_EMAIL": bool(os.environ.get("MYMEMORY_EMAIL")),
        },
        "engine_chain": ("deepl > azure > bing > gtx > clients5 > deep_translator "
                         "> pollinations(LLM,免key) > mymemory"),
    }
    try:
        import importlib.util as _ilu
        out["zhconv"] = _ilu.find_spec("zhconv") is not None
    except Exception:
        out["zhconv"] = False

    # ── 原始 HTTP 端點測試（找出網路層失敗原因）─────────────────────
    try:
        import requests as _req
        _hdr = {"User-Agent": "Mozilla/5.0 (compatible; NewsMonitor/1.0)"}
        raw_endpoints = {
            "gtx": ("https://translate.googleapis.com/translate_a/single",
                    {"client": "gtx", "sl": "auto", "tl": "zh-TW", "dt": "t", "q": sample}),
            "clients5": ("https://clients5.google.com/translate_a/t",
                         {"client": "dict-chrome-ex", "sl": "auto", "tl": "zh-TW", "q": sample}),
            "mymemory": ("https://api.mymemory.translated.net/get",
                         {"q": sample, "langpair": "en|zh-TW"}),
            "bing_token_page": ("https://www.bing.com/translator", {}),
        }
        for name, (url, params) in raw_endpoints.items():
            try:
                r = _req.get(url, params=params, headers=_hdr, timeout=15)
                out["raw"][name] = {"status": r.status_code, "body": r.text[:200]}
            except Exception as e:
                out["raw"][name] = {"status": None, "error": "%s: %s" % (type(e).__name__, str(e)[:180])}

        # ── Bing Translator 免 key 完整流程原始測試（含 token 取得）─────
        try:
            import re as _r2
            _bs = _req.Session()
            _bs.headers.update({"User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")})
            _tp = _bs.get("https://www.bing.com/translator", timeout=20)
            _mm = _r2.search(r'params_AbusePreventionHelper\s*=\s*\[([^\]]+)\]', _tp.text)
            if _tp.status_code != 200:
                out["raw"]["bing"] = {"status": _tp.status_code,
                                      "error": "token 頁面非 200"}
            elif not _mm:
                out["raw"]["bing"] = {"status": 200,
                                      "error": "找不到 AbusePreventionHelper（頁面格式已變更）"}
            else:
                _p = [x.strip().strip('"') for x in _mm.group(1).split(',')]
                _ig = _r2.search(r'IG:"([^"]+)"', _tp.text)
                _iid = _r2.search(r'data-iid="([^"]+)"', _tp.text)
                _tr = _bs.post(
                    "https://www.bing.com/ttranslatev3?isVertical=1&&IG=%s&IID=%s" % (
                        _ig.group(1) if _ig else "",
                        _iid.group(1) if _iid else "translator.5028"),
                    data={"fromLang": "en", "text": sample, "to": "zh-Hant",
                          "token": _p[1], "key": _p[0]},
                    headers={"Referer": "https://www.bing.com/translator"}, timeout=20,
                )
                out["raw"]["bing"] = {"status": _tr.status_code, "body": _tr.text[:250]}
        except Exception as e:
            out["raw"]["bing"] = {"status": None,
                                  "error": "%s: %s" % (type(e).__name__, str(e)[:180])}

        # ══════════════════════════════════════════════════════════════
        # 免 key 翻譯候選矩陣（在伺服器實際網路上逐一實測）
        # 目的: 找出 Render 共享 IP 可用的免 key 翻譯途徑
        # ══════════════════════════════════════════════════════════════
        import time as _t2
        import urllib.parse as _up
        _deadline = _t2.time() + 48  # 總時間預算，避免端點逾時
        probe = {}
        # ?skip=a,b,c 可跳過指定項目（時間預算不足時分批探測）
        _skip = set(x.strip() for x in request.args.get("skip", "").split(",") if x.strip())
        _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

        def _digest(r):
            """把回應壓成短摘要，方便一眼判斷成敗。"""
            body = (r.text or "").strip().replace("\n", " ")[:200]
            ok = any('\u4e00' <= c <= '\u9fff' for c in body)
            return {"status": r.status_code, "zh": ok, "body": body}

        def _probe(name, fn):
            if name in _skip:
                probe[name] = {"status": None, "error": "已指定跳過"}
                return
            if _t2.time() > _deadline:
                probe[name] = {"status": None, "error": "跳過（時間預算用盡）"}
                return
            try:
                probe[name] = _digest(fn())
            except Exception as e:
                probe[name] = {"status": None, "error": "%s: %s" % (type(e).__name__, str(e)[:110])}

        # ── 出口 IP 穩定性（判斷 token 綁 IP 類服務是否可行）────────
        _ips = []
        for _i in range(2):
            try:
                _ips.append(_req.get("https://api.ipify.org", timeout=10).text.strip())
            except Exception as _e:
                _ips.append("ERR:%s" % type(_e).__name__)
        probe["egress_ip"] = {"calls": _ips, "stable": len(set(_ips)) == 1}

        # ── Bing 變體 1: 完整 token 流程 + 齊全 headers ────────────
        def bing_full():
            s = _req.Session()
            s.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
            tp = s.get("https://www.bing.com/translator", timeout=15)
            m = _r2.search(r'params_AbusePreventionHelper\s*=\s*\[([^\]]+)\]', tp.text)
            if not m:
                raise RuntimeError("找不到 AbusePreventionHelper（頁面長度 %d）" % len(tp.text))
            p = [x.strip().strip('"') for x in m.group(1).split(',')]
            ig = _r2.search(r'IG:"([^"]+)"', tp.text)
            iid = _r2.search(r'data-iid="([^"]+)"', tp.text)
            probe["bing_meta"] = {
                "page_len": len(tp.text),
                "ig": (ig.group(1) if ig else "(無)")[:20],
                "iid": (iid.group(1) if iid else "(無)"),
                "key_len": len(p[0]), "token_len": len(p[1]),
                "cookies": len(s.cookies),
            }
            return s.post(
                "https://www.bing.com/ttranslatev3?isVertical=1&&IG=%s&IID=%s" % (
                    ig.group(1) if ig else "", iid.group(1) if iid else "translator.5028"),
                data={"fromLang": "en", "text": sample, "to": "zh-Hant",
                      "token": p[1], "key": p[0]},
                headers={"Referer": "https://www.bing.com/translator",
                         "Origin": "https://www.bing.com",
                         "Accept": "*/*",
                         "X-Requested-With": "XMLHttpRequest"},
                timeout=20)

        _probe("bing_full_headers", bing_full)

        # ── Bing 變體 2: 無 IG/IID ─────────────────────────────────
        def bing_noig():
            s = _req.Session()
            s.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
            tp = s.get("https://www.bing.com/translator", timeout=15)
            m = _r2.search(r'params_AbusePreventionHelper\s*=\s*\[([^\]]+)\]', tp.text)
            p = [x.strip().strip('"') for x in m.group(1).split(',')]
            return s.post("https://www.bing.com/ttranslatev3?isVertical=1",
                          data={"fromLang": "en", "text": sample, "to": "zh-Hant",
                                "token": p[1], "key": p[0]},
                          headers={"Referer": "https://www.bing.com/translator"}, timeout=20)

        _probe("bing_no_ig_iid", bing_noig)

        # ── Bing 變體 3: cn.bing.com ───────────────────────────────
        def bing_cn():
            s = _req.Session()
            s.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
            tp = s.get("https://cn.bing.com/translator", timeout=15)
            m = _r2.search(r'params_AbusePreventionHelper\s*=\s*\[([^\]]+)\]', tp.text)
            if not m:
                raise RuntimeError("cn.bing 無 token（HTTP %d）" % tp.status_code)
            p = [x.strip().strip('"') for x in m.group(1).split(',')]
            return s.post("https://cn.bing.com/ttranslatev3?isVertical=1",
                          data={"fromLang": "en", "text": sample, "to": "zh-Hant",
                                "token": p[1], "key": p[0]},
                          headers={"Referer": "https://cn.bing.com/translator"}, timeout=20)

        _probe("bing_cn_host", bing_cn)

        # ── Google 行動版頁面（與 translate_a/single 不同路徑）─────
        def g_mobile():
            return _req.get("https://translate.google.com/m",
                            params={"sl": "en", "tl": "zh-TW", "q": sample},
                            headers={"User-Agent": _UA}, timeout=15)

        _probe("google_mobile_page", g_mobile)

        # ── 免 key CORS 代理 → Google ──────────────────────────────
        _gturl = ("https://translate.google.com/translate_a/single?client=gtx"
                  "&sl=en&tl=zh-TW&dt=t&q=" + _up.quote(sample, safe=""))

        def ao_proxy():
            return _req.get("https://api.allorigins.win/raw", params={"url": _gturl},
                            headers={"User-Agent": _UA}, timeout=25)

        _probe("allorigins_proxy", ao_proxy)

        def ct_proxy():
            return _req.get("https://api.codetabs.com/v1/proxy", params={"quest": _gturl},
                            headers={"User-Agent": _UA}, timeout=25)

        _probe("codetabs_proxy", ct_proxy)

        # ── 免 key LLM 端點（LLM 翻譯）────────────────────────────
        def pollinations():
            pr = ("Translate this English financial headline into Traditional Chinese. "
                  "Output ONLY the translation:\n\n" + sample)
            return _req.get("https://text.pollinations.ai/" + _up.quote(pr),
                            headers={"User-Agent": _UA}, timeout=30)

        _probe("pollinations_llm", pollinations)

        # ── Yandex 行動端（免 key）────────────────────────────────
        def yandex():
            return _req.get("https://translate.yandex.net/api/v1/tr.json/translate",
                            params={"srv": "android", "format": "text",
                                    "lang": "en-zh", "text": sample},
                            headers={"User-Agent": _UA}, timeout=15)

        _probe("yandex_mobile", yandex)

        # ── Reverso（免 key）──────────────────────────────────────
        def reverso():
            return _req.post("https://api.reverso.net/translate/v1/translation",
                             json={"input": sample, "from": "eng", "to": "chi",
                                   "format": "text",
                                   "options": {"contextResults": False,
                                               "languageDetection": False,
                                               "sentenceSplitter": True}},
                             headers={"User-Agent": _UA,
                                      "Origin": "https://www.reverso.net",
                                      "Referer": "https://www.reverso.net/"}, timeout=15)

        _probe("reverso_api", reverso)

        # ── LibreTranslate 官方站（免 key 額度）────────────────────
        def libre_official():
            return _req.post("https://libretranslate.com/translate",
                             json={"q": sample, "source": "en", "target": "zh", "format": "text"},
                             headers={"User-Agent": _UA}, timeout=15)

        _probe("libretranslate_official", libre_official)

        # ── MyMemory 附 email 提額（配額綁 email 而非共享 IP）──────
        def mymemory_email():
            return _req.get("https://api.mymemory.translated.net/get",
                            params={"q": sample, "langpair": "en|zh-TW",
                                    "de": os.environ.get("MYMEMORY_EMAIL", "probe@example.com")},
                            headers={"User-Agent": _UA}, timeout=15)

        _probe("mymemory_with_email", mymemory_email)

        out["probe"] = probe

        # Azure Translator 原始測試（有設定 key 時）
        _az_key = os.environ.get("AZURE_TRANSLATOR_KEY", "").strip()
        if _az_key:
            try:
                _az_region = os.environ.get("AZURE_TRANSLATOR_REGION", "global").strip() or "global"
                r = _req.post(
                    "https://api.cognitive.microsofttranslator.com/translate",
                    params={"api-version": "3.0", "to": "zh-Hant"},
                    headers={
                        "Ocp-Apim-Subscription-Key": _az_key,
                        "Ocp-Apim-Subscription-Region": _az_region,
                        "Content-Type": "application/json",
                    },
                    json=[{"Text": sample}], timeout=20,
                )
                out["raw"]["azure"] = {
                    "status": r.status_code,
                    "region": _az_region,
                    "key_suffix": _az_key[-4:],
                    "body": r.text[:250],
                }
            except Exception as e:
                out["raw"]["azure"] = {"status": None, "error": "%s: %s" % (type(e).__name__, str(e)[:180])}
        else:
            out["raw"]["azure"] = {"status": None, "error": "未設定 AZURE_TRANSLATOR_KEY"}
    except Exception as e:
        out["raw"]["error"] = str(e)[:200]

    # ── 引擎層測試 ────────────────────────────────────────────────
    try:
        from news_monitor import NewsMonitor
        mon = NewsMonitor(str(CONFIG_FILE))
        engines = (
            ("deepl", lambda t: mon._deepl_translate(t)),
            ("azure", lambda t: mon._azure_translate(t)),
            ("bing", lambda t: mon._bing_translate(t)),
            ("gtx", lambda t: mon._google_gtx_translate(t)),
            ("clients5", lambda t: mon._google_clients5_translate(t)),
            ("deep_translator", lambda t: mon.translator.translate(t)),
            ("mymemory", lambda t: mon._fallback_translate(t)),
        )
        for name, fn in engines:
            try:
                r = fn(sample)
                r = r or ""
                entry = {"ok": bool(r and NewsMonitor._has_cjk(r)), "text": r[:150]}
                if not entry["ok"] and name == "azure" and NewsMonitor._last_azure_error:
                    entry["error"] = NewsMonitor._last_azure_error
                if not entry["ok"] and name == "bing" and NewsMonitor._last_bing_error:
                    entry["error"] = NewsMonitor._last_bing_error
                out["engines"][name] = entry
            except Exception as e:
                out["engines"][name] = {"ok": False, "error": str(e)[:200]}
        try:
            out["final"] = mon.translate_text(sample)
        except Exception as e:
            out["final"] = "ERROR: %s" % str(e)[:200]

        # ── 免 key LLM 批次翻譯實測（?llm=1 時執行，較耗時）──────────
        if request.args.get("llm") == "1":
            import time as _t3
            _sample_titles = [
                "BNY Chief Economist on Fed Rate Hike, BOJ Outlook",
                "Coeur Mining: A Buyable Q3 Dip, Strong Free Cash Flow Outlook",
                "Musk Backer Valor Hands Investors $8.5 Billion of SpaceX Stock",
                "Apple's Quality Is Tempting, But I'm Put Off By The Valuation",
                "Wall Street Breakfast With Steven Cress (Generating Alpha)",
                "Gold Holds Decline After Fed Tilts Hawkish and Raises Rates",
                "Asian Bonds Decline as Fed Hikes, Dollar Jumps: Markets Wrap",
                "Tempus AI Inc Reports Quarterly Growth, Raises Guidance",
            ]
            _t0 = _t3.time()
            try:
                _m = mon._pollinations_translate_batch(_sample_titles)
            except Exception as e:
                _m = {}
                out["llm_batch"] = {"error": "%s: %s" % (type(e).__name__, str(e)[:200])}
            _dur = _t3.time() - _t0
            out["llm_batch"] = {
                "elapsed_sec": round(_dur, 1),
                "ok_count": len(_m),
                "total": len(_sample_titles),
                "per_item_sec": round(_dur / max(len(_sample_titles), 1), 2),
                "results": {k[:45]: v[:70] for k, v in _m.items()},
                "last_error": NewsMonitor._last_polli_error,
            }
    except Exception as e:
        out["error"] = str(e)[:300]
    return jsonify(out)


@app.route("/api/ai-bond-test")
def api_ai_bond_test():
    """AI 相關公司債診斷：驗證 BondTerminal 連線與 CoreWeave 偵測。

    ?force=1 可略過 12 小時快取重新抓取。
    """
    import ai_bond_monitor
    force = request.args.get("force") == "1"
    out = {"error": None}
    try:
        data = ai_bond_monitor.fetch_ai_high_yield_bonds(force=force)
        out["source"] = data.get("source")
        out["asof"] = data.get("asof")
        out["note"] = data.get("note")
        out["errors"] = data.get("errors")
        out["companies"] = {
            t: {"name": c["name"], "max_ytw": c["max_ytw"],
                "bonds": c["bonds"]}
            for t, c in (data.get("companies") or {}).items()
        }
        out["has_coreweave"] = "CRWV" in (data.get("companies") or {})
        out["preview"] = ai_bond_monitor.format_section(data)
        # [2026-10-20] CDS 警示驗證欄位
        cds = data.get("cds") or {}
        out["cds_alerts"] = [
            {"ticker": a.get("ticker"), "bps": a.get("bps"),
             "link": a.get("link")}
            for a in (cds.get("alerts") or [])
        ]
        out["cds_errors"] = cds.get("errors")
        out["has_oracle_cds"] = any(
            a.get("ticker") == "ORCL" for a in (cds.get("alerts") or []))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return jsonify(out)


@app.route("/api/topics-test")
def api_topics_test():
    """投資熱門話題診斷：驗證伺服器環境對 Reddit 的匿名連線能力。"""
    from topics_monitor import fetch_reddit_hot
    out = {"reddit": [], "error": None}
    try:
        for it in fetch_reddit_hot(per_sub=6, top_n=10, retries=2):
            out["reddit"].append({"title": it.get("title"), "url": it.get("url"),
                                  "sub": it.get("sub")})
    except Exception as e:
        out["error"] = f"Reddit: {type(e).__name__}: {e}"
    return jsonify(out)


@app.route("/api/vip-test")
def api_vip_test():
    """名人交易診斷：驗證伺服器環境對 Google News 查詢式來源的連線能力與命中數。"""
    import feedparser as _fp
    from news_monitor import NewsMonitor
    out = {"google_news_status": None, "google_news_entries": 0,
           "matched": 0, "categories": {}, "samples": [], "error": None}
    try:
        probe = _fp.parse("https://news.google.com/rss/search?q=Pelosi+stock+disclosure"
                          "&hl=en-US&gl=US&ceid=US:en")
        out["google_news_status"] = probe.get("status")
        out["google_news_entries"] = len(probe.entries)
    except Exception as e:
        out["error"] = f"probe: {type(e).__name__}: {e}"
    try:
        nm = NewsMonitor(config_file=str(CONFIG_FILE))
        items = nm._fetch_vip_google_news()
        out["matched"] = len(items)
        cnt = {}
        for it in items:
            cnt[it["category"]] = cnt.get(it["category"], 0) + 1
        out["categories"] = cnt
        for it in sorted(items, key=lambda x: x["published"], reverse=True)[:15]:
            out["samples"].append({
                "politician": it["politician"], "category": it["category"],
                "source": it["source"],
                "published": it["published"].strftime("%Y-%m-%d %H:%M"),
                "title": it["title"],
            })
    except Exception as e:
        out["error"] = (out["error"] or "") + f" | fetch: {type(e).__name__}: {e}"
    return jsonify(out)


@app.route("/api/calendar-test")
def api_calendar_test():
    """財經行事曆診斷：驗證事件日期／時間標示是否為台灣時間。"""
    from news_monitor import NewsMonitor
    out = {"events": [], "section": "", "taipei_now": None, "error": None}
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        out["taipei_now"] = _dt.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
        nm = NewsMonitor(config_file=str(CONFIG_FILE))
        evs = nm.fetch_financial_calendar(days=30)
        for e in evs:
            out["events"].append({
                "date": e["date"], "weekday": e["weekday"],
                "category": e["category"], "title": e["title"],
                "note": e.get("note", ""), "days_left": e["days_left"],
            })
        out["section"] = nm._format_financial_calendar_section(evs)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return jsonify(out)


@app.route("/api/stock-test")
def api_stock_test():
    """台股監控診斷：驗證監控清單、MIS 即時行情命中數與排行榜內容。"""
    from stock_monitor import StockMonitor
    out = {"universe": 0, "quotes": 0, "top_stocks": [], "top_etfs": [],
           "drop_stocks": [], "error": None}
    try:
        m = StockMonitor()
        items = m._load_tw_universe()
        out["universe"] = len(items)
        quotes = m._fetch_tw_mis_quotes(items)
        out["quotes"] = len(quotes)
        vol_rank = m.rank_tw_volume(quotes)
        drop = m.rank_tw_drop(quotes)
        for s in vol_rank["stocks"]:
            out["top_stocks"].append({"code": s["code"], "name": s["name"],
                                      "vol": s["volume"], "price": s["price"],
                                      "chg": round(s["change_percent"], 2)})
        for e in vol_rank["etfs"]:
            out["top_etfs"].append({"code": e["code"], "name": e["name"],
                                    "vol": e["volume"], "price": e["price"],
                                    "chg": round(e["change_percent"], 2)})
        for s in drop["stocks"]:
            out["drop_stocks"].append({"code": s["code"], "name": s["name"],
                                       "chg": round(s["change_percent"], 2)})
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return jsonify(out)


@app.route("/api/taifex-test")
def api_taifex_test():
    """台指期多空指標診斷：三大法人臺股期貨未平倉淨部位。"""
    from news_monitor import NewsMonitor
    out = {"date": None, "weekday": None, "items": [], "total_net": None,
           "source": None, "section": "", "debug": {}, "error": None}
    try:
        nm = NewsMonitor(config_file=str(CONFIG_FILE))
        senti = nm.fetch_taifex_sentiment()
        out["date"] = senti.get("date")
        out["weekday"] = senti.get("weekday")
        out["items"] = senti.get("items", [])
        out["total_net"] = senti.get("total_net")
        out["source"] = senti.get("source")
        out["debug"] = senti.get("debug", {})
        out["error"] = senti.get("error")
        out["section"] = nm._format_taifex_sentiment_section(senti)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return jsonify(out)


@app.route("/api/taifex-probe")
def api_taifex_probe():
    """資料源連通性探測：確認各候選來源在目前環境(如 Render)的回應樣態。"""
    import requests as _rq
    targets = {
        "taifex_openapi": (
            "https://openapi.taifex.com.tw/v1/"
            "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
        ),
        "finmind_tx": (
            "https://api.finmindtrade.com/api/v4/data"
            "?dataset=TaiwanFuturesInstitutionalInvestors&data_id=TX"
        ),
        "taifex_swagger": "https://openapi.taifex.com.tw/swagger.json",
    }
    headers = {"User-Agent": "Mozilla/5.0 (compatible; WorkBuddyMonitor/1.0)"}
    out = {}
    for name, url in targets.items():
        rec = {"status": None, "ctype": None, "len": 0, "head": "", "error": None}
        try:
            r = _rq.get(url, headers=headers, timeout=30)
            rec["status"] = r.status_code
            rec["ctype"] = r.headers.get("Content-Type", "")
            rec["len"] = len(r.content)
            rec["head"] = r.text[:260]
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        out[name] = rec
    return jsonify(out)


@app.route("/api/bot-status")
def api_bot_status():
    """Telegram Bot 子程序診斷：是否被 spawn、是否仍在運行、是否啟用輪詢。"""
    info = {"version": "bot-ai-20261017", "spawned": False,
            "pid": None, "exit_code": None, "polling": None, "cmdline": None}
    p = BOT_PROC
    if p is not None:
        info["spawned"] = True
        info["pid"] = p.pid
        info["exit_code"] = p.poll()   # None = 仍在運行
        try:
            if os.name == "posix":
                with open(f"/proc/{p.pid}/cmdline", "rb") as f:
                    cmd = f.read().decode("utf-8", "replace").split("\x00")
                cmd = [c for c in cmd if c]
                info["cmdline"] = cmd
                joined = " ".join(cmd)
                info["polling"] = ("telegram_bot.py" in joined) and ("--no-bot" not in cmd)
        except Exception as e:
            info["cmdline_error"] = str(e)
    # 讀取 telegram_bot 寫入的運行狀態檔
    try:
        status_file = SCRIPT_DIR / "logs" / "bot_runtime_status.json"
        if status_file.exists():
            with open(status_file, encoding="utf-8") as f:
                info["runtime"] = json.load(f)
    except Exception as e:
        info["runtime_error"] = str(e)
    return jsonify(info)


@app.route("/api/sentiment-test")
def api_sentiment_test():
    """自訂多空評分指標診斷：五項指標分數、合計與建議方向（不寫入狀態檔）。"""
    import sentiment_index as senti_mod

    def _plain(data):
        """把 datetime 轉字串，讓診斷回應可 JSON 序列化"""
        if not isinstance(data, dict):
            return data
        out = {}
        for k, v in data.items():
            if k == "items" and isinstance(v, list):
                out[k] = [{kk: vv for kk, vv in r.items()}
                          for r in v if isinstance(r, dict)]
            elif hasattr(v, "isoformat"):
                out[k] = v.isoformat()
            else:
                out[k] = v
        return out

    out = {"run_time": None, "items": [], "total": None, "direction": None,
           "oil_spot": [], "hit_stats": {}, "section": "", "debug": {}, "error": None}
    try:
        idx = senti_mod.fetch_sentiment_index()
        out["run_time"] = idx.get("run_time")
        out["items"] = [{"key": i["key"], "name": i["name"], "score": i["score"],
                         "why": i["why"], "data": _plain(i.get("data"))}
                        for i in idx["items"]]
        out["total"] = idx.get("total")
        out["direction"] = idx.get("direction")
        out["oil_spot"] = idx.get("oil_spot") or []
        out["debug"] = idx.get("debug") or {}
        out["hit_stats"] = {k: v for k, v in senti_mod.hit_stats().items() if k != "records"}
        out["section"] = senti_mod.format_section(idx)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return jsonify(out)


# ── CLI entry ──────────────────────────────────────────────────────
def main():
    import argparse, os
    p = argparse.ArgumentParser(description="FinanceMonitor Web UI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)), help="Port (default: 8080 / Render PORT env)")
    p.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    p.add_argument("--no-scheduler", action="store_true", help="Web UI only (no background scheduler)")
    args = p.parse_args()

    # 啟動時立即生成 config.json（避免 subprocess 找不到）
    if not CONFIG_FILE.exists():
        logger.info("[Startup] config.json not found — generating from env vars...")
        try:
            gen_script = SCRIPT_DIR / "scripts" / "generate_config.py"
            if gen_script.exists():
                import subprocess as _sp
                _r = _sp.run([sys.executable, str(gen_script)],
                             capture_output=True, text=True, timeout=30)
                if _r.returncode == 0:
                    logger.info("[Startup] config.json generated successfully")
                else:
                    logger.error(f"[Startup] generate_config.py failed: {_r.stderr[-500:]}")
            else:
                logger.error(f"[Startup] generate_config.py not found at {gen_script}")
        except Exception as e:
            logger.error(f"[Startup] Failed to generate config.json: {e}")

    # 啟動診斷：顯示關鍵環境變數狀態
    import os as _os
    tg_token_ok = bool(_os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    tg_chat_ok = bool(_os.environ.get("TELEGRAM_CHAT_ID", ""))
    dc_webhook_ok = bool(_os.environ.get("DISCORD_WEBHOOK_URL", ""))
    logger.info(f"[DIAG] Env vars: TELEGRAM_BOT_TOKEN={'yes' if tg_token_ok else 'no'}, "
                f"TELEGRAM_CHAT_ID={'yes' if tg_chat_ok else 'no'}, "
                f"DISCORD_WEBHOOK_URL={'yes' if dc_webhook_ok else 'no'}, "
                f"PORT={args.port}, TZ={_os.environ.get('TZ', 'NOT SET')}, "
                f"config.json={'exists' if CONFIG_FILE.exists() else 'MISSING'}")

    if not args.no_scheduler:
        t = threading.Thread(target=start_scheduler, daemon=True)
        t.start()
        logger.info("Background scheduler started")

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    # ── Render keep-alive: ping own /health every 10 min to prevent free-tier sleep ──
    start_keepalive()


    # Register atexit handler to clean up child processes
    def cleanup_child_processes():
        "Terminate child processes started by start_scheduler()."
        for p in SCHEDULER_PROCS:
            try:
                p.terminate()
                p.wait(timeout=5)
                logger.info(f"Terminated child process (PID={p.pid})")
            except Exception as e:
                logger.warning(f"Failed to terminate child process: {e}")
    atexit.register(cleanup_child_processes)


    _ensure_port_free(args.port)

    logger.info(f"Web UI starting on http://localhost:{args.port}")
    print(f"WorkBuddy FinanceMonitor Web UI")
    print(f"  Open: http://localhost:{args.port}")
    print(f"  Press Ctrl+C to stop")
    app.run(host="0.0.0.0", port=args.port, debug=False)


def start_keepalive():
    """Keep Render free tier awake by pinging own /health endpoint every 10 minutes.
    On Render, RENDER_EXTERNAL_URL is automatically set. When running locally, this is a no-op."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        logger.info("[KeepAlive] Not on Render (RENDER_EXTERNAL_URL not set), skipping keep-alive")
        return

    import urllib.request

    def keepalive_loop():
        while True:
            try:
                url = f"{render_url.rstrip('/')}/health"
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    logger.info(f"[KeepAlive] Pinged {url} → {resp.status}")
            except Exception as e:
                logger.warning(f"[KeepAlive] Ping failed: {e}")
            time.sleep(600)  # 10 minutes

    t = threading.Thread(target=keepalive_loop, daemon=True)
    t.start()
    logger.info(f"[KeepAlive] Started — pinging {render_url}/health every 10 min")


def start_scheduler():
    """Start scheduler processes in background (non-blocking).

    Launches two background processes:
      1. telegram_bot.py  (monitor scheduling + Telegram bot polling/AI chat)
      2. scripts/render_scheduler.py (backup)
    """
    global SCHEDULER_PROCS, BOT_PROC
    python = shutil.which("python3") or shutil.which("python") or sys.executable

    # 0. [2026-10-22] 還原跨部署狀態（必須在任何 worker 啟動前完成，
    #    否則 11:30 命中紀錄／月度損益會因重新部署而歸零）
    try:
        import state_persist
        _rs = state_persist.restore()
        logger.info("State restore: %s", _rs)
    except Exception as e:
        logger.warning("State restore skipped: %s", e)

    procs = []
    # 1. Monitor scheduler + Telegram Bot (telegram_bot.py --no-webui)
    #    --no-webui: prevent circular webui.py startup (webui.py already running)
    #    2026-10-17: 移除 --no-bot，啟用 Telegram Bot 輪詢（指令 + AI 自由對話）
    #    注意：getUpdates 只能單一消費者，本機若另行手動跑 telegram_bot.py 會 409 衝突
    #    stdout/stderr inherit so monitor logs appear in Render log panel
    try:
        p1 = subprocess.Popen(
            [python, str(SCRIPT_DIR / "telegram_bot.py"), "--no-webui"],
            cwd=str(SCRIPT_DIR),
        )
        procs.append(p1)
        BOT_PROC = p1
        logger.info(f"Monitor scheduler + Telegram Bot started (PID={p1.pid})")
    except Exception as e:
        logger.error(f"Failed to start monitor scheduler: {e}")

    # 2. Backup scheduler (render_scheduler.py)
    render_sched = SCRIPT_DIR / "scripts" / "render_scheduler.py"
    if render_sched.exists():
        try:
            p2 = subprocess.Popen(
                [python, str(render_sched)],
                cwd=str(SCRIPT_DIR),
            )
            procs.append(p2)
            logger.info(f"Render scheduler started (PID={p2.pid})")
        except Exception as e:
            logger.error(f"Failed to start render scheduler: {e}")
    else:
        logger.warning(f"render_scheduler.py not found at {render_sched}")

    # Store in global variable for shutdown cleanup
    SCHEDULER_PROCS = procs


if __name__ == "__main__":
    main()
