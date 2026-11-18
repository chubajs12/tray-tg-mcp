"""Компактное представление сообщений, диалогов и пользователей Telethon в виде JSON-словарей."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from telethon import types, utils


def iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    try:
        return dt.astimezone().isoformat(timespec="seconds")
    except (ValueError, OSError):
        return dt.isoformat()


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(c for c in str(phone) if c.isdigit())
    if len(digits) <= 4:
        return "+" + "*" * len(digits)
    return "+" + digits[:1] + "*" * (len(digits) - 5) + digits[-4:]


def display_name(entity: Any) -> str | None:
    if entity is None:
        return None
    if isinstance(entity, types.User):
        name = " ".join(x for x in (entity.first_name, entity.last_name) if x)
        if not name:
            name = "Удаленный аккаунт" if getattr(entity, "deleted", False) else (entity.username or str(entity.id))
        return name
    title = getattr(entity, "title", None)
    if title:
        return title
    return utils.get_display_name(entity) or None


def entity_type(entity: Any) -> str:
    if isinstance(entity, types.User):
        return "bot" if entity.bot else "user"
    if isinstance(entity, (types.Chat, types.ChatForbidden)):
        return "group"
    if isinstance(entity, (types.Channel, types.ChannelForbidden)):
        if getattr(entity, "forum", False):
            return "forum"
        if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
            return "supergroup"
        return "channel"
    return "unknown"


def peer_id(entity: Any) -> int | None:
    try:
        return utils.get_peer_id(entity)
    except (TypeError, ValueError):
        return None


def format_user(u: Any, with_phone: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": peer_id(u),
        "name": display_name(u),
        "username": getattr(u, "username", None),
    }
    if getattr(u, "bot", False):
        d["bot"] = True
    if getattr(u, "deleted", False):
        d["deleted"] = True
    if with_phone and getattr(u, "phone", None):
        d["phone"] = "+" + u.phone.lstrip("+")
    return {k: v for k, v in d.items() if v is not None}


def short_entity(e: Any) -> dict[str, Any]:
    d = {"id": peer_id(e), "title": display_name(e), "type": entity_type(e),
         "username": getattr(e, "username", None)}
    return {k: v for k, v in d.items() if v is not None}


def _size_h(n: int | None) -> str | None:
    if not n:
        return None
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return None


def media_summary(msg: Any) -> dict[str, Any] | None:
    """Краткое описание медиа сообщения (без загрузки)."""
    media = getattr(msg, "media", None)
    if media is None:
        return None
    if isinstance(media, types.MessageMediaPhoto):
        return {"kind": "photo"}
    if isinstance(media, types.MessageMediaDocument):
        doc = media.document
        d: dict[str, Any] = {"kind": "document"}
        if isinstance(doc, types.Document):
            d["mime"] = doc.mime_type
            d["size"] = _size_h(doc.size)
            for a in doc.attributes:
                if isinstance(a, types.DocumentAttributeFilename):
                    d["file_name"] = a.file_name
                elif isinstance(a, types.DocumentAttributeSticker):
                    d["kind"] = "sticker"
                    d["emoji"] = a.alt
                elif isinstance(a, types.DocumentAttributeVideo):
                    d["kind"] = "video_note" if a.round_message else "video"
                    d["duration"] = int(a.duration or 0)
                elif isinstance(a, types.DocumentAttributeAudio):
                    d["kind"] = "voice" if a.voice else "audio"
                    d["duration"] = int(a.duration or 0)
                    if a.title:
                        d["title"] = a.title
                elif isinstance(a, types.DocumentAttributeAnimated):
                    d["kind"] = "gif"
        return {k: v for k, v in d.items() if v is not None}
    if isinstance(media, types.MessageMediaWebPage):
        wp = media.webpage
        url = getattr(wp, "url", None)
        return {k: v for k, v in {"kind": "webpage", "url": url,
                                   "title": getattr(wp, "title", None)}.items() if v}
    if isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue)):
        geo = getattr(media, "geo", None)
        d = {"kind": "geo"}
        if isinstance(geo, types.GeoPoint):
            d.update(lat=geo.lat, long=geo.long)
        if isinstance(media, types.MessageMediaVenue):
            d["title"] = media.title
        return d
    if isinstance(media, types.MessageMediaContact):
        return {"kind": "contact", "name": " ".join(x for x in (media.first_name, media.last_name) if x)}
    if isinstance(media, types.MessageMediaPoll):
        q = media.poll.question
        q = getattr(q, "text", q)
        return {"kind": "poll", "question": q}
    if isinstance(media, types.MessageMediaDice):
        return {"kind": "dice", "emoji": media.emoticon, "value": media.value}
    name = type(media).__name__.replace("MessageMedia", "").lower() or "media"
    return {"kind": name}


def topic_and_reply(msg: Any, is_forum: bool = False) -> tuple[int | None, int | None]:
    """(topic_id, reply_to_msg_id) с учетом тем форумов."""
    rt = getattr(msg, "reply_to", None)
    if not isinstance(rt, types.MessageReplyHeader):
        return (1 if is_forum else None), None
    if rt.forum_topic:
        if rt.reply_to_top_id:
            return rt.reply_to_top_id, rt.reply_to_msg_id
        # ответ на корень темы = просто сообщение в теме
        return rt.reply_to_msg_id, None
    if is_forum:
        return 1, rt.reply_to_msg_id
    return None, rt.reply_to_msg_id


def format_message(msg: Any, *, is_forum: bool = False, text_limit: int | None = None) -> dict[str, Any]:
    text = getattr(msg, "message", None) or ""
    if isinstance(msg, types.MessageService):
        text = ""
    if text_limit and len(text) > text_limit:
        text = text[:text_limit] + "…"
    sender = getattr(msg, "sender", None)
    sender_id = getattr(msg, "sender_id", None)
    d: dict[str, Any] = {
        "id": msg.id,
        "date": iso(getattr(msg, "date", None)),
        "from": ({k: v for k, v in {"id": sender_id, "name": display_name(sender),
                                    "username": getattr(sender, "username", None)}.items() if v is not None}
                 if sender_id is not None else None),
        "out": bool(getattr(msg, "out", False)) or None,
        "text": text,
    }
    topic, reply_to = topic_and_reply(msg, is_forum)
    if reply_to:
        d["reply_to"] = reply_to
    if topic:
        d["topic"] = topic
    m = media_summary(msg)
    if m:
        d["media"] = m
    if isinstance(msg, types.MessageService):
        d["service"] = type(msg.action).__name__.replace("MessageAction", "")
    fwd = getattr(msg, "fwd_from", None)
    if fwd is not None:
        d["forwarded"] = {k: v for k, v in {
            "from_id": peer_id(fwd.from_id) if fwd.from_id else None,
            "from_name": fwd.from_name,
            "date": iso(fwd.date)}.items() if v is not None}
    if getattr(msg, "edit_date", None):
        d["edited"] = iso(msg.edit_date)
    reactions = getattr(msg, "reactions", None)
    if reactions and getattr(reactions, "results", None):
        r = {}
        for rc in reactions.results:
            key = getattr(rc.reaction, "emoticon", None) or "custom"
            r[key] = r.get(key, 0) + rc.count
        d["reactions"] = r
    return {k: v for k, v in d.items() if v is not None}


def format_dialog(dialog: Any, last_text_limit: int = 200) -> dict[str, Any]:
    e = dialog.entity
    typ = entity_type(e)
    is_forum = typ == "forum"
    d: dict[str, Any] = {
        "id": dialog.id,
        "title": display_name(e) or dialog.name,
        "type": typ,
        "username": getattr(e, "username", None),
        "unread": dialog.unread_count,
        "mentions": dialog.unread_mentions_count,
        "forum": is_forum,
    }
    if getattr(dialog, "pinned", False):
        d["pinned"] = True
    if getattr(dialog, "archived", False):
        d["archived"] = True
    msg = dialog.message
    if msg is not None:
        last = format_message(msg, is_forum=is_forum, text_limit=last_text_limit)
        d["last_message"] = {k: last[k] for k in ("id", "date", "from", "text", "media", "out", "topic")
                             if k in last}
    return {k: v for k, v in d.items() if v is not None}
