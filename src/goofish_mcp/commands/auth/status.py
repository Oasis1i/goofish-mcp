"""auth status — 检查登录态是否有效。调用 mtop.taobao.idlemessage.pc.loginuser.get"""

from typing import Any

from goofish_mcp.core import Session, Strategy, command
from goofish_mcp.core.errors import AuthRequiredError
from goofish_mcp.core.mtop import call
from goofish_mcp.core.refresh import is_enabled
from goofish_mcp.core.relogin import relogin_fields


@command(
    namespace="auth",
    name="status",
    description="检查登录态是否有效，返回 unb / tracknick / 昵称",
    strategy=Strategy.COOKIE,
    columns=["unb", "tracknick", "nick", "valid"],
)
def status() -> dict[str, Any]:
    session = Session.load()
    try:
        raw = call(
            session,
            api="mtop.taobao.idlemessage.pc.loginuser.get",
            data={},
            version="1.0",
            spm_cnt="a21ybx.im.0.0",
        )
        user = raw.get("data", {}) or {}
        return {
            "unb": session.unb,
            "tracknick": session.tracknick,
            "nick": user.get("nick", ""),
            "valid": True,
            "needs_relogin": False,
            "status": "ok",
            "auto_refresh": is_enabled(),
        }
    except AuthRequiredError as exc:
        body = {
            "unb": session.unb,
            "tracknick": session.tracknick,
            "nick": "",
            "valid": False,
            "needs_relogin": False,
            "status": "invalid",
            "auto_refresh": is_enabled(),
            "error": str(exc),
        }
        if exc.needs_relogin:
            body.update(relogin_fields())
        return body
    except Exception as e:  # noqa: BLE001
        return {
            "unb": session.unb,
            "tracknick": session.tracknick,
            "nick": "",
            "valid": False,
            "needs_relogin": False,
            "status": "invalid",
            "auto_refresh": is_enabled(),
            "error": str(e),
        }
