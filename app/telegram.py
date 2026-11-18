"""Жизненный цикл клиента Telethon: подключение, прокси, вход по QR, «не в сети», переподключение.

Все методы корутины выполняются в одном asyncio-loop (поток клиента). Другие потоки (Qt, проверка прокси) читают
состояние через snapshot() и вызывают корутины через asyncio.run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import threading
import time
from typing import Any, Callable

from telethon import TelegramClient, errors, functions
from telethon.network.connection import (ConnectionTcpMTProxyIntermediate,
                                         ConnectionTcpMTProxyRandomizedIntermediate)
from telethon.sessions import StringSession

from . import APP_VERSION, DEVICE_MODEL
from .config import Config, ConfigError, check_mtproto_secret, proxy_title
from .formatting import display_name, mask_phone
from .ratelimit import WriteLimiter
from .session_store import SessionStore

log = logging.getLogger("tgmcp.telegram")

# Состояния
NO_API = "no_api"
CONNECTING = "connecting"
NEED_LOGIN = "need_login"
ONLINE = "online"
ERROR = "error"
STOPPED = "stopped"

STATE_TEXT = {
    NO_API: "не настроено (нет api_id/api_hash)",
    CONNECTING: "подключение…",
    NEED_LOGIN: "не авторизован (нужен вход по QR)",
    ONLINE: "в сети",
    ERROR: "ошибка",
    STOPPED: "остановлен",
}

OFFLINE_INTERVAL = 60  # сек — периодически ставим статус «не в сети»
CONNECT_TIMEOUT = 40
BACKOFF = (5, 10, 30, 60, 120)
SECURITY_ON_OPEN = 120        # при открытии «Статуса» — если сведения старше 2 минут
SECURITY_PERIODIC = 30 * 60   # фоном — раз в 30 минут


def _ts(iso_s: str | None) -> float:
    from datetime import datetime  # noqa: PLC0415
    try:
        return datetime.fromisoformat(iso_s).timestamp() if iso_s else 0.0
    except ValueError:
        return 0.0


class NotReady(Exception):
    """Клиент не готов выполнять запросы (нет api_id, не авторизован, нет связи)."""


def build_proxy_kwargs(proxy: dict[str, Any] | None) -> dict[str, Any]:
    """Параметры TelegramClient для прокси из записи config."""
    if not proxy:
        return {}
    t = proxy["type"]
    if t in ("socks5", "http"):
        d: dict[str, Any] = {
            "proxy_type": t,
            "addr": proxy["server"],
            "port": int(proxy["port"]),
            "rdns": True,
        }
        if proxy.get("username"):
            d["username"] = proxy["username"]
            d["password"] = proxy.get("password") or ""
        return {"proxy": d}
    if t == "mtproto":
        try:
            secret, kind = check_mtproto_secret(proxy.get("secret") or "")
        except ConfigError as e:
            raise ValueError(str(e)) from None
        return {
            "connection": (ConnectionTcpMTProxyRandomizedIntermediate if kind == "dd"
                           else ConnectionTcpMTProxyIntermediate),
            "proxy": (proxy["server"], int(proxy["port"]), secret),
        }
    raise ValueError(f"неизвестный тип прокси: {t}")


class TelegramService:
    """Один аккаунт Telegram: свой клиент, своя сессия, свой ограничитель записи."""

    def __init__(self, config: Config, account_id: str, store: SessionStore,
                 on_change: Callable[[], None] | None = None):
        self.config = config
        self.account_id = account_id
        self.store = store
        self.on_change = on_change
        self.client: TelegramClient | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.limiter = WriteLimiter(1.0)
        self.dialog_cache: list[Any] = []
        self.dialog_cache_at = 0.0
        self._lock = threading.Lock()
        self._state = NO_API if not config.configured else CONNECTING
        self._error: str | None = None
        self._me: dict[str, Any] | None = None
        self._security: dict[str, Any] | None = None
        self._security_at = 0.0
        self._security_busy = False
        self._proxy_title = proxy_title(config.account_proxy(account_id))
        self._reconfigure: asyncio.Event | None = None
        self._stopping = False
        self._tasks: list[asyncio.Task] = []
        self._connected_since: float | None = None
        # вход по QR
        self._qr_task: asyncio.Task | None = None
        self._qr: dict[str, Any] = {"state": "idle"}
        self._pw_future: asyncio.Future | None = None

    # ---------- состояние для других потоков ----------
    def _set(self, state: str | None = None, error: str | None | bool = False, **kw) -> None:
        with self._lock:
            if state is not None:
                self._state = state
            if error is not False:
                self._error = error  # type: ignore[assignment]
            for k, v in kw.items():
                setattr(self, "_" + k, v)
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                log.exception("on_change")

    def _set_qr(self, **kw) -> None:
        with self._lock:
            self._qr = {**self._qr, **kw}
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def avatar_path(self):
        return self.store.account_dir(self.account_id) / "avatar.jpg"

    async def _load_avatar(self) -> None:
        c = self.client
        if c is None:
            return
        try:
            self.avatar_path.parent.mkdir(parents=True, exist_ok=True)
            got = await c.download_profile_photo("me", file=str(self.avatar_path), download_big=False)
            if not got:
                self.avatar_path.unlink(missing_ok=True)
            self._set()
        except Exception:  # noqa: BLE001
            log.debug("[%s] аватар", self.account_id, exc_info=True)

    @property
    def label(self) -> str:
        a = self.config.account(self.account_id)
        return a["label"] if a else self.account_id

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "account_id": self.account_id,
                "state": self._state,
                "state_text": STATE_TEXT.get(self._state, self._state),
                "error": self._error,
                "me": dict(self._me) if self._me else None,
                "proxy": self._proxy_title,
                "connected_since": self._connected_since,
                "qr": dict(self._qr),
                "avatar": str(self.avatar_path) if self.avatar_path.exists() else None,
                "security": dict(self._security) if self._security else None,
            }

    # ---------- запуск/остановка ----------
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._reconfigure = asyncio.Event()
        self._tasks.append(asyncio.create_task(self._supervisor(), name="tg-supervisor"))
        self._tasks.append(asyncio.create_task(self._offline_keeper(), name="tg-offline"))

    def reconfigure(self) -> None:
        """Перечитать api_id/прокси и переподключиться (вызывать из loop)."""
        self._proxy_title = proxy_title(self.config.account_proxy(self.account_id))
        if self._reconfigure:
            self._reconfigure.set()

    async def shutdown(self) -> None:
        self._stopping = True
        if self._reconfigure:
            self._reconfigure.set()
        await self._cancel_qr()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._drop_client()
        self._set(STOPPED)

    def _persist(self) -> None:
        """Сохранить StringSession авторизованного клиента в хранилище (keyring/DPAPI)."""
        c = self.client
        if c is None or self.state != ONLINE:
            return
        try:
            s = c.session.save()
            if s:
                me = self._me or {}
                self.store.save(self.account_id, s, {"label": self.label, "user_id": me.get("id"),
                                                     "name": me.get("name"), "username": me.get("username")})
        except Exception:  # noqa: BLE001
            log.warning("[%s] не удалось сохранить сессию", self.account_id, exc_info=True)

    async def _drop_client(self) -> None:
        self._persist()
        self.dialog_cache = []
        c, self.client = self.client, None
        if c is not None:
            try:
                await asyncio.wait_for(c.disconnect(), timeout=10)
            except Exception:  # noqa: BLE001
                log.warning("disconnect", exc_info=True)

    def _make_client(self) -> TelegramClient:
        kwargs = build_proxy_kwargs(self.config.account_proxy(self.account_id))
        return TelegramClient(
            StringSession(self.store.load(self.account_id) or ""),
            self.config.api_id,
            self.config.api_hash,
            device_model=DEVICE_MODEL,
            system_version=f"Windows {platform.release()}",
            app_version=APP_VERSION,
            lang_code="ru",
            system_lang_code="ru-RU",
            timeout=15,
            request_retries=3,
            connection_retries=3,
            retry_delay=3,
            auto_reconnect=True,
            flood_sleep_threshold=5,  # короткие FloodWait ждем один раз, длинные — ошибкой наверх
            receive_updates=True,  # нужно для входа по QR (UpdateLoginToken)
            catch_up=False,
            **kwargs,
        )

    async def _supervisor(self) -> None:
        attempt = 0
        assert self._reconfigure is not None
        while not self._stopping:
            self._reconfigure.clear()
            self._proxy_title = proxy_title(self.config.account_proxy(self.account_id))
            if not self.config.configured:
                await self._drop_client()
                self._set(NO_API, error=None, me=None, security=None)
                await self._reconfigure.wait()
                continue
            if os.environ.get("TGMCP_NO_CONNECT"):
                # режим проверки (selftest/скриншоты): к Telegram не подключаемся вовсе
                self._set(ERROR, error="подключение к Telegram отключено (TGMCP_NO_CONNECT, режим проверки)")
                await self._reconfigure.wait()
                continue
            self._set(CONNECTING, error=None)
            try:
                if self.client is None:
                    self.client = self._make_client()
                await asyncio.wait_for(self.client.connect(), timeout=CONNECT_TIMEOUT)
                authorized = await self.client.is_user_authorized()
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                log.warning("[%s] подключение не удалось: %s", self.account_id, msg)
                await self._drop_client()
                delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                attempt += 1
                self._set(ERROR, error=f"{msg}; повтор через {delay} с")
                try:
                    await asyncio.wait_for(self._reconfigure.wait(), timeout=delay)
                    attempt = 0
                except asyncio.TimeoutError:
                    pass
                continue
            attempt = 0
            self._connected_since = time.time()
            if authorized:
                await self._after_login()
            else:
                self._set(NEED_LOGIN, error=None, me=None, security=None)
            # ждем обрыва (auto_reconnect не справился) или смены настроек
            disc = asyncio.ensure_future(self.client.disconnected)
            rec = asyncio.ensure_future(self._reconfigure.wait())
            done, pending = await asyncio.wait({disc, rec}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            if rec in done:
                log.info("переподключение по смене настроек")
                await self._cancel_qr()
                await self._drop_client()
            else:
                log.warning("соединение потеряно, переподключаюсь")
                await self._drop_client()
                if not self._stopping:
                    self._set(CONNECTING, error="соединение потеряно")
                    await asyncio.sleep(3)

    async def _after_login(self) -> None:
        assert self.client is not None
        me = await self.client.get_me()
        self._set(ONLINE, error=None, me={
            "id": me.id,
            "name": display_name(me),
            "username": me.username,
            "phone": mask_phone(me.phone),
        })
        self._persist()
        await self._go_offline()
        asyncio.create_task(self._load_avatar())
        asyncio.create_task(self.refresh_security(max_age=0))

    async def _go_offline(self) -> None:
        c = self.client
        if c is None or self.state != ONLINE:
            return
        try:
            await c(functions.account.UpdateStatusRequest(offline=True))
        except Exception:  # noqa: BLE001
            log.debug("UpdateStatus offline", exc_info=True)

    async def _offline_keeper(self) -> None:
        while not self._stopping:
            await asyncio.sleep(OFFLINE_INTERVAL)
            await self._go_offline()
            await self.refresh_security(max_age=SECURITY_PERIODIC)

    # ---------- безопасность аккаунта (только чтение) ----------
    async def refresh_security(self, max_age: float = SECURITY_ON_OPEN) -> None:
        """Облачный пароль и активные сеансы. Не чаще, чем раз в max_age секунд; ничего не меняет."""
        c = self.client
        if c is None or self.state != ONLINE or self._security_busy:
            return
        if self._security is not None and time.time() - self._security_at < max_age:
            return
        self._security_busy = True
        try:
            pw = await c(functions.account.GetPasswordRequest())
            auths = await c(functions.account.GetAuthorizationsRequest())
            sessions = []
            for a in getattr(auths, "authorizations", []):
                sessions.append({
                    "current": bool(a.current),
                    "device": " ".join(x for x in (a.device_model, a.platform, a.system_version) if x).strip(),
                    "app": " ".join(x for x in (a.app_name, a.app_version) if x).strip(),
                    "official": bool(a.official_app),
                    "ip": a.ip,
                    "location": ", ".join(x for x in (a.country, a.region) if x),
                    "active": a.date_active.astimezone().isoformat(timespec="minutes") if a.date_active else None,
                    "created": a.date_created.astimezone().isoformat(timespec="minutes") if a.date_created else None,
                })
            sessions.sort(key=lambda x: (not x["current"], -(_ts(x["active"]))))
            self._security_at = time.time()
            self._set(security={"has_password": bool(getattr(pw, "has_password", False)),
                                "sessions": sessions, "checked_at": self._security_at})
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] безопасность: %s", self.account_id, type(e).__name__)
        finally:
            self._security_busy = False

    # ---------- доступ для инструментов ----------
    def require(self) -> TelegramClient:
        st = self.state
        if st == NO_API:
            raise NotReady("Telegram не настроен: задайте api_id и api_hash (окно TG MCP → раздел «Настройки»).")
        if st == NEED_LOGIN:
            raise NotReady("Не выполнен вход в Telegram: окно TG MCP → раздел «Аккаунты» → «Войти по QR».")
        if st != ONLINE or self.client is None:
            err = self.snapshot().get("error")
            raise NotReady(f"Нет соединения с Telegram ({STATE_TEXT.get(st, st)}"
                           + (f": {err}" if err else "") + ").")
        return self.client

    # ---------- вход по QR ----------
    async def start_qr(self) -> None:
        if self.state != NEED_LOGIN or self.client is None:
            st = self.state
            if st == ONLINE:
                self._set_qr(state="done", error=None)
            elif st == NO_API:
                self._set_qr(state="error", error="Сначала задайте api_id и api_hash в «Настройках».")
            else:
                self._set_qr(state="error", error=f"Нет соединения с Telegram: {STATE_TEXT.get(st, st)}")
            return
        if self._qr_task and not self._qr_task.done():
            return
        self._set_qr(state="starting", url=None, error=None, hint=None)
        self._qr_task = asyncio.create_task(self._qr_flow(), name="tg-qr")

    async def _qr_flow(self) -> None:
        c = self.client
        assert c is not None
        try:
            qr = await c.qr_login()
            while True:
                self._set_qr(state="waiting", url=qr.url, expires=qr.expires.timestamp(), error=None)
                try:
                    await qr.wait()
                    break
                except asyncio.TimeoutError:
                    await qr.recreate()
                    continue
                except errors.SessionPasswordNeededError:
                    await self._password_flow(c)
                    break
            await self._after_login()
            self._set_qr(state="done", url=None, error=None)
            log.info("вход по QR выполнен")
        except asyncio.CancelledError:
            self._set_qr(state="idle", url=None)
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("вход по QR: %s", type(e).__name__)
            self._set_qr(state="error", url=None, error=f"{type(e).__name__}: {e}")

    async def _password_flow(self, c: TelegramClient) -> None:
        hint = None
        try:
            pw = await c(functions.account.GetPasswordRequest())
            hint = pw.hint
        except Exception:  # noqa: BLE001
            pass
        while True:
            self._pw_future = asyncio.get_running_loop().create_future()
            self._set_qr(state="password", url=None, hint=hint)
            password = await self._pw_future
            self._pw_future = None
            try:
                await c.sign_in(password=password)
                return
            except errors.PasswordHashInvalidError:
                self._set_qr(error="Неверный пароль, попробуйте еще раз.")
            finally:
                password = None  # noqa: F841 — не держим пароль дольше нужного

    def submit_password(self, password: str) -> None:
        """Вызывать в loop. Пароль нигде не сохраняется и не логируется."""
        if self._pw_future and not self._pw_future.done():
            self._set_qr(state="checking", error=None)
            self._pw_future.set_result(password)

    async def _cancel_qr(self) -> None:
        t, self._qr_task = self._qr_task, None
        if t and not t.done():
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        self._pw_future = None
        if self._qr.get("state") not in ("done",):
            self._set_qr(state="idle", url=None)

    async def cancel_qr(self) -> None:
        await self._cancel_qr()
        self._set_qr(state="idle", url=None, error=None)

    # ---------- выход из аккаунта ----------
    async def log_out(self) -> bool:
        """Завершить сеанс на сервере и удалить ключ сессии из хранилища."""
        c = self.client
        ok = False
        if c is not None and self.state == ONLINE:
            self._set(NEED_LOGIN, me=None, security=None)  # до log_out: _drop_client не должен пересохранить ключ
            try:
                ok = bool(await c.log_out())
            except Exception:  # noqa: BLE001
                log.warning("[%s] log_out", self.account_id, exc_info=True)
        self.store.delete(self.account_id)
        try:
            self.avatar_path.unlink(missing_ok=True)
        except OSError:
            pass
        log.info("[%s] выход из аккаунта: %s", self.account_id, ok)
        self._set(NEED_LOGIN, me=None, security=None)
        self.reconfigure()  # клиент пересоздастся с пустой сессией
        return ok
