"""Права MCP по аккаунтам. Меняются только руками в окне приложения (раздел «Права MCP»), через MCP — нельзя."""

from __future__ import annotations

from typing import Any

# группа → заголовок для окна и сообщений
GROUPS: dict[str, str] = {
    "read": "Чтение (чаты, история, поиск, инфо, участники, контакты, темы)",
    "download": "Скачивание вложений",
    "send_saved": "Отправка только в Избранное",
    "send": "Отправка сообщений и файлов",
    "forward": "Пересылка",
    "edit": "Редактирование своих сообщений",
    "delete": "Удаление сообщений",
    "react": "Реакции",
    "mark_read": "Отметка прочитанным",
    "proxy": "Управление прокси через MCP (set_proxy/clear_proxy)",
}

PRESETS: dict[str, dict[str, bool]] = {
    "read_only": {g: g == "read" for g in GROUPS},
    "read_write": {g: g in ("read", "download", "send", "forward", "edit", "react", "mark_read") for g in GROUPS},
    "all": {g: True for g in GROUPS},
}
PRESET_TITLES = {"read_only": "Только чтение", "read_write": "Чтение и запись", "all": "Всё"}

# пояснения под тумблерами в окне «Права MCP»
GROUP_HINTS: dict[str, str] = {
    "send_saved": "send_message, send_file и пересылка — только в собственное «Избранное» аккаунта. "
                  "Общая «Отправка» включает и Избранное.",
}

# новый аккаунт: безопасный минимум — читать, скачивать и писать себе в «Избранное»
DEFAULT_NEW_ACCOUNT = {g: g in ("read", "download", "send_saved") for g in GROUPS}

# инструменты, которые при праве send_saved (или send) разрешены, если цель — «Избранное» аккаунта
SAVED_TARGET_TOOLS = {"send_message", "send_file", "forward_messages"}
SAVED_ONLY_DENIED = "Запрещено настройками прав: разрешена отправка только в Избранное"

# инструмент → группа прав (None — доступен всегда: служебные сведения без содержимого чатов)
TOOL_GROUP: dict[str, str | None] = {
    "list_accounts": None,
    "get_permissions": None,
    "get_proxy": None,
    "get_me": "read",
    "list_chats": "read",
    "get_messages": "read",
    "get_message": "read",
    "search_messages": "read",
    "list_topics": "read",
    "get_chat_info": "read",
    "get_participants": "read",
    "list_contacts": "read",
    "resolve": "read",
    "download_media": "download",
    "send_message": "send",
    "send_file": "send",
    "forward_messages": "forward",
    "edit_message": "edit",
    "delete_messages": "delete",
    "send_reaction": "react",
    "mark_read": "mark_read",
    "set_proxy": "proxy",
    "clear_proxy": "proxy",
}


def default_permissions() -> dict[str, bool]:
    return dict(DEFAULT_NEW_ACCOUNT)


def migrate(perms: Any) -> tuple[dict[str, bool], bool]:
    """Права старого формата (без send_saved) → добавить send_saved=True. Возвращает (права, изменены ли)."""
    raw = perms if isinstance(perms, dict) else {}
    changed = False
    if "send_saved" not in raw:
        raw = {**raw, "send_saved": True}
        changed = True
    return normalize(raw), changed


def normalize(perms: Any) -> dict[str, bool]:
    """Неизвестные ключи отбрасываются, отсутствующие — запрещены."""
    perms = perms if isinstance(perms, dict) else {}
    return {g: bool(perms.get(g, False)) for g in GROUPS}


def preset_of(perms: dict[str, bool]) -> str | None:
    for name, p in PRESETS.items():
        if normalize(perms) == p:
            return name
    return None


class PermissionDenied(Exception):
    pass
