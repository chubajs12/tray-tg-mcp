"""Qt-интерфейс: трей (Открыть/Выход), главное окно с разделами, темы, межпоточный мост.

Цикл Qt — в главном потоке; asyncio/Telethon — в потоке tg-loop. Другие потоки обращаются к GUI только
через post() (сигнал с QueuedConnection в главный поток).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QFont, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from .. import APP_TITLE
from ..icon import make_icon
from . import theme
from .pages import PAGES, ProxyChecker
from .widgets import Toggle, glyph_icon, label

if TYPE_CHECKING:
    from ..main import App

log = logging.getLogger("tgmcp.gui")


def pil_to_icon(img) -> QIcon:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    pm = QPixmap()
    pm.loadFromData(buf.getvalue(), "PNG")
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, ui: "QtUI"):
        super().__init__()
        self.ui = ui
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(pil_to_icon(make_icon("online", 256)))
        self.resize(1000, 720)
        self.setMinimumSize(820, 560)
        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        nav_wrap = QWidget()
        nav_wrap.setObjectName("navWrap")
        nav_wrap.setAttribute(Qt.WA_StyledBackground, True)
        nav_wrap.setFixedWidth(230)
        nv = QVBoxLayout(nav_wrap)
        nv.setContentsMargins(0, 0, 0, 0)
        nv.setSpacing(0)
        nv.addWidget(label(APP_TITLE, "appTitle"))
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(22, 22))
        nv.addWidget(self.nav, 1)
        self.stack = QStackedWidget()
        h.addWidget(nav_wrap)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.keys: list[str] = []
        self.nav.currentRowChanged.connect(self._switch)
        self.build_pages()

    def build_pages(self, current: str = "status"):
        self.nav.blockSignals(True)
        self.nav.clear()
        while self.stack.count():
            w = self.stack.widget(0)
            self.stack.removeWidget(w)
            w.deleteLater()
        self.keys = []
        pal = self.ui.palette
        for key, title, cls in PAGES:
            it = QListWidgetItem(glyph_icon({"status": "status", "accounts": "accounts", "rights": "rights",
                                             "proxy": "proxy", "log": "log", "settings": "settings"}[key],
                                            pal.accent), "  " + title)
            self.nav.addItem(it)
            self.stack.addWidget(cls(self.ui))
            self.keys.append(key)
        self.nav.blockSignals(False)
        self.go(current)

    def _switch(self, row: int):
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)
            self.refresh_current()
            self._on_show()

    def _on_show(self):
        w = self.stack.currentWidget()
        if w is not None and hasattr(w, "on_show"):
            try:
                w.on_show()
            except Exception:  # noqa: BLE001
                log.exception("on_show")

    def page(self, key: str):
        return self.stack.widget(self.keys.index(key))

    def go(self, key: str, account: str | None = None):
        if key not in self.keys:
            return
        self.nav.setCurrentRow(self.keys.index(key))
        if account and key == "rights":
            self.page("rights").select(account)

    def refresh_current(self):
        w = self.stack.currentWidget()
        if w is not None and self.isVisible():
            try:
                w.refresh()
            except Exception:  # noqa: BLE001
                log.exception("refresh")

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.refresh_current()
        self._on_show()

    def closeEvent(self, e):  # noqa: N802
        if self.ui.quitting:
            e.accept()
            return
        e.ignore()  # крестик — свернуть в трей
        self.hide()


class QtUI(QObject):
    invoke = Signal(object)

    def __init__(self, app: "App", qapp: QApplication):
        super().__init__()
        self.app = app
        self.qapp = qapp
        self.quitting = False
        self._dirty = True
        self.invoke.connect(self._run, Qt.QueuedConnection)
        self.palette = theme.resolve(app.config.theme)
        qapp.setStyleSheet(theme.qss(self.palette))
        self.proxy_checker = ProxyChecker(self)
        self.window = MainWindow(self)
        # трей
        self.tray = QSystemTrayIcon(pil_to_icon(make_icon("connecting")), self)
        menu = QMenu()
        self.act_open = QAction("Открыть", menu)
        f = QFont(self.act_open.font())
        f.setBold(True)
        self.act_open.setFont(f)
        self.act_open.triggered.connect(self.window.show_window)
        menu.addAction(self.act_open)
        menu.setDefaultAction(self.act_open)
        menu.addSeparator()
        act_quit = QAction("Выход", menu)
        act_quit.triggered.connect(self.app.quit)
        menu.addAction(act_quit)
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)
        self.tray.setToolTip(APP_TITLE)
        self._tray_key = None
        # таймеры
        self.t_fast = QTimer(self)
        self.t_fast.timeout.connect(self._tick_fast)
        self.t_fast.start(300)
        self.t_slow = QTimer(self)
        self.t_slow.timeout.connect(self.window.refresh_current)
        self.t_slow.start(1000)
        try:
            QGuiApplication.styleHints().colorSchemeChanged.connect(self._system_scheme)
        except Exception:  # noqa: BLE001
            pass

    # ---------- межпоточное ----------
    def post(self, fn: Callable[[], Any]) -> None:
        self.invoke.emit(fn)

    @Slot(object)
    def _run(self, fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("GUI")

    def mark_dirty(self) -> None:
        self._dirty = True

    def _tick_fast(self):
        if not self._dirty:
            return
        self._dirty = False
        state, text = self.app.overall_state()
        key = (state, text)
        if key != self._tray_key:
            self._tray_key = key
            self.tray.setIcon(pil_to_icon(make_icon(state)))
            self.tray.setToolTip((APP_TITLE + " — " + text)[:120])
        self.window.refresh_current()

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.window.show_window()

    # ---------- темы ----------
    def set_theme(self, mode: str):
        self.app.config.set_theme(mode)
        self.apply_theme()

    def apply_theme(self):
        pal = theme.resolve(self.app.config.theme)
        if pal is self.palette and self.qapp.styleSheet():
            return
        self.palette = pal
        self.qapp.setStyleSheet(theme.qss(pal))
        cur = self.window.keys[self.window.stack.currentIndex()] if self.window.keys else "status"
        self.window.build_pages(cur)

    def _system_scheme(self, *_):
        if self.app.config.theme == "system":
            self.apply_theme()

    # ---------- действия ----------
    def mcp_command(self) -> str:
        return (f"claude mcp add --transport http tg {self.app.mcp.url} "
                f'--header "Authorization: Bearer {self.app.config.mcp_token}"')

    def copy_mcp(self):
        QGuiApplication.clipboard().setText(self.mcp_command())
        QMessageBox.information(self.window, APP_TITLE,
                                "В буфер обмена скопирована команда подключения к Claude Code:\n\n"
                                f"claude mcp add --transport http tg {self.app.mcp.url} "
                                '--header "Authorization: Bearer …"\n\nТокен — секрет, не публикуйте его.')

    def rotate_token(self):
        if QMessageBox.question(self.window, APP_TITLE,
                                "Сгенерировать новый токен MCP?\n\nСтарый токен сразу перестанет приниматься — "
                                "подключение в Claude Code нужно будет обновить.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.app.config.rotate_token()
        self.app.actions.write("token_rotated", {}, True, source="tray")
        QMessageBox.information(self.window, APP_TITLE, "Токен сменен. Скопируйте новую команду кнопкой "
                                "«Скопировать адрес и токен MCP».")

    # ---------- жизненный цикл ----------
    def start(self, show_window: bool = False):
        self.tray.show()
        if not self.app.config.configured:
            # первый запуск: без api_id/api_hash сразу открыть «Настройки» с инструкцией
            self.window.go("settings")
            show_window = True
        if show_window:
            self.window.show_window()

    def run(self) -> int:
        return self.qapp.exec()

    def quit(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.quitting = True
        self.t_fast.stop()
        self.t_slow.stop()
        self.proxy_checker.shutdown()
        self.tray.hide()
        self.window.close()
        self.qapp.quit()


def check_toggles(w: QWidget) -> str:
    """Все видимые тумблеры раздела должны стоять у правого края своей строки, ручка — по состоянию."""
    bad = []
    tgs = [t for t in w.stack.currentWidget().findChildren(Toggle) if t.isVisible()]
    for t in tgs:
        row = t.parentWidget()
        x = t.mapTo(row, QPoint(0, 0)).x()
        if x < row.width() / 2 or abs(t.knob - (1.0 if t.isChecked() else 0.0)) > 1e-6:
            bad.append(f"x={x}/{row.width()} knob={t.knob} checked={t.isChecked()}")
    if not tgs:
        return ""
    return f"; тумблеров {len(tgs)}, " + ("все справа, ручки по состоянию" if not bad else "ОШИБКА: " + "; ".join(bad))


def selftest(ui: QtUI, out: Path, hold: float) -> None:
    """Снимки всех разделов и диалога прокси в светлой и темной теме (Qt grab, без показа на экране),
    проверка «крестик — в трей», затем выход тем же путем, что «Выход» в трее."""
    from .proxy_dialog import ProxyDialog  # noqa: PLC0415

    out.mkdir(parents=True, exist_ok=True)
    report: list[str] = []
    app = ui.app
    w = ui.window
    orig_theme = app.config.theme

    def demo():
        """TGMCP_SELFTEST_DEMO=1: показать первый аккаунт как вошедший «Demo» (только для снимков, без сети)."""
        import os  # noqa: PLC0415
        import time as _t  # noqa: PLC0415
        if not os.environ.get("TGMCP_SELFTEST_DEMO"):
            return
        app.config._api = {"api_id": 1, "api_hash": "0" * 32, "source": "user"}  # только в памяти, без записи
        accs = app.config.accounts()
        svc = app.manager.services.get(accs[0]["id"]) if accs else None
        if svc is None:
            return
        sessions = [
            {"current": True, "device": "TG MCP Desktop Windows 11", "app": "TG MCP 1.0.0", "official": False,
             "ip": "203.0.113.10", "location": "Demo", "active": "2026-01-01T12:00+00:00", "created": None},
            {"current": False, "device": "Phone Android 15", "app": "Telegram Android 12.0", "official": True,
             "ip": "198.51.100.20", "location": "Demo", "active": "2026-01-01T11:30+00:00", "created": None},
        ]
        app.call(lambda: svc._set("online", error=None,
                                  me={"id": 1, "name": "Demo User", "username": "demo_user", "phone": "+1******0000"},
                                  security={"has_password": False, "sessions": sessions, "checked_at": _t.time()}))

    def shots():
        demo()
        w.setAttribute(Qt.WA_DontShowOnScreen, True)
        w.show()
        w.resize(1000, 720)
        report.append(f"tray: available={QSystemTrayIcon.isSystemTrayAvailable()} visible={ui.tray.isVisible()} "
                      f"tooltip={ui.tray.toolTip()!r}")
        from .. import paths, winutil  # noqa: PLC0415
        report.append(f"frozen={paths.FROZEN}; data={paths.DATA_DIR}; autostart command: {winutil.autostart_command()}")
        report.append("tray menu: " + ", ".join(a.text() or "—" for a in ui._menu.actions())
                      + f"; default={ui._menu.defaultAction().text()}")
        # клик пользователя по тумблеру: анимация ручки не двигает сам виджет
        from PySide6.QtCore import QElapsedTimer  # noqa: PLC0415
        w.go("rights")
        ui.qapp.processEvents()
        tgs = [t for t in w.page("rights").findChildren(Toggle) if t.isVisible()]
        if len(tgs) > 1:
            t = tgs[1]
            before = (t.geometry().getRect(), t.isChecked())
            t.click()
            timer = QElapsedTimer()
            timer.start()
            while timer.elapsed() < 400:
                ui.qapp.processEvents()
            mid = (t.geometry().getRect(), t.isChecked(), round(t.knob, 2))
            t.click()
            timer.restart()
            while timer.elapsed() < 400:
                ui.qapp.processEvents()
            after = (t.geometry().getRect(), t.isChecked(), round(t.knob, 2))
            report.append(f"toggle click: geometry stable={before[0] == mid[0] == after[0]}, "
                          f"state {before[1]}→{mid[1]}→{after[1]}, knob {mid[2]}→{after[2]}")
        for mode in ("light", "dark", "light"):  # третий проход — повторная смена темы на лету
            app.config.set_theme(mode)
            ui.apply_theme()
            for key in w.keys:
                w.go(key)
                ui.qapp.processEvents()
                w.refresh_current()
                ui.qapp.processEvents()
                p = out / f"{mode}_{key}.png"
                w.grab().save(str(p))
                report.append(f"{mode}/{key}: {p.name}" + check_toggles(w))
            accs = app.config.accounts()
            if accs:
                w.go("accounts")
                w.page("accounts").start_login(accs[0]["id"])
                ui.qapp.processEvents()
                w.grab().save(str(out / f"{mode}_accounts_login.png"))
                w.page("accounts").cancel_login()
                report.append(f"{mode}/accounts_login: ok")
            for name, proxy in (("socks5", {"type": "socks5", "server": "proxy.example", "port": 1080,
                                            "username": "user"}),
                                ("mtproto_faketls", {"type": "mtproto", "server": "mt.example", "port": 443,
                                                     "secret": "ee" + "0123456789abcdef" * 2 + "676f6f676c652e636f6d"})):
                d = ProxyDialog(w, proxy)
                d.setAttribute(Qt.WA_DontShowOnScreen, True)
                d.show()
                ui.qapp.processEvents()
                d.grab().save(str(out / f"{mode}_proxy_dialog_{name}.png"))
                report.append(f"{mode}/proxy_dialog_{name}: error={d.error.text()[:60]!r}")
                d.close()
                d.deleteLater()
        for s in app.manager.snapshots():
            if s.get("security"):
                from .pages import SessionsDialog  # noqa: PLC0415
                d = SessionsDialog(w, s)
                d.setAttribute(Qt.WA_DontShowOnScreen, True)
                d.show()
                ui.qapp.processEvents()
                d.grab().save(str(out / "sessions_dialog.png"))
                d.close()
                report.append("sessions_dialog: ok")
                break
        app.config.set_theme(orig_theme)
        ui.apply_theme()
        # крестик сворачивает в трей, процесс живет
        w.close()
        ui.qapp.processEvents()
        report.append(f"close(): window hidden={not w.isVisible()}, quitting={ui.quitting}")
        w.setAttribute(Qt.WA_DontShowOnScreen, False)
        (out / "selftest.txt").write_text("\n".join(report), encoding="utf-8")
        QTimer.singleShot(int(hold * 1000), app.quit)

    QTimer.singleShot(2500, shots)
