"""Windows: один экземпляр (именованный mutex), сообщение, автозапуск (HKCU\\...\\Run)."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from . import APP_TITLE, paths

MUTEX_NAME = "Local\\tg-mcp-single-instance"
ERROR_ALREADY_EXISTS = 183
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "tg-mcp"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class SingleInstance:
    def __init__(self, name: str = MUTEX_NAME):
        self.handle = _kernel32.CreateMutexW(None, False, name)
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None


def message_box(text: str, title: str = APP_TITLE, timeout_ms: int = 5000, icon: int = 0x40) -> None:
    """Сообщение, закрывающееся само через timeout_ms (MessageBoxTimeoutW), иначе обычное."""
    user32 = ctypes.WinDLL("user32")
    flags = icon | 0x00040000 | 0x00010000  # MB_TOPMOST | MB_SETFOREGROUND
    try:
        fn = user32.MessageBoxTimeoutW
        fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT, wintypes.WORD,
                       wintypes.DWORD]
        fn(None, text, title, flags, 0, timeout_ms)
    except AttributeError:
        user32.MessageBoxW(None, text, title, flags)


def pythonw_exe() -> Path:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def autostart_command() -> str:
    r"""Команда для HKCU\...\Run: в сборке — сам exe, из исходников — pythonw venv + run.pyw. Окно не открывается."""
    if paths.FROZEN:
        return f'"{paths.EXE_PATH}"'
    return f'"{pythonw_exe()}" "{paths.PROJECT_DIR / "run.pyw"}"'


def autostart_enabled() -> bool:
    import winreg  # noqa: PLC0415
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    import winreg  # noqa: PLC0415
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


# ---------- корректное завершение извне (run.pyw --quit, uninstall.ps1) ----------
QUIT_EVENT = "Local\\tg-mcp-quit"
_kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD


SHOW_EVENT = "Local" + chr(92) + "tg-mcp-show"
_kernel32.ResetEvent.argtypes = [wintypes.HANDLE]


def _watch(name: str, callback, repeat: bool) -> None:
    import threading  # noqa: PLC0415

    handle = _kernel32.CreateEventW(None, True, False, name)
    if not handle:
        return

    def wait():
        while _kernel32.WaitForSingleObject(handle, 0xFFFFFFFF) == 0:
            _kernel32.ResetEvent(handle)
            callback()
            if not repeat:
                break

    threading.Thread(target=wait, name="event-" + name.rsplit(chr(92), 1)[-1], daemon=True).start()


def watch_quit_event(on_quit) -> None:
    """Поток ждет именованное событие и вызывает on_quit (тот же путь, что «Выход» в трее)."""
    _watch(QUIT_EVENT, on_quit, repeat=False)


def watch_show_event(on_show) -> None:
    """Повторный запуск с --show (ярлык) — показать окно уже запущенного экземпляра."""
    _watch(SHOW_EVENT, on_show, repeat=True)


def _signal(name: str) -> bool:
    handle = _kernel32.OpenEventW(0x0002, False, name)  # EVENT_MODIFY_STATE
    if not handle:
        return False
    try:
        return bool(_kernel32.SetEvent(handle))
    finally:
        _kernel32.CloseHandle(handle)


def signal_quit() -> bool:
    """True — работающему экземпляру отправлен сигнал завершиться."""
    return _signal(QUIT_EVENT)


def signal_show() -> bool:
    return _signal(SHOW_EVENT)
