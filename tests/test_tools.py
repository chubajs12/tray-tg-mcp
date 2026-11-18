from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from telethon import errors, functions, types

from app.formatting import format_dialog, format_message, mask_phone
from app.permissions import GROUPS, PRESETS, TOOL_GROUP
from app.ratelimit import WriteLimiter
from app.tools import TOOL_NAMES

from conftest import DT, AIter, journal, make_msg, make_user



async def add(env, label="Личный", preset="read_only"):
    acc_id = await env.mgr.add_account(label)
    env.cfg.set_permissions(acc_id, PRESETS[preset])
    return acc_id, env.mgr.services[acc_id]


# ---------- форматирование ----------
async def test_format_message_reply_topic_media():
    u = make_user()
    doc = types.Document(id=1, access_hash=1, file_reference=b"", date=DT, mime_type="application/pdf",
                         size=2048, dc_id=2, attributes=[types.DocumentAttributeFilename("акт.pdf")])
    m = make_msg(mid=7, text="см. файл", sender=u,
                 reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=55, reply_to_top_id=40),
                 media=types.MessageMediaDocument(document=doc))
    d = format_message(m, is_forum=True)
    assert d["id"] == 7 and d["text"] == "см. файл"
    assert d["date"].startswith("2026-11-16T")
    assert d["from"] == {"id": 10, "name": "Иван Петров", "username": "ivan"}
    assert d["topic"] == 40 and d["reply_to"] == 55
    assert d["media"] == {"kind": "document", "mime": "application/pdf", "size": "2.0 КБ", "file_name": "акт.pdf"}


async def test_format_message_topic_root_is_not_reply():
    m = make_msg(reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=40))
    d = format_message(m, is_forum=True)
    assert d["topic"] == 40 and "reply_to" not in d


async def test_format_dialog():
    ch = types.Channel(id=123, title="Форум", photo=types.ChatPhotoEmpty(), date=DT, megagroup=True, forum=True,
                       access_hash=1)
    dlg = SimpleNamespace(entity=ch, id=-100123, name="Форум", unread_count=3, unread_mentions_count=1,
                          pinned=True, archived=False, message=make_msg(text="x" * 300))
    d = format_dialog(dlg)
    assert d["type"] == "forum" and d["forum"] is True and d["unread"] == 3 and d["mentions"] == 1
    assert d["last_message"]["text"].endswith("…") and len(d["last_message"]["text"]) == 201


async def test_mask_phone():
    assert mask_phone("79991234567") == "+7******4567"
    assert mask_phone(None) is None


# ---------- чтение не помечает прочитанным ----------
async def test_read_tools_never_mark_read(env):
    acc, svc = await add(env)
    msgs = [make_msg(mid=i, text=f"секрет {i}") for i in range(3, 0, -1)]
    svc.client.iter_messages = MagicMock(side_effect=lambda *a, **k: AIter(msgs))
    svc.client.get_messages = AsyncMock(return_value=msgs[0])
    out = json.loads(await env.tools.get_messages(chat="@ivan", limit=3))
    assert [m["id"] for m in out["messages"]] == [1, 2, 3]  # хронологически
    await env.tools.get_message(chat="@ivan", message_id=3)
    await env.tools.search_messages(query="секрет")
    await env.tools.list_chats()
    svc.client.send_read_acknowledge.assert_not_called()
    for call in svc.client.await_args_list:  # прямые запросы client(Request)
        req = call.args[0] if call.args else None
        assert not isinstance(req, (functions.messages.ReadHistoryRequest, functions.channels.ReadHistoryRequest,
                                    functions.messages.ReadDiscussionRequest, functions.messages.ReadMentionsRequest))


async def test_mark_read_only_explicit(env):
    acc, svc = await add(env, preset="all")
    await env.tools.mark_read(chat="@ivan")
    svc.client.send_read_acknowledge.assert_awaited_once()


# ---------- журнал ----------
async def test_journal_read_has_no_content_write_has_text(env):
    acc, svc = await add(env, preset="read_write")
    svc.client.iter_messages = MagicMock(side_effect=lambda *a, **k: AIter([make_msg(text="тайное содержимое")]))
    await env.tools.get_messages(chat="@ivan")
    await env.tools.send_message(chat="@ivan", text="Отправленный текст")
    j = journal(env)
    read = next(r for r in j if r["tool"] == "get_messages")
    assert "тайное" not in json.dumps(read, ensure_ascii=False)
    assert read["result"]["count"] == 1 and read["account"]["label"] == "Личный"
    sent = next(r for r in j if r["tool"] == "send_message")
    assert sent["params"]["text"] == "Отправленный текст" and sent["result"]["message_id"] == 100


