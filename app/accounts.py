"""Несколько аккаунтов Telegram: по TelegramService на аккаунт, все в одном asyncio-loop."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from .config import Config
from .logs import ActionLog
from .session_store import SessionStore
from .telegram import NotReady, TelegramService

log = logging.getLogger("tgmcp.accounts")


class AccountError(Exception):
    pass


class AccountManager:
    def __init__(self, config: Config, store: SessionStore, actions: ActionLog | None = None,
                 on_change: Callable[[], None] | None = None,
                 service_factory: Callable[..., TelegramService] | None = None):
        self.config = config
        self.store = store
        self.actions = actions
        self.on_change = on_change
        self._factory = service_factory or TelegramService
        self.services: dict[str, TelegramService] = {}
        self._started = False

    def _new_service(self, account_id: str) -> TelegramService:
        return self._factory(self.config, account_id, self.store, on_change=self.on_change)

    # ---------- в loop ----------
    async def start(self) -> None:
        self._started = True
        for a in self.config.accounts():
            svc = self._new_service(a["id"])
            self.services[a["id"]] = svc
            await svc.start()
        self._changed()

    async def add_account(self, label: str) -> str:
        acc = self.config.add_account(label)
        svc = self._new_service(acc["id"])
        self.services[acc["id"]] = svc
        if self._started:
            await svc.start()
        self._journal("account_added", {"account": acc["label"], "account_id": acc["id"]})
        self._changed()
        return acc["id"]

    async def remove_account(self, account_id: str) -> bool:
        """log_out на сервере (если в сети), удаление ключа сессии и папки аккаунта."""
        svc = self.services.pop(account_id, None)
        acc = self.config.account(account_id)
        logged_out = False
        if svc is not None:
            try:
                logged_out = await svc.log_out()
            except Exception:  # noqa: BLE001
                log.warning("remove_account log_out", exc_info=True)
            await svc.shutdown()
        self.store.purge(account_id)
        self.config.remove_account(account_id)
        self._journal("account_removed", {"account": acc["label"] if acc else account_id,
                                          "account_id": account_id, "logged_out": logged_out})
        self._changed()
        return logged_out

    async def log_out(self, account_id: str) -> bool:
        svc = self.services.get(account_id)
        if svc is None:
            return False
        ok = await svc.log_out()
        self._journal("account_logged_out", {"account": svc.label, "account_id": account_id, "ok": ok})
        return ok

    def reconfigure_all(self) -> None:
        for svc in self.services.values():
            svc.reconfigure()

    def reconfigure(self, account_id: str) -> None:
        svc = self.services.get(account_id)
        if svc:
            svc.reconfigure()

    async def shutdown(self) -> None:
        await asyncio.gather(*(s.shutdown() for s in self.services.values()), return_exceptions=True)

    # ---------- из любых потоков ----------
    def snapshots(self) -> list[dict[str, Any]]:
        out = []
        for a in self.config.accounts():
            svc = self.services.get(a["id"])
            snap = svc.snapshot() if svc else {"state": "stopped", "state_text": "остановлен"}
            snap["label"] = a["label"]
            snap["account_id"] = a["id"]
            snap["proxy_mode"] = a.get("proxy", "app")
            out.append(snap)
        return out

    def resolve(self, account: str | None) -> TelegramService:
        """Аккаунт по метке, id, @username, имени или Telegram id. Без параметра — единственный аккаунт."""
        accs = self.config.accounts()
        if not accs:
            raise NotReady("Нет ни одного аккаунта Telegram: окно TG MCP (трей → «Открыть») → раздел «Аккаунты».")
        if account is None or not str(account).strip():
            if len(accs) == 1:
                return self._svc(accs[0]["id"])
            raise AccountError("Аккаунтов несколько — укажите account: "
                               + ", ".join(f"«{a['label']}»" for a in accs) + " (list_accounts).")
        key = str(account).strip().casefold().lstrip("@")
        for a in accs:
            if key in (a["id"].casefold(), a["label"].casefold()):
                return self._svc(a["id"])
        for a in accs:
            svc = self.services.get(a["id"])
            me = (svc.snapshot().get("me") if svc else None) or {}
            if key in {str(me.get("id") or "").casefold(), str(me.get("username") or "").casefold(),
                       str(me.get("name") or "").casefold()} - {""}:
                return self._svc(a["id"])
        raise AccountError(f"Аккаунт «{account}» не найден. Есть: "
                           + ", ".join(f"«{a['label']}»" for a in accs) + ".")

    def _svc(self, account_id: str) -> TelegramService:
        svc = self.services.get(account_id)
        if svc is None:
            raise NotReady("Аккаунт еще не запущен.")
        return svc

    def _journal(self, event: str, params: dict[str, Any]) -> None:
        if self.actions:
            self.actions.write(event, params, True, result=None, source="tray")

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass
