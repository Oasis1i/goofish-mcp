"""message backfill — 用 list-chats 加 history 补回断线或睡眠期间漏掉的消息。

message watch / message collect 重连不会回放历史：run_forever 只是重新 /reg，
ackDiff 的 pts 取当前时间而不是 0，也不调用 listUserMessages。list-chats 里
pts=0 的短时发现只拿到会话指针，没有正文。正文只能靠本命令读 history。
"""

from __future__ import annotations

from typing import Any

from goofish_mcp.commands.message.history import history
from goofish_mcp.commands.message.list_chats import list_chats
from goofish_mcp.core import Session, Strategy, command
from goofish_mcp.core.errors import GoofishError
from goofish_mcp.core.inbox import Inbox, event_from_history


@command(
    namespace="message",
    name="backfill",
    description="用会话列表和历史消息补回 watch 断线期间漏掉的正文（有界，按 message_id 去重）",
    strategy=Strategy.COOKIE,
    columns=["chats", "inserted", "duplicates"],
)
def backfill(
    fetch_num: int = 20,
    watch_secs: float = 0,
    per_chat_limit: int = 20,
    max_chats: int = 20,
    timeout: float = 30.0,
    inbox: str = "",
) -> dict[str, Any]:
    if not 1 <= fetch_num <= 50:
        raise GoofishError("fetch-num 必须为 1–50")
    if not 1 <= per_chat_limit <= 50:
        raise GoofishError("per-chat-limit 必须为 1–50，避免一次翻完整历史")
    if not 1 <= max_chats <= 30:
        raise GoofishError("max-chats 必须为 1–30")
    if watch_secs < 0 or watch_secs > 15:
        raise GoofishError("watch-secs 必须为 0–15；补洞默认 0，重连本身不会回放历史")
    if timeout <= 0:
        raise GoofishError("timeout 必须大于 0")

    session = Session.load()
    snapshot = list_chats(fetch_num=fetch_num, watch_secs=watch_secs, timeout=timeout)
    selected = [
        row
        for row in (snapshot.get("sessions") or [])
        if row.get("session_id") and row.get("session_type") in (0, 1)
    ]
    selected.sort(key=lambda row: (row.get("ts") or 0, row["session_id"]), reverse=True)
    selected = selected[:max_chats]

    inserted = 0
    duplicates = 0
    errors: list[dict[str, str]] = []
    store = Inbox(inbox)
    try:
        for row in selected:
            cid = str(row["session_id"])
            try:
                messages = history(cid, limit=per_chat_limit, timeout=timeout)
            except GoofishError as exc:
                errors.append({"cid": cid, "error": str(exc)})
                continue
            for item in messages:
                event = event_from_history(
                    item,
                    my_user_id=session.unb,
                    session_type=row.get("session_type"),
                    item_id=str(row.get("item_id") or ""),
                    peer_user_id=str(row.get("peer_user_id") or ""),
                    peer_nick=str(row.get("peer_nick") or ""),
                )
                if store.append_event(event, source="history"):
                    inserted += 1
                else:
                    duplicates += 1
    finally:
        store.close()
    return {
        "ok": not errors,
        "inbox": str(store.path),
        "chats": len(selected),
        "inserted": inserted,
        "duplicates": duplicates,
        "errors": errors,
        "reconnect_replays_history": False,
        "watch_secs": watch_secs,
    }
