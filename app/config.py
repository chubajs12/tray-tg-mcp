"""Настройки приложения: data/config.json.

Прокси по умолчанию НЕ задан: список пуст, активного нет. Прокси задается только вручную
(раздел «Прокси» в окне приложения или MCP-инструмент set_proxy).
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import socket
import threading
import uuid
from pathlib import Path
from typing import Any

from . import paths
from . import permissions as perms_mod

DEFAULT_MCP_PORT = 8791
PROXY_TYPES = ("socks5", "http", "mtproto")
PROXY_TYPE_TITLES = {"socks5": "SOCKS5", "http": "HTTP", "mtproto": "MTProto"}


class ConfigError(ValueError):
    pass


def make_api_store():
    """Хранилище api_id/api_hash: Диспетчер учетных данных (служба tg-mcp, запись api_credentials), иначе DPAPI."""
    from .secret_store import SecretStore  # noqa: PLC0415
    return SecretStore("tg-mcp", "api_credentials", paths.DATA_DIR / "api_credentials.dpapi")




def _default() -> dict[str, Any]:
    return {
        "proxies": [],
        "active_proxy": None,  # прокси приложения (по умолчанию для аккаунтов); None — без прокси
        "accounts": [],  # [{id, label, proxy: "app"|"none"|<id прокси>, permissions: {...}}]
        "mcp_port": None,
        "mcp_token": "",
        "theme": "system",  # system | light | dark
    }


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_free_port(start: int = DEFAULT_MCP_PORT, span: int = 100) -> int:
    for p in range(start, start + span):
        if port_is_free(p):
            return p
    raise ConfigError(f"нет свободного порта в диапазоне {start}-{start + span - 1}")


FAKETLS_MSG = ("Секрет FakeTLS (начинается с ee…) не поддерживается: библиотека Telethon не умеет "
               "TLS-маскировку MTProto, такой прокси (например, mtg) работать не будет. Нужен секрет вида dd… "
               "или обычный (32 hex-символа) — либо используйте SOCKS5/HTTP.")


def check_mtproto_secret(secret: str) -> tuple[str, str]:
    """Проверка секрета MTProto под возможности Telethon. Возвращает (hex, вид: plain|dd).

    Telethon поддерживает обычный секрет (16 байт) и dd-секрет (random padding); FakeTLS (ee…) — нет.
    Принимаются hex и base64 (в т.ч. urlsafe).
    """
    import base64
    import binascii

    s = (secret or "").strip()
    if not s:
        raise ConfigError("для MTProto нужен секрет (ключ)")
    raw: bytes | None = None
    if all(c in "0123456789abcdefABCDEF" for c in s) and len(s) % 2 == 0:
        raw = bytes.fromhex(s)
    else:
        try:
            raw = base64.b64decode(s.replace("-", "+").replace("_", "/") + "=" * (-len(s) % 4), validate=True)
        except (binascii.Error, ValueError):
            raise ConfigError("секрет MTProto — hex или base64") from None
    if raw[:1] == bytes([0xEE]):
        raise ConfigError(FAKETLS_MSG)
    if len(raw) == 17 and raw[0] == 0xDD:
        return raw.hex(), "dd"
    if len(raw) == 16:
        return raw.hex(), "plain"
    raise ConfigError("секрет MTProto: 32 hex-символа (16 байт) или dd + 32 hex-символа")


def normalize_proxy(p: dict[str, Any]) -> dict[str, Any]:
    """Проверяет и приводит запись прокси к единому виду."""
    ptype = str(p.get("type") or "").strip().lower()
    if ptype in ("socks", "socks5h"):
        ptype = "socks5"
    if ptype in ("https",):
        ptype = "http"
    if ptype not in PROXY_TYPES:
        raise ConfigError("тип прокси: socks5, http или mtproto")
    server = str(p.get("server") or "").strip()
    if not server:
        raise ConfigError("не указан сервер прокси")
    try:
        port = int(p.get("port"))
    except (TypeError, ValueError):
        raise ConfigError("порт прокси — число 1..65535") from None
    if not 1 <= port <= 65535:
        raise ConfigError("порт прокси — число 1..65535")
    out = {
        "id": str(p.get("id") or uuid.uuid4().hex[:8]),
        "type": ptype,
        "server": server,
        "port": port,
        "username": "",
        "password": "",
        "secret": "",
    }
    if ptype == "mtproto":
        out["secret"], _ = check_mtproto_secret(str(p.get("secret") or ""))
    else:
        out["username"] = str(p.get("username") or "")
        out["password"] = str(p.get("password") or "")
    return out


def parse_proxy_link(text: str) -> dict | None:
    """tg://proxy?server=..&port=..&secret=.., tg://socks?.., https://t.me/proxy?.. / t.me/socks?.."""
    from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

    t = text.strip()
    if not t:
        return None
    if t.startswith("t.me/"):
        t = "https://" + t
    u = urlparse(t)
    kind = None
    if u.scheme == "tg" and u.netloc in ("proxy", "socks"):
        kind = u.netloc
    elif u.netloc in ("t.me", "telegram.me") and u.path.strip("/") in ("proxy", "socks"):
        kind = u.path.strip("/")
    if not kind:
        return None
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    out = {"type": "mtproto" if kind == "proxy" else "socks5", "server": q.get("server", ""),
           "port": q.get("port", "")}
    if kind == "proxy":
        out["secret"] = q.get("secret", "")
    else:
        out["username"] = q.get("user", "")
        out["password"] = q.get("pass", "")
    return out


