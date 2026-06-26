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
SCHEDULER_PROCS = []   # child processes started by start_scheduler()
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
        <li class="nav-item"><a class="nav-link {{'active' if page=='report'}}" href="/report"><i class="bi bi-file-earmark-bar-graph me-2"></i>每日報告</a></li>
        <li class="nav-item"><a class="nav-link {{'active' if page=='settings'}}" href="/settings"><i class="bi bi-gear me-2"></i>設定</a></li>
        <li class="nav-item"><a class="nav-link {{'active' if page=='tasks'}}" href="/tasks"><i class="bi bi-list-check me-2"></i>任務</a></li>
        <li class="nav-item"><a class="nav-link {{'active' if page=='logs'}}" href="/logs"><i class="bi bi-journal-text me-2"></i>日誌</a></li>
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
    """Render health check endpoint — does not require config.json"""
    config_exists = CONFIG_FILE.exists()
    return jsonify({
        "status": "ok",
        "config_loaded": config_exists,
        "timestamp": datetime.now().isoformat()
    }), 200 if config_exists else 503


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
      <tr><td class="text-muted" style="width:200px">排程模式</td><td>每日 08:00 ET 新聞 + 14:00 ET 活動 + 16:00 商品追蹤 + 每半小時股市</td></tr>
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
    <div class="card-header"><i class="bi bi-clock me-2"></i>排程（ET 美東時間）</div>
    <div class="card-body">
      <div class="row g-3">
        <div class="col-sm-4">
          <label class="form-label">每日新聞推送時間</label>
          <select class="form-select" name="news_hour">
            {% for h in range(24) %}
            <option value="{{h}}" {{'selected' if h==dc.news_hour}}>{{'%02d:00' % h}} ET</option>
            {% endfor %}
          </select>
          <small class="text-muted">目前：{{'%02d:00' % dc.news_hour}} ET</small>
        </div>
        <div class="col-sm-4">
          <label class="form-label">每日活動推送時間</label>
          <select class="form-select" name="events_hour">
            {% for h in range(24) %}
            <option value="{{h}}" {{'selected' if h==dc.events_hour}}>{{'%02d:00' % h}} ET</option>
            {% endfor %}
          </select>
          <small class="text-muted">目前：{{'%02d:00' % dc.events_hour}} ET</small>
        </div>
        <div class="col-sm-4 d-flex align-items-end">
          <small class="text-muted">每半小時股市監控自動運行</small>
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


