import logging
import os

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger("tiktok_bot")
log.setLevel(logging.DEBUG)

_fmt = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_fmt)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(_fmt)

log.addHandler(_file_handler)
log.addHandler(_console_handler)


async def send_admin_alert(bot, admin_id: int, text: str):
    try:
        await bot.send_message(admin_id, f"⚠️ [Bot Alert]\n{text}")
    except Exception as e:
        log.warning(f"Admin alert failed to send: {e}")
