"""message collect — 跑 watch，并把消息事件按 message_id 写入本地收件箱。

断线后 run_forever 只会重新 /reg，ackDiff 的 pts 是当前时间，不会拉历史。
漏掉的正文用 message backfill 补。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from goofish_mcp.commands.message.watch import events_from_frame, write_events
from goofish_mcp.core import Session, Strategy, command
from goofish_mcp.core.inbox import Inbox
from goofish_mcp.core.ws import run_forever


def record_frame(store: Inbox, msg: dict[str, Any], *, my_user_id: str) -> list[dict[str, Any]]:
    """解析一帧、去重写入收件箱，并按 watch 的 JSONL 打到 stdout。"""
    recorded: list[dict[str, Any]] = []
    visible: list[dict[str, Any]] = []
    for event in events_from_frame(msg, my_user_id=my_user_id):
        stored = dict(event)
        if event.get("event") == "message":
            stored["inserted"] = store.append_event(event, source="watch")
        visible.append(event)
        recorded.append(stored)
    write_events(visible)
    return recorded


@command(
    namespace="message",
    name="collect",
    description="常驻接收消息并写入本地收件箱（按 message_id 去重，Ctrl-C 退出）",
    strategy=Strategy.COOKIE,
    columns=[],
)
def collect(inbox: str = "") -> dict[str, Any]:
    session = Session.load()
    logger.info(f"[collect] unb={session.unb} inbox={inbox or 'default'}")
    store = Inbox(inbox)

    async def _handler(msg: dict[str, Any], _ws: Any) -> None:
        record_frame(store, msg, my_user_id=session.unb)

    try:
        asyncio.run(run_forever(session, handler=_handler))
    except KeyboardInterrupt:
        logger.info("[collect] 用户中断")
    finally:
        store.close()
    return {"ok": True, "inbox": str(store.path)}