async def test_journal_hides_proxy_secrets(env):
    acc, svc = await add(env, preset="all")
    await env.tools.set_proxy(type="socks5", server="proxy.example", port=1080, username="u",
                              password="P@ss-123", account="Личный", scope="account")
    await env.tools.set_proxy(type="mtproto", server="mt.example", port=443, secret="dd" + "ab" * 16,
                              account="Личный", scope="account")
    raw = (env.tmp / "actions.jsonl").read_text(encoding="utf-8")
    assert "P@ss-123" not in raw and "abababab" not in raw
    got = json.loads(await env.tools.get_proxy())
    assert "P@ss-123" not in json.dumps(got) and all(p["password"] in ("", "***") for p in got["proxies"])


# ---------- лимит частоты и FloodWait ----------
async def test_write_limiter_spacing():
    now = [100.0]
    slept = []

    async def sleep(s):
        slept.append(s)
        now[0] += s

    lim = WriteLimiter(1.0, clock=lambda: now[0], sleep=sleep)
    await lim.acquire()
    await lim.acquire()
    now[0] += 0.3
    await lim.acquire()
    assert slept[0] == pytest.approx(1.0) and slept[1] == pytest.approx(0.7)


async def test_tools_use_limiter_for_writes_only(env):
    acc, svc = await add(env, preset="read_write")
    svc.limiter = MagicMock(blocked_for=MagicMock(return_value=0), acquire=AsyncMock())
    await env.tools.get_messages(chat="@ivan")
    svc.limiter.acquire.assert_not_called()
    await env.tools.send_message(chat="@ivan", text="1")
    await env.tools.send_message(chat="@ivan", text="2")
    assert svc.limiter.acquire.await_count == 2


async def test_floodwait_blocks_further_writes(env):
    acc, svc = await add(env, preset="read_write")
    svc.client.send_message = AsyncMock(side_effect=errors.FloodWaitError(request=None, capture=120))
    with pytest.raises(ToolError, match="FloodWait"):
        await env.tools.send_message(chat="@ivan", text="1")
    svc.client.send_message.reset_mock()
    with pytest.raises(ToolError, match="запрещена после FloodWait"):
        await env.tools.send_message(chat="@ivan", text="2")
    svc.client.send_message.assert_not_called()


# ---------- права ----------
async def test_new_account_defaults_read_download_saved(env):
    acc = await env.mgr.add_account("Новый")
    p = env.cfg.permissions(acc)
    assert {g for g, v in p.items() if v} == {"read", "download", "send_saved"}


def _ents(svc, mapping):
    """get_entity по строке/числу → пользователь; self — id 1 (как get_me в FakeService)."""
    async def get_entity(x):
        key = str(x).lstrip("@")
        if key not in mapping:
            raise ValueError("нет")
        return mapping[key]
    svc.client.get_entity = AsyncMock(side_effect=get_entity)


SAVED_ONLY = {g: g in ("read", "send_saved") for g in GROUPS}


async def test_saved_only_allows_own_saved_messages(env):
    acc, svc = await add(env)
    env.cfg.set_permissions(acc, SAVED_ONLY)
    me = make_user(uid=1, first="Я", last=None, username="myname")
    _ents(svc, {"1": me, "myname": me, "ivan": make_user()})
    for target in ("me", "self", "избранное", "saved", "1", "@myname"):
        await env.tools.send_message(chat=target, text=f"в избранное ({target})")
    assert svc.client.send_message.await_count == 6
    # reply и topic в Избранном допустимы
    await env.tools.send_message(chat="me", text="ответ", reply_to=5)
    assert svc.client.send_message.await_count == 7


