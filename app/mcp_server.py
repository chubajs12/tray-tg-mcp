"""MCP-сервер (FastMCP, streamable HTTP) на 127.0.0.1 с обязательным Bearer-токеном.

Работает в том же asyncio-loop, что и клиенты Telethon, — все вызовы Telegram идут в одном loop.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import APP_NAME, APP_VERSION
from .config import Config
from .tools import TOOL_NAMES, Tools

log = logging.getLogger("tgmcp.mcp")

INSTRUCTIONS = (
    "Доступ к Telegram пользователя (один или несколько аккаунтов). Начните с list_accounts. "
    "Чтение никогда не помечает сообщения прочитанными — только явный mark_read. "
    "Права на группы операций (чтение, отправка, удаление и т.д.) задает пользователь в окне приложения по каждому "
    "аккаунту; запрещенный вызов вернет ошибку. Через MCP права не меняются. "
    "Запись ограничена ~1 операцией в секунду на аккаунт; при FloodWait не повторяйте раньше указанного времени."
)

DESTRUCTIVE = {"delete_messages", "edit_message"}
WRITE_IDEMPOTENT = {"mark_read", "send_reaction", "set_proxy", "clear_proxy"}


def build_mcp(tools: Tools) -> FastMCP:
    mcp = FastMCP(APP_NAME, instructions=INSTRUCTIONS, host="127.0.0.1", streamable_http_path="/mcp",
                  log_level="WARNING")
    for name in TOOL_NAMES:
        fn = getattr(tools, name)
        kind = getattr(fn, "tool_kind", "read")
        read_only = kind == "read" or name in ("get_proxy", "list_accounts", "get_permissions")
        ann = ToolAnnotations(
            readOnlyHint=read_only,
            destructiveHint=(name in DESTRUCTIVE) if not read_only else False,
            idempotentHint=read_only or name in WRITE_IDEMPOTENT,
            openWorldHint=name not in ("get_proxy", "set_proxy", "clear_proxy", "list_accounts", "get_permissions"),
        )
        mcp.add_tool(fn, name=name, description=(fn.__doc__ or "").strip(), annotations=ann,
                     structured_output=False)
    return mcp


class BearerAuth:
    """ASGI-обертка: каждый HTTP-запрос — только с Authorization: Bearer <токен из config>.

    Токен читается из config на каждый запрос, поэтому смена токена действует сразу.
    """

    def __init__(self, app, config: Config):
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = ""
            for k, v in scope.get("headers") or []:
                if k == b"authorization":
                    auth = v.decode("latin-1")
                    break
            expected = "Bearer " + self.config.mcp_token
            if not hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
                body = json.dumps({"error": "unauthorized",
                                   "message": "Нужен заголовок Authorization: Bearer <токен tg-mcp>"},
                                  ensure_ascii=False).encode("utf-8")
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                        (b"www-authenticate", b'Bearer realm="tg-mcp"'),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class McpHttpServer:
    def __init__(self, tools: Tools, config: Config):
        self.tools = tools
        self.config = config
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self.port: int | None = None
        self.error: str | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port or self.config.mcp_port}/mcp"

    def _build(self, port: int) -> uvicorn.Server:
        mcp = build_mcp(self.tools)
        app = BearerAuth(mcp.streamable_http_app(), self.config)
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False,
                             lifespan="on", log_level="warning", server_header=False)
        return uvicorn.Server(cfg)

    async def start(self) -> None:
        port = self.config.mcp_port
        self.port = port
        self.error = None
        self._server = self._build(port)
        self._task = asyncio.create_task(self._serve(self._server), name="mcp-http")
        # ждем запуска или ошибки привязки
        for _ in range(100):
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.05)
        if not self._server.started:
            self.error = self.error or f"MCP-сервер не запустился на порту {port} (порт занят?)"
            log.error(self.error)
        else:
            log.info("MCP-сервер: %s", self.url)

    async def _serve(self, server: uvicorn.Server) -> None:
        try:
            await server.serve()
        except SystemExit:
            # uvicorn вызывает sys.exit(1), если не смог занять порт
            self.error = f"порт {self.port} занят"
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            log.exception("MCP-сервер")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=8)
            except asyncio.TimeoutError:
                if self._server is not None:
                    self._server.force_exit = True
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        self._server = None
        self._task = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def status(self) -> dict[str, Any]:
        running = bool(self._server and self._server.started and self._task and not self._task.done())
        return {"url": self.url, "running": running, "error": self.error}
