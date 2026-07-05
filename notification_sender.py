#!/usr/bin/env python3
"""
notification_sender.py - 通知發送模組（最小化實現）
提供 Telegram、Discord、Email 通知功能
所有配置從環境變數讀取
"""
import os
import logging

logger = logging.getLogger(__name__)

# ==================== NotificationSender 類別 ====================
class NotificationSender:
    """通知發送器類別"""
    
    def __init__(self, config: dict = None):
        """初始化通知發送器
        
        Args:
            config: 配置字典，包含通知相關配置
        """
        self.config = config or {}
        
        # 從 config 或環境變數讀取配置
        # config.json 結構: { "telegram": {"bot_token": "...", "chat_id": "..."}, "discord": {...} }
        tg = self.config.get("telegram", {})
        dc = self.config.get("discord", {})
        self.telegram_token = tg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = tg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.discord_webhook = dc.get("webhook_url") or os.environ.get("DISCORD_WEBHOOK_URL", "")
        self.gmail_sender = self.config.get("GMAIL_SENDER") or os.environ.get("GMAIL_SENDER", "")
        self.gmail_password = self.config.get("GMAIL_APP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD", "")
        self.recipient = self.config.get("REPORT_RECIPIENT") or os.environ.get("REPORT_RECIPIENT", "")
        
        logger.info(f"NotificationSender init: telegram={'✓' if self.telegram_token else '✗'}, "
                     f"discord={'✓' if self.discord_webhook else '✗'}, "
                     f"email={'✓' if self.gmail_sender else '✗'}")
    
    def send_telegram(self, message: str, parse_mode: str = "HTML") -> bool:
        """發送 Telegram 消息"""
        return send_telegram_message(message, parse_mode,
                                      bot_token=self.telegram_token,
                                      chat_id=self.telegram_chat_id)
    
    def send_discord(self, message: str) -> bool:
        """發送 Discord 消息"""
        return send_discord_message(message, webhook_url=self.discord_webhook)
    
    def send_email(self, subject: str, body: str, recipient: str = None) -> bool:
        """發送 Email"""
        return send_email(subject, body, recipient)
    
    def send_all(self, message: str, subject: str = None) -> dict:
        """發送通知到所有渠道"""
        return send_notification(message, subject,
                                 bot_token=self.telegram_token,
                                 chat_id=self.telegram_chat_id,
                                 webhook_url=self.discord_webhook)
    
    def send_to_all(self, message: str, subject: str = None) -> dict:
        """發送通知到所有渠道（別名）"""
        return self.send_all(message, subject)
    
    def send_message(self, message: str, subject: str = None, methods: list = None) -> dict:
        """發送消息（通用方法）"""
        return send_notification(message, subject, methods,
                                 bot_token=self.telegram_token,
                                 chat_id=self.telegram_chat_id,
                                 webhook_url=self.discord_webhook)

# ==================== Telegram 通知 ====================
def send_telegram_message(message: str, parse_mode: str = "HTML",
                          bot_token: str = None, chat_id: str = None) -> bool:
    """發送 Telegram 消息
    
    Args:
        message: 消息內容
        parse_mode: 解析模式（HTML/Markdown）
        bot_token: Bot Token（可選，預設從環境變數讀取）
        chat_id: Chat ID（可選，預設從環境變數讀取）
    """
    try:
        import telegram
        
        bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        
        if not bot_token or not chat_id:
            logger.warning("Telegram 配置未設置，跳過發送")
            return False
        
        bot = telegram.Bot(token=bot_token)
        bot.send_message(chat_id=chat_id, text=message, parse_mode=parse_mode)
        logger.info("Telegram 消息已發送")
        return True
    except ImportError:
        logger.warning("python-telegram-bot 未安裝，無法發送 Telegram 消息")
        return False
    except Exception as e:
        logger.error(f"發送 Telegram 消息失敗: {e}")
        return False

# ==================== Discord 通知 ====================
def send_discord_message(message: str, webhook_url: str = None) -> bool:
    """發送 Discord 消息"""
    try:
        import requests
        
        if not webhook_url:
            webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
        
        if not webhook_url:
            logger.warning("Discord Webhook URL 未設置，跳過發送")
            return False
        
        data = {"content": message}
        response = requests.post(webhook_url, json=data, timeout=10)
        
        if response.status_code == 204:
            logger.info("Discord 消息已發送")
            return True
        else:
            logger.error(f"Discord 發送失敗: {response.status_code}")
            return False
    except ImportError:
        logger.warning("requests 未安裝，無法發送 Discord 消息")
        return False
    except Exception as e:
        logger.error(f"發送 Discord 消息失敗: {e}")
        return False

# ==================== Email 通知 ====================
def send_email(subject: str, body: str, recipient: str = None) -> bool:
    """發送 Email"""
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        sender = os.environ.get("GMAIL_SENDER", "")
        password = os.environ.get("GMAIL_APP_PASSWORD", "")
        recipient = recipient or os.environ.get("REPORT_RECIPIENT", "")
        
        if not sender or not password or not recipient:
            logger.warning("Gmail 配置未設置，跳過發送")
            return False
        
        # 創建郵件
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        
        # 添加正文
        msg.attach(MIMEText(body, "plain", "utf-8"))
        
        # 發送
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        
        logger.info(f"Email 已發送到 {recipient}")
        return True
    except ImportError:
        logger.warning("smtplib 不可用，無法發送 Email")
        return False
    except Exception as e:
        logger.error(f"發送 Email 失敗: {e}")
        return False

# ==================== 通用通知 ====================
def send_notification(message: str, subject: str = None, methods: list = None,
                      bot_token: str = None, chat_id: str = None,
                      webhook_url: str = None) -> dict:
    """發送通知到所有配置的渠道
    
    Args:
        message: 消息內容
        subject: 郵件主題
        methods: 發送渠道列表
        bot_token: Telegram Bot Token（可選）
        chat_id: Telegram Chat ID（可選）
        webhook_url: Discord Webhook URL（可選）
    """
    if methods is None:
        methods = ["telegram", "discord", "email"]
    
    results = {}
    
    if "telegram" in methods:
        results["telegram"] = send_telegram_message(message, bot_token=bot_token, chat_id=chat_id)
    
    if "discord" in methods:
        results["discord"] = send_discord_message(message, webhook_url=webhook_url)
    
    if "email" in methods and subject:
        results["email"] = send_email(subject, message)
    
    return results

# ==================== 測試 ====================
if __name__ == "__main__":
    print("測試通知發送...")
    
    # 測試 Telegram
    result = send_telegram_message("測試消息")
    print(f"Telegram: {'成功' if result else '失敗'}")
    
    # 測試 Discord
    result = send_discord_message("測試消息")
    print(f"Discord: {'成功' if result else '失敗'}")
    
    # 測試 Email
    result = send_email("測試郵件", "這是測試郵件")
    print(f"Email: {'成功' if result else '失敗'}")
