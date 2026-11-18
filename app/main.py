"""Сборка приложения: главный поток — Qt (окно, трей), поток tg-loop — asyncio (Telethon + MCP)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Coroutine

from . import APP_TITLE, APP_VERSION, paths
from .accounts import AccountManager
from .config import Config
from .logs import ActionLog, setup_logging
from .mcp_server import McpHttpServer
from .session_store import SessionStore
from .tools import Tools

log = logging.getLogger("tgmcp")


class App:
    def __init__(self, qapp):
        from .gui.qt_app import QtUI  # noqa: PLC0415

        paths.ensure_dirs()
        self.config = Config()
        self.actions = ActionLog()
        self.store = SessionStore()
        self.manager = AccountManager(self.config, self.store, self.actions, on_change=self._changed)
        self.tools = Tools(self.manager, self.config, self.actions)
        self.mcp = McpHttpServer(self.tools, self.config)
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, name="tg-loop", daemon=True)
        self._quitting = False
        self.ui = QtUI(self, qapp)

    # ---------- loop ----------
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._startup())
        self.loop.run_forever()
        try:
            pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
            for t in pending:
                t.cancel()
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            self.loop.close()

    async def _startup(self) -> None:
        await self.mcp.start()
        await self.manager.start()
        self._changed()

    def run_coro(self, coro: Coroutine) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn: Callable[[], Any]) -> None:
        """Выполнить обычную функцию в loop (для методов, которые трогают asyncio-объекты)."""
        self.loop.call_soon_threadsafe(fn)

    def _changed(self) -> None:
        self.ui.mark_dirty()

    # ---------- состояние для трея ----------
    def overall_state(self) -> tuple[str, str]:
        mst = self.mcp.status()
        if not mst["running"]:
            return "error", "MCP не запущен" + (f": {mst['error']}" if mst.get("error") else "")
        if not self.config.configured:
            return "no_api", "нет api_id (Настройки)"
        snaps = self.manager.snapshots()
        if not snaps:
            return "no_accounts", "нет аккаунтов (Аккаунты → Добавить)"
        states = [s.get("state") for s in snaps]
        online = states.count("online")
        summary = f"в сети {online}/{len(snaps)}"
        if "error" in states:
            return "error", summary + ", есть ошибка"
        if "connecting" in states:
            return "connecting", summary + ", подключение"
        if "need_login" in states:
            return "need_login", summary + ", нужен вход"
        return "online", summary

    # ---------- выход ----------
    def quit(self) -> None:
        """«Выход» в трее (и --quit извне): отключить клиентов, остановить MCP, закрыть Qt. Не блокирует GUI."""
        if self._quitting:
            return
        self._quitting = True
        log.info("выход")

        async def stop_all():
            try:
                await asyncio.wait_for(self.manager.shutdown(), timeout=15)
            except Exception:  # noqa: BLE001
                log.warning("остановка клиентов", exc_info=True)
            await self.mcp.stop()

        def finish(_f=None):
            if self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
            self.ui.post(self.ui.quit)

        if self.loop.is_running():
            self.run_coro(stop_all()).add_done_callback(finish)
        else:
            finish()

        def guard():  # страховка: если остановка зависла — закрыть GUI через 25 с
            from PySide6.QtCore import QTimer  # noqa: PLC0415
            QTimer.singleShot(25000, self.ui.quit)

        self.ui.post(guard)

    # ---------- запуск ----------
    def run(self, show_window: bool = False) -> int:
        log.info("старт %s %s, данные: %s", APP_TITLE, APP_VERSION, paths.DATA_DIR)
        self._loop_thread.start()
        self.ui.start(show_window)
        rc = self.ui.run()  # до quit()
        self._loop_thread.join(timeout=10)
        return rc


def diagnostics() -> dict:
    """Проверка окружения без Telegram: хранилища сессий (пробная запись/чтение/удаление), версии библиотек."""
    import tempfile  # noqa: PLC0415

    out: dict[str, Any] = {"frozen": paths.FROZEN, "data_dir": str(paths.DATA_DIR), "version": APP_VERSION}
    probe = "diag-" + __import__("secrets").token_hex(4)
    try:
        import keyring  # noqa: PLC0415
        out["keyring_backend"] = type(keyring.get_keyring()).__name__
        st = SessionStore(base_dir=Path(tempfile.mkdtemp(prefix="tgmcp-diag-")))
        backend = st.save(probe, "diag-value")
        ok = st.load(probe) == "diag-value"
        st.purge(probe)
        out["session_store"] = {"backend": backend, "roundtrip": ok, "deleted": st.load(probe) is None}
    except Exception as e:  # noqa: BLE001
        out["session_store"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        st = SessionStore(base_dir=Path(tempfile.mkdtemp(prefix="tgmcp-diag-")), use_keyring=False)
        out["dpapi"] = {"backend": st.save(probe, "diag-value"), "roundtrip": st.load(probe) == "diag-value"}
        st.purge(probe)
    except Exception as e:  # noqa: BLE001
        out["dpapi"] = {"error": f"{type(e).__name__}: {e}"}
    for mod in ("telethon", "mcp", "PySide6", "uvicorn", "qrcode", "cryptg", "python_socks"):
        try:
            m = __import__(mod)
            out.setdefault("modules", {})[mod] = getattr(m, "__version__", "ok")
        except Exception as e:  # noqa: BLE001
            out.setdefault("modules", {})[mod] = f"ОШИБКА {type(e).__name__}: {e}"
    try:
        from telethon.crypto import AES  # noqa: PLC0415
        out["telethon_fast_crypto"] = bool(getattr(__import__("telethon.crypto.aes", fromlist=["cryptg"]), "cryptg", None))
    except Exception:  # noqa: BLE001
        pass
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tg-mcp")
    ap.add_argument("--show", action="store_true", help="сразу открыть окно")
    ap.add_argument("--data-dir", help="отдельный каталог данных (обрабатывается в run.pyw до импорта)")
    ap.add_argument("--selftest", type=Path, help="снимки окна (Qt grab) в каталог, затем выход")
    ap.add_argument("--selftest-hold", type=float, default=2.0, help="пауза перед выходом в --selftest, с")
    ap.add_argument("--quit", action="store_true", help="попросить запущенный экземпляр корректно завершиться")
    ap.add_argument("--diag", type=Path, help="диагностика окружения (keyring, DPAPI, версии) в JSON-файл и выход")
    ap.add_argument("--forget-sessions", action="store_true",
                    help="удалить ключи сессий всех аккаунтов из хранилища Windows (для uninstall -RemoveData)")
    args = ap.parse_args(argv)

    from .winutil import (SingleInstance, message_box, signal_quit, signal_show,  # noqa: PLC0415
                          watch_quit_event, watch_show_event)
    if args.quit:
        return 0 if signal_quit() else 3
    if args.diag:
        args.diag.write_text(json.dumps(diagnostics(), ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    if args.forget_sessions:
        if paths.CONFIG_FILE.exists():
            cfg = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
            store = SessionStore()
            for a in cfg.get("accounts", []):
                if a.get("id"):
                    store.delete(a["id"])
        from .config import make_api_store  # noqa: PLC0415
        make_api_store().delete()  # api_id/api_hash (запись tg-mcp / api_credentials)
        return 0

    paths.ensure_dirs()
    setup_logging()
    inst = SingleInstance()
    if inst.already_running:
        log.info("второй экземпляр — выход")
        if args.show and signal_show():
            return 0  # ярлык: показать окно уже запущенного экземпляра
        message_box("TG MCP уже запущен — иконка в области уведомлений (трее).", timeout_ms=4000)
        return 1
    try:
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
        qapp = QApplication(sys.argv[:1])
        qapp.setApplicationName(APP_TITLE)
        qapp.setQuitOnLastWindowClosed(False)
        app = App(qapp)
        watch_quit_event(app.quit)
        watch_show_event(lambda: app.ui.post(app.ui.window.show_window))
        if args.selftest:
            from .gui.qt_app import selftest  # noqa: PLC0415
            selftest(app.ui, args.selftest, args.selftest_hold)
        rc = app.run(show_window=args.show)
        # Qt-объекты удаляем до выхода процесса: иначе выгрузка DLL Qt при живом QApplication падает (0xC0000005)
        app.ui.tray.hide()
        app.ui.window.deleteLater()
        app.ui.tray.deleteLater()
        qapp.processEvents()
        del app
        qapp.shutdown()
        return rc
    except Exception:  # noqa: BLE001
        log.exception("аварийное завершение")
        return 2
    finally:
        inst.release()
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(main())