@app.route("/logs")
def logs_view():
    log_lines = []
    if LOG_FILE.exists():
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                log_lines = f.readlines()[-200:]
        except Exception:
            log_lines = ["Failed to read log file."]
    else:
        log_lines = ["No log file found yet."]
    log_text = "".join(log_lines)

    body = """\
<h4 class="mb-3 d-flex justify-content-between align-items-center">
  <span><i class="bi bi-journal-text me-2"></i>執行日誌</span>
  <button class="btn btn-sm btn-outline-light" onclick="location.reload()"><i class="bi bi-arrow-clockwise me-1"></i>重新整理</button>
</h4>
<div class="card">
  <div class="card-body p-0">
    <div class="log-output" id="logBox">{{log_text}}</div>
  </div>
</div>
"""
    return page("logs", body, log_text=log_text)


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
    events_hour = nw.get("events_hour", 14)

    # Build block info from code constants
    news_sources = ["CNBC", "WSJ", "Bloomberg", "MarketWatch", "Yahoo Finance", "FT", "Seeking Alpha", "TechNews", "MacroMicro", "Digitimes"]
    sec_api_13f = ["Berkshire Hathaway", "ARK Invest", "Baupost Group", "Appaloosa", "Tiger Global", "Bridgewater", "Soros Fund", "Dalio"]
    mag7 = ["AAPL (Apple)", "MSFT (Microsoft)", "NVDA (NVIDIA)", "GOOGL (Alphabet)", "AMZN (Amazon)", "META (Meta)", "TSLA (Tesla)"]
    vip_categories = ["Trump family", "Pelosi", "Musk/Gates/Bezos/Zuck/Huang/Cook", "Buffett/Druckenmiller/Dalio/Burry"]
    bls_indicators = ["CPI (CUUR0000SA0)", "Core CPI (CUUR0000SA0L1E)", "PPI (WPSFD4)", "Unemployment (LNS14000000)", "Non-Farm Payrolls (CES0000000001)"]
    ict_sources = ["TechCrunch", "VentureBeat", "The Verge", "Wired", "MIT Tech Review", "Ars Technica", "AnandTech", "CNET", "Tom\u2019s Hardware", "Hacker News", "ACCUPASS (5 keywords)", "EventPlus (all)", "Huodongxing (5 keywords)", "Allevents.in (weekdays)"]

    body = f"""\
<h4 class="mb-3"><i class="bi bi-list-check me-2"></i>推播任務總覽</h4>

<div class="card mb-4">
  <div class="card-header"><i class="bi bi-clock me-2"></i>排程</div>
  <div class="card-body">
    <table class="table table-dark table-borderless mb-0">
      <tr><td class="text-muted" style="width:200px">每日完整推播</td><td><b>{news_hour:02d}:00 ET</b> — 區塊①~⑩（新聞/13F/Form4/IPO/財報/BLS）</td></tr>
      <tr><td class="text-muted">每日活動推播</td><td><b>{events_hour:02d}:00 ET</b> — 區塊⑪（ICT/AI 活動，未來90天）</td></tr>
      <tr><td class="text-muted">每日商品追蹤</td><td><b>16:00 台北時間</b> — 雅虎拍賣商品監控（9 關鍵字，價格 $2,000~$15,000，排除NG）</td></tr>
      <tr><td class="text-muted">每半小時股市監控</td><td>美股交易時段自動執行（跌幅>3%個股+ETF）</td></tr>
    </table>
  </div>
</div>

<h5 class="mb-3"><i class="bi bi-newspaper me-2"></i>新聞推播區塊（①~⑪）</h5>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num">①</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">熱門財經新聞（七巨頭 + OpenAI/SpaceX/Anthropic）</div>
      <div class="mb-2"><span class="source-tag">10 來源</span> <span class="filter-tag">僅限七巨頭 + OpenAI/SpaceX/Anthropic</span> <span class="filter-tag">1 週內</span> <span class="filter-tag">每來源最多 5 篇</span></div>
      <div><small class="text-muted">來源：</small> {" &bull; ".join(news_sources)}</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num">③</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">VIP 交易揭露</div>
      <div class="mb-2"><span class="source-tag">同上 10 來源</span> <span class="filter-tag">關鍵字過濾</span> <span class="filter-tag">1 週內</span></div>
      <div><small class="text-muted">監控對象：</small> {" &bull; ".join(vip_categories)}</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num">④-A</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">13F 官方（SEC EDGAR API）</div>
      <div class="mb-2"><span class="source-tag">SEC API</span> <span class="filter-tag">每季申報</span></div>
      <div><small class="text-muted">追蹤機構：</small> {" &bull; ".join(sec_api_13f)}</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num">④-B</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">13F 媒體報導</div>
      <div class="mb-2"><span class="source-tag">CNBC/WSJ/Bloomberg/MarketWatch/FT/SeekingAlpha/MacroMicro</span> <span class="filter-tag">關鍵詞過濾</span> <span class="filter-tag">1 週內</span></div>
      <div><small class="text-muted">關鍵詞：</small> 13F, quarterly filing, fund holdings, portfolio disclosure</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num">⑤-A</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">SEC Form 4 官方（高管持股異動）</div>
      <div class="mb-2"><span class="source-tag">SEC EDGAR Atom</span> <span class="filter-tag">CEO/CFO 職位</span> <span class="filter-tag">12h 快取</span></div>
      <div><small class="text-muted">監控公司：</small> {" &bull; ".join(mag7)}</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num">⑤-B</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">Form 4 媒體報導</div>
      <div class="mb-2"><span class="source-tag">CNBC/WSJ/Bloomberg/MarketWatch/FT/SeekingAlpha</span> <span class="filter-tag">CEO/CFO 關鍵字</span></div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num">⑧</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">IPO 重要訊息</div>
      <div class="mb-2"><span class="source-tag">10 來源</span> <span class="filter-tag">關鍵字過濾</span> <span class="filter-tag">7 天去重</span> <span class="filter-tag">12h 快取</span></div>
      <div><small class="text-muted">關鍵詞：</small> IPO, initial public offering, debut, listing, direct listing, SPAC merger</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num">⑨</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">美股財報公布（科技七巨頭）</div>
      <div class="mb-2"><span class="source-tag">10 來源</span> <span class="filter-tag">Mag 7 公司名</span> <span class="filter-tag">1 週內</span> <span class="filter-tag">12h 快取</span></div>
      <div><small class="text-muted">監控公司：</small> {" &bull; ".join(mag7)}</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num">⑩</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">經濟指標相關新聞</div>
      <div class="mb-2"><span class="source-tag">10 媒體</span> <span class="filter-tag">CPI / PPI / 失業率 / 非農就業 / Fed 利率</span> <span class="filter-tag">一週內</span></div>
      <div><small class="text-muted">涵蓋：</small> BLS / Fed / ECB / BOJ / BOE 等官方機構</div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num">⑪</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">ICT/AI 活動資訊（美/中/台，未來 90 天）</div>
      <div class="mb-2"><span class="source-tag">14 來源</span> <span class="filter-tag">未來 90 天</span> <span class="filter-tag">14 天連續推送後去重</span></div>
      <div><small class="text-muted">來源：</small> {" &bull; ".join(ict_sources)}</div>
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
          <div><small class="text-muted">{len(us_stocks)} 追蹤：</small><br><code>{" ".join(us_stocks)}</code></div>
        </div>
        <div class="col-md-6">
          <div class="fw-bold mb-1"><span class="filter-tag">ETF（Top 10 交易量）</span> <span class="filter-tag">跌幅 >3%</span></div>
          <div><small class="text-muted">{len(us_etfs)} 追蹤：</small><br><code>{" ".join(us_etfs)}</code></div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="block-card">
  <div class="d-flex align-items-start gap-3">
    <span class="block-num" style="font-size:1.5rem">NT</span>
    <div class="flex-grow-1">
      <div class="fw-bold mb-1">台股監控</div>
      <div class="mb-2">
        <span class="tag {'tag-success' if tw_mkt.get('enabled') else 'tag-danger'}">{'ON' if tw_mkt.get('enabled') else 'OFF'}</span>
        <small class="text-muted ms-2">{tw_mkt.get('start_hour',9):02d}:{tw_mkt.get('start_minute',0):02d} - {tw_mkt.get('end_hour',13):02d}:{tw_mkt.get('end_minute',30):02d} (Asia/Taipei)</small>
      </div>
      <div><small class="text-muted">{len(tw_stocks)} 個股：</small><br><code>{" ".join([s.replace('.TW','') for s in tw_stocks])}</code></div>
      <div class="mt-2"><small class="text-muted">{len(tw_etfs)} ETF：</small><br><code>{" ".join([s.replace('.TW','') for s in tw_etfs])}</code></div>
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


# ── Report route ────────────────────────────────────────────────────
@app.route("/report")
def report_view():
    """Display the latest daily report."""
    import glob
    
    # Find the latest report file
    report_pattern = str(SCRIPT_DIR / "scripts" / "report_*.html")
    report_files = glob.glob(report_pattern)
    
    if not report_files:
        body = """\
