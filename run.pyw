"""Точка входа TG MCP: из исходников — через pythonw.exe venv проекта; в сборке — это entry-point exe."""

import os
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import paths  # noqa: E402 — легкий модуль, без тяжелых импортов

# pythonw / exe без консоли: stdout/stderr отсутствуют — пишем их в файл, а не роняем процесс
if sys.stdout is None or sys.stderr is None:
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    _f = open(paths.LOG_DIR / "stderr.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or _f
    sys.stderr = sys.stderr or _f

from app.main import main  # noqa: E402

if __name__ == "__main__":
    os._exit(main())
