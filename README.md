# WorkBuddy 自動化監控系統

安全版本的 WorkBuddy 自動化任務系統，所有敏感信息都從環境變數讀取。

## 功能

- **每日報告**: 生成並發送每日工作報告到指定郵箱
- **GitHub 備份**: 自動備份工作目錄到 GitHub
- **經濟監控**: 監控經濟指標變化
- **新聞監控**: 監控相關新聞
- **股票監控**: 監控股票價格變化
- **整合監控**: 整合所有監控任務

## 安裝

1. 克隆此 repo:
```bash
git clone https://github.com/hedynet2016/financemonitor-clean.git
cd financemonitor-clean
```

2. 安裝依賴:
```bash
pip install -r requirements.txt
```

3. 設置環境變數:
```bash
cp .env.example .env
# 編輯 .env 填入實際值
```

## 使用方法

### 執行所有任務一次
```bash
python all_tasks.py --once
```

### 執行單個任務
```bash
python all_tasks.py --task daily_report
python all_tasks.py --task github_backup
python all_tasks.py --task economic_monitor
```

### 列出所有任務
```bash
python all_tasks.py --list
```

### 檢查配置
```bash
python all_tasks.py --check-config
```

## 環境變數

所有敏感信息都必須通過環境變數設置，請參考 `.env.example`。

必要環境變數:
- `TELEGRAM_BOT_TOKEN`: Telegram Bot Token
- `GMAIL_SENDER`: Gmail 發件人
- `GMAIL_APP_PASSWORD`: Gmail App Password
- `GITHUB_PAT`: GitHub Personal Access Token

## 安全注意事項

- ⚠️ **永遠不要將 `.env` 文件提交到 Git**
- ⚠️ **確保 `.gitignore` 包含 `.env` 和 `config.json`**
- ⚠️ **定期檢查代碼中是否意外包含敏感信息**

## 授權

MIT License
