"""Разделы главного окна: Статус, Аккаунты, Права MCP, Прокси, Журнал, Настройки."""

from __future__ import annotations

import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import qrcode
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QComboBox, QDialog, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QRadioButton, QScrollArea,
                               QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from .. import paths, winutil
from ..config import ConfigError, PROXY_TYPE_TITLES, mask_proxy, port_is_free, proxy_title
from ..permissions import GROUP_HINTS, GROUPS, PRESET_TITLES, PRESETS, preset_of
from .proxy_dialog import ProxyDialog
from .theme import Palette
from .widgets import Avatar, Card, SettingRow, StatusDot, Toggle, glyph_icon, label, restyle, sep

if TYPE_CHECKING:
    from .qt_app import QtUI

STATE_OBJ = {"online": "ok", "need_login": "warn", "connecting": "warn", "error": "err", "no_api": "muted",
             "stopped": "muted"}


def clear_layout(lay) -> None:
    """Убрать виджеты из layout сразу (скрыть и отцепить), а не только по deleteLater — иначе перекрываются."""
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()


def text_button(text: str, obj: str = "text") -> QPushButton:
    b = QPushButton(text)
    b.setObjectName(obj)
    b.setCursor(Qt.PointingHandCursor)
    return b


class Page(QScrollArea):
    title = ""

    def __init__(self, ui: "QtUI"):
        super().__init__()
        self.ui = ui
        self.app = ui.app
        self.pal: Palette = ui.palette
        self.setObjectName("pageScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        inner.setObjectName("page")
        outer = QHBoxLayout(inner)
        outer.setContentsMargins(24, 18, 24, 24)
        col = QWidget()
        col.setObjectName("page")
        col.setMaximumWidth(760)
        self.col = QVBoxLayout(col)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(14)
        outer.addWidget(col, 1)
        self.col.addWidget(label(self.title, "pageTitle"))
        self.setWidget(inner)
        self.build()
        self.col.addStretch(1)

    def build(self):  # переопределяется
        pass

    def refresh(self):  # раз в секунду, пока раздел открыт
        pass

    def on_show(self):  # раздел открыт (переключение или показ окна)
        pass

    def icon(self, name: str):
        return glyph_icon(name, self.pal.accent)

    def info(self, text: str):
        QMessageBox.information(self, "TG MCP", text)

    def ask(self, text: str) -> bool:
        return QMessageBox.question(self, "TG MCP", text, QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes


# ======================================================================
class StatusPage(Page):
    title = "Статус"

    def build(self):
        self.tg = Card("Telegram")
        self.tg_rows = QVBoxLayout()
        self.tg_rows.setSpacing(2)
        self.tg.add(layout=self.tg_rows)
        self.col.addWidget(self.tg)
        self._sig = None

        self.sec = Card("Безопасность")
        self.sec_rows = QVBoxLayout()
        self.sec_rows.setSpacing(2)
        self.sec.add(layout=self.sec_rows)
        self.col.addWidget(self.sec)
        self._sec_sig = None

        mcp = Card("MCP-сервер")
        self.url_val = label("", "value", select=True)
        mcp.add(SettingRow(self.icon("link"), "Адрес", self.url_val))
        self.mcp_state = label("", "ok")
        mcp.add(SettingRow(self.icon("status"), "Состояние", self.mcp_state, sub="вход только с заголовком "
                           "Authorization: Bearer <токен>"))
        b = QHBoxLayout()
        copy = text_button("Скопировать адрес и токен MCP")
        copy.clicked.connect(self.ui.copy_mcp)
        rot = text_button("Сменить токен", "danger")
        rot.clicked.connect(self.ui.rotate_token)
        b.addWidget(copy)
        b.addWidget(rot)
        b.addStretch(1)
        mcp.add(layout=b)
        self.col.addWidget(mcp)

        px = Card("Прокси")
        self.proxy_val = label("", "value")
        px.add(SettingRow(self.icon("proxy"), "Прокси приложения", self.proxy_val))
        self.col.addWidget(px)

        act = Card("Активность")
        self.count_val = label("", "value")
        self.last_val = label("", "value")
        act.add(SettingRow(self.icon("log"), "Запросов MCP за сеанс", self.count_val))
        act.add(SettingRow(self.icon("refresh"), "Последнее действие", self.last_val))
        self.col.addWidget(act)
        self.refresh()

    def refresh(self):
        st = self.app.mcp.status()
        self.url_val.setText(st["url"])
        restyle(self.mcp_state, "ok" if st["running"] else "err")
        self.mcp_state.setText("работает" if st["running"] else ("ошибка: " + (st.get("error") or "не запущен")))
        self.proxy_val.setText(proxy_title(self.app.config.active_proxy()))
        count, last = self.app.actions.stats()
        self.count_val.setText(str(count))
        if last:
            t = last["ts"][11:19]
            self.last_val.setText(f"{last['tool']} · {'ок' if last['ok'] else 'ошибка'} · {t}"
                                  + (f" · {last['account']}" if last.get("account") else ""))
        else:
            self.last_val.setText("—")
        snaps = self.app.manager.snapshots()
        self.refresh_security_card(snaps)
        sig = (self.app.config.configured, tuple((s["account_id"], s["label"], s.get("state"), s.get("error"),
                                                  json.dumps(s.get("me")), s.get("avatar")) for s in snaps))
        if sig == self._sig:
            return
        self._sig = sig
        clear_layout(self.tg_rows)
        if not self.app.config.configured:
            r = SettingRow(self.icon("key"), "api_id и api_hash не заданы", label("Настройки ›", "value"),
                           sub="Без них приложение не подключается к Telegram")
            r.clicked.connect(lambda: self.ui.window.go("settings"))
            r.setCursor(Qt.PointingHandCursor)
            self.tg_rows.addWidget(r)
        if not snaps:
            r = SettingRow(self.icon("add"), "Аккаунтов нет", label("Добавить ›", "value"))
            r.clicked.connect(lambda: self.ui.window.go("accounts"))
            r.setCursor(Qt.PointingHandCursor)
            self.tg_rows.addWidget(r)
        for s in snaps:
            self.tg_rows.addWidget(account_line(s, 40))

    def on_show(self):
        # при открытии «Статуса» — обновить сведения о безопасности, если они старше 2 минут
        for svc in list(self.app.manager.services.values()):
            self.app.run_coro(svc.refresh_security())

    def refresh_security_card(self, snaps):
        online = [s for s in snaps if s.get("state") == "online"]
        sig = tuple((s["account_id"], s["label"], json.dumps(s.get("security"), sort_keys=True)) for s in online)
        if sig == self._sec_sig:
            return
        self._sec_sig = sig
        clear_layout(self.sec_rows)
        if not online:
            self.sec_rows.addWidget(label("Появится после входа в аккаунт.", "muted"))
            return
        for s in online:
            sec = s.get("security")
            name = s["label"]
            if sec is None:
                self.sec_rows.addWidget(SettingRow(self.icon("rights"), name, label("проверка…", "muted")))
                continue
            if sec["has_password"]:
                pw = SettingRow(self.icon("rights"), f"{name}: облачный пароль", label("включен", "ok"))
            else:
                pw = SettingRow(self.icon("rights"), f"{name}: облачный пароль", label("не включен", "warn"),
                                sub="Включите в Telegram: Настройки → Конфиденциальность → Облачный пароль")
                pw.set_sub("Включите в Telegram: Настройки → Конфиденциальность → Облачный пароль", "warn")
            self.sec_rows.addWidget(pw)
            btn = text_button("Показать")
            btn.clicked.connect(lambda _=False, ss=s: SessionsDialog(self, ss).exec())
            n = len(sec.get("sessions") or [])
            self.sec_rows.addWidget(SettingRow(self.icon("accounts"), f"{name}: активных сеансов — {n}", btn,
                                               sub="Только просмотр; завершить чужие сеансы можно в Telegram: "
                                                   "Настройки → Устройства"))


class SessionsDialog(QDialog):
    """Активные сеансы аккаунта — только просмотр."""

    def __init__(self, parent, snap: dict):
        super().__init__(parent)
        self.setWindowTitle(f"Активные сеансы — {snap['label']}")
        self.resize(820, 420)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 12)
        v.addWidget(label(f"Активные сеансы — {snap['label']}", "bigName"))
        sessions = (snap.get("security") or {}).get("sessions") or []
        t = QTableWidget(len(sessions), 4)
        t.setHorizontalHeaderLabels(["Устройство", "Приложение", "IP / место", "Активность"])
        t.verticalHeader().hide()
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setShowGrid(False)
        hh = t.horizontalHeader()
        for i in range(3):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        for r, x in enumerate(sessions):
            dev = x.get("device") or ""
            if x.get("current"):
                dev = "● этот сеанс — " + dev
            vals = (dev, x.get("app") or "", f"{x.get('ip') or ''}  {x.get('location') or ''}".strip(),
                    (x.get("active") or "").replace("T", " "))
            for c, val in enumerate(vals):
                t.setItem(r, c, QTableWidgetItem(val))
        v.addWidget(t, 1)
        v.addWidget(label("Только просмотр. Завершить сеанс: Telegram → Настройки → Устройства.", "muted"))
        b = QHBoxLayout()
        b.addStretch(1)
        ok = text_button("Закрыть")
        ok.clicked.connect(self.accept)
        b.addWidget(ok)
        v.addLayout(b)


def account_line(s: dict, size: int = 40) -> QWidget:
    w = QWidget()
    w.setObjectName("row")
    w.setAttribute(Qt.WA_StyledBackground, True)
    h = QHBoxLayout(w)
    h.setContentsMargins(4, 6, 4, 6)
    h.setSpacing(12)
    me = s.get("me") or {}
    av = Avatar(size)
    av.set(me.get("name") or s["label"], s.get("avatar"), key=s["account_id"])
    h.addWidget(av)
    v = QVBoxLayout()
    v.setSpacing(0)
    name = me.get("name") or s["label"]
    v.addWidget(label(f"{name}  ·  {s['label']}" if me.get("name") else name))
    st = label(s.get("state_text", ""), STATE_OBJ.get(s.get("state"), "muted"))
    v.addWidget(st)
    h.addLayout(v, 1)
    dot = StatusDot({"ok": "#3fb04f", "warn": "#e5a12a", "err": "#e53935"}.get(
        STATE_OBJ.get(s.get("state"), ""), "#9e9e9e"))
    h.addWidget(dot)
    return w


# ======================================================================
class AccountsPage(Page):
    title = "Аккаунты"

    def build(self):
        self.cards_box = QVBoxLayout()
        self.cards_box.setSpacing(14)
        self.col.addLayout(self.cards_box)
        self._sig = None

        # добавление
        self.add_card = Card("Добавить аккаунт")
        row = QHBoxLayout()
        self.new_label = QLineEdit()
        self.new_label.setPlaceholderText("Метка аккаунта, например «Личный» или «Рабочий»")
        self.new_label.returnPressed.connect(self.add_account)
        b = text_button("Продолжить")
        b.clicked.connect(self.add_account)
        row.addWidget(self.new_label, 1)
        row.addWidget(b)
        self.add_card.add(layout=row)
        self.add_card.add(label("Каждый аккаунт — своя сессия Telegram и свои права MCP. Вход — по QR-коду.",
                                "muted", wrap=True))
        self.col.addWidget(self.add_card)

        # вход по QR
        self.login = Card()
        self.login_title = label("", "sectionTitle")
        self.login.add(self.login_title)
        body = QHBoxLayout()
        body.setSpacing(22)
        self.qr = QLabel()
        self.qr.setFixedSize(232, 232)
        self.qr.setAlignment(Qt.AlignCenter)
        self.qr.setStyleSheet("background: white; border-radius: 12px;")
        body.addWidget(self.qr, 0, Qt.AlignTop)
        right = QVBoxLayout()
        right.setSpacing(8)
        self.login_info = label("", wrap=True)
        right.addWidget(self.login_info)
        self.pw_box = QWidget()
        pv = QVBoxLayout(self.pw_box)
        pv.setContentsMargins(0, 6, 0, 0)
        self.pw_hint = label("", "muted", wrap=True)
        pv.addWidget(label("Облачный пароль (двухэтапная аутентификация)", "sectionTitle"))
        pv.addWidget(self.pw_hint)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw.setPlaceholderText("Пароль")
        self.pw.returnPressed.connect(self.submit_password)
        pv.addWidget(self.pw)
        go = text_button("Войти", "primary")
        go.clicked.connect(self.submit_password)
        pv.addWidget(go, 0, Qt.AlignLeft)
        right.addWidget(self.pw_box)
        self.login_err = label("", "err", wrap=True)
        right.addWidget(self.login_err)
        right.addStretch(1)
        cancel = text_button("Отмена")
        cancel.clicked.connect(self.cancel_login)
        right.addWidget(cancel, 0, Qt.AlignRight)
        body.addLayout(right, 1)
        self.login.add(layout=body)
        self.login.hide()
        self.col.insertWidget(2, self.login)  # после списка аккаунтов, перед «Добавить аккаунт»
        self.login_acc: str | None = None
        self._qr_url = None
        self._qr_started = False
        self.refresh()

    # ---- карточки аккаунтов ----
    def refresh(self):
        snaps = self.app.manager.snapshots()
        sig = tuple((s["account_id"], s["label"], s.get("state"), s.get("state_text"), json.dumps(s.get("me")),
                     s.get("avatar"), s.get("proxy_mode"), s.get("error")) for s in snaps) + (
            tuple(p["id"] for p in self.app.config.proxies()),)
        if sig != self._sig:
            self._sig = sig
            clear_layout(self.cards_box)
            for s in snaps:
                self.cards_box.addWidget(self.account_card(s))
        self.refresh_login()

    def account_card(self, s: dict) -> QWidget:
        c = Card()
        top = QHBoxLayout()
        top.setSpacing(16)
        me = s.get("me") or {}
        av = Avatar(64)
        av.set(me.get("name") or s["label"], s.get("avatar"), key=s["account_id"])
        top.addWidget(av, 0, Qt.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(2)
        v.addWidget(label(me.get("name") or "Вход не выполнен", "bigName"))
        sub = " · ".join(x for x in ((("@" + me["username"]) if me.get("username") else ""),
                                     me.get("phone") or "") if x)
        if sub:
            v.addWidget(label(sub, "muted"))
        st = label(s.get("state_text", "") + (f" — {s['error']}" if s.get("error") else ""),
                   STATE_OBJ.get(s.get("state"), "muted"), wrap=True)
        v.addWidget(st)
        top.addLayout(v, 1)
        chip = label(s["label"], "value")
        top.addWidget(chip, 0, Qt.AlignTop)
        c.add(layout=top)
        c.add(sep())
        # прокси аккаунта
        combo = QComboBox()
        opts = [("app", "как у приложения"), ("none", "без прокси")] + [
            (p["id"], proxy_title(p)) for p in self.app.config.proxies()]
        for k, t in opts:
            combo.addItem(t, k)
        idx = max(0, combo.findData(s.get("proxy_mode", "app")))
        combo.setCurrentIndex(idx)
        combo.setMinimumWidth(240)
        combo.currentIndexChanged.connect(lambda _i, cb=combo, acc=s: self.set_proxy(acc, cb.currentData()))
        c.add(SettingRow(self.icon("proxy"), "Прокси", combo))
        perms = self.app.config.permissions(s["account_id"])
        pr = SettingRow(self.icon("rights"), "Права MCP",
                        label(PRESET_TITLES.get(preset_of(perms) or "", "особые") + " ›", "value"))
        pr.setCursor(Qt.PointingHandCursor)
        pr.clicked.connect(lambda acc=s["account_id"]: self.ui.window.go("rights", account=acc))
        c.add(pr)
        b = QHBoxLayout()
        st_ = s.get("state")
        if st_ in ("need_login", "error", "connecting", "no_api"):
            q = text_button("Войти по QR")
            q.clicked.connect(lambda _=False, acc=s["account_id"]: self.start_login(acc))
            b.addWidget(q)
        if st_ == "online":
            lo = text_button("Выйти")
            lo.clicked.connect(lambda _=False, acc=s: self.logout(acc))
            b.addWidget(lo)
        b.addStretch(1)
        rm = text_button("Удалить из списка", "danger")
        rm.clicked.connect(lambda _=False, acc=s: self.remove(acc))
        b.addWidget(rm)
        c.add(layout=b)
        return c

    def set_proxy(self, acc: dict, key: str):
        if key == acc.get("proxy_mode"):
            return
        self.app.config.set_account_proxy(acc["account_id"], key)
        self.app.actions.write("account_proxy_changed", {"proxy": key}, True, source="tray",
                               account={"id": acc["account_id"], "label": acc["label"]})
        self.app.call(lambda: self.app.manager.reconfigure(acc["account_id"]))

    def logout(self, s: dict):
        if self.ask(f"Выйти из аккаунта «{s['label']}»?\n\nСеанс будет завершен на сервере Telegram, ключ "
                    "сессии удален с этого ПК. Для повторного входа понадобится QR."):
            self.app.run_coro(self.app.manager.log_out(s["account_id"]))

    def remove(self, s: dict):
        if self.ask(f"Удалить аккаунт «{s['label']}» из списка?\n\nЕсли вход выполнен — сеанс будет завершен "
                    "на сервере Telegram; ключ сессии, папка аккаунта и его права MCP на этом ПК удаляются."):
            if self.login_acc == s["account_id"]:
                self.cancel_login()
            self.app.run_coro(self.app.manager.remove_account(s["account_id"]))

    def add_account(self):
        text = self.new_label.text().strip()
        if not text:
            self.new_label.setFocus()
            return
        fut = self.app.run_coro(self.app.manager.add_account(text))

        def done(f):
            try:
                acc_id = f.result()
            except Exception as e:  # noqa: BLE001
                self.ui.post(lambda: self.info(f"Аккаунт не добавлен: {e}"))
                return
            self.ui.post(lambda: (self.new_label.clear(), self.start_login(acc_id)))

        fut.add_done_callback(done)

    # ---- вход по QR ----
    def start_login(self, account_id: str):
        self.login_acc = account_id
        self._qr_url = None
        self._qr_started = False
        self.qr.clear()
        self.pw_box.hide()
        self.login_err.setText("")
        a = self.app.config.account(account_id)
        self.login_title.setText(f"Вход по QR — {a['label'] if a else account_id}")
        self.login.show()
        self.add_card.hide()
        self.ensureWidgetVisible(self.login)
        self.refresh_login()

    def cancel_login(self):
        svc = self.app.manager.services.get(self.login_acc or "")
        if svc is not None and svc.snapshot()["qr"].get("state") not in ("done", "idle"):
            self.app.run_coro(svc.cancel_qr())
        self.login_acc = None
        self.login.hide()
        self.add_card.show()

    def submit_password(self):
        pw = self.pw.text()
        self.pw.clear()
        svc = self.app.manager.services.get(self.login_acc or "")
        if pw and svc is not None:
            self.app.call(lambda: svc.submit_password(pw))

    def _qr_pixmap(self, url: str) -> QPixmap:
        img = qrcode.make(url, box_size=8, border=2).get_image().convert("RGB")
        data = img.tobytes("raw", "RGB")
        qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
        return QPixmap.fromImage(qimg).scaled(220, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def refresh_login(self):
        if not self.login_acc:
            return
        self.qr.setVisible(self._qr_url is not None)
        svc = self.app.manager.services.get(self.login_acc)
        if svc is None:
            self.cancel_login()
            return
        if not self.app.config.configured:
            self.qr.clear()
            self.login_info.setText("Не заданы api_id и api_hash.\n\nОткройте раздел «Настройки», впишите api_id "
                                    "и api_hash (my.telegram.org → API development tools), затем снова "
                                    "«Войти по QR».")
            return
        snap = svc.snapshot()
        if not self._qr_started:
            if snap["state"] == "online":
                self.login_info.setText("Вход уже выполнен.")
                QTimer.singleShot(1200, self.cancel_login)
                self._qr_started = True
                return
            if snap["state"] == "need_login":
                self._qr_started = True
                self.app.run_coro(svc.start_qr())
            else:
                self.login_info.setText(f"Жду соединения с Telegram: {snap['state_text']}"
                                        + (f"\n{snap['error']}" if snap.get("error") else "")
                                        + f"\nПрокси: {snap['proxy']}")
                return
        qr = snap["qr"]
        st = qr.get("state")
        if st == "waiting" and qr.get("url"):
            if qr["url"] != self._qr_url:
                self._qr_url = qr["url"]
                self.qr.setPixmap(self._qr_pixmap(qr["url"]))
                self.qr.show()
            left = max(0, int(qr.get("expires", time.time()) - time.time()))
            self.login_info.setText("1. Откройте Telegram на телефоне.\n"
                                    "2. Настройки → Устройства → «Подключить устройство».\n"
                                    "3. Наведите камеру на этот код.\n\n"
                                    f"Код обновляется сам (еще ~{left} с).")
            self.pw_box.hide()
        elif st in ("password", "checking"):
            self.qr.clear()
            self._qr_url = None
            self.pw_box.show()
            hint = qr.get("hint")
            self.pw_hint.setText("Аккаунт защищен облачным паролем." + (f" Подсказка: {hint}" if hint else ""))
            self.login_info.setText("Проверяю пароль…" if st == "checking" else "QR-код принят.")
            self.login_err.setText(qr.get("error") or "")
            if st == "password":
                self.pw.setFocus()
        elif st == "done":
            self.login_info.setText("Вход выполнен.")
            self.pw_box.hide()
            QTimer.singleShot(900, self.cancel_login)
        elif st == "error":
            self.login_err.setText(f"Ошибка входа: {qr.get('error')}")
        else:
            self.login_info.setText("Получаю QR-код…")


# ======================================================================
class RightsPage(Page):
    title = "Права MCP"

    def build(self):
        top = Card("Аккаунт")
        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(self.load)
        top.add(self.combo)
        top.add(label("Что Claude Code может делать через MCP с выбранным аккаунтом. Изменения действуют "
                      "сразу; через MCP права изменить нельзя — только здесь.", "muted", wrap=True))
        self.col.addWidget(top)

        self.groups_card = Card("Разрешено через MCP")
        self.toggles: dict[str, Toggle] = {}
        for g, t in GROUPS.items():
            tg = Toggle(self.pal)
            tg.toggled.connect(lambda on, key=g: self.changed(key, on))
            self.toggles[g] = tg
            self.groups_card.add(SettingRow(None, t, tg, sub=GROUP_HINTS.get(g)))
        self.col.addWidget(self.groups_card)

        pre = Card("Быстрые наборы")
        h = QHBoxLayout()
        for key, t in PRESET_TITLES.items():
            b = text_button(t)
            b.clicked.connect(lambda _=False, k=key: self.preset(k))
            h.addWidget(b)
        h.addStretch(1)
        pre.add(layout=h)
        self.preset_label = label("", "muted")
        pre.add(self.preset_label)
        self.col.addWidget(pre)
        self.empty = label("Аккаунтов пока нет — добавьте в разделе «Аккаунты».", "muted")
        self.col.addWidget(self.empty)
        self._accs = None
        self._loading = False
        self.refresh()

    def select(self, account_id: str):
        i = self.combo.findData(account_id)
        if i >= 0:
            self.combo.setCurrentIndex(i)

    def refresh(self):
        accs = tuple((a["id"], a["label"]) for a in self.app.config.accounts())
        if accs != self._accs:
            cur = self.combo.currentData()
            self._accs = accs
            self.combo.blockSignals(True)
            self.combo.clear()
            for i, lbl in accs:
                self.combo.addItem(lbl, i)
            self.combo.blockSignals(False)
            if cur:
                self.select(cur)
            self.load()
        has = bool(accs)
        self.groups_card.setVisible(has)
        self.empty.setVisible(not has)

    def load(self, *_):
        acc = self.combo.currentData()
        if not acc:
            return
        self._loading = True
        perms = self.app.config.permissions(acc)
        for g, tg in self.toggles.items():
            tg.setChecked(perms[g])
        self._loading = False
        self.preset_label.setText("Сейчас: " + PRESET_TITLES.get(preset_of(perms) or "", "особый набор"))

    def _save(self, new: dict):
        acc = self.combo.currentData()
        if not acc:
            return
        before = self.app.config.permissions(acc)
        after = self.app.config.set_permissions(acc, new)
        if before != after:
            a = self.app.config.account(acc) or {}
            self.app.actions.write("permissions_changed", {"before": before, "after": after}, True,
                                   source="tray", account={"id": acc, "label": a.get("label")})
        self.preset_label.setText("Сейчас: " + PRESET_TITLES.get(preset_of(after) or "", "особый набор"))

    def changed(self, key: str, on: bool):
        if self._loading:
            return
        self._save({g: (on if g == key else t.isChecked()) for g, t in self.toggles.items()})

    def preset(self, key: str):
        self._save(dict(PRESETS[key]))
        self.load()


# ======================================================================
class ProxyChecker:
    """TCP-проверка прокси в фоне (как «доступен (пинг: N мс)» в Telegram)."""

    def __init__(self, ui: "QtUI"):
        self.ui = ui
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="proxy-check")
        self.results: dict[str, tuple[str, int | None, float]] = {}  # id → (state, ms, when)
        self.listeners: list = []

    def check(self, p: dict):
        key = f"{p['server']}:{p['port']}"
        self.results[key] = ("checking", None, time.time())
        self._notify()

        def work():
            t0 = time.monotonic()
            try:
                with socket.create_connection((p["server"], int(p["port"])), timeout=5):
                    res = ("ok", int((time.monotonic() - t0) * 1000), time.time())
            except OSError:
                res = ("fail", None, time.time())
            self.ui.post(lambda: (self.results.__setitem__(key, res), self._notify()))

        self.pool.submit(work)

    def status(self, p: dict) -> tuple[str, int | None]:
        r = self.results.get(f"{p['server']}:{p['port']}")
        return (r[0], r[1]) if r else ("unknown", None)

    def stale(self, p: dict, age: float = 30) -> bool:
        r = self.results.get(f"{p['server']}:{p['port']}")
        return r is None or (r[0] != "checking" and time.time() - r[2] > age)

    def _notify(self):
        for fn in list(self.listeners):
            try:
                fn()
            except RuntimeError:
                self.listeners.remove(fn)

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


class ProxyPage(Page):
    title = "Прокси"

    def build(self):
        self.checker: ProxyChecker = self.ui.proxy_checker
        self.checker.listeners.append(self.render_status)
        mode = Card("Настройки прокси")
        self.rb_off = QRadioButton("Отключить прокси")
        self.rb_on = QRadioButton("Использовать собственный прокси")
        g = QButtonGroup(self)
        g.addButton(self.rb_off)
        g.addButton(self.rb_on)
        self.rb_off.clicked.connect(self.disable)
        self.rb_on.clicked.connect(self.enable)
        mode.add(self.rb_off)
        mode.add(self.rb_on)
        self.col.addWidget(mode)

        self.list_card = Card("Список прокси")
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(2)
        self.list_card.add(layout=self.rows_box)
        add = text_button("+  Добавить прокси")
        add.clicked.connect(lambda: self.edit(None))
        self.list_card.add(add)
        self.col.addWidget(self.list_card)

        note = Card("Подсказка")
        note.add(label("Аккаунты используют прокси приложения, если в разделе «Аккаунты» не выбран свой. "
                       "MTProto: поддерживаются секреты dd… и обычные; FakeTLS (ee…, например mtg) библиотека "
                       "Telethon не поддерживает — для такого сервера используйте SOCKS5/HTTP.",
                       "muted", wrap=True))
        self.col.addWidget(note)
        self.group = QButtonGroup(self)
        self.status_labels: dict[str, QLabel] = {}
        self._sig = None
        self.refresh()

    def refresh(self):
        cfg = self.app.config
        active = cfg.active_proxy()
        proxies = cfg.proxies()
        sig = (tuple((p["id"], p["type"], p["server"], p["port"]) for p in proxies), (active or {}).get("id"))
        if sig != self._sig:
            self._sig = sig
            self.rebuild(proxies, active)
        for p in proxies:
            if self.checker.stale(p):
                self.checker.check(p)

    def rebuild(self, proxies, active):
        self.rb_on.setChecked(active is not None)
        self.rb_off.setChecked(active is None)
        clear_layout(self.rows_box)
        for b in self.group.buttons():
            self.group.removeButton(b)
        self.status_labels = {}
        if not proxies:
            self.rows_box.addWidget(label("Список пуст. Прокси не задан — подключение напрямую.", "muted"))
        for p in proxies:
            row = QWidget()
            row.setObjectName("row")
            row.setAttribute(Qt.WA_StyledBackground, True)
            h = QHBoxLayout(row)
            h.setContentsMargins(4, 6, 4, 6)
            h.setSpacing(12)
            rb = QRadioButton()
            rb.setChecked(active is not None and p["id"] == active["id"])
            rb.clicked.connect(lambda _=False, pid=p["id"]: self.use(pid))
            self.group.addButton(rb)
            h.addWidget(rb)
            v = QVBoxLayout()
            v.setSpacing(0)
            v.addWidget(label(f"{p['server']}:{p['port']}"))
            st = label("", "muted")
            self.status_labels[p["id"]] = st
            v.addWidget(st)
            h.addLayout(v, 1)
            h.addWidget(label(PROXY_TYPE_TITLES[p["type"]], "muted"))
            dots = QToolButton()
            dots.setObjectName("dots")
            dots.setText("⋮")
            dots.setPopupMode(QToolButton.InstantPopup)
            m = QMenu(dots)
            m.addAction("Изменить", lambda pp=p: self.edit(pp))
            m.addAction("Проверить", lambda pp=p: self.checker.check(pp))
            m.addAction("Удалить", lambda pp=p: self.delete(pp))
            dots.setMenu(m)
            h.addWidget(dots)
            self.rows_box.addWidget(row)
        self.render_status()

    def render_status(self):
        for p in self.app.config.proxies():
            lb = self.status_labels.get(p["id"])
            if lb is None:
                continue
            try:
                state, ms = self.checker.status(p)
                if state == "ok":
                    lb.setText(f"доступен (пинг: {ms} мс)")
                    restyle(lb, "ok")
                elif state == "fail":
                    lb.setText("недоступен")
                    restyle(lb, "err")
                else:
                    lb.setText("проверка…")
                    restyle(lb, "muted")
            except RuntimeError:  # виджет уже удален
                pass

    def _journal(self, event: str, p: dict | None = None):
        self.app.actions.write(event, {"proxy": mask_proxy(p)} if p else {}, True, source="tray")

    def _apply(self):
        self.app.call(self.app.manager.reconfigure_all)
        self._sig = None
        self.refresh()

    def disable(self):
        if self.app.config.active_proxy() is None:
            return
        self.app.config.set_active_proxy(None)
        self._journal("proxy_disabled")
        self._apply()

    def enable(self):
        proxies = self.app.config.proxies()
        if not proxies:
            self.rb_off.setChecked(True)
            self.edit(None, activate=True)
            return
        if self.app.config.active_proxy() is None:
            self.use(proxies[0]["id"])

    def use(self, pid: str):
        self.app.config.set_active_proxy(pid)
        self._journal("proxy_activated", next((p for p in self.app.config.proxies() if p["id"] == pid), None))
        self._apply()

    def edit(self, p: dict | None, activate: bool = False):
        d = ProxyDialog(self, p)
        if d.exec() and d.result_proxy:
            was_active = (self.app.config.active_proxy() or {}).get("id")
            saved = self.app.config.upsert_proxy(d.result_proxy, make_active=activate or p is None and was_active is None)
            self._journal("proxy_saved", saved)
            self.checker.check(saved)
            self._apply()

    def delete(self, p: dict):
        if self.ask(f"Удалить прокси {p['server']}:{p['port']}?"):
            self.app.config.remove_proxy(p["id"])
            self._journal("proxy_removed", p)
            self._apply()


# ======================================================================
class LogPage(Page):
    title = "Журнал"

    def build(self):
        c = Card("Последние действия")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Время", "Аккаунт", "Действие", "Итог"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setMinimumHeight(420)
        c.add(self.table)
        b = QHBoxLayout()
        r = text_button("Обновить")
        r.clicked.connect(self.load)
        f = text_button("Открыть файл")
        f.clicked.connect(lambda: _startfile(paths.ACTIONS_LOG))
        lg = text_button("Технический лог")
        lg.clicked.connect(lambda: _startfile(paths.LOG_DIR))
        b.addWidget(r)
        b.addWidget(f)
        b.addWidget(lg)
        b.addStretch(1)
        c.add(layout=b)
        self.col.addWidget(c)
        self._mtime = None
        self.load()

    def refresh(self):
        try:
            m = paths.ACTIONS_LOG.stat().st_mtime
        except OSError:
            m = None
        if m != self._mtime:
            self.load()

    def load(self):
        try:
            self._mtime = paths.ACTIONS_LOG.stat().st_mtime
            with paths.ACTIONS_LOG.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 400_000))
                lines = f.read().decode("utf-8", "replace").splitlines()[-300:]
        except OSError:
            lines = []
        rows = []
        for ln in reversed(lines):
            try:
                rows.append(json.loads(ln))
            except ValueError:
                continue
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            ts = (r.get("ts") or "").replace("T", " ")[:19]
            acc = (r.get("account") or {}).get("label") or ""
            tool = r.get("tool", "")
            if r.get("source") == "tray":
                tool = "трей: " + tool
            res = "ок" if r.get("ok") else ("ошибка: " + str(r.get("error") or ""))
            for j, val in enumerate((ts, acc, tool, res)):
                it = QTableWidgetItem(val)
                if j == 3 and not r.get("ok"):
                    it.setForeground(QColor(self.pal.err))
                if j == 3:
                    it.setToolTip(val)
                self.table.setItem(i, j, it)