<h4 class="mb-3"><i class="bi bi-file-earmark-bar-graph me-2"></i>每日報告</h4>
<div class="alert alert-warning">
  <i class="bi bi-exclamation-triangle me-2"></i>
  尚未生成任何報告。請先執行每日報告生成任務。
</div>
<button class="btn btn-primary" onclick="generateReport()">
  <i class="bi bi-play-fill me-1"></i>立即生成報告
</button>
<script>
function generateReport() {
  const btn = event.target;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>生成中...';
  fetch('/api/generate-report', {method:'POST'})
    .then(r=>r.json()).then(d=>{
      if(d.ok){ 
        showToast('報告生成中,請稍候...', 'success');
        setTimeout(()=>location.reload(), 5000);
      } else {
        showToast(d.error||'生成失敗', 'error');
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-play-fill me-1"></i>立即生成報告';
      }
    }).catch(e=>{
      showToast('Error: '+e, 'error');
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-play-fill me-1"></i>立即生成報告';
    });
}
</script>
"""
        return page("report", body)
    
    # Sort by modification time (newest first)
    report_files.sort(key=os.path.getmtime, reverse=True)
    latest_report = report_files[0]
    
    # Read the report HTML
    try:
        with open(latest_report, "r", encoding="utf-8") as f:
            report_html = f.read()
        
        # Extract the report date from filename
        report_date = os.path.basename(latest_report).replace("report_", "").replace(".html", "")
        
        # Process report_html to escape backticks for JavaScript
        report_html_js = report_html.replace('`', '\\`').replace('${', '\\${')
        
        # Display the report in an iframe
        body = f"""\
