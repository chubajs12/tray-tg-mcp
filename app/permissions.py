"""Права MCP по аккаунтам. Меняются только руками в окне приложения (раздел «Права MCP»), через MCP — нельзя."""

from __future__ import annotations

from typing import Any

# группа → заголовок для окна и сообщений
GROUPS: dict[str, str] = {
    "read": "Чтение (чаты, история, поиск, инфо, участники, контакты, темы)",
    "download": "Скачивание вложений",
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

DEFAULT_PRESET = "read_only"

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
    return dict(PRESETS[DEFAULT_PRESET])


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
