"""Диалог «Редактирование прокси» в духе Telegram Desktop: MTPROTO / SOCKS5 / HTTP."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QRadioButton, QSizePolicy, QVBoxLayout, QWidget)

from ..config import ConfigError, check_mtproto_secret, normalize_proxy, parse_proxy_link
from .widgets import label


class ProxyDialog(QDialog):
    TYPES = (("mtproto", "MTPROTO"), ("socks5", "SOCKS5"), ("http", "HTTP"))

    def __init__(self, parent=None, proxy: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Редактирование прокси")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.result_proxy: dict | None = None
        self._id = (proxy or {}).get("id")
        v = QVBoxLayout(self)
        v.setContentsMargins(26, 22, 26, 16)
        v.setSpacing(10)
        title = label("Редактирование прокси", "bigName")
        v.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(18)
        self.group = QButtonGroup(self)
        self.radios: dict[str, QRadioButton] = {}
        for key, text in self.TYPES:
            rb = QRadioButton(text)
            self.group.addButton(rb)
            self.radios[key] = rb
            row.addWidget(rb)
            rb.toggled.connect(self._layout)
        row.addStretch(1)
        v.addLayout(row)

        v.addSpacing(4)
        v.addWidget(label("Адрес сокета", "sectionTitle"))
        hp = QHBoxLayout()
        hp.setSpacing(14)
        self.host = QLineEdit()
        self.host.setPlaceholderText("Хост (или вставьте ссылку tg://proxy / t.me/socks)")
        self.port = QLineEdit()
        self.port.setPlaceholderText("Порт")
        self.port.setFixedWidth(90)
        hp.addWidget(self.host, 1)
        hp.addWidget(self.port)
        v.addLayout(hp)
        self.host.textChanged.connect(self._maybe_link)

        v.addSpacing(4)
        v.addWidget(label("Учетные данные (необязательно)", "sectionTitle"))
        self.cred_label = v.itemAt(v.count() - 1).widget()
        self.user = QLineEdit()
        self.user.setPlaceholderText("Имя пользователя")
        self.password = QLineEdit()
        self.password.setPlaceholderText("Пароль")
        self.password.setEchoMode(QLineEdit.Password)
        self.secret = QLineEdit()
        self.secret.setPlaceholderText("Ключ")
        self.secret.textChanged.connect(self._validate_secret)
        for w in (self.user, self.password, self.secret):
            v.addWidget(w)
        self.note = label("", "muted", wrap=True)
        v.addWidget(self.note)
        self.error = label("", "err", wrap=True)
        self.error.setVisible(False)
        v.addWidget(self.error)
        for w in (self.note, self.error):  # перенос по словам, высота — по содержимому
            w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.setObjectName("text")
        cancel.clicked.connect(self.reject)
        self.save = QPushButton("Сохранить")
        self.save.setObjectName("text")
        self.save.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(self.save)
        v.addLayout(btns)

        p = proxy or {"type": "socks5"}
        self.radios.get(p.get("type", "socks5"), self.radios["socks5"]).setChecked(True)
        self.host.setText(p.get("server", ""))
        self.port.setText(str(p.get("port", "")) if p.get("port") else "")
        self.user.setText(p.get("username", ""))
        self.password.setText(p.get("password", ""))
        self.secret.setText(p.get("secret", ""))
        self._layout()

    def ptype(self) -> str:
        for k, rb in self.radios.items():
            if rb.isChecked():
                return k
        return "socks5"

    def _layout(self, *_):
        mt = self.ptype() == "mtproto"
        self.user.setVisible(not mt)
        self.password.setVisible(not mt)
        self.secret.setVisible(mt)
        self.cred_label.setText("Учетные данные" if mt else "Учетные данные (необязательно)")
        self.note.setText("Поддерживаются секреты dd… и обычные (32 hex). FakeTLS (ee…) — нет: Telethon "
                          "не умеет TLS-маскировку." if mt else "")
        self.note.setVisible(mt)
        self._validate_secret()
        self._fit()

    def _maybe_link(self, text: str):
        p = parse_proxy_link(text)
        if not p:
            return
        self.host.blockSignals(True)
        self.radios[p["type"]].setChecked(True)
        self.host.setText(p["server"])
        self.host.blockSignals(False)
        self.port.setText(str(p["port"]))
        self.secret.setText(p.get("secret", ""))
        self.user.setText(p.get("username", ""))
        self.password.setText(p.get("password", ""))

    def _show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))
        self._fit()

    def _fit(self) -> None:
        """Подогнать высоту диалога под текст подсказок при текущей ширине (длинные сообщения не обрезаются)."""
        lay = self.layout()
        lay.invalidate()
        lay.activate()
        w = max(self.width(), self.minimumWidth())
        h = lay.heightForWidth(w) if lay.hasHeightForWidth() else lay.sizeHint().height()
        h = max(h, lay.minimumSize().height())
        self.setMinimumHeight(h)
        if self.height() != h:
            self.resize(w, h)

    def resizeEvent(self, e):  # noqa: N802 — при изменении ширины пересчитать высоту переносов
        super().resizeEvent(e)
        lay = self.layout()
        if lay is not None and lay.hasHeightForWidth():
            need = lay.heightForWidth(self.width())
            if need > 0 and need != self.minimumHeight():
                self.setMinimumHeight(need)

    def _set_err(self, w: QWidget, on: bool):
        w.setProperty("error", "true" if on else "false")
        w.style().unpolish(w)
        w.style().polish(w)

    def _validate_secret(self, *_):
        if self.ptype() != "mtproto" or not self.secret.text().strip():
            self._set_err(self.secret, False)
            self._show_error("")
            return True
        try:
            check_mtproto_secret(self.secret.text())
            self._set_err(self.secret, False)
            self._show_error("")
            return True
        except ConfigError as e:
            self._set_err(self.secret, True)
            self._show_error(str(e))
            return False

    def _accept(self):
        try:
            self.result_proxy = normalize_proxy({
                "id": self._id, "type": self.ptype(), "server": self.host.text(), "port": self.port.text(),
                "username": self.user.text(), "password": self.password.text(), "secret": self.secret.text()})
        except ConfigError as e:
            self._show_error(str(e)[:1].upper() + str(e)[1:])
            return
        self.accept()
