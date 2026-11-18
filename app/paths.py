r"""Пути приложения.

- Из исходников: данные в data/ проекта (вне git).
- Собранный exe (PyInstaller, sys.frozen): данные в %APPDATA%\TG MCP\, а не рядом с exe.
- TGMCP_DATA_DIR переопределяет каталог данных в обоих режимах (тесты, проверки).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_DIR = Path(__file__).resolve().parent.parent  # в exe — временная папка распаковки (_MEIPASS)
EXE_PATH = Path(sys.executable) if FROZEN else None


def _default_data_dir() -> Path:
    if FROZEN:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "TG MCP"
    return PROJECT_DIR / "data"


DATA_DIR = Path(os.environ.get("TGMCP_DATA_DIR") or _default_data_dir())
# Каталог данных переопределен (--data-dir / TGMCP_DATA_DIR): отдельный экземпляр со своими именами mutex/событий
# и своей записью api_id/api_hash в хранилище — не пересекается с основным запущенным приложением.
DATA_DIR_OVERRIDDEN = bool(os.environ.get("TGMCP_DATA_DIR"))


def instance_suffix() -> str:
    if not DATA_DIR_OVERRIDDEN:
        return ""
    import hashlib  # noqa: PLC0415
    return "-" + hashlib.sha1(str(DATA_DIR.resolve()).lower().encode("utf-8")).hexdigest()[:10]

CONFIG_FILE = DATA_DIR / "config.json"
ACCOUNTS_DIR = DATA_DIR / "accounts"  # data/accounts/<id>/meta.json (+ session.dpapi, если нет keyring)
LOG_DIR = DATA_DIR / "logs"
APP_LOG = LOG_DIR / "app.log"
ACTIONS_LOG = DATA_DIR / "actions.jsonl"
DOWNLOADS_DIR = DATA_DIR / "downloads"


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR, DOWNLOADS_DIR, ACCOUNTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
