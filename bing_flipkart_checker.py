import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

try:
    import winsound
except ImportError:
    winsound = None

try:
    import ctypes
except ImportError:
    ctypes = None

# Configure Windows console output encoding to utf-8 if supported
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("RewardsChecker")

CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "target_reward": "Flipkart",
    "check_interval_seconds": 300,
    "rewards_url": "https://rewards.bing.com/redeem/sku/000885000034",
    "mode": "http",  # "http" is fast & direct, or "selenium"
    "headless": True,
    "notify_on_in_stock": True,
    "notify_on_out_of_stock": True,
    "notify_on_startup": True,
    "user_data_dir": "./browser_profile",
    "http_cookie": "",
    "notifications": {
        "windows_toast": True,
        "sound_alert": True,
        "modal_popup": False,
        "ntfy": {
            "enabled": True,
            "topic": "ms_rewards_flipkart_alerts"
        },
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "chat_id": ""
        },
        "discord": {
            "enabled": False,
            "webhook_url": ""
        }
    }
}

def load_or_create_config():
    config = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read {CONFIG_FILE}: {e}. Using default configuration.")
    else:
        logger.info(f"{CONFIG_FILE} not found. Using defaults and environment variables.")

    for key, val in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = val

    # Environment variables override (GitHub Secrets / Cloud hosting)
    notif = config.setdefault("notifications", {})
    tg = notif.setdefault("telegram", {})
    env_tg_token = (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
        or os.environ.get("TELEGRAM_TOKEN")
    )
    env_tg_chat = (
        os.environ.get("TELEGRAM_CHAT_ID")
        or os.environ.get("CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT")
    )
    if env_tg_token:
        tg["bot_token"] = env_tg_token.strip()
        tg["enabled"] = True
    if env_tg_chat:
        tg["chat_id"] = str(env_tg_chat).strip()

    env_ntfy_topic = os.environ.get("NTFY_TOPIC") or os.environ.get("TOPIC")
    if env_ntfy_topic:
        notif.setdefault("ntfy", {})["topic"] = env_ntfy_topic.strip()
        notif["ntfy"]["enabled"] = True

    return config

# ==========================================
# Notification Helpers
# ==========================================

def play_alert_sound(in_stock: bool = True):
    try:
        if in_stock:
            # Ascending celebratory tones
            for freq in [1000, 1200, 1500, 2000]:
                winsound.Beep(freq, 120)
                time.sleep(0.04)
        else:
            # Low tone for out-of-stock
            winsound.Beep(600, 300)
    except Exception as e:
        logger.debug(f"Audio beep failed: {e}")

def send_windows_toast(title: str, message: str):
    try:
        # Sanitize double quotes for powershell
        safe_title = title.replace('"', "'")
        safe_msg = message.replace('"', "'")
        ps_script = f'''
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $textNodes = $template.GetElementsByTagName("text")
        $textNodes.Item(0).AppendChild($template.CreateTextNode("{safe_title}")) > $null
        $textNodes.Item(1).AppendChild($template.CreateTextNode("{safe_msg}")) > $null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Microsoft Rewards Alert").Show($toast)
        '''
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True,
            timeout=10,
            check=False
        )
    except Exception as e:
        logger.warning(f"Windows Toast notification failed: {e}")

def show_modal_popup(title: str, message: str):
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x00000040 | 0x00040000)
    except Exception as e:
        logger.warning(f"Popup message box failed: {e}")

