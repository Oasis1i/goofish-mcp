"""message watch — 常驻接收消息，每条解密后以 JSONL 打到 stdout。

不做自动回复；Agent 自己拿 JSONL 决策再调 message send。
Ctrl-C 退出。
"""

import asyncio
import json
import sys
from typing import Any

from loguru import logger

from goofish_mcp.core import Session, Strategy, command
from goofish_mcp.core.ws import (
    extract_incoming_text,
    extract_meta_event,
    extract_push_messages,
    run_forever,
)


def events_from_frame(msg: dict[str, Any], *, my_user_id: str = "") -> list[dict[str, Any]]:
    """把一帧下行解析成 watch 会输出的事件。不连网。

    `/s/para`（正在输入）和没有正文的激活帧被丢掉。元事件优先于正文。
    """
    if msg.get("lwp") == "/s/para":
        return []
    events: list[dict[str, Any]] = []
    for decoded in extract_push_messages(msg):
        meta = extract_meta_event(decoded)
        if meta is not None:
            events.append(meta)
            continue
        item = extract_incoming_text(decoded, my_user_id=my_user_id)
        if item is None:
            continue
        # 真消息：send_message 非空 或 contentType=1
        if item.get("send_message") or item.get("content_type") == 1:
            events.append(item)
    return events


def write_events(events: list[dict[str, Any]]) -> None:
    for event in events:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    if events:
        sys.stdout.flush()


@command(
    namespace="message",
    name="watch",
    description="常驻 IM 长连接，下行事件以 JSONL 输出到 stdout（Ctrl-C 退出）",
    strategy=Strategy.COOKIE,
    columns=[],
)
def watch() -> dict[str, Any]:
    session = Session.load()
    logger.info(f"[watch] unb={session.unb} tracknick={session.tracknick}")

    async def _handler(msg: dict[str, Any], _ws) -> None:
        write_events(events_from_frame(msg, my_user_id=session.unb))

    try:
        asyncio.run(run_forever(session, handler=_handler))
    except KeyboardInterrupt:
        logger.info("[watch] 用户中断")
    return {"ok": True}
