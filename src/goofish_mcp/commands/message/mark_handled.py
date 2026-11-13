"""message mark-handled — 把一个会话标成已处理。"""

from __future__ import annotations

from typing import Any

from goofish_mcp.core import Strategy, command
from goofish_mcp.core.inbox import Inbox


@command(
    namespace="message",
    name="mark-handled",
    description="把一个会话标成已处理，未处理队列里不再返回它",
    strategy=Strategy.COOKIE,
    columns=["cid", "handled", "marked_messages"],
)
def mark_handled(cid: str, inbox: str = "") -> dict[str, Any]:
    with Inbox(inbox) as store:
        result = store.mark_handled(cid)
        result["inbox"] = str(store.path)
        return result