def send_ntfy_push(topic: str, title: str, message: str):
    if not topic:
        return
    try:
        url = "https://ntfy.sh"
        payload = json.dumps({
            "topic": topic,
            "title": title,
            "message": message,
            "priority": 4,
            "tags": ["tada", "gift", "shopping_cart"]
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                logger.info(f"ntfy.sh push notification sent successfully to topic: {topic}")
    except Exception as e:
        logger.error(f"ntfy.sh notification failed: {e}")

def send_telegram_alert(bot_token: str, chat_id: str, message: str):
    if not bot_token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": False
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                logger.info("Telegram notification sent successfully.")
    except Exception as e:
        logger.error(f"Telegram notification failed: {e}")

def send_discord_alert(webhook_url: str, message: str):
    if not webhook_url:
        return
    try:
        payload = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "RewardsWatcher/1.0"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                logger.info("Discord notification sent successfully.")
    except Exception as e:
        logger.error(f"Discord notification failed: {e}")

def notify_user(config: dict, status_info: dict):
    in_stock = status_info.get("in_stock", False)
    status_label = status_info.get("status", "UPDATE")
    emoji = "🎉" if in_stock else "⚠️"
    title = f"{emoji} Flipkart Gift Card: {status_label}"
    details = status_info.get("details", "")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    url = config.get("rewards_url", "https://rewards.bing.com/redeem/sku/000885000034")

    full_message = f"{title}\n{details}\nTime: {timestamp}\nLink: {url}"

    logger.info(f"\n{'='*55}\n[NOTIFICATION DISPATCHED]\n{full_message}\n{'='*55}")

    notif_cfg = config.get("notifications", {})

    # Sound alert (disabled by user request)
    # if notif_cfg.get("sound_alert", True):
    #     play_alert_sound(in_stock=in_stock)

    # Windows Toast notification (disabled by user request - only Telegram + ntfy enabled)
    # if notif_cfg.get("windows_toast", True):
    #     send_windows_toast(title, f"{details}\n{url}")

    # Modal popup (disabled)
    # if notif_cfg.get("modal_popup", False):
    #     show_modal_popup(title, full_message)

    # 1. ntfy mobile push notification
    ntfy_cfg = notif_cfg.get("ntfy", {})
    if ntfy_cfg.get("enabled", False):
        send_ntfy_push(ntfy_cfg.get("topic", "ms_rewards_flipkart_alerts"), title, full_message)

    # 2. Telegram mobile alert
    tg_cfg = notif_cfg.get("telegram", {})
    if tg_cfg.get("enabled", False):
        send_telegram_alert(tg_cfg.get("bot_token"), tg_cfg.get("chat_id"), f"{title}\n\n{details}\n\nRedeem now: {url}")

    # Discord webhook (disabled)
    # dc_cfg = notif_cfg.get("discord", {})
    # if dc_cfg.get("enabled", False):
    #     send_discord_alert(dc_cfg.get("webhook_url"), f"**{title}**\n{details}\n<{url}>")

# ==========================================
# Stock Parsing Engine
# ==========================================

def parse_rewards_page(html: str) -> dict:
    """Parses HTML from Microsoft Rewards (supports both SKU page and Redeem catalog)."""
    # 1. Check for specific SKU JSON object embedded by Microsoft Rewards
    # Matches both escaped (\"id\": \"...\") and unescaped ("id": "...")
    sku_pattern = r'\{\\?"id\\?":\s*\\?"(0008[0-9]+)\\?",\s*\\?"title\\?":\s*\\?"Flipkart Gift Card\\?"[^\r\n]+?\}'
    sku_match = re.search(sku_pattern, html)
    if sku_match:
        try:
            raw = sku_match.group(0)
            raw = raw.replace(r'\"', '"').replace(r'\\', '\\')
            data = json.loads(raw)
            title = data.get("fullTitle") or data.get("title") or "Flipkart Gift Card"
            is_disabled = data.get("isDisabled", True)
            price = data.get("price")
            price_str = f" ({price:,} points)" if price else ""

            if not is_disabled:
                return {
                    "in_stock": True,
                    "status": "IN STOCK",
                    "details": f"🎉 {title}{price_str} is IN STOCK and ready to redeem!"
                }
            else:
                return {
                    "in_stock": False,
                    "status": "OUT OF STOCK",
                    "details": f"{title}{price_str} is currently Out of Stock (So popular we're restocking!)."
                }
        except Exception as e:
            logger.debug(f"JSON SKU parsing error: {e}")

    # 2. Check for explicit disabled state in JSON (unquoted or quoted)
    if re.search(r'\\?"isDisabled\\?"\s*:\s*true', html, re.I):
        return {
            "in_stock": False,
            "status": "OUT OF STOCK",
            "details": "Flipkart Gift Card is marked Out of Stock (isDisabled: true)."
        }

    # 3. Check for disabled button tag in rendered HTML (e.g. <button disabled="" data-disabled="true">...Redeem now...</button>)
    if re.search(r'<button[^>]+(?:disabled=""|data-disabled="true")[^>]*>.*?redeem.*?</button>', html, re.I | re.DOTALL):
        return {
            "in_stock": False,
            "status": "OUT OF STOCK",
            "details": "Flipkart Gift Card Redeem button is currently disabled."
        }

    # 4. Check for common Microsoft Rewards out-of-stock banners
    html_lower = html.lower()
    restocking_phrases = [
        "so popular we're restocking",
        "temporarily out of stock",
        "out of stock",
        "restocking soon",
        "temporarily unavailable"
    ]
    for phrase in restocking_phrases:
        if phrase in html_lower:
            return {
                "in_stock": False,
                "status": "OUT OF STOCK",
                "details": f"Flipkart Gift Card is currently Out of Stock (Status: '{phrase.title()}')."
            }

    # 5. Check for active redeem button without disabled attributes
    if "redeem now" in html_lower or "redeem reward" in html_lower:
        if 'disabled=""' not in html_lower and 'data-disabled="true"' not in html_lower:
            return {
                "in_stock": True,
                "status": "IN STOCK",
                "details": "🎉 Flipkart Gift Card Redeem button is ACTIVE and ready to claim!"
            }

    # 6. Check if Flipkart is found at all
    if "flipkart" in html_lower:
        return {
            "in_stock": True,
            "status": "Available",
            "details": "Flipkart Gift Card found without out-of-stock indicators."
        }

    return {
        "in_stock": False,
        "status": "Not Found",
        "details": "Flipkart Gift Card was not found on the page (may be unlisted or region-locked)."
    }

# ==========================================
# Checker Implementations
# ==========================================

def check_stock_http(config: dict) -> dict:
    url = config.get("rewards_url", "https://rewards.bing.com/redeem/sku/000885000034")
    cookie = config.get("http_cookie", "")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://rewards.bing.com/"
    }
    if cookie:
        headers["Cookie"] = cookie

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="ignore")

            if "login.live.com" in resp.geturl():
                return {
                    "in_stock": False,
                    "status": "Login Required",
                    "details": "Redirected to login page. Please provide Cookie or use 'selenium' mode."
                }

            return parse_rewards_page(content)

    except urllib.error.HTTPError as e:
        return {"in_stock": False, "status": f"HTTP Error {e.code}", "details": f"HTTP request failed: {e}"}
    except Exception as e:
        return {"in_stock": False, "status": "Request Failed", "details": str(e)}

