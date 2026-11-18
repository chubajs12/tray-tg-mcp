"""Технический лог (с ротацией) и журнал действий data/actions.jsonl."""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger("tgmcp")

# Параметры, значения которых в журнал не пишем никогда.
SECRET_KEYS = {"password", "secret", "api_hash", "token", "mcp_token"}


def _drop_benign(record: logging.LogRecord) -> bool:
    """SDK MCP пишет ERROR, когда клиент закрывает SSE-поток (обычное отключение) — не засоряем лог."""
    if record.name == "mcp.server.streamable_http" and "standalone SSE writer" in record.getMessage():
        return False
    return True


def setup_logging(level: int = logging.INFO) -> None:
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.handlers.RotatingFileHandler(
        paths.APP_LOG, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"))
    handler.addFilter(_drop_benign)
    root.addHandler(handler)
    root.setLevel(level)
    # Telethon и uvicorn слишком подробны на INFO
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def sanitize(value: Any) -> Any:
    """Убирает секреты из параметров перед записью в журнал."""
    if isinstance(value, dict):
        return {k: ("***" if (k in SECRET_KEYS and v) else sanitize(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    return value


class ActionLog:
    """Журнал действий MCP: одна JSON-строка на вызов инструмента."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or paths.ACTIONS_LOG)
        self._lock = threading.Lock()
        self.count = 0
        self.last: dict[str, Any] | None = None

    def write(self, tool: str, params: dict[str, Any], ok: bool,
              result: Any = None, error: str | None = None,
              account: dict[str, Any] | None = None, source: str = "mcp") -> dict[str, Any]:
        rec: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "source": source,
            "tool": tool,
        }
        if account:
            rec["account"] = account
        rec["params"] = sanitize(params)
        rec["ok"] = ok
        if ok:
            rec["result"] = result
        else:
            rec["error"] = error
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            if source == "mcp":
                self.count += 1
                self.last = {"ts": rec["ts"], "tool": tool, "ok": ok,
                             "account": (account or {}).get("label")}
        return rec

    def stats(self) -> tuple[int, dict[str, Any] | None]:
        with self._lock:
            return self.count, (dict(self.last) if self.last else None)