async def test_saved_only_denies_other_chats(env, tmp_path):
    acc, svc = await add(env)
    env.cfg.set_permissions(acc, SAVED_ONLY)
    _ents(svc, {"ivan": make_user(), "10": make_user()})
    for target in ("@ivan", "10"):
        with pytest.raises(ToolError, match="разрешена отправка только в Избранное"):
            await env.tools.send_message(chat=target, text="чужому")
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    svc.client.send_file = AsyncMock()
    with pytest.raises(ToolError, match="только в Избранное"):
        await env.tools.send_file(chat="@ivan", path=str(f))
    svc.client.send_message.assert_not_called()
    svc.client.send_file.assert_not_called()
    assert [r["ok"] for r in journal(env) if r["tool"] in ("send_message", "send_file")] == [False, False, False]


async def test_general_send_does_not_depend_on_saved_flag(env):
    acc, svc = await add(env)
    env.cfg.set_permissions(acc, {g: g in ("read", "send") for g in GROUPS})  # send_saved выключено
    _ents(svc, {"ivan": make_user()})
    await env.tools.send_message(chat="@ivan", text="всем можно")
    await env.tools.send_message(chat="me", text="и себе")
    assert svc.client.send_message.await_count == 2


async def test_forward_saved_only_and_forward_right(env):
    acc, svc = await add(env)
    env.cfg.set_permissions(acc, SAVED_ONLY)
    _ents(svc, {"ivan": make_user(), "chan": make_user(uid=30, username="chan")})
    svc.client.forward_messages = AsyncMock(return_value=[make_msg(mid=77)])
    await env.tools.forward_messages(from_chat="@ivan", message_ids=[1, 2], to_chat="me")
    svc.client.forward_messages.assert_awaited_once()
    with pytest.raises(ToolError, match="только в Избранное"):
        await env.tools.forward_messages(from_chat="@ivan", message_ids=[1], to_chat="@chan")
    assert svc.client.forward_messages.await_count == 1
    # с правом «Пересылка» — в любой чат
    env.cfg.set_permissions(acc, {g: g in ("read", "forward") for g in GROUPS})
    await env.tools.forward_messages(from_chat="@ivan", message_ids=[1], to_chat="@chan")
    assert svc.client.forward_messages.await_count == 2
    # без send_saved, send и forward — отказ даже в Избранное
    env.cfg.set_permissions(acc, {g: g == "read" for g in GROUPS})
    with pytest.raises(ToolError, match="Запрещено настройками прав"):
        await env.tools.forward_messages(from_chat="@ivan", message_ids=[1], to_chat="me")


async def test_permissions_report_send_saved(env):
    acc, svc = await add(env)
    env.cfg.set_permissions(acc, SAVED_ONLY)
    got = json.loads(await env.tools.get_permissions())
    assert got["groups"]["send_saved"] == "Отправка только в Избранное"
    assert got["accounts"][0]["permissions"]["send_saved"] is True
    accs = json.loads(await env.tools.list_accounts())
    assert accs["accounts"][0]["permissions"]["send_saved"] is True


def test_migration_adds_send_saved(tmp_path):
    from app.config import Config
    path = tmp_path / "config.json"
    old = {"accounts": [
        {"id": "a1b2c3d4", "label": "Личный", "proxy": "app",
         "permissions": {"read": True, "download": True, "send": False, "forward": False, "edit": False,
                         "delete": False, "react": False, "mark_read": False, "proxy": False}},
        {"id": "b0b0b0b0", "label": "Новый формат", "proxy": "app",
         "permissions": {**{g: False for g in GROUPS}, "read": True}},  # send_saved уже есть = False
    ], "mcp_port": 18791, "mcp_token": "t"}
    path.write_text(json.dumps(old), encoding="utf-8")
    cfg = Config(path)
    p1 = cfg.permissions("a1b2c3d4")
    assert p1["send_saved"] is True and p1["read"] and p1["download"] and not p1["send"]
    assert cfg.permissions("b0b0b0b0")["send_saved"] is False  # явное значение не трогаем
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["accounts"][0]["permissions"]["send_saved"] is True  # записано в файл
    # повторная загрузка ничего не меняет
    assert Config(path).permissions("a1b2c3d4") == p1


