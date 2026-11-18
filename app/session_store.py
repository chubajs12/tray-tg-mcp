"""Хранение сессий Telethon (StringSession) вне открытых файлов.

Основное место — Диспетчер учетных данных Windows через keyring (служба «tg-mcp/<account_id>»).
Если keyring недоступен — файл data/accounts/<id>/session.dpapi, зашифрованный DPAPI текущего пользователя.
В data/accounts/<id>/meta.json — только метаданные (без ключа).
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger("tgmcp.session")

SERVICE_PREFIX = "tg-mcp/"
USERNAME = "session"


class SessionStore:
    def __init__(self, base_dir: Path | None = None, use_keyring: bool = True):
        self.base_dir = Path(base_dir or paths.ACCOUNTS_DIR)
        self.use_keyring = use_keyring
        self._lock = threading.Lock()

    def account_dir(self, account_id: str) -> Path:
        return self.base_dir / account_id

    # ---------- keyring ----------
    @staticmethod
    def _kr():
        import keyring  # noqa: PLC0415
        return keyring

    def _kr_get(self, account_id: str) -> str | None:
        if not self.use_keyring:
            return None
        try:
            return self._kr().get_password(SERVICE_PREFIX + account_id, USERNAME)
        except Exception:  # noqa: BLE001
            log.warning("keyring недоступен (чтение)", exc_info=True)
            return None

    def _kr_set(self, account_id: str, value: str) -> bool:
        if not self.use_keyring:
            return False
        try:
            self._kr().set_password(SERVICE_PREFIX + account_id, USERNAME, value)
            return True
        except Exception:  # noqa: BLE001
            log.warning("keyring недоступен (запись), будет DPAPI-файл", exc_info=True)
            return False

    def _kr_del(self, account_id: str) -> None:
        if not self.use_keyring:
            return
        try:
            self._kr().delete_password(SERVICE_PREFIX + account_id, USERNAME)
        except Exception:  # noqa: BLE001 — записи может не быть
            pass

    # ---------- DPAPI ----------
    def _dpapi_path(self, account_id: str) -> Path:
        return self.account_dir(account_id) / "session.dpapi"

    def _dpapi_get(self, account_id: str) -> str | None:
        p = self._dpapi_path(account_id)
        if not p.exists():
            return None
        try:
            import win32crypt  # noqa: PLC0415
            _, data = win32crypt.CryptUnprotectData(p.read_bytes(), None, None, None, 0)
            return data.decode("ascii")
        except Exception:  # noqa: BLE001
            log.warning("DPAPI: не удалось расшифровать сессию %s", account_id, exc_info=True)
            return None

    def _dpapi_set(self, account_id: str, value: str) -> None:
        import win32crypt  # noqa: PLC0415
        blob = win32crypt.CryptProtectData(value.encode("ascii"), "tg-mcp session", None, None, None, 0)
        p = self._dpapi_path(account_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(blob)
        tmp.replace(p)

    # ---------- API ----------
    def load(self, account_id: str) -> str | None:
        with self._lock:
            return self._kr_get(account_id) or self._dpapi_get(account_id)

    def save(self, account_id: str, session_string: str, meta: dict[str, Any] | None = None) -> str:
        """Возвращает место хранения: keyring или dpapi."""
        with self._lock:
            if self._kr_set(account_id, session_string):
                backend = "keyring"
                self._dpapi_path(account_id).unlink(missing_ok=True)
            else:
                self._dpapi_set(account_id, session_string)
                backend = "dpapi"
            self._write_meta(account_id, {**(meta or {}), "storage": backend})
            return backend

    def delete(self, account_id: str) -> None:
        """Удаляет ключ сессии (keyring и DPAPI-файл); метаданные — тоже."""
        with self._lock:
            self._kr_del(account_id)
            self._dpapi_path(account_id).unlink(missing_ok=True)
            (self.account_dir(account_id) / "meta.json").unlink(missing_ok=True)

    def purge(self, account_id: str) -> None:
        """Удалить аккаунт полностью: ключ и папку data/accounts/<id>."""
        self.delete(account_id)
        shutil.rmtree(self.account_dir(account_id), ignore_errors=True)

    def _write_meta(self, account_id: str, meta: dict[str, Any]) -> None:
        d = self.account_dir(account_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


class MemorySessionStore(SessionStore):
    """Для тестов: ничего не пишет в систему."""

    def __init__(self, base_dir: Path | None = None):
        import tempfile  # noqa: PLC0415
        super().__init__(base_dir=Path(base_dir or tempfile.mkdtemp(prefix="tgmcp-test-")), use_keyring=False)
        self.data: dict[str, str] = {}

    def load(self, account_id: str) -> str | None:
        return self.data.get(account_id)

    def save(self, account_id: str, session_string: str, meta: dict[str, Any] | None = None) -> str:
        self.data[account_id] = session_string
        return "memory"

    def delete(self, account_id: str) -> None:
        self.data.pop(account_id, None)

    def purge(self, account_id: str) -> None:
        self.delete(account_id)
