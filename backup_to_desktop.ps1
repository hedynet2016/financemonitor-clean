# Backup FinanceMonitor core files to desktop
# This script copies critical files to desktop WorkBuddy backup folder

$ErrorActionPreference = "Stop"

$timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$desktop = [System.Environment]::GetFolderPath("Desktop")
$backupRoot = Join-Path $desktop "WorkBuddyBackup"
$backupDir = Join-Path $backupRoot $timestamp

# Create backup directory
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
Write-Host "[$timestamp] Starting backup to $backupDir..." -ForegroundColor Cyan

# Source directory
$sourceDir = "C:\Users\Ben\WorkBuddy\2026-06-24-10-18-32"

# Core files to backup
$coreFiles = @(
    "webui.py",
    "news_monitor.py",
    "all_tasks.py",
    "integrated_monitor.py",
    "telegram_bot.py",
    "config.json.example",
    "Dockerfile",
    "render.yaml",
    "requirements.txt",
    "scripts/daily_report.py",
    "scripts/generate_config.py"
)

# Copy core files
$copiedCount = 0
foreach ($file in $coreFiles) {
    $sourcePath = Join-Path $sourceDir $file
    if (Test-Path $sourcePath) {
        $destPath = Join-Path $backupDir $file
        $destDir = Split-Path $destPath -Parent
        if (!(Test-Path $destDir)) {
            New-Item -ItemType Directory -Force -Path $destDir | Out-Null
        }
        Copy-Item -Path $sourcePath -Destination $destPath -Force
        $copiedCount++
        Write-Host "  ✓ Copied: $file" -ForegroundColor Green
    } else {
        Write-Host "  ✗ Not found: $file" -ForegroundColor Yellow
    }
}

# Also backup .workbuddy/memory folder
$memorySource = Join-Path $sourceDir ".workbuddy\memory"
$memoryDest = Join-Path $backupDir ".workbuddy\memory"
if (Test-Path $memorySource) {
    Copy-Item -Path $memorySource -Destination $memoryDest -Recurse -Force
    Write-Host "  ✓ Copied: .workbuddy/memory/" -ForegroundColor Green
}

# Create backup manifest
$manifest = @"
FinanceMonitor Backup Manifest
==========================
Backup Time: $timestamp
Source: $sourceDir
Destination: $backupDir

Files Backed Up:
$($coreFiles | ForEach-Object { "  - $_" })

Restore Command:
  Copy-Item -Path "$backupDir\*" -Destination "$sourceDir" -Recurse -Force
"@

$manifest | Out-File -FilePath (Join-Path $backupDir "BACKUP_MANIFEST.txt") -Encoding UTF8

Write-Host "`n[$timestamp] Backup completed! $copiedCount files copied." -ForegroundColor Cyan
Write-Host "Backup location: $backupDir" -ForegroundColor Cyan
