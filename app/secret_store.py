"""Одиночный секрет в Диспетчере учетных данных Windows (keyring), запасной вариант — файл DPAPI.

Используется для api_id/api_hash: служба «tg-mcp», запись «api_credentials» (JSON).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("tgmcp.secret")


class SecretStore:
    def __init__(self, service: str, username: str, dpapi_path: Path, use_keyring: bool = True):
        self.service = service
        self.username = username
        self.dpapi_path = Path(dpapi_path)
        self.use_keyring = use_keyring
        self._lock = threading.Lock()
        self.backend: str | None = None  # где лежит сейчас: keyring | dpapi

    def _kr(self):
        import keyring  # noqa: PLC0415
        return keyring

    def load(self) -> dict[str, Any] | None:
        with self._lock:
            raw = None
            if self.use_keyring:
                try:
                    raw = self._kr().get_password(self.service, self.username)
                    if raw:
                        self.backend = "keyring"
                except Exception:  # noqa: BLE001
                    log.warning("keyring недоступен (чтение)", exc_info=True)
            if not raw and self.dpapi_path.exists():
                try:
                    import win32crypt  # noqa: PLC0415
                    _, data = win32crypt.CryptUnprotectData(self.dpapi_path.read_bytes(), None, None, None, 0)
                    raw = data.decode("utf-8")
                    self.backend = "dpapi"
                except Exception:  # noqa: BLE001
                    log.warning("DPAPI: не удалось прочитать %s", self.dpapi_path.name, exc_info=True)
            if not raw:
                return None
            try:
                val = json.loads(raw)
                return val if isinstance(val, dict) else None
            except ValueError:
                return None

    def save(self, value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":"))
        with self._lock:
            if self.use_keyring:
                try:
                    self._kr().set_password(self.service, self.username, raw)
                    self.dpapi_path.unlink(missing_ok=True)
                    self.backend = "keyring"
                    return "keyring"
                except Exception:  # noqa: BLE001
                    log.warning("keyring недоступен (запись), будет DPAPI-файл", exc_info=True)
            import win32crypt  # noqa: PLC0415
            blob = win32crypt.CryptProtectData(raw.encode("utf-8"), "tg-mcp secret", None, None, None, 0)
            self.dpapi_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.dpapi_path.with_suffix(".tmp")
            tmp.write_bytes(blob)
            tmp.replace(self.dpapi_path)
            self.backend = "dpapi"
            return "dpapi"

    def delete(self) -> None:
        with self._lock:
            if self.use_keyring:
                try:
                    self._kr().delete_password(self.service, self.username)
                except Exception:  # noqa: BLE001 — записи может не быть
                    pass
            self.dpapi_path.unlink(missing_ok=True)
            self.backend = None


class MemorySecretStore(SecretStore):
    """Для тестов: ничего не пишет в систему."""

    def __init__(self):
        super().__init__("test", "test", Path("nonexistent.dpapi"), use_keyring=False)
        self.value: dict[str, Any] | None = None

    def load(self):
        return dict(self.value) if self.value else None

    def save(self, value):
        self.value = dict(value)
        self.backend = "memory"
        return "memory"

    def delete(self):
        self.value = None
        self.backend = None