<h4 class="mb-3 d-flex justify-content-between align-items-center">
  <span><i class="bi bi-file-earmark-bar-graph me-2"></i>每日報告</span>
  <div>
    <span class="badge bg-info me-2">報告日期: {report_date}</span>
    <button class="btn btn-sm btn-outline-light me-2" onclick="location.reload()">
      <i class="bi bi-arrow-clockwise me-1"></i>重新整理
    </button>
    <button class="btn btn-sm btn-primary" onclick="generateReport()">
      <i class="bi bi-play-fill me-1"></i>重新生成
    </button>
  </div>
</h4>
<div class="card">
  <div class="card-body p-3">
    <iframe id="reportFrame" style="width:100%; height:800px; border:1px solid var(--border); border-radius:8px;"></iframe>
  </div>
</div>
<script>
document.getElementById('reportFrame').srcdoc = `{report_html_js}`;

function generateReport() {{
  const btn = event.target;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>生成中...';
  fetch('/api/generate-report', {{method:'POST'}})
    .then(r=>r.json()).then(d=>{{
      if(d.ok){{ 
        showToast('報告生成中,請稍候...', 'success');
        setTimeout(()=>location.reload(), 5000);
      }} else {{
        showToast(d.error||'生成失敗', 'error');
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-play-fill me-1"></i>重新生成';
      }}
    }}).catch(e=>{{
      showToast('Error: '+e, 'error');
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-play-fill me-1"></i>重新生成';
    }});
}}
</script>
"""
        return page("report", body)
    except Exception as e:
        body = f"""\
<h4 class="mb-3"><i class="bi bi-file-earmark-bar-graph me-2"></i>每日報告</h4>
<div class="alert alert-danger">
  <i class="bi bi-exclamation-triangle me-2"></i>
  讀取報告失敗: {str(e)}
</div>
"""
        return page("report", body)


# ── API ────────────────────────────────────────────────────────────
@app.route("/api/generate-report", methods=["POST"])
def api_generate_report():
    """Trigger daily report generation."""
    def run_report():
        try:
            python = shutil.which("python3") or shutil.which("python") or sys.executable
            cmd = [python, str(SCRIPT_DIR / "scripts" / "daily_report.py")]
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SCRIPT_DIR), timeout=300)
            return result.returncode == 0
        except Exception as e:
            print(f"Report generation failed: {e}")
            return False
    
    t = threading.Thread(target=run_report, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Report generation started"})



def api_run():
    data = request.get_json() or {}
    mode = data.get("mode", "once")
    if mode not in ("once", "news", "events", "stocks"):
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
def api_logs():
    lines = request.args.get("lines", 100, type=int)
    if LOG_FILE.exists():
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return jsonify({"log": "".join(all_lines[-lines:])})
    return jsonify({"log": ""})


# ── CLI entry ──────────────────────────────────────────────────────
def main():
    import argparse, os
    p = argparse.ArgumentParser(description="FinanceMonitor Web UI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)), help="Port (default: 8080 / Render PORT env)")
    p.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    p.add_argument("--no-scheduler", action="store_true", help="Web UI only (no background scheduler)")
    args = p.parse_args()

    if not args.no_scheduler:
        t = threading.Thread(target=start_scheduler, daemon=True)
        t.start()
        logger.info("Background scheduler started")

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()


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


def start_scheduler():
    """Start scheduler processes in background (non-blocking).

    Launches two background processes:
      1. telegram_bot.py --no-bot  (monitor scheduling)
      2. scripts/render_scheduler.py (daily report + backup)
    """
    global SCHEDULER_PROCS
    python = shutil.which("python3") or shutil.which("python") or sys.executable

    procs = []
    # 1. Monitor scheduler (telegram_bot.py --no-bot)
    try:
        p1 = subprocess.Popen(
            [python, str(SCRIPT_DIR / "telegram_bot.py"), "--no-bot"],
            cwd=str(SCRIPT_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p1)
        logger.info(f"Monitor scheduler started (PID={p1.pid})")
    except Exception as e:
        logger.error(f"Failed to start monitor scheduler: {e}")

    # 2. Daily report + backup scheduler (render_scheduler.py)
    render_sched = SCRIPT_DIR / "scripts" / "render_scheduler.py"
    if render_sched.exists():
        try:
            p2 = subprocess.Popen(
                [python, str(render_sched)],
                cwd=str(SCRIPT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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
