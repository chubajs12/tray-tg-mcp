from __future__ import annotations

import json

from starlette.testclient import TestClient

from app.mcp_server import BearerAuth, build_mcp
from app.tools import TOOL_NAMES

HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "pytest", "version": "1"}}}


def _sse_json(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return json.loads(text)


def test_bearer_required_and_rotation(env):
    app = BearerAuth(build_mcp(env.tools).streamable_http_app(), env.cfg)
    with TestClient(app, base_url="http://127.0.0.1:8791") as c:
        r = c.post("/mcp", json=INIT, headers=HDR)
        assert r.status_code == 401
        r = c.post("/mcp", json=INIT, headers={**HDR, "Authorization": "Bearer wrong"})
        assert r.status_code == 401
        tok = {"Authorization": "Bearer " + env.cfg.mcp_token}
        r = c.post("/mcp", json=INIT, headers={**HDR, **tok})
        assert r.status_code == 200, r.text
        sid = r.headers.get("mcp-session-id")
        h = {**HDR, **tok, "mcp-session-id": sid, "mcp-protocol-version": "2025-06-18"}
        c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h)
        names = [t["name"] for t in _sse_json(r.text)["result"]["tools"]]
        assert sorted(names) == sorted(TOOL_NAMES)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "get_me", "arguments": {}}}, headers=h)
        res = _sse_json(r.text)["result"]
        assert res["isError"] is True and "Нет ни одного аккаунта" in res["content"][0]["text"]
        # смена токена: старый перестает приниматься сразу
        old = env.cfg.mcp_token
        env.cfg.rotate_token()
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                   headers={**h, "Authorization": "Bearer " + old})
        assert r.status_code == 401