def check_stock_selenium(config: dict) -> dict:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    user_data = os.path.abspath(config.get("user_data_dir", "./browser_profile"))
    headless = config.get("headless", True)
    url = config.get("rewards_url", "https://rewards.bing.com/redeem/sku/000885000034")

    driver = None
    try:
        edge_opts = EdgeOptions()
        if headless:
            edge_opts.add_argument("--headless=new")
        edge_opts.add_argument(f"--user-data-dir={user_data}")
        edge_opts.add_argument("--disable-gpu")
        edge_opts.add_argument("--no-sandbox")
        edge_opts.add_argument("--disable-dev-shm-usage")
        edge_opts.add_argument("--disable-blink-features=AutomationControlled")
        edge_opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        edge_opts.add_experimental_option("useAutomationExtension", False)
        
        driver = webdriver.Edge(options=edge_opts)
    except Exception as edge_err:
        logger.debug(f"Edge initialization failed: {edge_err}. Trying Chrome...")
        chrome_opts = ChromeOptions()
        if headless:
            chrome_opts.add_argument("--headless=new")
        chrome_opts.add_argument(f"--user-data-dir={user_data}")
        chrome_opts.add_argument("--disable-gpu")
        chrome_opts.add_argument("--no-sandbox")
        chrome_opts.add_argument("--disable-dev-shm-usage")
        driver = webdriver.Chrome(options=chrome_opts)

    try:
        logger.info(f"Navigating to {url} ...")
        driver.get(url)

        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(4)

        page_source = driver.page_source
        return parse_rewards_page(page_source)

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

# ==========================================
# Main Execution Loop
# ==========================================

