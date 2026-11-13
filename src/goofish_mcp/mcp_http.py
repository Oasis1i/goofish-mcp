"""Account-scoped Streamable HTTP MCP, with separate read/write bearer tokens."""

from __future__ import annotations

import asyncio
import inspect
import os
import secrets
from contextvars import ContextVar
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from goofish_mcp.agent_ops import AgentOps

_WRITE_ACCESS: ContextVar[bool] = ContextVar("goofish_write_access", default=False)
READ_TOOLS = frozenset({"inbox_pending", "daily_stats"})
WRITE_TOOLS = frozenset({"reply", "escalate", "mark_handled"})


class BearerGate:
    def __init__(self, app, read_token: str, write_token: str):
        self.app = app
        self.read_token = read_token
        self.write_token = write_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
        token = ""
        if len(headers) == 1:
            parts = headers[0].decode("latin-1").split()
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1]
        # Compare bytes so malformed non-ASCII credentials also produce 401.
        credential = token.encode("utf-8")
        write = secrets.compare_digest(credential, self.write_token.encode("utf-8"))
        read = secrets.compare_digest(credential, self.read_token.encode("utf-8"))
        if not (write or read):
            return await JSONResponse(
                {"error": "Unauthorized"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
        reset = _WRITE_ACCESS.set(write)
        try:
            await self.app(scope, receive, send)
        finally:
            _WRITE_ACCESS.reset(reset)


def create_app(ops: AgentOps | None = None):
    read_token = os.environ.get("GOOFISH_MCP_TOKEN", "")
    write_token = os.environ.get("GOOFISH_MCP_WRITE_TOKEN", "")
    if not read_token.strip() or not write_token.strip():
        raise ValueError("GOOFISH_MCP_TOKEN 和 GOOFISH_MCP_WRITE_TOKEN 必须配置")
    if read_token == write_token or any(c.isspace() for c in read_token + write_token):
        raise ValueError("读写 token 必须不同且不能包含空白")
    # Explicit allow-list supports tunnels without disabling DNS rebinding protection.
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    hosts += [h.strip() for h in os.environ.get("GOOFISH_MCP_ALLOWED_HOSTS", "").split(",")
              if h.strip()]
    server = FastMCP(
        "goofish-agent", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts,
            allowed_origins=[o.strip() for o in
                             os.environ.get("GOOFISH_MCP_ALLOWED_ORIGINS", "").split(",")
                             if o.strip()],
        ),
    )
    ops = ops or AgentOps()
    for name in sorted(READ_TOOLS | WRITE_TOOLS):
        _register(server, ops, name)
    return BearerGate(server.streamable_http_app(), read_token, write_token)


def _register(server: FastMCP, ops: AgentOps, name: str) -> None:
    func = getattr(ops, name)

    async def handler(**kwargs: Any) -> dict[str, Any]:
        if name in WRITE_TOOLS and not _WRITE_ACCESS.get():
            raise PermissionError("write token required")
        return await asyncio.to_thread(func, **kwargs)

    handler.__name__ = name
    handler.__signature__ = inspect.signature(func)  # type: ignore[attr-defined]
    descriptions = {
        "inbox_pending": "读取本地待自动处理会话，附商品信息、合并买家正文和最近聊天记录。",
        "daily_stats": "读取服务器本机当天发送额度、冷却、待处理会话数和人工接管次数。",
        "reply": "从本地上下文带护栏回复；dry_run 仅预演，仍要求写 token。请检查 action/reason。",
        "escalate": "将会话持久转给人工并记录原因，不发送消息；新消息不会恢复自动处理。",
        "mark_handled": "标记会话现有消息已处理；不会清除人工接管状态。",
    }
    server.tool(name=name, description=descriptions[name])(handler)


def main() -> None:
    import uvicorn

    app = create_app()
    uvicorn.run(app, host=os.environ.get("GOOFISH_MCP_HOST", "127.0.0.1"),
                port=int(os.environ.get("GOOFISH_MCP_PORT", "8000")))


if __name__ == "__main__":
    main()
