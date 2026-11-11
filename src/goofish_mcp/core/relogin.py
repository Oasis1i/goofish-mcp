"""自动刷新关闭时，登录失效的机器可读状态。不打开浏览器。"""

from __future__ import annotations

import json
import sys
from typing import Any

from goofish_mcp.core.errors import AuthRequiredError, GoofishError


def relogin_fields() -> dict[str, Any]:
    return {
        "needs_relogin": True,
        "status": "needs_relogin",
        "auto_refresh": False,
    }


def error_payload(exc: GoofishError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": False,
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    if isinstance(exc, AuthRequiredError) and exc.needs_relogin:
        body.update(relogin_fields())
    return body


def emit_relogin(exc: AuthRequiredError) -> None:
    """给守护进程的 stdout 写一行 JSON。不是 needs_relogin 时什么都不写。"""
    if not exc.needs_relogin:
        return
    payload = {"ok": False, "valid": False, **relogin_fields(), "error": str(exc)}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