async def test_permissions_checked_on_server(env):
    acc, svc = await add(env, preset="read_only")
    await env.tools.get_messages(chat="@ivan")  # чтение разрешено
    for call in (lambda: env.tools.send_message(chat="@ivan", text="x"),
                 lambda: env.tools.delete_messages(chat="@ivan", message_ids=[1]),
                 lambda: env.tools.mark_read(chat="@ivan"),
                 lambda: env.tools.download_media(chat="@ivan", message_id=1),
                 lambda: env.tools.set_proxy(type="socks5", server="h", port=1, account="Личный")):
        with pytest.raises(ToolError, match="Запрещено настройками прав в трее"):
            await call()
    svc.client.send_message.assert_not_called()
    svc.client.delete_messages.assert_not_called()
    assert env.cfg.proxies() == []
    denied = [r for r in journal(env) if not r["ok"]]
    assert len(denied) == 5
    # изменение прав в трее действует сразу, без перезапуска
    env.cfg.set_permissions(acc, PRESETS["read_write"])
    await env.tools.send_message(chat="@ivan", text="теперь можно")
    svc.client.send_message.assert_awaited_once()


async def test_every_tool_has_permission_group():
    assert set(TOOL_NAMES) == set(TOOL_GROUP)
    assert all(g is None or g in GROUPS for g in TOOL_GROUP.values())


async def test_mcp_cannot_change_permissions(env):
    acc, svc = await add(env, preset="all")
    before = env.cfg.permissions(acc)
    # ни один инструмент не принимает параметр прав и не называется set/grant
    for name in TOOL_NAMES:
        params = inspect.signature(getattr(env.tools, name)).parameters
        assert not any("perm" in p or "right" in p or "grant" in p for p in params), name
        assert not name.startswith(("set_perm", "grant", "allow")), name
    # вызов всех служебных инструментов не меняет права
    await env.tools.get_permissions()
    await env.tools.list_accounts()
    await env.tools.set_proxy(type="socks5", server="h", port=1080, account="Личный", scope="account")
    await env.tools.clear_proxy(account="Личный", scope="account")
    assert env.cfg.permissions(acc) == before
    got = json.loads(await env.tools.get_permissions(account="Личный"))
    assert got["accounts"][0]["permissions"] == before


# ---------- аккаунты ----------
async def test_accounts_isolated(env):
    a, sa = await add(env, "Личный", "read_write")
    b, sb = await add(env, "Рабочий", "read_only")
    with pytest.raises(ToolError, match="Аккаунтов несколько"):
        await env.tools.get_messages(chat="@ivan")
    await env.tools.send_message(chat="@ivan", text="лично", account="Личный")
    sa.client.send_message.assert_awaited_once()
    sb.client.send_message.assert_not_called()
    with pytest.raises(ToolError, match="Рабочий"):
        await env.tools.send_message(chat="@ivan", text="работа", account="рабочий")
    sb.client.send_message.assert_not_called()
    await env.tools.get_messages(chat="@ivan", account=b)  # по id
    assert sb.client.iter_messages.called and sa.client.iter_messages.call_count == 0
    # FloodWait одного аккаунта не блокирует другой
    sa.limiter.block(300)
    assert sb.limiter.blocked_for() == 0
    accs = json.loads(await env.tools.list_accounts())
    assert [x["label"] for x in accs["accounts"]] == ["Личный", "Рабочий"]
    assert accs["accounts"][1]["preset"] == "Только чтение"
    assert all("4567" in x["phone"] and "9991234" not in x["phone"] for x in accs["accounts"])
    j = journal(env)
    assert {r.get("account", {}).get("label") for r in j if r["tool"] == "send_message"} == {"Личный", "Рабочий"}


async def test_unknown_account(env):
    await add(env, "Личный")
    with pytest.raises(ToolError, match="не найден"):
        await env.tools.get_me(account="Чужой")


async def test_no_accounts_and_not_configured(env, tmp_path):
    with pytest.raises(ToolError, match="Нет ни одного аккаунта"):
        await env.tools.get_me()
    # настоящий TelegramService без api_id — понятная ошибка
    from app.accounts import AccountManager
    from app.session_store import MemorySessionStore
    from app.tools import Tools
    mgr = AccountManager(env.cfg, MemorySessionStore(tmp_path / "acc2"), env.actions)
    t = Tools(mgr, env.cfg, env.actions)
    await mgr.add_account("Личный")  # не запущен: состояние no_api
    with pytest.raises(ToolError, match="не настроен"):
        await t.get_me()
    # прокси-инструменты работают и без api_id
    assert json.loads(await t.get_proxy())["app_proxy"] is None


async def test_no_default_proxy(env):
    assert env.cfg.proxies() == [] and env.cfg.active_proxy() is None