def run_checker():
    parser = argparse.ArgumentParser(description="Check Flipkart Gift Card status on Microsoft / Bing Rewards.")
    parser.add_argument("--mode", choices=["http", "selenium"], help="Checking mode (http is fast & lightweight, selenium uses browser)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--once", action="store_true", help="Run once and exit immediately")
    parser.add_argument("--notify-always", action="store_true", help="Send notification regardless of stock status (useful for manual testing)")
    parser.add_argument("--test-notify", action="store_true", help="Send a test notification to verify all channels")
    parser.add_argument("--interval", type=int, help="Check interval in seconds")
    args = parser.parse_args()

    config = load_or_create_config()

    if args.mode:
        config["mode"] = args.mode
    if args.headless:
        config["headless"] = True
    if args.interval:
        config["check_interval_seconds"] = args.interval

    if args.test_notify:
        logger.info("Sending test notification to configured channels...")
        test_info = {
            "in_stock": True,
            "status": "TEST NOTIFICATION",
            "details": "This is a test notification from your Flipkart Gift Card Bing Rewards Checker!"
        }
        notify_user(config, test_info)
        return

    mode = config.get("mode", "http")
    interval = config.get("check_interval_seconds", 300)
    target = config.get("target_reward", "Flipkart")
    url = config.get("rewards_url", "https://rewards.bing.com/redeem/sku/000885000034")

    logger.info("=" * 60)
    logger.info("   Microsoft Rewards (Bing) Flipkart Stock Checker")
    logger.info("=" * 60)
    logger.info(f"Target URL: {url}")
    logger.info(f"Checker Mode: {mode.upper()}")
    logger.info(f"Check Interval: {interval} seconds (~{interval // 60} mins)")
    logger.info("Active Notifications: " + ", ".join(
        [k for k, v in config.get("notifications", {}).items() if (isinstance(v, bool) and v) or (isinstance(v, dict) and v.get("enabled"))]
    ))
    tg_conf = config.get("notifications", {}).get("telegram", {})
    if tg_conf.get("bot_token") and tg_conf.get("chat_id"):
        chat_masked = str(tg_conf.get("chat_id"))[:4] + "***"
        logger.info(f"Telegram Configured: YES (Chat ID: {chat_masked})")
    else:
        logger.info("Telegram Configured: NO (Waiting for stock or secrets not injected)")

    logger.info("Press Ctrl+C to stop.")
    logger.info("=" * 60)

    last_in_stock_state = None

    while True:
        logger.info(f"Checking status for '{target}'...")
        try:
            if mode == "selenium":
                result = check_stock_selenium(config)
            else:
                result = check_stock_http(config)

            in_stock = result.get("in_stock", False)
            status = result.get("status", "Unknown")
            details = result.get("details", "")

            logger.info(f"Result: [{status}] -> {details}")

            # Check if this is a manual trigger (manual button click on GitHub or --notify-always)
            is_manual_run = args.notify_always or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

            # State transition detection
            if in_stock:
                if last_in_stock_state is not True:
                    logger.info("Item is IN STOCK!")
                    if config.get("notify_on_in_stock", True):
                        notify_user(config, result)
                    last_in_stock_state = True
                else:
                    logger.info("Item is still in stock (notification already sent).")
            else:
                if last_in_stock_state is True:
                    logger.info("Item went back OUT OF STOCK!")
                    if config.get("notify_on_out_of_stock", True):
                        out_info = {
                            "in_stock": False,
                            "status": "OUT OF STOCK",
                            "details": f"⚠️ {details}"
                        }
                        notify_user(config, out_info)
                elif is_manual_run:
                    # Send feedback when user manually clicks "Run workflow" so they know it worked
                    logger.info(f"Manual check notification dispatched: {status}")
                    manual_info = {
                        "in_stock": in_stock,
                        "status": f"MANUAL CHECK: {status}",
                        "details": f"Manual run successful. Current status: {details}"
                    }
                    notify_user(config, manual_info)
                elif last_in_stock_state is None and config.get("notify_on_startup", False) and not args.once:
                    # Notify once on startup when running continuous daemon
                    logger.info(f"Initial status notification sent: {status}")
                    startup_info = {
                        "in_stock": in_stock,
                        "status": f"INITIAL: {status}",
                        "details": f"Script started. Current status: {details}"
                    }
                    notify_user(config, startup_info)
                last_in_stock_state = False

        except Exception as e:
            logger.error(f"Error during check: {e}")

        if args.once:
            break

        logger.info(f"Sleeping for {interval} seconds...\n")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Checker stopped by user.")
            break

if __name__ == "__main__":
    run_checker()