def _startfile(p):
    try:
        os.startfile(str(p))  # noqa: S606 — действие пользователя
    except OSError:
        pass


# ======================================================================
class SettingsPage(Page):
    title = "Настройки"

    def build(self):
        cfg = self.app.config
        snap = cfg.snapshot()
        api = Card("Telegram API")
        # блок первого запуска: что такое api_id/api_hash и где взять
        self.need = QWidget()
        nv = QVBoxLayout(self.need)
        nv.setContentsMargins(0, 0, 0, 6)
        nv.setSpacing(4)
        nv.addWidget(label("Нужны api_id и api_hash", "bigName"))
        steps = label(
            "Это ключи вашего собственного «приложения» Telegram; без них клиент не подключается.<br>"
            "1. Откройте <a href=\"https://my.telegram.org\">my.telegram.org</a> и войдите по номеру телефона "
            "(код придет в Telegram).<br>"
            "2. API development tools → Create application: App title и Short name — простое латинское имя "
            "без цифр и точек, Platform — Desktop, Description можно оставить пустым.<br>"
            "3. Скопируйте сюда App api_id и App api_hash и нажмите «Сохранить».<br>"
            "Если сайт отвечает «Too many attempts» или ERROR — попробуйте позже или с мобильного интернета: "
            "запросы через VPN и дата-центры часто отклоняются.", "muted", wrap=True)
        steps.setOpenExternalLinks(True)
        steps.setTextFormat(Qt.RichText)
        nv.addWidget(steps)
        api.add(self.need)
        # источник и место хранения; хеш не показывается
        self.src_row = SettingRow(self.icon("key"), "", None)
        api.add(self.src_row)
        bh = QHBoxLayout()
        self.btn_own = text_button("Изменить…")
        self.btn_own.clicked.connect(lambda: self.update_api_ui(own_open=True))
        bh.addWidget(self.btn_own)
        bh.addStretch(1)
        api.add(layout=bh)
        self.own = QWidget()
        ov = QVBoxLayout(self.own)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(8)
        self.api_id = QLineEdit()
        self.api_id.setPlaceholderText("api_id (число)")
        self.api_hash = QLineEdit()
        self.api_hash.setPlaceholderText("api_hash (32 символа)")
        self.api_hash.setEchoMode(QLineEdit.Password)
        ov.addWidget(SettingRow(self.icon("key"), "api_id", self.api_id))
        eye = QToolButton()
        eye.setObjectName("dots")
        eye.setIcon(glyph_icon("eye", self.pal.text2))
        eye.setToolTip("Показать / скрыть")
        eye.setCheckable(True)
        eye.toggled.connect(lambda on: self.api_hash.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        hh = QWidget()
        hl = QHBoxLayout(hh)
        hl.setContentsMargins(0, 0, 0, 0)
        self.api_hash.setMinimumWidth(300)
        hl.addWidget(self.api_hash)
        hl.addWidget(eye)
        ov.addWidget(SettingRow(self.icon("key"), "api_hash", hh))
        hint = label("Хранятся в Диспетчере учетных данных Windows (или в файле, зашифрованном DPAPI), "
                     "не в config.json. Один набор на приложение — для всех аккаунтов.", "muted", wrap=True)
        ov.addWidget(hint)
        api.add(self.own)
        self.col.addWidget(api)
        self.update_api_ui(own_open=cfg.api_source is None)

        mcp = Card("MCP-сервер")
        self.port = QLineEdit(str(snap.get("mcp_port")))
        self.port.setFixedWidth(90)
        mcp.add(SettingRow(self.icon("port"), "Порт (127.0.0.1)", self.port))
        self.col.addWidget(mcp)

        b = QHBoxLayout()
        b.addStretch(1)
        self.saved = label("", "ok")
        b.addWidget(self.saved)
        save = text_button("Сохранить", "primary")
        save.clicked.connect(self.save)
        b.addWidget(save)
        self.col.addLayout(b)

        sys_ = Card("Система")
        self.autostart = Toggle(self.pal, winutil.autostart_enabled())
        self.autostart.toggled.connect(self.set_autostart)
        sys_.add(SettingRow(self.icon("power"), "Запускать при входе в Windows", self.autostart,
                            sub="Окно не открывается — только иконка в трее"))
        self.col.addWidget(sys_)

        th = Card("Оформление")
        g = QButtonGroup(self)
        for key, text in (("system", "Как в системе"), ("light", "Светлая"), ("dark", "Темная")):
            rb = QRadioButton(text)
            rb.setChecked(cfg.theme == key)
            rb.clicked.connect(lambda _=False, k=key: self.ui.set_theme(k))
            g.addButton(rb)
            th.add(rb)
        self.col.addWidget(th)

    def set_autostart(self, on: bool):
        try:
            winutil.set_autostart(on)
            self.app.actions.write("autostart", {"enabled": on}, True, source="tray")
        except OSError as e:
            self.info(f"Не удалось изменить автозапуск: {e}")

    def update_api_ui(self, own_open: bool = False):
        cfg = self.app.config
        where = {"keyring": "хранятся в Диспетчере учетных данных Windows",
                 "dpapi": "хранятся в файле, зашифрованном DPAPI"}.get(cfg.api_storage or "", "")
        configured = cfg.configured
        self.need.setVisible(not configured)
        self.src_row.setVisible(configured)
        if configured:
            self.src_row.text.setText(f"Заданы: api_id {cfg.api_id}, api_hash скрыт")
            self.src_row.set_sub(where)
        self.own.setVisible(own_open or not configured)
        self.btn_own.setVisible(configured and not self.own.isVisible())

    def save(self):
        cfg = self.app.config
        old = cfg.snapshot()
        old_api = (cfg.api_id, cfg.api_hash)
        try:
            if self.own.isVisible() and (self.api_id.text().strip() or self.api_hash.text().strip()):
                cfg.set_api(self.api_id.text(), self.api_hash.text())
                self.api_id.clear()
                self.api_hash.clear()
            new_port = int(self.port.text())
            port_changed = new_port != old.get("mcp_port")
            if port_changed:
                if not port_is_free(new_port):
                    raise ConfigError(f"порт {new_port} занят")
                cfg.set_mcp_port(new_port)
        except (ConfigError, ValueError) as e:
            self.saved.setText("")
            self.info(f"Не сохранено: {e}")
            return
        api_changed = old_api != (cfg.api_id, cfg.api_hash)
        self.update_api_ui()
        if api_changed:
            self.app.call(self.app.manager.reconfigure_all)
        if port_changed:
            self.app.run_coro(self.app.mcp.restart())
        self.app.actions.write("settings_changed", {"api_changed": api_changed, "mcp_port": cfg.mcp_port},
                               True, source="tray")
        self.saved.setText("Сохранено")
        QTimer.singleShot(2500, lambda: self.saved.setText(""))


PAGES = [
    ("status", "Статус", StatusPage),
    ("accounts", "Аккаунты", AccountsPage),
    ("rights", "Права MCP", RightsPage),
    ("proxy", "Прокси", ProxyPage),
    ("log", "Журнал", LogPage),
    ("settings", "Настройки", SettingsPage),
]
