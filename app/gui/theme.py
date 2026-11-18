"""Темы в стиле Telegram Desktop: светлая и темная. Палитра + QSS."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str           # фон окна (серый вокруг карточек)
    card: str         # карточки-секции
    nav: str          # левая панель
    nav_active: str   # выделенный раздел
    text: str
    text2: str        # вторичный текст
    accent: str       # синий Telegram
    accent_hover: str
    border: str
    input_bg: str
    ok: str
    warn: str
    err: str
    row_hover: str


LIGHT = Palette(
    name="light", bg="#f1f2f6", card="#ffffff", nav="#ffffff", nav_active="#e9f2fc",
    text="#000000", text2="#8a8a8a", accent="#3390ec", accent_hover="#2a7fd4", border="#e7e7e7",
    input_bg="#ffffff", ok="#3fb04f", warn="#e5a12a", err="#e53935", row_hover="#f4f4f5",
)
DARK = Palette(
    name="dark", bg="#0e1621", card="#17212b", nav="#17212b", nav_active="#2b5278",
    text="#f5f5f5", text2="#708499", accent="#5288c1", accent_hover="#6ab3f3", border="#101921",
    input_bg="#242f3d", ok="#5bc06a", warn="#e5a12a", err="#ef5350", row_hover="#202b36",
)


def system_is_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:  # noqa: BLE001
        return False


def resolve(mode: str) -> Palette:
    if mode == "dark":
        return DARK
    if mode == "light":
        return LIGHT
    return DARK if system_is_dark() else LIGHT


def qss(p: Palette) -> str:
    return f"""
* {{ font-family: "Segoe UI", "Open Sans", sans-serif; font-size: 10pt; color: {p.text}; }}
QMainWindow, QDialog, #page, #pageScroll, #pageScroll > QWidget > QWidget {{ background: {p.bg}; }}
QScrollArea {{ border: none; background: {p.bg}; }}
QToolTip {{ background: {p.card}; color: {p.text}; border: 1px solid {p.border}; padding: 4px; }}

#nav {{ background: {p.nav}; border: none; border-right: 1px solid {p.border}; outline: 0; padding: 8px 0; }}
#nav::item {{ height: 44px; padding-left: 14px; border: none; color: {p.text}; }}
#nav::item:hover {{ background: {p.row_hover}; }}
#nav::item:selected {{ background: {p.nav_active}; color: {p.text}; }}
#appTitle {{ font-size: 13pt; font-weight: 600; padding: 16px 18px 8px 18px; background: {p.nav}; }}
#navWrap {{ background: {p.nav}; border-right: 1px solid {p.border}; }}

#card {{ background: {p.card}; border-radius: 12px; }}
#sectionTitle {{ color: {p.accent}; font-weight: 600; font-size: 10.5pt; padding: 0 0 2px 0; }}
#pageTitle {{ font-size: 15pt; font-weight: 600; padding: 4px 0 6px 2px; }}
#muted {{ color: {p.text2}; }}
#value {{ color: {p.accent}; }}
#ok {{ color: {p.ok}; }}
#warn {{ color: {p.warn}; }}
#err {{ color: {p.err}; }}
#bigName {{ font-size: 12.5pt; font-weight: 600; }}
#row {{ background: transparent; border-radius: 8px; }}
#row:hover {{ background: {p.row_hover}; }}
#sep {{ background: {p.border}; max-height: 1px; min-height: 1px; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
    background: {p.input_bg}; border: none; border-bottom: 2px solid {p.border};
    padding: 6px 2px; selection-background-color: {p.accent}; border-radius: 0;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-bottom: 2px solid {p.accent}; }}
QLineEdit[error="true"] {{ border-bottom: 2px solid {p.err}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {p.card}; border: 1px solid {p.border};
    selection-background-color: {p.nav_active}; selection-color: {p.text}; outline: 0; }}

QPushButton#text {{ background: transparent; border: none; color: {p.accent}; font-weight: 600;
    padding: 8px 14px; border-radius: 6px; }}
QPushButton#text:hover {{ background: {p.row_hover}; }}
QPushButton#text:disabled {{ color: {p.text2}; }}
QPushButton#danger {{ background: transparent; border: none; color: {p.err}; font-weight: 600;
    padding: 8px 14px; border-radius: 6px; }}
QPushButton#danger:hover {{ background: {p.row_hover}; }}
QPushButton#primary {{ background: {p.accent}; color: white; border: none; font-weight: 600;
    padding: 9px 18px; border-radius: 8px; }}
QPushButton#primary:hover {{ background: {p.accent_hover}; }}
QToolButton#dots {{ border: none; background: transparent; color: {p.text2}; font-size: 14pt;
    padding: 0 6px; border-radius: 14px; }}
QToolButton#dots:hover {{ background: {p.row_hover}; }}
QToolButton#dots::menu-indicator {{ image: none; }}
QMenu {{ background: {p.card}; border: 1px solid {p.border}; padding: 6px 0; border-radius: 8px; }}
QMenu::item {{ padding: 7px 26px 7px 18px; }}
QMenu::item:selected {{ background: {p.row_hover}; }}

QRadioButton, QCheckBox {{ spacing: 10px; padding: 4px 0; }}
QRadioButton::indicator {{ width: 18px; height: 18px; border-radius: 10px; border: 2px solid {p.text2};
    background: transparent; }}
QRadioButton::indicator:checked {{ border: 5px solid {p.accent}; background: {p.card}; width: 12px; height: 12px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 4px; border: 2px solid {p.text2}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border: 2px solid {p.accent}; }}

QTableWidget {{ background: {p.card}; border: none; gridline-color: transparent; outline: 0;
    alternate-background-color: {p.row_hover}; }}
QTableWidget::item {{ padding: 4px 8px; border: none; }}
QTableWidget::item:selected {{ background: {p.nav_active}; color: {p.text}; }}
QHeaderView::section {{ background: {p.card}; color: {p.text2}; border: none;
    border-bottom: 1px solid {p.border}; padding: 6px 8px; font-weight: 600; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.text2}; border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.text2}; border-radius: 3px; min-width: 30px; }}
"""
