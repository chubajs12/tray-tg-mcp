from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import types

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.accounts import AccountManager  # noqa: E402
from app.config import Config  # noqa: E402
from app.logs import ActionLog  # noqa: E402
from app.ratelimit import WriteLimiter  # noqa: E402
from app.session_store import MemorySessionStore  # noqa: E402
from app.tools import Tools  # noqa: E402

DT = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def make_user(uid=10, first="Иван", last="Петров", username="ivan", phone="79991234567", **kw):
    return types.User(id=uid, first_name=first, last_name=last, username=username, phone=phone,
                      access_hash=1, **kw)


def make_msg(mid=1, text="привет", peer=10, sender=None, reply_to=None, media=None, out=False):
    m = types.Message(id=mid, peer_id=types.PeerUser(peer), date=DT, message=text,
                      from_id=types.PeerUser(sender.id if sender else peer), reply_to=reply_to,
                      media=media, out=out)
    m._sender = sender
    return m


class AIter:
    def __init__(self, items):
        self.items = list(items)

    def __aiter__(self):
        self._it = iter(self.items)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


def make_client(messages=None, user=None):
    """Мок TelegramClient: все сетевые методы — AsyncMock, итераторы — асинхронные списки."""
    user = user or make_user()
    c = AsyncMock(name="TelegramClient")  # await client(Request) — тоже мок
    c.get_me = AsyncMock(return_value=make_user(uid=1, first="Я", last=None, username="me"))
    c.get_entity = AsyncMock(return_value=user)
    c.iter_messages = MagicMock(side_effect=lambda *a, **k: AIter(messages or []))
    c.iter_dialogs = MagicMock(side_effect=lambda *a, **k: AIter([]))
    c.get_messages = AsyncMock(return_value=(messages or [None])[0])
    c.send_message = AsyncMock(side_effect=lambda e, text, **k: make_msg(mid=100, text=text, out=True))
    c.send_read_acknowledge = AsyncMock(return_value=True)
    c.delete_messages = AsyncMock(return_value=[SimpleNamespace(pts_count=1)])
    return c


class FakeService:
    """Заменитель TelegramService: тот же интерфейс, что использует Tools/AccountManager."""

    def __init__(self, config, account_id, store, on_change=None):
        self.config = config
        self.account_id = account_id
        self.store = store
        self.client = make_client()
        self.limiter = WriteLimiter(1.0, sleep=AsyncMock())
        self.dialog_cache: list[Any] = []
        self.dialog_cache_at = 0.0
        self.state = "online"
        self.started = False

    @property
    def label(self):
        a = self.config.account(self.account_id)
        return a["label"] if a else self.account_id

    async def start(self):
        self.started = True

    async def shutdown(self):
        pass

    async def log_out(self):
        return True

    def reconfigure(self):
        pass

    def require(self):
        if self.state != "online":
            from app.telegram import NotReady
            raise NotReady("нет соединения")
        return self.client

    def snapshot(self):
        return {"account_id": self.account_id, "state": self.state, "state_text": "в сети", "error": None,
                "me": {"id": 1, "name": "Я", "username": "me_" + self.account_id, "phone": "+7******4567"},
                "proxy": "без прокси", "qr": {"state": "idle"}}


@pytest.fixture(autouse=True)
def memory_api_store(monkeypatch):
    """Тесты не трогают Диспетчер учетных данных: api_id/api_hash — в памяти."""
    import app.config as config_mod
    from app.secret_store import MemorySecretStore
    store = MemorySecretStore()
    monkeypatch.setattr(config_mod, "make_api_store", lambda: store)
    return store


@pytest.fixture
def env(tmp_path):
    cfg = Config(tmp_path / "config.json")
    actions = ActionLog(tmp_path / "actions.jsonl")
    mgr = AccountManager(cfg, MemorySessionStore(tmp_path / "accounts"), actions, service_factory=FakeService)
    tools = Tools(mgr, cfg, actions, downloads_dir=tmp_path / "downloads")
    return SimpleNamespace(cfg=cfg, actions=actions, mgr=mgr, tools=tools, tmp=tmp_path)


def journal(env) -> list[dict]:
    p = env.tmp / "actions.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
