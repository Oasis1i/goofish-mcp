"""message pending — 按会话读出还没处理的消息。"""

from __future__ import annotations

from typing import Any

from goofish_mcp.core import Strategy, command
from goofish_mcp.core.inbox import Inbox


@command(
    namespace="message",
    name="pending",
    description="读取本地收件箱里未处理的消息，按会话分组并合并买家正文",
    strategy=Strategy.COOKIE,
    columns=["cid", "peer_nick", "session_type", "merged_text"],
)
def pending(inbox: str = "") -> dict[str, Any]:
    with Inbox(inbox) as store:
        result = store.pending()
        result["inbox"] = str(store.path)
        return result
