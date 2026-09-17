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

# Setup logging to stderr (Chrome captures this)
logger.remove()
logger.add(sys.stderr, level="INFO")


def read_message():
    """Read message from Chrome (Native Messaging protocol)"""
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length or len(raw_length) < 4:
        return None

    length = struct.unpack('=I', raw_length)[0]

    if length > 64 * 1024 * 1024:
        return None

    message = sys.stdin.buffer.read(length).decode('utf-8')
    return json.loads(message)


def send_message(message):
    """Send message to Chrome"""
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


def sync_keys_to_config(keys: dict) -> bool:
    """Merge API keys into ~/.nextera/config.json"""
    try:
        config_dir = Path.home() / ".nextera"
        config_file = config_dir / "config.json"

        config_dir.mkdir(parents=True, exist_ok=True)

        if config_file.exists():
            cfg = json.loads(config_file.read_text())
        else:
            cfg = {"cookies": {}}

        # Merge keys (don't overwrite cookies)
        for k, v in keys.items():
            if v:
                cfg[k] = v

        config_file.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        return True
    except Exception as e:
        log_to_chrome(f"sync_keys_to_config error: {e}", "error")
        return False


def main():
    # Monkey-patch nextera's logger
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

        # ============ RUN ============
        if action == "run":
            slug = message.get("slug")
            mode = message.get("mode", "complete")

            log_to_chrome(f"Starting nextera for: {slug} (mode: {mode})", "info")

            try:
                use_llm = mode in ("llm", "quizzes", "graded", "discussions")
                nextera = Nextera(slug, use_llm, mode=mode)
                nextera.get_course()
                log_to_chrome("Course completed!", "success")
                send_message({"type": "done"})
            except Exception as e:
                log_to_chrome(f"Error: {str(e)}", "error")
                send_message({"type": "done"})

        # ============ SYNC KEYS ============
        elif action == "sync_keys":
            keys = message.get("keys", {})
            if sync_keys_to_config(keys):
                log_to_chrome("API keys synced to config.json", "success")
            else:
                log_to_chrome("Failed to sync API keys", "error")
            send_message({"type": "keys_synced"})


if __name__ == "__main__":
    main()