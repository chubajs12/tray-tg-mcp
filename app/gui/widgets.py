"""Свои виджеты в стиле Telegram: тумблер, круглый аватар, карточка-секция, строки, значки."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import (Property, QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSize, Qt,
                            Signal)
from PySide6.QtGui import QBrush, QColor, QFont, QFontDatabase, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout,
                               QWidget)

from .theme import Palette

# ---------- значки: шрифт Segoe Fluent Icons / Segoe MDL2 Assets (есть в Windows 10/11) ----------
GLYPHS = {
    "status": "\ue9d9",    # Diagnostic
    "accounts": "\ue716",  # Contact
    "rights": "\ue72e",    # Lock
    "proxy": "\ue968",     # Network
    "log": "\ue81c",       # History
    "settings": "\ue713",  # Settings
    "copy": "\ue8c8",
    "refresh": "\ue72c",
    "add": "\ue710",
    "key": "\ue8d7",
    "link": "\ue71b",
    "power": "\ue7e8",
    "theme": "\ue790",
    "port": "\ue968",
    "check": "\ue73e",
    "eye": "\ue7b3",
}
_icon_font: str | None = None


def icon_font() -> str:
    global _icon_font
    if _icon_font is None:
        fams = set(QFontDatabase.families())
        _icon_font = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), "")
    return _icon_font


def glyph_icon(name: str, color: str, size: int = 20) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    fam = icon_font()
    if fam:
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(fam)
        f.setPixelSize(int(size * 1.25))
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(pm.rect(), Qt.AlignCenter, GLYPHS.get(name, "?"))
        p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


# ---------- тумблер ----------
class Toggle(QAbstractButton):
    """Переключатель как в Telegram: синий трек, белый кругляш.

    Анимация — только при клике пользователя (сигнал clicked). Программная установка состояния
    (setChecked, смена темы, загрузка прав) ставит ручку сразу. Своё animatable-свойство называется
    «knob»: имя «pos» перекрывало бы Qt-свойство QWidget::pos, и анимация двигала бы сам виджет.
    """

    def __init__(self, palette: Palette, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._p = palette
        self._knob = 1.0 if checked else 0.0
        super().setChecked(checked)
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(120)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)
        self.clicked.connect(self._animate)
        self.setFixedSize(40, 24)

    def set_palette(self, p: Palette) -> None:
        self._p = p
        self.update()

    def _animate(self, on: bool):
        self._anim.stop()
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _get_knob(self):
        return self._knob

    def _set_knob(self, v):
        self._knob = float(v)
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def setChecked(self, on: bool) -> None:  # noqa: N802
        self._anim.stop()
        self._knob = 1.0 if on else 0.0
        super().setChecked(on)
        self.update()

    def sizeHint(self):  # noqa: N802
        return QSize(40, 24)

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        k = max(0.0, min(1.0, self._knob))
        track_off = QColor(self._p.text2)
        track_off.setAlpha(110)
        on = QColor(self._p.accent)
        c = QColor(
            int(track_off.red() + (on.red() - track_off.red()) * k),
            int(track_off.green() + (on.green() - track_off.green()) * k),
            int(track_off.blue() + (on.blue() - track_off.blue()) * k),
        )
        if not self.isEnabled():
            c.setAlpha(90)
        # геометрия — от текущего rect виджета
        rect = QRectF(self.rect())
        d = min(rect.height() - 6, 18.0)               # диаметр ручки
        track_h = d * 0.78
        track = QRectF(rect.left() + 2, rect.center().y() - track_h / 2, rect.width() - 4, track_h)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(track, track_h / 2, track_h / 2)
        x0 = track.left()
        x1 = track.right() - d
        x = x0 + (x1 - x0) * k
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(c, 2))
        p.drawEllipse(QRectF(x, rect.center().y() - d / 2, d, d))
        p.end()


# ---------- аватар ----------
AVATAR_COLORS = ["#e17076", "#7bc862", "#65aadd", "#a695e7", "#ee7aae", "#6ec9cb", "#faa774"]


class Avatar(QLabel):
    def __init__(self, size: int = 56, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)

    def set(self, name: str | None, image: str | Path | None = None, key: str = "") -> None:
        s = self._size
        dpr = 2
        pm = QPixmap(s * dpr, s * dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, s * dpr, s * dpr))
        p.setClipPath(path)
        src = QPixmap(str(image)) if image and Path(image).exists() else QPixmap()
        if not src.isNull():
            p.drawPixmap(pm.rect(), src.scaled(s * dpr, s * dpr, Qt.KeepAspectRatioByExpanding,
                                               Qt.SmoothTransformation))
        else:
            h = int(hashlib.md5((key or name or "?").encode("utf-8")).hexdigest(), 16)
            p.fillRect(pm.rect(), QColor(AVATAR_COLORS[h % len(AVATAR_COLORS)]))
            initials = "".join(w[0] for w in (name or "?").split()[:2]).upper() or "?"
            f = QFont("Segoe UI")
            f.setPixelSize(int(s * dpr * 0.38))
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#ffffff"))
            p.drawText(pm.rect(), Qt.AlignCenter, initials)
        p.end()
        pm.setDevicePixelRatio(dpr)
        self.setPixmap(pm)


class StatusDot(QWidget):
    def __init__(self, color: str = "#999", parent=None):
        super().__init__(parent)
        self._c = color
        self.setFixedSize(10, 10)

    def set_color(self, c: str):
        self._c = c
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(self._c)))
        p.drawEllipse(QPointF(5, 5), 4.5, 4.5)
        p.end()


# ---------- карточка-секция ----------
class Card(QFrame):
    def __init__(self, title: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(22, 16, 22, 16)
        self.lay.setSpacing(8)
        if title:
            t = QLabel(title)
            t.setObjectName("sectionTitle")
            self.lay.addWidget(t)

    def add(self, w: QWidget | None = None, layout=None):
        if w is not None:
            self.lay.addWidget(w)
        if layout is not None:
            self.lay.addLayout(layout)
        return w


def label(text: str = "", obj: str | None = None, wrap: bool = False, select: bool = False) -> QLabel:
    lb = QLabel(text)
    if obj:
        lb.setObjectName(obj)
    lb.setWordWrap(wrap)
    if select:
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


def sep() -> QFrame:
    f = QFrame()
    f.setObjectName("sep")
    return f


class SettingRow(QWidget):
    """Строка настроек: значок — текст (+подпись) — справа значение/виджет."""

    clicked = Signal()

    def __init__(self, icon: QIcon | None, text: str, right: QWidget | None = None, sub: str | None = None,
                 parent=None):
        super().__init__(parent)
        self.setObjectName("row")
        self.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 6, 4, 6)
        h.setSpacing(14)
        if icon is not None:
            ic = QLabel()
            ic.setPixmap(icon.pixmap(22, 22))
            ic.setFixedWidth(26)
            h.addWidget(ic)
        v = QVBoxLayout()
        v.setSpacing(0)
        self.text = QLabel(text)
        v.addWidget(self.text)
        self.sub = QLabel(sub or "")
        self.sub.setObjectName("muted")
        self.sub.setWordWrap(True)
        self.sub.setVisible(bool(sub))
        v.addWidget(self.sub)
        h.addLayout(v, 1)
        if right is not None:
            h.addWidget(right, 0, Qt.AlignRight | Qt.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_sub(self, text: str, obj: str = "muted"):
        self.sub.setText(text)
        self.sub.setObjectName(obj)
        self.sub.style().unpolish(self.sub)
        self.sub.style().polish(self.sub)
        self.sub.setVisible(bool(text))

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


def restyle(w: QWidget, obj: str | None = None) -> None:
    if obj is not None:
        w.setObjectName(obj)
    w.style().unpolish(w)
    w.style().polish(w)
