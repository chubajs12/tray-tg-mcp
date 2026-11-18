"""TelegramService без сети: вход по QR (обновление токена, 2FA), выход, «не в сети», прокси."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import errors, functions

from app import telegram as tg
from app.config import Config
from app.session_store import MemorySessionStore

from conftest import make_user



class FakeQR:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.n = 0
        self.url = "tg://login?token=t0"
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)

    async def wait(self, timeout=None):
        await asyncio.sleep(0)
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException) or (isinstance(o, type) and issubclass(o, BaseException)):
            raise o
        return o

    async def recreate(self):
        self.n += 1
        self.url = f"tg://login?token=t{self.n}"


def service(tmp_path, client):
    cfg = Config(tmp_path / "config.json")
    cfg.set_api(1, "a" * 32)
    acc = cfg.add_account("Личный")
    store = MemorySessionStore(tmp_path / "accounts")
    svc = tg.TelegramService(cfg, acc["id"], store)
    svc.client = client
    svc._set(tg.NEED_LOGIN)
    return svc, store


def fake_client(qr):
    c = AsyncMock()
    c.qr_login = AsyncMock(return_value=qr)
    c.get_me = AsyncMock(return_value=make_user(uid=5, phone="79990001122"))
    c.session = MagicMock()
    c.session.save = MagicMock(return_value="1SESSIONSTRING")
    c.log_out = AsyncMock(return_value=True)
    return c


async def wait_state(svc, state, timeout=2.0):
    t = asyncio.get_running_loop().time()
    while svc.snapshot()["qr"].get("state") != state:
        if asyncio.get_running_loop().time() - t > timeout:
            raise AssertionError(f"qr state {svc.snapshot()['qr']} != {state}")
        await asyncio.sleep(0.01)


async def test_qr_refresh_then_2fa_then_online(tmp_path):
    secret = "пароль-2FA"
    qr = FakeQR([asyncio.TimeoutError(), errors.SessionPasswordNeededError(request=None)])
    c = fake_client(qr)
    calls = []

    async def sign_in(password=None):
        calls.append(password)
        if len(calls) == 1:
            raise errors.PasswordHashInvalidError(request=None)
        return make_user(uid=5)

    c.sign_in = sign_in
    svc, store = service(tmp_path, c)
    await svc.start_qr()
    await wait_state(svc, "password")
    assert qr.n == 1  # токен истек — QR пересоздан
    svc.submit_password("неверный")
    await asyncio.sleep(0.05)
    assert "Неверный пароль" in (svc.snapshot()["qr"].get("error") or "")
    svc.submit_password(secret)
    await wait_state(svc, "done")
    snap = svc.snapshot()
    assert snap["state"] == tg.ONLINE and snap["me"]["phone"] == "+7******1122"
    assert store.data[svc.account_id] == "1SESSIONSTRING"  # ключ — в хранилище
    assert secret not in repr(snap)
    # после входа — статус «не в сети»
    assert any(isinstance(call.args[0], functions.account.UpdateStatusRequest) and call.args[0].offline is True
               for call in c.await_args_list)
    log_text = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert secret not in log_text


async def test_log_out_removes_session(tmp_path):
    c = fake_client(FakeQR([make_user()]))
    svc, store = service(tmp_path, c)
    svc._set(tg.ONLINE, me={"id": 5})
    store.data[svc.account_id] = "KEY"
    assert await svc.log_out() is True
    c.log_out.assert_awaited_once()
    assert svc.account_id not in store.data
    assert svc.state == tg.NEED_LOGIN


async def test_require_messages(tmp_path):
    cfg = Config(tmp_path / "config.json")
    acc = cfg.add_account("X")
    svc = tg.TelegramService(cfg, acc["id"], MemorySessionStore(tmp_path / "accounts"))
    with pytest.raises(tg.NotReady, match="не настроен"):
        svc.require()
    svc._set(tg.NEED_LOGIN)
    with pytest.raises(tg.NotReady, match="Войти по QR"):
        svc.require()


def test_proxy_kwargs():
    assert tg.build_proxy_kwargs(None) == {}
    k = tg.build_proxy_kwargs({"type": "socks5", "server": "h", "port": 1080, "username": "", "password": ""})
    assert k["proxy"]["proxy_type"] == "socks5" and k["proxy"]["addr"] == "h"
    k = tg.build_proxy_kwargs({"type": "mtproto", "server": "h", "port": 443, "secret": "dd" + "0" * 32})
    assert k["proxy"] == ("h", 443, "dd" + "0" * 32)
    with pytest.raises(ValueError, match="FakeTLS"):
        tg.build_proxy_kwargs({"type": "mtproto", "server": "h", "port": 443, "secret": "ee" + "0" * 32})


def test_account_proxy_modes(tmp_path):
    cfg = Config(tmp_path / "config.json")
    a = cfg.add_account("A")
    assert cfg.account_proxy(a["id"]) is None
    p = cfg.upsert_proxy({"type": "http", "server": "h", "port": 3128}, make_active=True)
    assert cfg.account_proxy(a["id"])["id"] == p["id"]  # как у приложения
    cfg.set_account_proxy(a["id"], "none")
    assert cfg.account_proxy(a["id"]) is None
    cfg.set_account_proxy(a["id"], p["id"])
    cfg.remove_proxy(p["id"])
    assert cfg.account(a["id"])["proxy"] == "app"


def test_proxy_links_and_secrets():
    from app.config import ConfigError, check_mtproto_secret, normalize_proxy, parse_proxy_link
    p = parse_proxy_link("tg://proxy?server=mt.example&port=443&secret=dd" + "ab" * 16)
    assert p == {"type": "mtproto", "server": "mt.example", "port": "443", "secret": "dd" + "ab" * 16}
    p = parse_proxy_link("https://t.me/socks?server=10.0.0.1&port=1080&user=u&pass=x")
    assert p["type"] == "socks5" and p["username"] == "u" and p["port"] == "1080"
    assert parse_proxy_link("просто текст") is None
    assert check_mtproto_secret("AB" * 16) == ("ab" * 16, "plain")
    with pytest.raises(ConfigError, match="FakeTLS"):
        normalize_proxy({"type": "mtproto", "server": "h", "port": 443, "secret": "ee" + "ab" * 16 + "6161"})
    with pytest.raises(ConfigError):
        normalize_proxy({"type": "socks5", "server": "", "port": 1080})


async def test_no_connect_mode_never_builds_client(tmp_path, monkeypatch):
    monkeypatch.setenv("TGMCP_NO_CONNECT", "1")
    cfg = Config(tmp_path / "config.json")
    cfg.set_api(1, "a" * 32)
    acc = cfg.add_account("X")
    svc = tg.TelegramService(cfg, acc["id"], MemorySessionStore(tmp_path / "accounts"))
    svc._make_client = MagicMock(side_effect=AssertionError("не должен создавать клиента"))
    await svc.start()
    await asyncio.sleep(0.05)
    assert svc.state == tg.ERROR and "TGMCP_NO_CONNECT" in svc.snapshot()["error"]
    svc._make_client.assert_not_called()
    await svc.shutdown()


def test_api_only_in_store(tmp_path, memory_api_store):
    import json
    cfg = Config(tmp_path / "config.json")
    assert cfg.api_source is None and not cfg.configured
    cfg.set_api(777, "C" * 32)
    assert memory_api_store.value == {"api_id": 777, "api_hash": "c" * 32, "source": "user"}
    cfg2 = Config(tmp_path / "config.json")  # повторный запуск — берется из хранилища
    assert cfg2.configured and cfg2.api_id == 777 and cfg2.api_hash == "c" * 32
    raw = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "c" * 32 not in raw and "api_hash" not in json.loads(raw) and "api_id" not in json.loads(raw)
    cfg2.clear_api()
    assert memory_api_store.value is None and not cfg2.configured
    with pytest.raises(Exception):
        cfg2.set_api("", "")


def test_legacy_config_api_migrates_to_store(tmp_path, memory_api_store):
    import json
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_id": 4242, "api_hash": "d" * 32}), encoding="utf-8")
    cfg = Config(path)
    assert cfg.api_source == "user" and cfg.api_id == 4242 and memory_api_store.value["api_hash"] == "d" * 32
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "api_hash" not in data and "api_id" not in data


async def test_security_info_cached_and_read_only(tmp_path):
    from datetime import datetime, timezone
    from telethon import types
    c = fake_client(FakeQR([make_user()]))
    svc, _ = service(tmp_path, c)
    svc._set(tg.ONLINE, me={"id": 5})

    def auth(cur, dev, active):
        return types.Authorization(hash=1, device_model=dev, platform="Windows", system_version="11", api_id=1,
                                   app_name="TG MCP", app_version="1.0", date_created=active,
                                   date_active=active, ip="198.51.100.7", country="Netherlands",
                                   region="NH", current=cur, official_app=False)

    now = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    calls = []

    async def call(req):
        calls.append(type(req).__name__)
        if isinstance(req, functions.account.GetPasswordRequest):
            return MagicMock(has_password=False)
        if isinstance(req, functions.account.GetAuthorizationsRequest):
            return types.account.Authorizations(authorization_ttl_days=180, authorizations=[
                auth(False, "Phone", now), auth(True, "TG MCP Desktop", now)])
        raise AssertionError(f"неожиданный запрос {req}")

    c.side_effect = call
    await svc.refresh_security(max_age=0)
    sec = svc.snapshot()["security"]
    assert sec["has_password"] is False and len(sec["sessions"]) == 2
    assert sec["sessions"][0]["current"] is True and sec["sessions"][0]["device"].startswith("TG MCP Desktop")
    assert sec["sessions"][1]["location"] == "Netherlands, NH" and sec["sessions"][1]["ip"] == "198.51.100.7"
    # повторно в пределах срока — без запросов в сеть
    n = len(calls)
    await svc.refresh_security()
    assert len(calls) == n
    # только чтение: никаких Reset*/Logout запросов
    assert set(calls) == {"GetPasswordRequest", "GetAuthorizationsRequest"}
    # выход из аккаунта сбрасывает сведения
    await svc.log_out()
    assert svc.snapshot()["security"] is None
