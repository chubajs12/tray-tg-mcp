"""Диалог «Редактирование прокси»: длинная подсказка (FakeTLS) переносится и не обрезается — в обеих темах.

Qt в режиме offscreen: на экран ничего не выводится.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":  # offscreen-платформе нужны настоящие шрифты, иначе метрики текста неверны
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.gui import theme  # noqa: E402
from app.gui.proxy_dialog import ProxyDialog  # noqa: E402

FAKETLS = "ee" + "0123456789abcdef" * 2 + "676f6f676c652e636f6d"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _fully_visible(d: ProxyDialog, w) -> bool:
    need = w.heightForWidth(w.width())
    bottom = w.mapTo(d, w.rect().bottomLeft()).y()
    return w.isVisible() and w.height() >= need and bottom <= d.height()


@pytest.mark.parametrize("pal", [theme.LIGHT, theme.DARK], ids=["light", "dark"])
def test_faketls_hint_wraps_and_fits(qapp, pal, tmp_path):
    qapp.setStyleSheet(theme.qss(pal))
    d = ProxyDialog(None, {"type": "socks5", "server": "proxy.example", "port": 1080})
    d.show()
    qapp.processEvents()
    h0 = d.height()
    # пользователь переключает тип и вводит FakeTLS-ключ уже в открытом окне
    d.radios["mtproto"].setChecked(True)
    d.secret.setText(FAKETLS)
    qapp.processEvents()
    assert "FakeTLS" in d.error.text() and d.error.wordWrap()
    assert _fully_visible(d, d.error), (d.error.height(), d.error.heightForWidth(d.error.width()), d.height())
    assert _fully_visible(d, d.note)
    assert d.height() > h0  # диалог вырос под текст
    # очень длинное сообщение тоже помещается
    d._show_error("Ошибка " + "очень длинного текста подсказки " * 12)
    qapp.processEvents()
    assert _fully_visible(d, d.error)
    # ошибка исчезла — подсказка скрыта
    d.secret.setText("dd" + "ab" * 16)
    qapp.processEvents()
    assert not d.error.isVisible()
    d.grab().save(str(tmp_path / f"dialog_{pal.name}.png"))
    d.close()
