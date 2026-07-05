# FinanceMonitor Daily Backup Script (financemonitor-clean)
# Copies core files to Desktop\WorkBuddy only if content changed (SHA256 check)
# Updated 2026-07-05: source path changed from old workspace to financemonitor-clean

$SrcDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$DstDir  = "$env:USERPROFILE\Desktop\WorkBuddy"
$LogFile = "$DstDir\backup.log"

# Core files to backup (synced with financemonitor-clean structure)
$Files = @(
    "config.json",
    "integrated_monitor.py",
    "news_monitor.py",
    "economic_monitor.py",
    "stock_monitor.py",
    "notification_sender.py",
    "telegram_bot.py",
    "product_monitor.py",
    "all_tasks.py",
    "webui.py",
    "wsgi.py",
    "requirements.txt",
    "Dockerfile",
    "render.yaml",
    "README.md",
    "scripts\create_test_logs.py",
    "scripts\daily_report.py",
    "scripts\deploy_to_render.py",
    "scripts\economic_news_push.py",
    "scripts\generate_config.py",
    "scripts\render_backup.py",
    "scripts\render_scheduler.py",
    "scripts\render_start.sh"
)

# Create destination folder if missing
if (-not (Test-Path $DstDir)) {
    New-Item -ItemType Directory -Path $DstDir -Force | Out-Null
    Write-Host "[+] Created backup directory: $DstDir" -ForegroundColor Green
}

# Counters
$Copied  = 0
$Skipped = 0
$Failed  = 0
$TimeStr = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

Write-Host ""
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "  FinanceMonitor Backup - $TimeStr" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "  Source : $SrcDir"
Write-Host "  Target : $DstDir"
Write-Host ""

$LogLines = [System.Collections.Generic.List[string]]::new()
$LogLines.Add("===== Backup $TimeStr =====")

foreach ($File in $Files) {
    $SrcPath = Join-Path $SrcDir $File
    $DstPath = Join-Path $DstDir $File

    # Ensure destination subdirectory exists (e.g. Desktop\WorkBuddy\scripts\)
    $DstParent = Split-Path $DstPath -Parent
    if ($DstParent -and (-not (Test-Path $DstParent))) {
        New-Item -ItemType Directory -Path $DstParent -Force | Out-Null
    }

    if (-not (Test-Path $SrcPath)) {
        Write-Host "  [SKIP] $File (source not found)" -ForegroundColor DarkGray
        $LogLines.Add("SKIP    $File (source not found)")
        $Skipped++
        continue
    }

    $SrcHash = (Get-FileHash -Path $SrcPath -Algorithm SHA256).Hash

    if (Test-Path $DstPath) {
        $DstHash = (Get-FileHash -Path $DstPath -Algorithm SHA256).Hash
        if ($SrcHash -eq $DstHash) {
            Write-Host "  [=]    $File (unchanged, skip)" -ForegroundColor DarkGray
            $LogLines.Add("NO_CHG  $File")
            $Skipped++
            continue
        }
    }

    try {
        Copy-Item -Path $SrcPath -Destination $DstPath -Force
        Write-Host "  [OK]   $File" -ForegroundColor Green
        $LogLines.Add("COPIED  $File")
        $Copied++
    } catch {
        Write-Host "  [ERR]  $File : $_" -ForegroundColor Red
        $LogLines.Add("ERROR   $File : $_")
        $Failed++
    }
}

# Write log
$LogLines.Add("RESULT  copied=$Copied skipped=$Skipped failed=$Failed")
$LogLines.Add("")
$LogLines | Out-File -FilePath $LogFile -Encoding UTF8 -Append

Write-Host ""
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "  Done! Copied=$Copied  Skipped=$Skipped  Failed=$Failed" -ForegroundColor Cyan
Write-Host "  Log: $LogFile" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host ""
