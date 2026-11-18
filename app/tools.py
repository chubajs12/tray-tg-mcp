"""Инструменты MCP поверх Telethon.

Правила:
- чтение никогда не помечает сообщения прочитанными (только явный mark_read);
- запись — через WriteLimiter (не чаще ~1 операции в секунду, блок после FloodWait);
- каждый вызов пишется в журнал действий: параметры (тексты отправленного — да), для чтения — только
  чат и число сообщений, без содержимого; пароли/секреты прокси — никогда.
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field
from telethon import errors, functions, types, utils

from . import paths
from .accounts import AccountError, AccountManager
from .config import Config, ConfigError, mask_proxy, normalize_proxy, proxy_title
from .formatting import (entity_type, format_dialog, format_message, format_user, iso,
                         short_entity)
from .logs import ActionLog
from .permissions import (GROUPS, PRESET_TITLES, SAVED_ONLY_DENIED, SAVED_TARGET_TOOLS, TOOL_GROUP,
                          preset_of)
from .telegram import NotReady, TelegramService

log = logging.getLogger("tgmcp.tools")

MAX_TEXT = 4096
MAX_CAPTION = 1024
DIALOG_CACHE_TTL = 120


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _read_summary(result: Any) -> Any:
    """Для журнала чтения: без содержимого, только чат и количество."""
    if not isinstance(result, dict):
        return None
    s: dict[str, Any] = {}
    chat = result.get("chat")
    if isinstance(chat, dict):
        s["chat"] = {k: chat.get(k) for k in ("id", "title") if chat.get(k) is not None}
    for k in ("count", "path", "found"):
        if k in result:
            s[k] = result[k]
    for k in ("messages", "chats", "users", "topics", "contacts"):
        if isinstance(result.get(k), list):
            s.setdefault("count", len(result[k]))
    return s or None


_current: contextvars.ContextVar[TelegramService | None] = contextvars.ContextVar("tgmcp_account", default=None)
# True — вызов разрешен только с целью «Избранное» (право send_saved без общего права группы)
_saved_only: contextvars.ContextVar[bool] = contextvars.ContextVar("tgmcp_saved_only", default=False)


def denied_message(group: str, label: str) -> str:
    return (f"Запрещено настройками прав в трее: «{GROUPS[group]}» для аккаунта «{label}». "
            "Права меняет только пользователь: окно TG MCP → раздел «Права MCP».")


def tool(kind: Literal["read", "write", "config"], account: Literal["required", "optional", "none"] = "required"):
    """Обертка инструмента: аккаунт, права, лимит записи, журнал, ошибки → ToolError, результат → JSON.

    account="required" — инструмент работает с аккаунтом (параметр account; права проверяются здесь);
    "optional" — аккаунт выбирается, если указан (права проверяет сам инструмент);
    "none" — аккаунт не нужен.
    """

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(self: "Tools", *args, **kwargs) -> str:
            name = fn.__name__
            params = dict(kwargs)
            acc_param = params.pop("account", None) if account != "none" else None
            acc_info: dict[str, Any] | None = None
            self.calls += 1
            svc: TelegramService | None = None
            token = None
            token_saved = None
            try:
                if account == "required" or (account == "optional" and acc_param):
                    svc = self.manager.resolve(acc_param)
                    acc_info = {"id": svc.account_id, "label": svc.label}
                    group = TOOL_GROUP.get(name)
                    perms = self.config.permissions(svc.account_id)
                    if account == "required" and group and not perms.get(group):
                        if name in SAVED_TARGET_TOOLS and (perms.get("send_saved") or perms.get("send")):
                            token_saved = _saved_only.set(True)  # цель проверит сам инструмент
                        else:
                            raise ToolError(denied_message(group, svc.label))
                token = _current.set(svc)
                if kind == "write" and svc is not None:
                    wait = svc.limiter.blocked_for()
                    if wait > 0:
                        raise ToolError(f"Запись временно запрещена после FloodWait: еще {int(wait) + 1} с.")
                    await svc.limiter.acquire()
                result = await fn(self, *args, **kwargs)
            except ToolError as e:
                self.actions.write(name, params, False, error=str(e), account=acc_info)
                raise
            except (NotReady, AccountError, ConfigError, FileNotFoundError) as e:
                self.actions.write(name, params, False, error=str(e), account=acc_info)
                raise ToolError(str(e)) from None
            except errors.FloodWaitError as e:
                if kind == "write" and svc is not None:
                    svc.limiter.block(e.seconds)
                msg = f"Telegram ограничил частоту (FloodWait): повторять не раньше чем через {e.seconds} с."
                self.actions.write(name, params, False, error=msg, account=acc_info)
                raise ToolError(msg) from None
            except errors.RPCError as e:
                msg = f"Ошибка Telegram: {type(e).__name__}: {e}"
                self.actions.write(name, params, False, error=msg, account=acc_info)
                raise ToolError(msg) from None
            except Exception as e:  # noqa: BLE001
                log.exception("инструмент %s", name)
                msg = f"{type(e).__name__}: {e}"
                self.actions.write(name, params, False, error=msg, account=acc_info)
                raise ToolError(msg) from None
            finally:
                if token is not None:
                    _current.reset(token)
                if token_saved is not None:
                    _saved_only.reset(token_saved)
            self.actions.write(name, params, True, account=acc_info,
                               result=_read_summary(result) if kind == "read" else result)
            return _dumps(result)

        wrapper.tool_kind = kind  # type: ignore[attr-defined]
        wrapper.account_mode = account  # type: ignore[attr-defined]
        return wrapper

    return deco


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"Дата в формате ISO, например 2026-11-16 или 2026-11-16T09:00:00+03:00: {s!r}") from None
    if d.tzinfo is None:
        d = d.astimezone()  # локальное время ПК
    return d.astimezone(timezone.utc)


def _clamp(n: int | None, lo: int, hi: int, default: int) -> int:
    if n is None:
        return default
    return max(lo, min(hi, int(n)))


_LINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(.+)$", re.I)

ChatArg = Annotated[str, Field(description="Чат: числовой id (как в list_chats), @username, ссылка t.me или название (точное или часть)")]
AccountArg = Annotated[str | None, Field(description="Аккаунт: метка (например «Личный»), id или @username из list_accounts; можно не указывать, если аккаунт один")]


class Tools:
    def __init__(self, manager: AccountManager, config: Config, actions: ActionLog,
                 downloads_dir: Path | None = None):
        self.manager = manager
        self.config = config
        self.actions = actions
        self.downloads_dir = Path(downloads_dir or paths.DOWNLOADS_DIR)
        self.calls = 0

    # ---------- вспомогательное ----------
    @property
    def service(self) -> TelegramService:
        svc = _current.get()
        if svc is None:
            raise NotReady("Аккаунт не выбран.")
        return svc

    @property
    def client(self):
        return self.service.require()

    async def _is_saved(self, entity: Any) -> bool:
        """Цель — собственное «Избранное» аккаунта (сравнение по id после резолва, не по строке)."""
        if not isinstance(entity, types.User):
            return False
        if getattr(entity, "is_self", False):
            return True
        me = await self.client.get_me()
        return utils.get_peer_id(entity) == utils.get_peer_id(me)

    async def _require_saved_if_limited(self, entity: Any) -> None:
        if _saved_only.get() and not await self._is_saved(entity):
            raise ToolError(SAVED_ONLY_DENIED + f" (аккаунт «{self.service.label}»).")

    async def _all_dialogs(self, force: bool = False) -> list[Any]:
        svc = self.service
        if force or not svc.dialog_cache or time.monotonic() - svc.dialog_cache_at > DIALOG_CACHE_TTL:
            svc.dialog_cache = [d async for d in self.client.iter_dialogs()]
            svc.dialog_cache_at = time.monotonic()
        return svc.dialog_cache

    async def _entity(self, chat: str | int) -> Any:
        c = self.client
        s = str(chat).strip()
        if not s:
            raise ToolError("Не указан чат.")
        if s.lower() in ("me", "self", "избранное", "saved"):
            return await c.get_me()
        if re.fullmatch(r"-?\d+", s):
            pid = int(s)
            try:
                return await c.get_entity(pid)
            except (ValueError, TypeError):
                for d in await self._all_dialogs(force=True):
                    if d.id == pid:
                        return d.entity
                raise ToolError(f"Чат с id {pid} не найден среди диалогов.") from None
        m = _LINK_RE.match(s)
        if s.startswith("@") or m:
            target = s.lstrip("@") if not m else m.group(1).split("?")[0].split("/")[0]
            if target.startswith("+") or target == "joinchat":
                raise ToolError("Это ссылка-приглашение — используйте resolve (вступление не выполняется).")
            try:
                return await c.get_entity(target)
            except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
                raise ToolError(f"Не найден чат/пользователь {s}.") from None
        # поиск по названию среди диалогов
        dialogs = await self._all_dialogs()
        key = s.casefold()
        exact = [d for d in dialogs if (d.name or "").casefold() == key]
        cands = exact or [d for d in dialogs if key in (d.name or "").casefold()]
        if len(cands) == 1:
            return cands[0].entity
        if len(cands) > 1:
            lst = ", ".join(f"{d.name} ({d.id})" for d in cands[:10])
            raise ToolError(f"Под «{s}» подходят несколько чатов, укажите id: {lst}")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", s):
            try:
                return await c.get_entity(s)
            except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
                pass
        raise ToolError(f"Чат «{s}» не найден.")

    @staticmethod
    def _is_forum(entity: Any) -> bool:
        return entity_type(entity) == "forum"

    def _parse_mode(self, parse_mode: str | None):
        pm = (parse_mode or "markdown").lower()
        if pm in ("none", "plain", "text", ""):
            return None
        if pm in ("md", "markdown"):
            return "md"
        if pm == "html":
            return "html"
        raise ToolError("parse_mode: markdown, html или none")

    # ================= чтение =================
    @tool("read")
    async def get_me(self, account: AccountArg = None) -> dict:
        """Текущий аккаунт Telegram и состояние подключения."""
        me = await self.client.get_me()
        u = format_user(me)
        snap = self.service.snapshot()
        return {"user": u, "state": snap["state_text"], "proxy": snap["proxy"]}

    async def _folder_filter(self, folder: str):
        """Пользовательская папка Telegram (DialogFilter) → функция-фильтр по диалогу."""
        res = await self.client(functions.messages.GetDialogFiltersRequest())
        filters = getattr(res, "filters", res)
        key = folder.strip().casefold()
        found = None
        for f in filters:
            title = getattr(getattr(f, "title", None), "text", getattr(f, "title", None))
            if isinstance(title, str) and (title.casefold() == key or str(getattr(f, "id", "")) == folder.strip()):
                found = f
                break
        if found is None:
            names = [getattr(getattr(f, "title", None), "text", getattr(f, "title", None)) for f in filters]
            names = [n for n in names if isinstance(n, str)]
            raise ToolError(f"Папка «{folder}» не найдена. Есть: {', '.join(names) or 'нет пользовательских папок'}"
                            ". Также: main (основная), archive (архив).")

        def pid(p):
            try:
                return utils.get_peer_id(p)
            except (TypeError, ValueError):
                return None

        include = {pid(p) for p in getattr(found, "include_peers", [])} | {
            pid(p) for p in getattr(found, "pinned_peers", [])}
        exclude = {pid(p) for p in getattr(found, "exclude_peers", []) or []}

        def match(d) -> bool:
            if d.id in exclude:
                return False
            if d.id in include:
                return True
            if isinstance(found, types.DialogFilterChatlist):
                return False
            e = d.entity
            t = entity_type(e)
            ok = False
            if isinstance(e, types.User):
                if e.bot:
                    ok = bool(found.bots)
                elif e.contact:
                    ok = bool(found.contacts)
                else:
                    ok = bool(found.non_contacts)
            elif t in ("group", "supergroup", "forum"):
                ok = bool(found.groups)
            elif t == "channel":
                ok = bool(found.broadcasts)
            if not ok:
                return False
            if found.exclude_read and not d.unread_count and not getattr(d.dialog, "unread_mark", False):
                return False
            if found.exclude_archived and d.archived:
                return False
            if found.exclude_muted:
                ns = getattr(d.dialog, "notify_settings", None)
                mu = getattr(ns, "mute_until", None)
                if mu and (mu.timestamp() if hasattr(mu, "timestamp") else mu) > time.time():
                    return False
            return True

        return match

    @tool("read")
    async def list_chats(
        self,
        unread_only: Annotated[bool, Field(description="Только чаты с непрочитанными")] = False,
        chat_type: Annotated[Literal["any", "user", "bot", "group", "supergroup", "forum", "channel"] | None,
                             Field(description="Тип чата")] = None,
        folder: Annotated[str | None, Field(description="Папка: main (основная), archive (архив) или название/id пользовательской папки Telegram")] = None,
        limit: Annotated[int, Field(description="Сколько чатов вернуть (1..500)")] = 50,
        account: AccountArg = None,
    ) -> dict:
        """Список диалогов (чаты, группы, каналы) с числом непрочитанных, упоминаний и последним сообщением. Ничего не помечает прочитанным."""
        limit = _clamp(limit, 1, 500, 50)
        c = self.client
        kw: dict[str, Any] = {}
        match = None
        if folder:
            f = folder.strip().casefold()
            if f in ("main", "основная", "0"):
                kw["folder"] = 0
            elif f in ("archive", "архив", "1"):
                kw["folder"] = 1
            else:
                match = await self._folder_filter(folder)
        out = []
        scanned = 0
        filtered = unread_only or (chat_type and chat_type != "any") or match is not None
        async for d in c.iter_dialogs(limit=None if filtered else limit, **kw):
            scanned += 1
            if unread_only and not (d.unread_count or d.unread_mentions_count
                                    or getattr(d.dialog, "unread_mark", False)):
                continue
            if chat_type and chat_type != "any" and entity_type(d.entity) != chat_type:
                continue
            if match is not None and not match(d):
                continue
            out.append(format_dialog(d))
            if len(out) >= limit:
                break
        return {"count": len(out), "chats": out}

    @tool("read")
    async def get_messages(
        self,
        chat: ChatArg,
        limit: Annotated[int, Field(description="Сколько сообщений (1..200)")] = 20,
        offset_id: Annotated[int, Field(description="Брать сообщения старше этого id (листание назад)")] = 0,
        min_id: Annotated[int, Field(description="Только сообщения новее этого id")] = 0,
        topic: Annotated[int | None, Field(description="id темы форума (из list_topics)")] = None,
        since: Annotated[str | None, Field(description="С даты/времени ISO (2026-11-16 или 2026-11-16T09:00:00+03:00); тогда берутся самые ранние после нее")] = None,
        account: AccountArg = None,
    ) -> dict:
        """Сообщения чата в хронологическом порядке (по умолчанию — последние). Не помечает прочитанным."""
        limit = _clamp(limit, 1, 200, 20)
        entity = await self._entity(chat)
        kw: dict[str, Any] = {"limit": limit}
        if offset_id:
            kw["offset_id"] = int(offset_id)
        if min_id:
            kw["min_id"] = int(min_id)
        if topic:
            kw["reply_to"] = int(topic)
        since_d = _parse_date(since)
        if since_d:
            kw["offset_date"] = since_d
            kw["reverse"] = True
        msgs = [m async for m in self.client.iter_messages(entity, **kw)]
        if not since_d:
            msgs.reverse()
        forum = self._is_forum(entity)
        return {"chat": short_entity(entity), "count": len(msgs),
                "messages": [format_message(m, is_forum=forum) for m in msgs]}

    @tool("read")
    async def get_message(
        self,
        chat: ChatArg,
        message_id: Annotated[int, Field(description="id сообщения")],
        account: AccountArg = None,
    ) -> dict:
        """Одно сообщение по id (полный текст). Не помечает прочитанным."""
        entity = await self._entity(chat)
        m = await self.client.get_messages(entity, ids=int(message_id))
        if m is None:
            raise ToolError(f"Сообщение {message_id} не найдено.")
        return {"chat": short_entity(entity), "count": 1,
                "message": format_message(m, is_forum=self._is_forum(entity))}

    @tool("read")
    async def search_messages(
        self,
        query: Annotated[str, Field(description="Текст для поиска")],
        chat: Annotated[str | None, Field(description="Чат (необязательно); без него — поиск по всем чатам")] = None,
        limit: Annotated[int, Field(description="Сколько результатов (1..100)")] = 20,
        topic: Annotated[int | None, Field(description="id темы форума (только вместе с chat)")] = None,
        account: AccountArg = None,
    ) -> dict:
        """Поиск сообщений по тексту — в одном чате или по всем. Не помечает прочитанным."""
        limit = _clamp(limit, 1, 100, 20)
        if not query or not query.strip():
            raise ToolError("Пустой запрос.")
        entity = await self._entity(chat) if chat else None
        kw: dict[str, Any] = {"search": query, "limit": limit}
        if topic and entity is not None:
            kw["reply_to"] = int(topic)
        out = []
        async for m in self.client.iter_messages(entity, **kw):
            ch = entity if entity is not None else getattr(m, "chat", None)
            d = format_message(m, is_forum=self._is_forum(ch) if ch is not None else False)
            if entity is None and ch is not None:
                d["chat"] = short_entity(ch)
            out.append(d)
        res: dict[str, Any] = {"count": len(out), "messages": out}
        if entity is not None:
            res["chat"] = short_entity(entity)
        return res

    @tool("read")
    async def list_topics(
        self,
        chat: ChatArg,
        query: Annotated[str | None, Field(description="Фильтр по названию темы")] = None,
        limit: Annotated[int, Field(description="Сколько тем (1..100)")] = 50,
        account: AccountArg = None,
    ) -> dict:
        """Темы форума (супергруппы с темами): id, название, непрочитанные, упоминания."""
        entity = await self._entity(chat)
        if not self._is_forum(entity):
            raise ToolError("Это не форум (в чате нет тем).")
        res = await self.client(functions.messages.GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0, offset_topic=0,
            limit=_clamp(limit, 1, 100, 50), q=query or None))
        topics = []
        for t in res.topics:
            if not isinstance(t, types.ForumTopic):
                continue
            d = {"id": t.id, "title": t.title, "unread": t.unread_count,
                 "mentions": t.unread_mentions_count, "top_message": t.top_message, "date": iso(t.date)}
            if t.closed:
                d["closed"] = True
            if t.pinned:
                d["pinned"] = True
            if t.hidden:
                d["hidden"] = True
            topics.append(d)
        return {"chat": short_entity(entity), "count": len(topics), "topics": topics}

    @tool("read")
    async def get_chat_info(self, chat: ChatArg, account: AccountArg = None) -> dict:
        """Сведения о чате, канале или пользователе: описание, число участников, username, связанный чат."""
        c = self.client
        e = await self._entity(chat)
        info: dict[str, Any] = short_entity(e)
        if isinstance(e, types.User):
            full = await c(functions.users.GetFullUserRequest(e))
            fu = full.full_user
            info.update(about=fu.about, bot=e.bot or None, contact=e.contact or None,
                        mutual_contact=e.mutual_contact or None, common_chats=fu.common_chats_count,
                        blocked=fu.blocked or None)
        elif isinstance(e, types.Chat):
            full = await c(functions.messages.GetFullChatRequest(e.id))
            fc = full.full_chat
            info.update(about=fc.about, participants=e.participants_count,
                        created=iso(e.date))
        elif isinstance(e, types.Channel):
            full = await c(functions.channels.GetFullChannelRequest(e))
            fc = full.full_chat
            info.update(about=fc.about, participants=fc.participants_count, created=iso(e.date),
                        forum=bool(e.forum) or None,
                        linked_chat_id=(utils.get_peer_id(types.PeerChannel(fc.linked_chat_id))
                                        if fc.linked_chat_id else None),
                        slowmode_seconds=fc.slowmode_seconds,
                        unread=getattr(fc, "unread_count", None))
        return {k: v for k, v in info.items() if v not in (None, "")}

    @tool("read")
    async def get_participants(
        self,
        chat: ChatArg,
        search: Annotated[str, Field(description="Фильтр по имени/username")] = "",
        limit: Annotated[int, Field(description="Сколько участников (1..1000)")] = 100,
        account: AccountArg = None,
    ) -> dict:
        """Участники группы/канала (для каналов — если есть права администратора)."""
        e = await self._entity(chat)
        users = await self.client.get_participants(e, limit=_clamp(limit, 1, 1000, 100), search=search or "")
        return {"chat": short_entity(e), "count": len(users), "total": getattr(users, "total", None),
                "users": [format_user(u) for u in users]}

    @tool("read")
    async def list_contacts(
        self,
        include_phones: Annotated[bool, Field(description="Включить номера телефонов контактов")] = False,
        account: AccountArg = None,
    ) -> dict:
        """Контакты аккаунта."""
        res = await self.client(functions.contacts.GetContactsRequest(hash=0))
        users = [format_user(u, with_phone=include_phones) for u in getattr(res, "users", [])]
        return {"count": len(users), "contacts": users}

    @tool("read")
    async def download_media(
        self,
        chat: ChatArg,
        message_id: Annotated[int, Field(description="id сообщения с медиа")],
        account: AccountArg = None,
    ) -> dict:
        """Скачать медиа сообщения в data\\downloads и вернуть путь к файлу."""
        e = await self._entity(chat)
        m = await self.client.get_messages(e, ids=int(message_id))
        if m is None:
            raise ToolError(f"Сообщение {message_id} не найдено.")
        if not m.media or isinstance(m.media, types.MessageMediaWebPage):
            raise ToolError("В сообщении нет файла/медиа для скачивания.")
        folder = self.downloads_dir / self.service.account_id / str(utils.get_peer_id(e))
        folder.mkdir(parents=True, exist_ok=True)
        path = await self.client.download_media(m, file=str(folder) + os.sep)
        if not path:
            raise ToolError("Не удалось скачать медиа.")
        p = Path(path).resolve()
        return {"chat": short_entity(e), "message_id": m.id, "path": str(p), "size": p.stat().st_size}

    @tool("read")
    async def resolve(
        self,
        query: Annotated[str, Field(description="@username, ссылка t.me (в т.ч. на сообщение или приглашение), id или название")],
        account: AccountArg = None,
    ) -> dict:
        """Определить чат по username/ссылке/id. Приглашения только проверяются, вступление не выполняется."""
        c = self.client
        s = query.strip()
        m = _LINK_RE.match(s)
        path = m.group(1).split("?")[0].strip("/") if m else None
        invite = None
        if path:
            if path.startswith("+"):
                invite = path[1:]
            elif path.startswith("joinchat/"):
                invite = path.split("/", 1)[1]
        elif s.startswith("tg://join?invite="):
            invite = s.split("=", 1)[1]
        if invite:
            r = await c(functions.messages.CheckChatInviteRequest(hash=invite))
            if isinstance(r, (types.ChatInviteAlready, types.ChatInvitePeek)):
                return {"invite": True, "joined": isinstance(r, types.ChatInviteAlready),
                        "chat": short_entity(r.chat)}
            return {"invite": True, "joined": False, "title": r.title,
                    "participants": r.participants_count,
                    "type": "channel" if r.broadcast else "group", "request_needed": bool(r.request_needed) or None}
        if path:
            parts = path.split("/")
            msg_id = None
            if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
                e = await self._entity(str(utils.get_peer_id(types.PeerChannel(int(parts[1])))))
                if len(parts) >= 3 and parts[-1].isdigit():
                    msg_id = int(parts[-1])
            else:
                e = await self._entity("@" + parts[0])
                if len(parts) >= 2 and parts[-1].isdigit():
                    msg_id = int(parts[-1])
                    if len(parts) >= 3 and parts[1].isdigit():
                        res = {"chat": short_entity(e), "topic": int(parts[1]), "message_id": msg_id}
                        return res
            res = {"chat": short_entity(e)}
            if msg_id:
                res["message_id"] = msg_id
            return res
        e = await self._entity(s)
        return {"chat": short_entity(e)}

    # ================= запись =================
    @tool("write")
    async def send_message(
        self,
        chat: ChatArg,
        text: Annotated[str, Field(description="Текст сообщения (до 4096 символов)")],
        reply_to: Annotated[int | None, Field(description="Ответить на сообщение с этим id")] = None,
        topic: Annotated[int | None, Field(description="id темы форума, куда отправить")] = None,
        parse_mode: Annotated[Literal["markdown", "html", "none"], Field(description="Разметка текста")] = "markdown",
        silent: Annotated[bool, Field(description="Без звука у получателей")] = False,
        link_preview: Annotated[bool, Field(description="Показывать превью ссылок")] = True,
        account: AccountArg = None,
    ) -> dict:
        """Отправить текстовое сообщение. «Печатает…» не отправляется."""
        if not text or not text.strip():
            raise ToolError("Пустой текст.")
        if len(text) > MAX_TEXT:
            raise ToolError(f"Текст длиннее {MAX_TEXT} символов ({len(text)}); разбейте на части.")
        e = await self._entity(chat)
        await self._require_saved_if_limited(e)
        m = await self.client.send_message(e, text, reply_to=reply_to or topic or None,
                                           parse_mode=self._parse_mode(parse_mode), silent=silent or None,
                                           link_preview=link_preview)
        return {"chat": short_entity(e), "message_id": m.id, "date": iso(m.date)}

    @tool("write")
    async def send_file(
        self,
        chat: ChatArg,
        path: Annotated[str, Field(description="Полный путь к файлу на этом ПК")],
        caption: Annotated[str | None, Field(description="Подпись (до 1024 символов)")] = None,
        reply_to: Annotated[int | None, Field(description="Ответить на сообщение с этим id")] = None,
        topic: Annotated[int | None, Field(description="id темы форума")] = None,
        force_document: Annotated[bool, Field(description="Отправить как файл, а не как фото/видео")] = False,
        parse_mode: Annotated[Literal["markdown", "html", "none"], Field(description="Разметка подписи")] = "markdown",
        account: AccountArg = None,
    ) -> dict:
        """Отправить файл (фото, документ, видео) с подписью."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Файл не найден: {path}")
        if caption and len(caption) > MAX_CAPTION:
            raise ToolError(f"Подпись длиннее {MAX_CAPTION} символов.")
        e = await self._entity(chat)
        await self._require_saved_if_limited(e)
        m = await self.client.send_file(e, str(p), caption=caption or None, reply_to=reply_to or topic or None,
                                        force_document=force_document, parse_mode=self._parse_mode(parse_mode))
        return {"chat": short_entity(e), "message_id": m.id, "date": iso(m.date), "file": p.name}

    @tool("write")
    async def forward_messages(
        self,
        from_chat: ChatArg,
        message_ids: Annotated[list[int], Field(description="id сообщений (до 100)")],
        to_chat: Annotated[str, Field(description="Куда переслать: id, @username, ссылка или название")],
        topic: Annotated[int | None, Field(description="id темы форума в чате назначения")] = None,
        drop_author: Annotated[bool, Field(description="Скрыть автора (как копия)")] = False,
        silent: Annotated[bool, Field(description="Без звука")] = False,
        account: AccountArg = None,
    ) -> dict:
        """Переслать сообщения из одного чата в другой (одним запросом)."""
        ids = [int(i) for i in message_ids][:100]
        if not ids:
            raise ToolError("Не указаны message_ids.")
        src = await self._entity(from_chat)
        dst = await self._entity(to_chat)
        await self._require_saved_if_limited(dst)  # без права «Пересылка» — только в Избранное
        c = self.client
        if topic:
            await c(functions.messages.ForwardMessagesRequest(
                from_peer=src, id=ids, to_peer=dst, top_msg_id=int(topic),
                random_id=[random.randrange(-2**63, 2**63) for _ in ids],
                silent=silent or None, drop_author=drop_author or None))
            return {"from": short_entity(src), "to": short_entity(dst), "forwarded": len(ids), "topic": topic}
        res = await c.forward_messages(dst, ids, src, silent=silent or None, drop_author=drop_author or None)
        res = res if isinstance(res, list) else [res]
        return {"from": short_entity(src), "to": short_entity(dst),
                "forwarded": len([m for m in res if m]), "new_ids": [m.id for m in res if m]}

    @tool("write")
    async def edit_message(
        self,
        chat: ChatArg,
        message_id: Annotated[int, Field(description="id своего сообщения")],
        text: Annotated[str, Field(description="Новый текст")],
        parse_mode: Annotated[Literal["markdown", "html", "none"], Field(description="Разметка")] = "markdown",
        account: AccountArg = None,
    ) -> dict:
        """Изменить текст своего сообщения."""
        if not text or not text.strip():
            raise ToolError("Пустой текст.")
        if len(text) > MAX_TEXT:
            raise ToolError(f"Текст длиннее {MAX_TEXT} символов.")
        e = await self._entity(chat)
        m = await self.client.edit_message(e, int(message_id), text, parse_mode=self._parse_mode(parse_mode))
        return {"chat": short_entity(e), "message_id": m.id, "edited": iso(m.edit_date)}

    @tool("write")
    async def delete_messages(
        self,
        chat: ChatArg,
        message_ids: Annotated[list[int], Field(description="id сообщений (до 100)")],
        revoke: Annotated[bool, Field(description="Удалить у всех (иначе только у себя, где возможно)")] = True,
        account: AccountArg = None,
    ) -> dict:
        """Удалить сообщения."""
        ids = [int(i) for i in message_ids][:100]
        if not ids:
            raise ToolError("Не указаны message_ids.")
        e = await self._entity(chat)
        res = await self.client.delete_messages(e, ids, revoke=revoke)
        deleted = sum(getattr(r, "pts_count", 0) for r in (res or []))
        return {"chat": short_entity(e), "requested": len(ids), "deleted": deleted, "revoke": revoke}

    @tool("write")
    async def mark_read(
        self,
        chat: ChatArg,
        max_id: Annotated[int | None, Field(description="Пометить прочитанным до этого id включительно (по умолчанию — все)")] = None,
        topic: Annotated[int | None, Field(description="Только эта тема форума")] = None,
        account: AccountArg = None,
    ) -> dict:
        """Явно пометить сообщения чата (или темы) прочитанными. Другие инструменты этого не делают."""
        e = await self._entity(chat)
        c = self.client
        if topic:
            top = max_id
            if not top:
                last = [m async for m in c.iter_messages(e, limit=1, reply_to=int(topic))]
                top = last[0].id if last else int(topic)
            await c(functions.messages.ReadDiscussionRequest(peer=e, msg_id=int(topic), read_max_id=int(top)))
            return {"chat": short_entity(e), "topic": topic, "read_max_id": top}
        await c.send_read_acknowledge(e, max_id=int(max_id) if max_id else None, clear_mentions=True)
        return {"chat": short_entity(e), "read_max_id": max_id or "all"}

    @tool("write")
    async def send_reaction(
        self,
        chat: ChatArg,
        message_id: Annotated[int, Field(description="id сообщения")],
        emoji: Annotated[str | None, Field(description="Эмодзи реакции, например 👍; пусто — снять реакцию")] = "👍",
        account: AccountArg = None,
    ) -> dict:
        """Поставить (или снять) реакцию на сообщение."""
        e = await self._entity(chat)
        reaction = [types.ReactionEmoji(emoticon=emoji)] if emoji else []
        await self.client(functions.messages.SendReactionRequest(peer=e, msg_id=int(message_id), reaction=reaction))
        return {"chat": short_entity(e), "message_id": message_id, "reaction": emoji or None}

    # ================= служебные: аккаунты, права, прокси =================
    def _account_row(self, snap: dict) -> dict:
        perms = self.config.permissions(snap["account_id"])
        me = snap.get("me") or {}
        row = {
            "account_id": snap["account_id"],
            "label": snap["label"],
            "name": me.get("name"),
            "username": me.get("username"),
            "phone": me.get("phone"),  # уже маскирован
            "state": snap.get("state_text"),
            "proxy": snap.get("proxy"),
            "permissions": {g: v for g, v in perms.items()},
            "preset": PRESET_TITLES.get(preset_of(perms) or "", "особые"),
        }
        if snap.get("error"):
            row["error"] = snap["error"]
        return {k: v for k, v in row.items() if v is not None}

    @tool("config", account="none")
    async def list_accounts(self) -> dict:
        """Аккаунты Telegram в приложении: метка, имя, @username, состояние, прокси и права MCP (телефон маскирован)."""
        rows = [self._account_row(s) for s in self.manager.snapshots()]
        return {"count": len(rows), "accounts": rows}

    @tool("config", account="optional")
    async def get_permissions(self, account: AccountArg = None) -> dict:
        """Права MCP по аккаунтам (только чтение). Менять права может только пользователь в окне приложения — через MCP нельзя."""
        snaps = self.manager.snapshots()
        if account:
            svc = self.service
            snaps = [s for s in snaps if s["account_id"] == svc.account_id]
        return {"groups": GROUPS,
                "accounts": [{"account_id": s["account_id"], "label": s["label"],
                              "permissions": self.config.permissions(s["account_id"]),
                              "preset": PRESET_TITLES.get(preset_of(self.config.permissions(s["account_id"])) or "",
                                                          "особые")}
                             for s in snaps],
                "note": "Права меняются только пользователем в окне TG MCP (раздел «Права MCP»)."}

    def _proxy_state(self) -> dict:
        accs = []
        for a in self.config.accounts():
            mode = a.get("proxy", "app")
            accs.append({"account": a["label"],
                         "mode": {"app": "как у приложения", "none": "без прокси"}.get(mode, "свой"),
                         "proxy": proxy_title(self.config.account_proxy(a["id"]))})
        return {"app_proxy": mask_proxy(self.config.active_proxy()),
                "proxies": [mask_proxy(p) for p in self.config.proxies()],
                "accounts": accs}

    def _check_proxy_rights(self, scope: str) -> None:
        if scope == "account":
            svc = self.service
            if not self.config.permissions(svc.account_id).get("proxy"):
                raise ToolError(denied_message("proxy", svc.label))
            return
        accs = self.config.accounts()
        if not accs:
            raise ToolError("Нет аккаунтов: прокси приложения задается в окне TG MCP (раздел «Прокси»).")
        bad = [a["label"] for a in accs
               if a.get("proxy", "app") == "app" and not self.config.permissions(a["id"]).get("proxy")]
        if bad:
            raise ToolError("Запрещено настройками прав в трее: управление прокси не разрешено для аккаунтов "
                            + ", ".join(f"«{b}»" for b in bad) + " (они используют прокси приложения).")

    @staticmethod
    def _scope(scope: str | None, account: str | None) -> str:
        sc = (scope or ("account" if account else "app")).lower()
        if sc not in ("account", "app"):
            raise ToolError("scope: account (только этот аккаунт) или app (прокси приложения)")
        if sc == "account" and not account:
            raise ToolError("Для scope=account укажите account.")
        return sc

    @tool("config", account="none")
    async def get_proxy(self) -> dict:
        """Прокси приложения, прокси каждого аккаунта и список сохраненных (пароль и секрет скрыты)."""
        return self._proxy_state()

    @tool("config", account="optional")
    async def set_proxy(
        self,
        type: Annotated[Literal["socks5", "http", "mtproto"], Field(description="Тип прокси")],
        server: Annotated[str, Field(description="Адрес сервера прокси")],
        port: Annotated[int, Field(description="Порт")],
        username: Annotated[str | None, Field(description="Логин (SOCKS5/HTTP)")] = None,
        password: Annotated[str | None, Field(description="Пароль (SOCKS5/HTTP)")] = None,
        secret: Annotated[str | None, Field(description="Секрет (MTProto, hex; FakeTLS ee… не поддерживается)")] = None,
        scope: Annotated[Literal["account", "app"] | None, Field(description="account — только для указанного аккаунта; app — прокси приложения (по умолчанию, если account не указан)")] = None,
        account: AccountArg = None,
    ) -> dict:
        """Задать прокси (добавить в список или обновить) и включить его; клиенты переподключатся. Нужно право «Управление прокси» (задается в окне приложения)."""
        sc = self._scope(scope, account)
        self._check_proxy_rights(sc)
        p = normalize_proxy({"type": type, "server": server, "port": port, "username": username,
                             "password": password, "secret": secret})
        if sc == "app":
            self.config.upsert_proxy(p, make_active=True)
            self.manager.reconfigure_all()
        else:
            saved = self.config.upsert_proxy(p, make_active=False)
            self.config.set_account_proxy(self.service.account_id, saved["id"])
            self.manager.reconfigure(self.service.account_id)
        return self._proxy_state()

    @tool("config", account="optional")
    async def clear_proxy(
        self,
        scope: Annotated[Literal["account", "app"] | None, Field(description="account — аккаунт без прокси; app — приложение без прокси (по умолчанию, если account не указан)")] = None,
        account: AccountArg = None,
    ) -> dict:
        """Отключить прокси (прямое подключение). Сохраненный список прокси остается. Нужно право «Управление прокси»."""
        sc = self._scope(scope, account)
        self._check_proxy_rights(sc)
        if sc == "app":
            self.config.set_active_proxy(None)
            self.manager.reconfigure_all()
        else:
            self.config.set_account_proxy(self.service.account_id, "none")
            self.manager.reconfigure(self.service.account_id)
        return self._proxy_state()


TOOL_NAMES = [
    "list_accounts", "get_permissions",
    "get_me", "list_chats", "get_messages", "get_message", "search_messages", "list_topics",
    "get_chat_info", "get_participants", "list_contacts", "download_media", "resolve",
    "send_message", "send_file", "forward_messages", "edit_message", "delete_messages",
    "mark_read", "send_reaction",
    "get_proxy", "set_proxy", "clear_proxy",
]
