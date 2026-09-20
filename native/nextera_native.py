#!/usr/bin/env python3
"""
Nextera Native Messaging Host for Chrome Extension
"""
import sys
import os
import json
import struct
from pathlib import Path

# Add native folder to Python path
_native_dir = os.path.dirname(os.path.abspath(__file__))
if _native_dir not in sys.path:
    sys.path.insert(0, _native_dir)

from nextera.main import Nextera
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")


def read_message():
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length or len(raw_length) < 4:
        return None
    length = struct.unpack('=I', raw_length)[0]
    if length > 64 * 1024 * 1024:
        return None
    message = sys.stdin.buffer.read(length).decode('utf-8')
    return json.loads(message)


def send_message(message):
    encoded = json.dumps(message).encode('utf-8')
    sys.stdout.buffer.write(struct.pack('=I', len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def log_to_chrome(message, level="info"):
    send_message({"type": "log", "message": message, "level": level})


class ChromeLogger:
    def info(self, msg):
        log_to_chrome(str(msg), "info")
    def success(self, msg):
        log_to_chrome(str(msg), "success")
    def error(self, msg):
        log_to_chrome(str(msg), "error")
    def warning(self, msg):
        log_to_chrome(str(msg), "warning")
    def debug(self, msg):
        pass
    def exception(self, msg):
        log_to_chrome(str(msg), "error")


def _load_config_file():
    config_dir = Path.home() / ".nextera"
    config_file = config_dir / "config.json"
    config_dir.mkdir(parents=True, exist_ok=True)
    if config_file.exists():
        try:
            return json.loads(config_file.read_text()), config_file
        except Exception:
            pass
    return {"cookies": {}}, config_file


def sync_keys_to_config(keys: dict) -> bool:
    try:
        cfg, config_file = _load_config_file()
        for k, v in keys.items():
            if v:
                cfg[k] = v
        config_file.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        return True
    except Exception as e:
        log_to_chrome(f"sync_keys_to_config error: {e}", "error")
        return False


def sync_cookies_to_config(cookies: dict) -> bool:
    try:
        cfg, config_file = _load_config_file()
        if "cookies" not in cfg:
            cfg["cookies"] = {}
        cfg["cookies"].update(cookies)
        config_file.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        log_to_chrome(f"Cookies saved: {', '.join(cookies.keys())}", "success")
        return True
    except Exception as e:
        log_to_chrome(f"sync_cookies_to_config error: {e}", "error")
        return False


def fetch_cookies_from_browser() -> dict:
    try:
        from nextera.config import fetch_browser_cookies
        cookies = fetch_browser_cookies()
        return cookies or {}
    except Exception as e:
        log_to_chrome(f"fetch_cookies error: {e}", "error")
        return {}


def main():
    import nextera.main
    nextera.main.logger = ChromeLogger()
    import nextera.watcher.watch
    nextera.watcher.watch.logger = ChromeLogger()
    import nextera.discussion.solver
    nextera.discussion.solver.logger = ChromeLogger()
    import nextera.assessment.solver
    nextera.assessment.solver.logger = ChromeLogger()

    while True:
        message = read_message()
        if message is None:
            break

        action = message.get("action")

        if action == "run":
            slug = message.get("slug")
            mode = message.get("mode", "complete")
            current_url = message.get("currentUrl", "")

            log_to_chrome(f"Starting nextera for: {slug} (mode: {mode})", "info")
            if current_url:
                log_to_chrome(f"URL: {current_url[:80]}", "info")

            try:
                use_llm = mode in ("llm", "quizzes", "graded", "discussions", "current")
                nextera = Nextera(slug, use_llm, mode=mode, current_url=current_url)
                nextera.get_course()
                log_to_chrome("Course completed!", "success")
                send_message({"type": "done"})
            except Exception as e:
                log_to_chrome(f"Error: {str(e)}", "error")
                send_message({"type": "done"})

        elif action == "sync_keys":
            keys = message.get("keys", {})
            if sync_keys_to_config(keys):
                log_to_chrome("API keys synced to config.json", "success")
            else:
                log_to_chrome("Failed to sync API keys", "error")
            send_message({"type": "keys_synced"})

        elif action == "sync_cookies":
            cookies = message.get("cookies", {})
            if sync_cookies_to_config(cookies):
                log_to_chrome("Cookies synced to config.json", "success")
            else:
                log_to_chrome("Failed to sync cookies", "error")
            send_message({"type": "cookies_synced"})

        elif action == "fetch_cookies":
            cookies = fetch_cookies_from_browser()
            if cookies:
                sync_cookies_to_config(cookies)
                log_to_chrome(f"Fetched {len(cookies)} cookies from browser", "success")
                send_message({"type": "cookies_fetched", "cookies": cookies})
            else:
                log_to_chrome("No cookies found in browser. Manually paste karo.", "error")
                send_message({"type": "cookies_fetched", "cookies": {}})

        elif action == "get_cache_stats":
            try:
                from nextera.assessment.cache import get_cache_count
                count = get_cache_count()
                send_message({"type": "cache_stats", "count": count})
            except Exception as e:
                send_message({"type": "cache_stats", "count": 0, "error": str(e)})


if __name__ == "__main__":
    main()