def mask_proxy(p: dict[str, Any] | None) -> dict[str, Any] | None:
    """Копия записи прокси без пароля и секрета (для MCP, журнала, окна статуса)."""
    if not p:
        return None
    m = {k: p.get(k) for k in ("id", "type", "server", "port", "username")}
    m["password"] = "***" if p.get("password") else ""
    sec = p.get("secret") or ""
    m["secret"] = (sec[:2] + "…" + f"({len(sec)} симв.)") if sec else ""
    return m


def proxy_title(p: dict[str, Any] | None) -> str:
    if not p:
        return "без прокси"
    return f"{PROXY_TYPE_TITLES.get(p['type'], p['type'])} {p['server']}:{p['port']}"


class Config:
    """Потокобезопасная обертка над config.json."""

    def __init__(self, path: Path | None = None, api_store=None):
        self.path = Path(path or paths.CONFIG_FILE)
        self._lock = threading.RLock()
        self._data = _default()
        self.api_store = api_store if api_store is not None else make_api_store()
        self._api: dict[str, Any] | None = None
        self.load()
        self._init_api()

    # --- api_id/api_hash: только из хранилища (Credential Manager / DPAPI), не из config.json ---
    def _init_api(self) -> None:
        with self._lock:
            # миграция: старые api_id/api_hash из config.json → хранилище, из файла — убрать
            old_id, old_hash = self._data.pop("api_id", None), self._data.pop("api_hash", None)
            if old_id is not None or old_hash is not None:
                self.save()
                if old_id and old_hash and len(str(old_hash)) == 32:
                    self.api_store.save({"api_id": int(old_id), "api_hash": str(old_hash).lower(),
                                         "source": "user"})
            cur = self.api_store.load()
            self._api = cur if self._valid(cur) else None

    @staticmethod
    def _valid(v) -> bool:
        try:
            return bool(v) and int(v["api_id"]) > 0 and len(str(v["api_hash"])) == 32
        except (KeyError, TypeError, ValueError):
            return False

    @property
    def api_storage(self) -> str | None:
        """keyring | dpapi | memory — где сейчас лежат api_id/api_hash."""
        return getattr(self.api_store, "backend", None)

    # --- файл ---
    def load(self) -> None:
        with self._lock:
            data = _default()
            if self.path.exists():
                try:
                    data.update(json.loads(self.path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    # битый файл — сохраняем копию, работаем с умолчаниями
                    try:
                        self.path.replace(self.path.with_suffix(".broken.json"))
                    except OSError:
                        pass
            changed = False
            if not data.get("mcp_token"):
                data["mcp_token"] = secrets.token_urlsafe(32)
                changed = True
            if not data.get("mcp_port"):
                data["mcp_port"] = pick_free_port()
                changed = True
            for key in ("proxies", "accounts"):
                if not isinstance(data.get(key), list):
                    data[key] = []
                    changed = True
            for a in data["accounts"]:
                a["permissions"] = perms_mod.normalize(a.get("permissions"))
                a.setdefault("proxy", "app")
            self._data = data
            if changed or not self.path.exists():
                self.save()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    # --- чтение ---
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    @property
    def api_source(self) -> str | None:
        """user — заданы пользователем; None — не заданы."""
        with self._lock:
            return (self._api or {}).get("source") if self._api else None

    @property
    def api_id(self) -> int | None:
        with self._lock:
            return int(self._api["api_id"]) if self._api else None

    @property
    def api_hash(self) -> str:
        with self._lock:
            return str(self._api["api_hash"]) if self._api else ""

    @property
    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash)

    @property
    def mcp_port(self) -> int:
        with self._lock:
            return int(self._data["mcp_port"])

    @property
    def mcp_token(self) -> str:
        with self._lock:
            return str(self._data["mcp_token"])

    def proxies(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._data["proxies"])

    def active_proxy(self) -> dict[str, Any] | None:
        with self._lock:
            pid = self._data.get("active_proxy")
            for p in self._data["proxies"]:
                if p.get("id") == pid:
                    return copy.deepcopy(p)
            return None

    # --- запись ---
    def set_api(self, api_id: int | str, api_hash: str) -> None:
        """Свои api_id/api_hash (из Настроек) → в хранилище; в config.json не пишутся."""
        api_hash = (api_hash or "").strip()
        try:
            api_id_v = int(str(api_id).strip())
        except (TypeError, ValueError):
            raise ConfigError("api_id — число") from None
        if api_id_v <= 0:
            raise ConfigError("api_id — положительное число")
        if len(api_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in api_hash):
            raise ConfigError("api_hash — 32 шестнадцатеричных символа")
        rec = {"api_id": api_id_v, "api_hash": api_hash.lower(), "source": "user"}
        with self._lock:
            self.api_store.save(rec)
            self._api = rec

    def clear_api(self) -> None:
        """Удалить api_id/api_hash из хранилища."""
        with self._lock:
            self.api_store.delete()
            self._api = None

    def set_mcp_port(self, port: int | str) -> None:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ConfigError("порт MCP — число") from None
        if not 1024 <= port <= 65535:
            raise ConfigError("порт MCP — 1024..65535")
        with self._lock:
            self._data["mcp_port"] = port
            self.save()

    def upsert_proxy(self, proxy: dict[str, Any], make_active: bool = False) -> dict[str, Any]:
        """Добавляет или обновляет прокси. Совпадение — по id, иначе по (тип, сервер, порт)."""
        p = normalize_proxy(proxy)
        with self._lock:
            lst = self._data["proxies"]
            idx = None
            for i, old in enumerate(lst):
                if proxy.get("id") and old.get("id") == proxy.get("id"):
                    idx = i
                    break
            if idx is None:
                for i, old in enumerate(lst):
                    if (old["type"], old["server"].lower(), int(old["port"])) == (
                        p["type"], p["server"].lower(), p["port"]):
                        idx = i
                        break
            if idx is None:
                lst.append(p)
            else:
                p["id"] = lst[idx]["id"]
                lst[idx] = p
            if make_active:
                self._data["active_proxy"] = p["id"]
            self.save()
            return copy.deepcopy(p)

    def remove_proxy(self, proxy_id: str) -> None:
        with self._lock:
            self._data["proxies"] = [p for p in self._data["proxies"] if p.get("id") != proxy_id]
            if self._data.get("active_proxy") == proxy_id:
                self._data["active_proxy"] = None
            for a in self._data["accounts"]:
                if a.get("proxy") == proxy_id:
                    a["proxy"] = "app"
            self.save()

    def set_active_proxy(self, proxy_id: str | None) -> None:
        with self._lock:
            if proxy_id is not None and not any(p.get("id") == proxy_id for p in self._data["proxies"]):
                raise ConfigError("нет прокси с таким id")
            self._data["active_proxy"] = proxy_id
            self.save()

    # --- аккаунты ---
    def accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._data["accounts"])

    def account(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            for a in self._data["accounts"]:
                if a["id"] == account_id:
                    return copy.deepcopy(a)
            return None

    def add_account(self, label: str) -> dict[str, Any]:
        label = (label or "").strip()
        if not label:
            raise ConfigError("метка аккаунта не задана")
        with self._lock:
            if any(a["label"].casefold() == label.casefold() for a in self._data["accounts"]):
                raise ConfigError(f"аккаунт с меткой «{label}» уже есть")
            acc = {"id": uuid.uuid4().hex[:8], "label": label, "proxy": "app",
                   "permissions": perms_mod.default_permissions()}
            self._data["accounts"].append(acc)
            self.save()
            return copy.deepcopy(acc)

    def remove_account(self, account_id: str) -> None:
        with self._lock:
            self._data["accounts"] = [a for a in self._data["accounts"] if a["id"] != account_id]
            self.save()

    def _acc(self, account_id: str) -> dict[str, Any]:
        for a in self._data["accounts"]:
            if a["id"] == account_id:
                return a
        raise ConfigError("нет такого аккаунта")

    def set_account_label(self, account_id: str, label: str) -> None:
        label = (label or "").strip()
        if not label:
            raise ConfigError("метка аккаунта не задана")
        with self._lock:
            if any(a["label"].casefold() == label.casefold() and a["id"] != account_id
                   for a in self._data["accounts"]):
                raise ConfigError(f"аккаунт с меткой «{label}» уже есть")
            self._acc(account_id)["label"] = label
            self.save()

    def set_account_proxy(self, account_id: str, proxy: str) -> None:
        """proxy: "app" — как у приложения, "none" — без прокси, иначе id прокси из списка."""
        with self._lock:
            if proxy not in ("app", "none") and not any(p["id"] == proxy for p in self._data["proxies"]):
                raise ConfigError("нет прокси с таким id")
            self._acc(account_id)["proxy"] = proxy
            self.save()

    def account_proxy(self, account_id: str) -> dict[str, Any] | None:
        """Действующий прокси аккаунта (с учетом «как у приложения»)."""
        with self._lock:
            try:
                mode = self._acc(account_id).get("proxy", "app")
            except ConfigError:
                mode = "app"
            if mode == "none":
                return None
            pid = self._data.get("active_proxy") if mode == "app" else mode
            for p in self._data["proxies"]:
                if p.get("id") == pid:
                    return copy.deepcopy(p)
            return None

    def permissions(self, account_id: str) -> dict[str, bool]:
        with self._lock:
            try:
                return perms_mod.normalize(self._acc(account_id).get("permissions"))
            except ConfigError:
                return perms_mod.normalize({})

    def set_permissions(self, account_id: str, perms: dict[str, bool]) -> dict[str, bool]:
        """Только из окна приложения (раздел «Права MCP»). Через MCP не вызывается."""
        with self._lock:
            new = perms_mod.normalize(perms)
            self._acc(account_id)["permissions"] = new
            self.save()
            return dict(new)

    def rotate_token(self) -> None:
        """Новый токен MCP; старый перестает приниматься сразу (проверка читает config на каждый запрос)."""
        with self._lock:
            self._data["mcp_token"] = secrets.token_urlsafe(32)
            self.save()

    @property
    def theme(self) -> str:
        with self._lock:
            t = self._data.get("theme") or "system"
            return t if t in ("system", "light", "dark") else "system"

    def set_theme(self, theme: str) -> None:
        if theme not in ("system", "light", "dark"):
            raise ConfigError("тема: system, light или dark")
        with self._lock:
            self._data["theme"] = theme
            self.save()
